#!/usr/bin/env python3
"""解包 popsicle-16（Xiaomi SM8850）构建输入，参考 note13-5G-15/pixel6-15 的 unpack 模式。

两个独立来源：
  * 官方 images 包（tgz）：只取 super.img（官方 sparse 分发，纯 Python 展开为 raw 到
    roms/<解压目录>/super/super.raw.img，官方 images/ 保持原样不动）；再按 AOSP liblp
    拆出 super 内各逻辑分区，落到 roms/<解压目录>/super/（参照件/基线校验源）。
  * lineage 底包（UTM 虚机）：从 zip 提取 vda（raw .img 直通 / 旧版 qcow2 转换），
    解析 GPT 分区表，导出 boot/vendor_boot/init_boot/super 等物理分区到
    lineage-rom/<底包>/，再拆 super 逻辑分区（vendor/vendor_dlkm 等基座）。

任务（可选组合）：
  extract   从官方 tgz 解出 super.img 并展开 raw 到 super/super.raw.img（幂等）
  lineage   解 lineage 底包 vda -> GPT 分区 -> 拆 super 逻辑分区（已解压跳过）
  super     拆官方 super.raw.img 逻辑分区到 roms/<解压目录>/super/（参照件）

本机无 simg2img，sparse 展开为纯 Python 实现（Android sparse format v2.1+，
RAW/FILL/DONT_CARE/CRC32 四类 chunk），输出保持稀疏文件（DONT_CARE 区用 seek 留洞）。

用法（可在任何目录）：
  python3 build/scripts/unpack-roms.py                              # extract+lineage+super
  python3 build/scripts/unpack-roms.py lineage                      # 只解 lineage 底包
  python3 build/scripts/unpack-roms.py lineage --list               # 只打印底包 GPT 分区表
  python3 build/scripts/unpack-roms.py extract super                # 只解官方 super
"""

import os
import shutil
import struct
import subprocess
import sys
import tarfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from buildenv import (  # noqa: E402
    LINEAGE_BOOT_IMG, LINEAGE_INIT_BOOT_IMG, LINEAGE_OUT_DIR, LINEAGE_SUPER_DIR,
    LINEAGE_VDA_MEMBERS, LINEAGE_VENDOR_BOOT_IMG, LINEAGE_ZIP, ROM_ROOT_DIR,
    ROM_TGZ, SUPER_IMG, SUPER_OUT, SUPER_RAW, TMP_DIR, _qemu_img,
    lineage_vda_member,
)

SECTOR = 512
GPT_SIGNATURE = b"EFI PART"
# AOSP 动态分区 (super) metadata 常量（liblp/metadata_format.h）
LP_METADATA_GEOMETRY_SIZE = 4096
LP_PARTITION_RESERVED_BYTES = 4096
LP_METADATA_HEADER_MAGIC = 0x414C5030
LP_PARTITION_ATTR_SLOT_SUFFIXED = 1 << 1
LP_PARTITION_ATTR_DISABLED = 1 << 3
# Android sparse image 常量（system/core/libsparse/sparse_format.h）
SPARSE_MAGIC = 0xED26FF3A
SPARSE_CHUNK_RAW = 0xCAC1
SPARSE_CHUNK_FILL = 0xCAC2
SPARSE_CHUNK_DONT_CARE = 0xCAC3
SPARSE_CHUNK_CRC32 = 0xCAC4
METADATA_OFFSET = LP_PARTITION_RESERVED_BYTES + LP_METADATA_GEOMETRY_SIZE * 2
LINEAGE_TMP = os.path.join(TMP_DIR, "lineage")       # 底包临时工作区（转换 raw）
LINEAGE_UNPACKED_MARK = os.path.join(LINEAGE_OUT_DIR, ".unpacked")  # lineage 解压完成标记

_CHUNK = 16 * 1024 * 1024


# ---------------- sparse 展开（纯 Python）----------------

def _is_sparse(path):
    with open(path, "rb") as f:
        return struct.unpack("<I", f.read(4))[0] == SPARSE_MAGIC


