#!/usr/bin/env python3
"""popsicle-16 vendor_boot.img 处理（以 lineage-23 UTM 底包 vendor_boot_a.img 为基座）。

处理内容（与 note13-5G-15 同语义，适配 Android 16 / Qualcomm GKI）：
  1. 删除 ramdisk fragment 内 first_stage fstab 副本：GRUB 的 `eval initrd` 会把
     boot 链 ramdisk 排在前面、vendor_boot fragment 排在后面，initramfs 解包时后者
     覆盖前者；删掉 fragment 副本后，boot/init_boot 注入的 fstab.qcom（build-boot.py）
     成为唯一来源。
  2. cmdline/bootconfig 修正：
     - `androidboot.verifiedbootstate=orange` -> green（等长空格补齐）：锁定设备配
       橙色引导 = 不一致 RootOfTrust，host secure_env 拒绝回应 → keymint 卡死。
     - `androidboot.hardware`（ro.hardware）对齐真机 **qcom**：init 据此拼
       fstab.qcom / init.qcom.rc（patch.img 注入，见 build-boot.py / vendor/patch）。
       cmdline 内替换为等长（空格补齐）；bootconfig 内按行重写（更新长度字段）。

vendor_boot v4 布局（page 对齐由 header 字段推导；**dtb 段在 vendor_ramdisk 与
fragment table 之间**）：
  header(2128) → [pad] → vendor_ramdisk（各 fragment 压缩块顺序拼接）
  → [pad] → dtb → [pad] → vendor_ramdisk_table
  （每 fragment: size/offset/type/name[32]/board_id[16]，条目尺寸取自 header）
  → [pad] → bootconfig
fragment 尺寸变化后须同步更新 header 的 vendor_ramdisk_size 与 table 内各 fragment 的
size/offset（table 条目数与条目大小不变）。非目标 fragment（如 recovery/dlkm）原样保留。

用法（可在任何目录）：
  python3 build/scripts/build-vendor-boot.py             # 产出 out/dist/rom/vendor_boot.img
  python3 build/scripts/build-vendor-boot.py --dry-run   # 只打印不执行
"""

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from buildenv import LINEAGE_OUT_DIR, VENDOR_BOOT_DIST, env, lz4_tool  # noqa: E402

# vendor_boot 基底：lineage-23 底包 vda 产出的 vendor_boot_a.img（与 boot.img 同源）
LINEAGE_DIR = env("CDH_LINEAGE_DIR", LINEAGE_OUT_DIR)
VENDOR_BOOT_BASE = os.path.join(LINEAGE_DIR, "vendor_boot_a.img")

# vendor_boot v4 header 字段偏移（小端 u32）
VENDOR_BOOT_MAGIC = b"VNDRBOOT"
HDR_VERSION_OFF = 8
PAGE_SIZE_OFF = 12
CMDLINE_OFF = 28
CMDLINE_SIZE = 2048
VENDOR_RAMDISK_SIZE_OFF = 24
HEADER_SIZE_OFF = 2096
DTB_SIZE_OFF = 2100
TABLE_SIZE_OFF = 2112
TABLE_NUM_OFF = 2116
TABLE_ESIZE_OFF = 2120
BOOTCONFIG_SIZE_OFF = 2124

# 要删除的 fstab 基线名（basename 前缀命中即删）：
# lineage 底包 fragment 内可能残留任意 fstab.<hw>（qcom/virtio...）副本；
# 本机构建注入的 first-stage fstab 在 boot/init_boot ramdisk（build-boot.py），
# fragment 内任何 first_stage fstab 都必须清除，否则覆盖注入件。
FSTAB_PREFIX = "fstab."


def _align_up(value, align):
    return (value + align - 1) // align * align


def _is_fstab(name):
    base = name.rsplit("/", 1)[-1]
    return base.startswith(FSTAB_PREFIX)


def _drop_fstab_from_cpio(archive):
    """newc cpio 删除目标 fstab 项，返回 (新 cpio, 删除数)。"""
    raw_out = bytearray()
    dropped = 0
    off = 0
    n = len(archive)

    def pad4():
        nonlocal raw_out
        raw_out += b"\x00" * ((4 - len(raw_out) % 4) % 4)

    while off + 110 <= n:
        hdr = archive[off:off + 110]
        if hdr[:6] not in (b"070701", b"070702"):
            raise ValueError(f"cpio magic 错误: {hdr[:6]!r} @ {off}")
        size = int(hdr[54:62], 16)
        namesize = int(hdr[94:102], 16)
        if namesize == 0:
            raise ValueError("cpio namesize 为 0")
        name_start = off + 110
        name_end = name_start + namesize
        name_raw = archive[name_start:name_end]
        name = name_raw.rstrip(b"\x00").decode("utf-8", "replace")
        data_start = _align_up(name_end, 4)
        data_end = data_start + size
        payload = archive[data_start:data_end]

        if not _is_fstab(name):
            # newc 对齐是"绝对偏移 mod 4"，须按 raw_out 累计长度对齐
            new_hdr = bytearray(hdr)
            new_hdr[54:62] = b"%08x" % len(payload)
            raw_out += bytes(new_hdr)
            raw_out += name_raw
            pad4()
            raw_out += payload
            pad4()
        else:
            dropped += 1
        off = _align_up(data_end, 4)
        if name == "TRAILER!!!":
            break
    return bytes(raw_out), dropped


