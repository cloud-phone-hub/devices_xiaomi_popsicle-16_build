"""popsicle-16 super.img（AOSP liblp 动态分区）纯 Python 读取与原位更新。

官方 super.img 为 sparse 分发，unpack 阶段已展开为 raw。本模块直接解析 liblp
metadata（格式见 AOSP system/core/fs_mgr/liblp/ 的 metadata_format.h），定位各
逻辑分区（vendor_a / vendor_dlkm_a 等）在 super 内的线性 extent，然后相对于基底
super.img 做「原位覆盖」：保留元数据、AVB vbmeta 链与其它分区不动，只把指定的
分区数据写回对应 extent，并做回读自检。

pack-roms 的增量补丁族（--patch）与 --list-partitions 借此把逻辑分区名映射到
vda.img 内的绝对字节偏移（super 物理分区在 vda 内再叠加 base 偏移）。
popsicle 采用「官方 super 零改动」路线（pack_super.py 字节直拷），本模块只做
读取/定位与增量覆盖，不做 super 重建。

主要接口：
  read_super(path, base=0)            解析全部逻辑分区 -> (list[Partition], extents)
  partition_extents(p, extents, name) 按名（含槽位规约）取线性 extent -> [(off_bytes, size_bytes)]
  update_partitions(path, upd)        原位覆盖若干逻辑分区数据，并校验容量
  verify_partition(path, name, img)   读回比对指定分区与宿主镜像是否逐字节一致
"""

import os
import struct
import sys

SECTOR = 512
LP_METADATA_GEOMETRY_SIZE = 4096
LP_PARTITION_RESERVED_BYTES = 4096
LP_METADATA_HEADER_MAGIC = 0x414C5030
LP_PARTITION_ATTR_SLOT_SUFFIXED = 1 << 1
LP_PARTITION_ATTR_DISABLED = 1 << 3
LP_TARGET_TYPE_LINEAR = 0
METADATA_OFFSET = LP_PARTITION_RESERVED_BYTES + LP_METADATA_GEOMETRY_SIZE * 2

COPY_CHUNK = 16 * 1024 * 1024


class Partition:
    """一个 liblp 逻辑分区。attr 低 4 位 / first_extent / num_extents 见官方结构体。"""

    __slots__ = ("name", "attr", "first_extent", "num_extents")

    def __init__(self, name, attr, first_extent, num_extents):
        self.name = name
        self.attr = attr
        self.first_extent = first_extent
        self.num_extents = num_extents

    @property
    def disabled(self):
        return bool(self.attr & LP_PARTITION_ATTR_DISABLED)

    def slot_name(self):
        """按槽位规约返回 A/B 槽位名：attr 带槽位后缀且未带 _a/_b 时补 _a。"""
        if (self.attr & LP_PARTITION_ATTR_SLOT_SUFFIXED
                and not self.name.endswith(("_a", "_b"))):
            return self.name + "_a"
        return self.name


def read_super(super_path, base=0):
    """解析 super.img 的 liblp 元数据，返回 (partitions, extents)。

    extents[i] = (num_sectors, target_data_sector)，target_data 为 super 内的扇区号。

    base：super 在 super_path 文件内的字节偏移。独立 super.img 传 0（默认）；
    直接解析嵌在 vda.img 里的 super 时传该 super 分区的绝对偏移。返回的 extent
    target_data 仍是 super 内相对扇区号（调用方按需叠加 base）。
    """
    with open(super_path, "rb") as f:
        f.seek(base + METADATA_OFFSET)
        hdr = f.read(256)
        if struct.unpack_from("<I", hdr, 0)[0] != LP_METADATA_HEADER_MAGIC:
            raise SystemExit(f"[err] {super_path} 不是合法 super 动态分区镜像")
        header_size = struct.unpack_from("<I", hdr, 8)[0]

        def table(header_off):
            off, num, size = struct.unpack_from("<III", hdr, header_off)
            f.seek(base + METADATA_OFFSET + header_size + off)
            return f.read(num * size), num, size

        pdata, pnum, psize = table(80)
        edata, _en, esize = table(92)

    parts = []
    for i in range(pnum):
        e = pdata[i * psize:(i + 1) * psize]
        if not e:
            break
        name = e[0:36].split(b"\x00", 1)[0].decode("ascii", "ignore")
        attr = struct.unpack_from("<I", e, 36)[0]
        first_ext, num_ext = struct.unpack_from("<II", e, 40)
        parts.append(Partition(name, attr, first_ext, num_ext))

    extents = []
    for i in range(_en):
        et = edata[i * esize:(i + 1) * esize]
        nsectors, _ttype, tdata, _tsource = struct.unpack_from("<QIQI", et, 0)
        extents.append((nsectors, tdata))
    return parts, extents


