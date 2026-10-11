#!/usr/bin/env python3
"""popsicle-16 vendor_dlkm.img 重打包编排（由 build-vendor_dlkm.py 薄入口调用）。

参考 note13-5G-15 的同一分层（debugfs 工具层 + modules/props 规则模块）：
  1. 以 lineage 底包 vendor_dlkm_a.img 为基座（virtio 内核匹配；真机厂商
     vendor_dlkm 是 SM8850 厂商内核模块/驱动，不可直接用于 virtio 云主机）；
  2. modules.py：内核模块产物（out/dist/kernel/modules/）就位时，debugfs 原地
     补丁注入 .ko 并重建 modules.load/dep/alias 索引；未就位时退化为基座直通；
  3. props.py：以官方基线覆盖 /etc/build.prop（popsicle 设备身份，参考
     note13-5G-15 的 gold 件思路）；
  4. e2fsck -fy 重打包 -> out/dist/rom/vendor_dlkm.img。

规则模块（modules / props）只产出 debugfs 命令清单，本模块编排一次 debugfs
会话 + 回读校验 + e2fsck + 输出。
"""

import os
import shutil
import sys

from buildenv import (
    LINEAGE_SUPER_DIR, ROM_DIST, TMP_DIR, VENDOR_DLKM_DIST,
)
from debugfs import E2FSCK, run, run_command_file
import modules
import props

# lineage 底包 vendor_dlkm 逻辑分区（virtio 内核匹配）作基座
VENDOR_DLKM_BASE = os.path.join(LINEAGE_SUPER_DIR, "vendor_dlkm_a.img")

WORK_DIR = os.path.join(TMP_DIR, "vendor_dlkm")


def build_vendor_dlkm(dry_run=False):
    if not os.path.isfile(VENDOR_DLKM_BASE):
        print(f"[err] 基座 vendor_dlkm_a.img 缺失: {VENDOR_DLKM_BASE}（先 unpack lineage）")
        sys.exit(1)

    os.makedirs(WORK_DIR, exist_ok=True)
    work_img = os.path.join(WORK_DIR, "vendor_dlkm.work.img")
    if not dry_run:
        shutil.copy2(VENDOR_DLKM_BASE, work_img)
    print(f"[ok] 基座副本: {VENDOR_DLKM_BASE} -> {work_img}")

    # 内核模块注入（modules）+ build.prop 基线覆盖（props），合并为一次 debugfs 会话
    cmds = []
    kois = modules.discover_modules()
    if kois:
        if dry_run:
            print(f"[..] (dry) 将注入 {len(kois)} 个模块并重建索引")
        else:
            cmds += modules.inject_cmds(work_img, kois, WORK_DIR)
    else:
        print("[..] 内核模块产物未就位，退化为基座直通（仅覆盖 build.prop）")

    if dry_run:
        print("[..] (dry) 将用官方基线覆盖 /etc/build.prop")
    else:
        cmds += props.prop_cmds(work_img, WORK_DIR)

    if not dry_run:
        if not run_command_file(work_img, cmds, WORK_DIR):
            print("[err] debugfs 写入失败")
            sys.exit(1)
        print("[ok] debugfs 写入完成")
        if kois:
            modules.verify(work_img, kois, WORK_DIR)

    # e2fsck 修复一致性（rc 1 = 已修复，视为成功）
    if not dry_run:
        res = run([E2FSCK, "-fy", work_img], capture_output=True)
        if res.returncode not in (0, 1):
            print(res.stderr.decode("utf8", "replace"))
            print(f"[err] e2fsck 失败: {res.returncode}")
            sys.exit(1)
        print("[ok] e2fsck 通过")

    os.makedirs(ROM_DIST, exist_ok=True)
    if not dry_run:
        shutil.copy2(work_img, VENDOR_DLKM_DIST)
    print(f"[ok] vendor_dlkm.img -> {VENDOR_DLKM_DIST}")
    return 0