def _strip_fragment(blob):
    """若 fragment 是 legacy-lz4(newc cpio) 且含 fstab 项，删掉后重压。

    非 cpio / 解压失败 / 无目标项 => 原样返回 (blob, 0)。
    """
    lz4 = lz4_tool()
    work = tempfile.mkdtemp(prefix="cdh-vb.")
    try:
        in_lz4 = os.path.join(work, "frag.in.lz4")
        cpio = os.path.join(work, "frag.cpio")
        out_lz4 = os.path.join(work, "frag.out.lz4")
        with open(in_lz4, "wb") as f:
            f.write(blob)
        if subprocess.run([lz4, "-d", "-f", in_lz4, cpio],
                          capture_output=True).returncode != 0:
            return blob, 0
        archive = open(cpio, "rb").read()
        if not archive.startswith((b"070701", b"070702")):
            return blob, 0
        patched, dropped = _drop_fstab_from_cpio(archive)
        if dropped == 0:
            return blob, 0
        subprocess.run([lz4, "-12", "-l", "-f", "-", out_lz4],
                       input=patched, check=True, capture_output=True)
        return open(out_lz4, "rb").read(), dropped
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _rewrite_cmdline(data, old, new):
    """cmdline（header 内定长区）等长替换：new 短于 old 时空格补齐。

    cmdline 在 header 中是 2048 字节定长区，变短会把其后的 header 字段整体左移；
    用空格补齐到原长即可（空格是 cmdline 分隔符，多余无害）。
    """
    if len(new) > len(old):
        print(f"[warn] cmdline 替换 {old!r} -> {new!r} 变长，跳过（须等长或更短）")
        return data, False
    repl = new + b" " * (len(old) - len(new))
    if old in data:
        return data.replace(old, repl), True
    return data, False


def _rewrite_bootconfig(bc, key, value):
    """bootconfig 行级重写（含 "#BOOTCONFIG" 头；返回新 blob, 是否变更）。

    bootconfig 位于 vendor_boot 末尾且自带长度字段，长度可变，直接改 header 的
    BOOTCONFIG_SIZE 即可。
    """
    lines = bc.split(b"\n")
    prefix = key.encode() + b"="
    changed = False
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            want = prefix + value.encode()
            if line != want:
                lines[i] = want
                changed = True
    if not changed:
        return bc, False
    return b"\n".join(lines), True