def find_partition(parts, name):
    """按输入名（vendor / vendor_a）匹配逻辑分区，返回 Partition 或 None。"""
    if not name.endswith(("_a", "_b")):
        # 先精确匹配无槽位名；再按槽位规约找当前槽位（_a）
        for p in parts:
            if p.name == name:
                return p
        target = name + "_a"
        for p in parts:
            if p.name == target:
                return p
            if p.slot_name() == target:
                return p
        return None
    for p in parts:
        if p.name == name:
            return p
    return None


def partition_extents(parts, extents, part_name):
    """返回命名分区的线性 extent 列表 [(offset_bytes, size_bytes)]（super 内绝对偏移）。

    仅考虑 LP_TARGET_TYPE_LINEAR；非 linear（如 ZERO）分区返回空列表。
    """
    part = find_partition(parts, part_name)
    if part is None:
        return []
    segs = []
    for k in range(part.num_extents):
        idx = part.first_extent + k
        if idx >= len(extents):
            break
        nsectors, tdata_sector = extents[idx]
        segments = tdata_sector * SECTOR
        segs.append((segments, nsectors * SECTOR))
    return segs


def partition_size(parts, extents, part_name):
    return sum(sz for _off, sz in partition_extents(parts, extents, part_name))


def update_partitions(super_path, updates):
    """原位覆盖 super.img 内若干逻辑分区数据。updates: dict[name->img_path]。

    要求每个镜像的文件大小 <= 分区分配大小（否则报错退出，调用方回退重建策略）。
    """
    parts, extents = read_super(super_path)
    for name, img_path in updates.items():
        segs = partition_extents(parts, extents, name)
        if not segs:
            print(f"[lp] 未在 super 中找到逻辑分区: {name}")
            sys.exit(1)
        alloc = sum(sz for _o, sz in segs)
        isize = os.path.getsize(img_path)
        if isize > alloc:
            # 分区分配小于自制镜像时直接报错，避免越界写坏其它分区；
            # 调用方需保证镜像 <= 分配大小。
            raise SystemExit(
                f"[lp] {name} 镜像 {isize} B > 分配 {alloc} B，无法原位覆盖")

    print(f"[lp] 原位覆盖 {len(updates)} 个逻辑分区 -> {super_path}")
    with open(super_path, "r+b") as f:
        for name, img_path in updates.items():
            segs = partition_extents(parts, extents, name)
            alloc = sum(sz for _o, sz in segs)
            written = 0
            with open(img_path, "rb") as src:
                for off, _sz in segs:
                    f.seek(off)
                    remaining = _sz
                    while remaining > 0:
                        buf = src.read(min(COPY_CHUNK, remaining))
                        if not buf:
                            break
                        f.write(buf)
                        written += len(buf)
                        remaining -= len(buf)
            if written < alloc:
                f.seek(sum(o for o, _s in segs) + written)
                f.write(b"\x00" * (alloc - written))
            print(f"[lp]   {name} -> {written} B / {alloc} B 写入完成")
    return True


def verify_partition(super_path, part_name, img_path):
    """回读比对 super 内逻辑分区与宿主镜像逐字节一致。返回 bool。

    逻辑分区镜像可能比分配 extent 小（extent 尾随被写零），只比对镜像自身大小。
    """
    parts, extents = read_super(super_path)
    segs = partition_extents(parts, extents, part_name)
    if not segs:
        print(f"    回读缺分区 {part_name}（super 中未找到）")
        return False
    isize = os.path.getsize(img_path)
    remaining = isize
    ok = True
    with open(super_path, "rb") as f, open(img_path, "rb") as ref:
        for off, _sz in segs:
            f.seek(off)
            chunk = _sz
            while chunk > 0 and remaining > 0:
                n = min(COPY_CHUNK, chunk, remaining)
                got = f.read(n)
                exp = ref.read(n)
                if got != exp:
                    ok = False
                remaining -= n
                chunk -= n
    if remaining > 0:
        ok = False
    print(f"    回读 {part_name}: {'OK' if ok else 'MISMATCH'}")
    return ok