def _unsparse_to(sparse_path, raw_path):
    """Android sparse -> raw（纯 Python，输出稀疏文件）。返回 raw_path。"""
    os.makedirs(os.path.dirname(raw_path), exist_ok=True)
    tmp = raw_path + ".part.%d" % os.getpid()
    total_written = 0
    with open(sparse_path, "rb") as src, open(tmp, "wb") as dst:
        hdr = src.read(28)
        if len(hdr) < 28 or struct.unpack_from("<I", hdr, 0)[0] != SPARSE_MAGIC:
            raise SystemExit(f"[err] 非 sparse 镜像: {sparse_path}")
        (major, minor, file_hdr_sz, chunk_hdr_sz, blk_sz,
         total_blks, total_chunks, _csum) = struct.unpack_from("<HHHHIIII", hdr, 4)
        if file_hdr_sz > 28:
            src.seek(file_hdr_sz)  # 兼容扩展头
        print(f"    sparse v{major}.{minor} blk={blk_sz} "
              f"blocks={total_blks}（{total_blks * blk_sz / 2**30:.2f} GiB）"
              f" chunks={total_chunks}")
        for i in range(total_chunks):
            ch = src.read(chunk_hdr_sz)
            if len(ch) < 12:
                raise SystemExit("[err] sparse chunk 头截断")
            ctype, _resv, csz, tsz = struct.unpack_from("<HHII", ch, 0)
            chunk_bytes = csz * blk_sz
            if ctype == SPARSE_CHUNK_RAW:
                remaining = chunk_bytes
                while remaining > 0:
                    buf = src.read(min(_CHUNK, remaining))
                    if not buf:
                        raise SystemExit("[err] sparse RAW chunk 数据截断")
                    dst.write(buf)
                    remaining -= len(buf)
                total_written += chunk_bytes
            elif ctype == SPARSE_CHUNK_FILL:
                pat = src.read(4)
                if len(pat) != 4:
                    raise SystemExit("[err] sparse FILL chunk 截断")
                block = pat * (_CHUNK // 4)
                remaining = chunk_bytes
                while remaining > 0:
                    n = min(len(block), remaining)
                    dst.write(block[:n])
                    remaining -= n
                total_written += chunk_bytes
            elif ctype == SPARSE_CHUNK_DONT_CARE:
                dst.seek(chunk_bytes, os.SEEK_CUR)  # 留洞，末尾 truncate 补齐
                total_written += chunk_bytes
            elif ctype == SPARSE_CHUNK_CRC32:
                src.read(4)
            else:
                raise SystemExit(f"[err] 未知 sparse chunk 类型 {ctype:#x}")
            if (i + 1) % max(1, total_chunks // 10) == 0 and total_blks:
                pct = 100.0 * min(total_written / blk_sz / total_blks, 1.0)
                print(f"    ... {pct:5.1f}% ({i + 1}/{total_chunks} chunks)")
        dst.truncate(total_blks * blk_sz)
    os.replace(tmp, raw_path)
    print(f"[ok] sparse 展开完成: {raw_path}（{os.path.getsize(raw_path)/2**30:.2f} GiB）")
    return raw_path


# ---------------- extract：官方 tgz（仅取 super.img）----------------

# tgz 内 super.img 的成员路径（解出到 images/ 原位；raw 展开到 super/super.raw.img）
SUPER_MEMBER = f"{os.path.basename(ROM_ROOT_DIR)}/images/super.img"


def extract_tgz():
    """确保官方 super 就位：images/super.img（tgz 解出，已存在即跳过）+ raw 展开，幂等。

    官方 images/ 目录保持原样（基线 sha256 不变）；raw 产物落 super/super.raw.img
    （pack_super / unpack super 逻辑分区拆解共用）。该机以 lineage 底包提供引导与
    boot 分区，故其余官方镜像均不需要在此处理。
    """
    if not os.path.isfile(SUPER_IMG):
        if not os.path.isfile(ROM_TGZ):
            print(f"[skip] 官方 super.img 与 tgz 均不存在（{ROM_ROOT_DIR}）")
            return False
        print(f"提取 tgz 内 {SUPER_MEMBER} -> {os.path.relpath(SUPER_IMG)}")
        os.makedirs(os.path.dirname(SUPER_IMG), exist_ok=True)
        with tarfile.open(ROM_TGZ, "r:gz") as tf:
            member = tf.getmember(SUPER_MEMBER)
            with tf.extractfile(member) as src, open(SUPER_IMG, "wb") as dst:
                shutil.copyfileobj(src, dst, _CHUNK)
    else:
        print(f"[skip] 官方 super.img 已就位: {SUPER_IMG}")
    if not _is_sparse(SUPER_IMG):
        print(f"[skip] super.img 已是 raw（无 sparse 头），仅确认路径: {SUPER_RAW}")
        if not os.path.isfile(SUPER_RAW):
            os.makedirs(os.path.dirname(SUPER_RAW), exist_ok=True)
            shutil.copy2(SUPER_IMG, SUPER_RAW)
        return True
    if os.path.isfile(SUPER_RAW):
        print(f"[skip] raw 已展开: {SUPER_RAW}（{os.path.getsize(SUPER_RAW)/2**30:.2f} GiB）")
        return True
    print(f"展开 sparse -> raw: {os.path.basename(SUPER_IMG)} -> super/super.raw.img"
          "（需一定时间与磁盘空间）")
    _unsparse_to(SUPER_IMG, SUPER_RAW)
    return True


# ---------------- GPT 磁盘（lineage 底包 vda）----------------

def read_partitions(img_path):
    """读取 GPT 磁盘分区表，返回 Partition-like 列表。"""
    with open(img_path, "rb") as f:
        hdr = f.read(SECTOR * 2)[SECTOR:SECTOR * 2]
        if hdr[0:8] != GPT_SIGNATURE:
            raise SystemExit(f"[err] 不是合法 GPT 磁盘镜像: {img_path}")
        part_lba, num_entries, entry_size = struct.unpack("<QII", hdr[72:72 + 16])
        if num_entries == 0 or entry_size == 0 or entry_size > 4096:
            raise SystemExit("[err] GPT 分区表字段异常")
        f.seek(part_lba * SECTOR)
        entries = f.read(num_entries * entry_size)
    parts = []
    for i in range(num_entries):
        e = entries[i * entry_size:(i + 1) * entry_size]
        if len(e) < entry_size:
            break
        _tguid, _uguid, first, last = struct.unpack("<16s16sQQ", e[:48])
        if first == 0 and last == 0:
            continue
        name = e[56:56 + 72].decode("utf-16-le", "ignore").rstrip("\x00 \t")
        parts.append({"name": name or f"p{i+1}", "first_lba": first, "last_lba": last})
    return parts


def export_partitions(img_path, parts, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    with open(img_path, "rb") as src:
        for p in parts:
            size = (p["last_lba"] - p["first_lba"] + 1) * SECTOR
            out = os.path.join(out_dir, f"{p['name']}.img")
            print(f"  导出 {p['name']} ({size/2**30:.2f} GiB) -> {os.path.basename(out)}")
            src.seek(p["first_lba"] * SECTOR)
            with open(out, "wb") as dst:
                _copy_stream(src, dst, size)
            n += 1
    return n


# ---------------- lineage 底包 ----------------

def extract_vda_raw():
    """提取底包 vda 并确保 raw 形态，返回 raw 路径（底包缺失返回 None）。

    zip 内载体两种形态：raw .img 直通拷贝（lineage-23.2 起）；旧版 qcow2 走
    qemu-img convert。
    """
    if not os.path.isfile(LINEAGE_ZIP):
        print(f"[skip] 底包不存在: {LINEAGE_ZIP}")
        return None
    os.makedirs(LINEAGE_TMP, exist_ok=True)
    tmp_raw = os.path.join(LINEAGE_TMP, "vda.raw")
    with zipfile.ZipFile(LINEAGE_ZIP) as zf:
        member = lineage_vda_member(zf.namelist())
        if member is None:
            raise SystemExit(f"[err] 底包中未找到 vda 成员 {LINEAGE_VDA_MEMBERS}")
        if member.endswith(".img"):
            print(f"  提取 {member}（raw 直通）-> {tmp_raw}")
            with zf.open(member) as src, open(tmp_raw, "wb") as dst:
                _copy_stream(src, dst)
            return tmp_raw
        tmp_qcow = os.path.join(LINEAGE_TMP, "vda.qcow2")
        print(f"  提取 {member} -> {tmp_qcow}")
        with zf.open(member) as src, open(tmp_qcow, "wb") as dst:
            _copy_stream(src, dst)
    print(f"  qemu-img convert qcow2 -> raw: {os.path.basename(tmp_raw)}")
    subprocess.run([_qemu_img(), "convert", "-f", "qcow2", "-O", "raw",
                    tmp_qcow, tmp_raw], check=True)
    return tmp_raw


def _lineage_unpacked():
    """lineage 底包是否已解压：.unpacked 标记，或现存产物补认（历史解压无标记）。

    产物补认以关键产物为准（GPT boot 链 + super + vendor 基座），半成品会重跑。
    """
    if os.path.isfile(LINEAGE_UNPACKED_MARK):
        return True
    done = [
        os.path.join(LINEAGE_OUT_DIR, "super.img"),
        LINEAGE_BOOT_IMG,
        LINEAGE_VENDOR_BOOT_IMG,
        os.path.join(LINEAGE_SUPER_DIR, "vendor_a.img"),
        os.path.join(LINEAGE_SUPER_DIR, "vendor_dlkm_a.img"),
    ]
    if not all(os.path.isfile(p) for p in done):
        return False
    with open(LINEAGE_UNPACKED_MARK, "w") as f:
        f.write("")
    print(f"[..] 现存产物补认完成标记: {LINEAGE_UNPACKED_MARK}")
    return True


def lineage_task(only_list=False):
    """底包 vda -> GPT 分区 -> 拆 super 逻辑分区；已解压过则跳过。"""
    if not only_list and _lineage_unpacked():
        print(f"[skip] lineage 底包已解压: {LINEAGE_OUT_DIR}")
        return 0
    try:
        tmp_vda = extract_vda_raw()
        if tmp_vda is None:
            return 0
        parts = read_partitions(tmp_vda)
        if only_list:
            print("分区表 (lineage 底包):")
            for p in parts:
                size = (p["last_lba"] - p["first_lba"] + 1) * SECTOR
                print(f"  {p['name']:<24} {size/2**30:8.2f} GiB  "
                      f"LBA {p['first_lba']}..{p['last_lba']}")
            print(f"\n共 {len(parts)} 个分区")
            return 0
        print(f"解析 GPT 完成，{len(parts)} 个分区 -> {LINEAGE_OUT_DIR}")
        n = export_partitions(tmp_vda, parts, LINEAGE_OUT_DIR)
        print(f"[ok] 底包分区已导出 {n} 个到 {LINEAGE_OUT_DIR}")
        sup = os.path.join(LINEAGE_OUT_DIR, "super.img")
        if os.path.isfile(sup):
            m = unpack_super(sup, LINEAGE_SUPER_DIR)
            print(f"[ok] 底包 super 拆出 {m} 个逻辑分区")
        for need in (LINEAGE_BOOT_IMG, LINEAGE_VENDOR_BOOT_IMG):
            if not os.path.isfile(need):
                print(f"[warn] 底包缺关键分区 {os.path.basename(need)}"
                      "（Android 16 底包应含 boot/vendor_boot；init_boot 可缺省）")
        if os.path.isfile(LINEAGE_INIT_BOOT_IMG):
            print(f"[ok] 底包含 init_boot_a.img（boot 阶段 ramdisk 载体）")
        with open(LINEAGE_UNPACKED_MARK, "w") as f:
            f.write("")
        print(f"[ok] 标记解压完成: {LINEAGE_UNPACKED_MARK}")
    finally:
        shutil.rmtree(LINEAGE_TMP, ignore_errors=True)
    return 0


# ---------------- super 逻辑分区拆解 ----------------

def _read_table(f, hdr, offset, desc):
    off, num, entry_size = desc
    header_size = struct.unpack_from("<I", hdr, 8)[0]
    f.seek(METADATA_OFFSET + header_size + off)
    return f.read(num * entry_size), num, entry_size


def unpack_super(super_path, out_dir):
    """Python 版 lpunpack：把 super.img 逻辑分区拆为独立镜像（AOSP liblp 通用）。

    输入须为 raw（官方 sparse 由 extract 任务先行展开到 super/super.raw.img）；
    是 sparse 时自动纯 Python 展开到临时文件后再解析。已拆过的分区（文件存在且
    尺寸一致）跳过，便于官方/lineage 两处共用与重跑。
    """
    if not os.path.isfile(super_path):
        print(f"[skip] 不存在: {super_path}")
        return 0
    raw = None
    if _is_sparse(super_path):
        raw = super_path + ".raw-unpack"
        if not os.path.isfile(raw):
            _unsparse_to(super_path, raw)
        super_path = raw
    os.makedirs(out_dir, exist_ok=True)
    with open(super_path, "rb") as f:
        f.seek(METADATA_OFFSET)
        hdr = f.read(256)
        if struct.unpack_from("<I", hdr, 0)[0] != LP_METADATA_HEADER_MAGIC:
            raise SystemExit(f"[err] {super_path} 不是合法 super 动态分区镜像")
        pdata, pnum, pesz = _read_table(f, hdr, 0, struct.unpack_from("<III", hdr, 80))
        edata, _en, eesz = _read_table(f, hdr, 0, struct.unpack_from("<III", hdr, 92))
        exported = 0
        for i in range(pnum):
            e = pdata[i * pesz:(i + 1) * pesz]
            name = e[0:36].split(b"\x00", 1)[0].decode("ascii", "ignore")
            attrs = struct.unpack_from("<I", e, 36)[0]
            first_ext, num_ext = struct.unpack_from("<II", e, 40)
            if not name or (attrs & LP_PARTITION_ATTR_DISABLED):
                continue
            if (attrs & LP_PARTITION_ATTR_SLOT_SUFFIXED) and not name.endswith(("_a", "_b")):
                name += "_a"
            segs, total = [], 0
            for k in range(num_ext):
                et = edata[(first_ext + k) * eesz:(first_ext + k + 1) * eesz]
                nsectors, ttype, tdata, _tsource = struct.unpack_from("<QIQI", et, 0)
                if ttype != 0:  # LP_TARGET_TYPE_LINEAR（0 以外如 ZERO 跳过，与 note13 一致）
                    continue
                segs.append((tdata * SECTOR, nsectors * SECTOR))
                total += nsectors * SECTOR
            if not segs:
                continue
            out = os.path.join(out_dir, f"{name}.img")
            if os.path.isfile(out) and os.path.getsize(out) == total:
                print(f"  逻辑分区 {name} 已存在（{total/2**30:.2f} GiB），跳过")
                exported += 1
                continue
            print(f"  逻辑分区 {name} ({total/2**30:.2f} GiB) -> {os.path.basename(out)}")
            with open(out, "wb") as dst:
                for soff, slen in segs:
                    f.seek(soff)
                    remaining = slen
                    while remaining > 0:
                        buf = f.read(min(_CHUNK, remaining))
                        if not buf:
                            raise SystemExit("[err] super 读取越界")
                        dst.write(buf)
                        remaining -= len(buf)
            exported += 1
    if raw is not None and os.path.exists(raw):
        os.remove(raw)  # 释放展开的临时 raw
    return exported


def super_task():
    """拆官方 super 逻辑分区到 roms/<解压目录>/super/（参照件/基线校验源）。"""
    src = SUPER_RAW if os.path.isfile(SUPER_RAW) else SUPER_IMG
    if not os.path.isfile(src):
        print(f"[skip] 官方 super 不存在: {SUPER_RAW}（先跑 extract）")
        return 0
    n = unpack_super(src, SUPER_OUT)
    print(f"[ok] 官方 super 拆出 {n} 个逻辑分区 -> {SUPER_OUT}")
    return 0


# ---------------- 工具 ----------------

def _copy_stream(src, dst, length=None):
    chunk = _CHUNK
    remaining = length  # None 表示读到底
    while remaining is None or remaining > 0:
        buf = src.read(min(chunk, remaining) if remaining else chunk)
        if not buf:
            break
        dst.write(buf)
        if remaining is not None:
            remaining -= len(buf)


def main():
    argv = sys.argv[1:]
    only_list = "--list" in argv
    keys = [a.lower() for a in argv if a != "--list"]
    if not keys:
        keys = ["extract", "lineage", "super"]

    todo = [t for t in ("extract", "lineage", "super") if t in keys]
    if not todo:
        print("未匹配到任务，可用关键字: extract / lineage / super")
        return 1

    for t in todo:
        print(f"==> [{t}]")
        if t == "extract":
            extract_tgz()
        elif t == "lineage":
            lineage_task(only_list=only_list)
        elif t == "super":
            super_task()
    return 0


if __name__ == "__main__":
    sys.exit(main())