def build_vendor_boot(dry_run=False):
    if not os.path.isfile(VENDOR_BOOT_BASE):
        print(f"[err] 缺少 lineage vendor_boot 基底: {VENDOR_BOOT_BASE}"
              "（先 unpack lineage 底包）")
        return 1
    if dry_run:
        print(f"[..] (dry) {VENDOR_BOOT_BASE} -> {VENDOR_BOOT_DIST}"
              "（fragment 内 fstab.* 清理 + verifiedbootstate/hardware 修正）")
        return 0

    data = bytearray(open(VENDOR_BOOT_BASE, "rb").read())
    if data[:8] != VENDOR_BOOT_MAGIC:
        print(f"[err] 非 vendor_boot 镜像: {VENDOR_BOOT_BASE}")
        return 1

    # —— verified boot state：lineage 基底 cmdline 可能自带
    #   `androidboot.verifiedbootstate=orange`；GRUB 拼 cmdline 顺序为
    #   boot.img → vendor_boot → kernel_cmdline_dynamic（pack-roms 注入
    #   green+locked+vbmeta.digest），init 取**第一个** verifiedbootstate。
    #   orange(6) vs green(5)：空格补齐 6 字节等长替换。
    data, ok = _rewrite_cmdline(data, b"androidboot.verifiedbootstate=orange",
                                b"androidboot.verifiedbootstate=green")
    print("[..] vendor_boot cmdline: verifiedbootstate orange -> green"
          if ok else "[..] vendor_boot cmdline 无 orange，跳过")

    # —— ro.hardware 对齐真机（qcom）：init 据此定 ro.hardware=qcom，进而拼
    #   fstab.qcom / init.qcom.rc（patch.img 注入）。cmdline 中 virtio(6) -> qcom(4)
    #   空格补齐等长；不在 cmdline 则看 bootconfig（Android16 GKI 常放 bootconfig）。
    data, ok = _rewrite_cmdline(data, b"androidboot.hardware=virtio",
                                b"androidboot.hardware=qcom")
    if ok:
        print("[..] vendor_boot cmdline: androidboot.hardware virtio -> qcom")
    else:
        print("[..] vendor_boot cmdline 无 androidboot.hardware，检查 bootconfig")

    hdr_ver = struct.unpack_from("<I", data, HDR_VERSION_OFF)[0]
    if hdr_ver != 4:
        print(f"[err] 仅支持 vendor_boot v4（当前 {hdr_ver}）")
        return 1

    page = struct.unpack_from("<I", data, PAGE_SIZE_OFF)[0]
    hdr_size = struct.unpack_from("<I", data, HEADER_SIZE_OFF)[0]
    vrs = struct.unpack_from("<I", data, VENDOR_RAMDISK_SIZE_OFF)[0]
    dtb_size = struct.unpack_from("<I", data, DTB_SIZE_OFF)[0]
    tbl_size, tbl_num, tbl_esize = struct.unpack_from("<III", data, TABLE_SIZE_OFF)
    bc_size = struct.unpack_from("<I", data, BOOTCONFIG_SIZE_OFF)[0]

    vr_off = _align_up(hdr_size, page)
    dtb_off = vr_off + _align_up(vrs, page)
    tb_off = dtb_off + _align_up(dtb_size, page)
    bc_off = tb_off + _align_up(tbl_size, page)
    seg = bytes(data[vr_off:vr_off + vrs])
    dtb = bytes(data[dtb_off:dtb_off + dtb_size])
    table = bytes(data[tb_off:tb_off + tbl_size])
    bootconfig = bytes(data[bc_off:bc_off + bc_size])
    print(f"[..] 布局: vrs={vrs} dtb={dtb_size} table={tbl_size} bootconfig={bc_size} "
          f"page={page}")

    # —— bootconfig：androidboot.hardware 行重写（cmdline 未命中时的落点）
    new_bc, bc_changed = _rewrite_bootconfig(bootconfig, "androidboot.hardware", "qcom")
    if bc_changed:
        print("[..] vendor_boot bootconfig: androidboot.hardware -> qcom")
    bootconfig = new_bc

    new_seg = bytearray()
    new_table = bytearray()
    dropped_total = 0
    for i in range(tbl_num):
        entry = bytearray(table[i * tbl_esize:(i + 1) * tbl_esize])
        rsize, roff, _rtype = struct.unpack_from("<III", entry, 0)
        blob = seg[roff:roff + rsize]
        new_blob, dropped = _strip_fragment(blob)
        dropped_total += dropped
        name = entry[12:44].rstrip(b"\x00").decode("latin1") or "(platform)"
        print(f"[..] fragment{i} {name}: {rsize} B -> {len(new_blob)} B"
              f"（删 fstab {dropped} 项）")
        struct.pack_into("<II", entry, 0, len(new_blob), len(new_seg))
        new_seg += new_blob
        new_table += entry
    if dropped_total == 0:
        print(f"[warn] fragment 内未发现 first_stage fstab 副本: {VENDOR_BOOT_BASE}")

    out = bytearray(data[:vr_off])
    out += new_seg
    out += b"\x00" * (_align_up(len(out), page) - len(out))
    out += dtb
    out += b"\x00" * (_align_up(len(out), page) - len(out))
    out += bytes(new_table)
    out += b"\x00" * (_align_up(len(out), page) - len(out))
    out += bootconfig
    struct.pack_into("<I", out, VENDOR_RAMDISK_SIZE_OFF, len(new_seg))
    struct.pack_into("<I", out, TABLE_SIZE_OFF, len(new_table))
    struct.pack_into("<I", out, BOOTCONFIG_SIZE_OFF, len(bootconfig))

    os.makedirs(os.path.dirname(VENDOR_BOOT_DIST), exist_ok=True)
    tmp = VENDOR_BOOT_DIST + ".new.%d" % os.getpid()
    with open(tmp, "wb") as f:
        f.write(out)
    os.replace(tmp, VENDOR_BOOT_DIST)
    print(f"[ok] vendor_boot.img -> {VENDOR_BOOT_DIST}"
          f"（{len(out)} B，删除 fstab {dropped_total} 项）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="popsicle-16 vendor_boot.img 处理")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args = ap.parse_args()
    sys.exit(build_vendor_boot(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
