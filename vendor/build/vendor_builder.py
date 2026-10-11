#!/usr/bin/env python3
"""popsicle-16 vendor.img 重打包编排（由 build-vendor.py 薄入口调用）。

两种基座（`CDH_VENDOR_BASE=gold|lineage`，默认 lineage，既有可引导链路不变）：

  gold（目标形态）：**厂商 vendor 为底**。super 是官方字节直拷，guest 的 /vendor 就是
  官方 vendor_a.img；本阶段产出的 vendor.img 只是「清单内的受控覆盖树」= 官方 verbatim
  （gold_tree 自官方 erofs 提取）+ lineage 搬运件 + CDH 自产注入，由 vdc 挂到
  /mnt/vendor，内核按覆盖清单逐路径重定向（fs/cdh_vendor_rules.h 由 gold_tree --emit
  生成）。清单外的每个 /vendor 路径仍照读官方原件。实现见 build_vendor_gold。

  lineage（现状）：以 lineage 底包 ext4 vendor_a.img 为基座整目录顶替
  /vendor/{etc,bin,lib,lib64}，在其上打官方对齐件。实现见 build_vendor。

两条路线共用同一批下游模块（inject/prune/sepolicy/fstab/ueventd/vintf/props），差别只在
基座内容；gold 下这些模块大多自然变成 no-op 或"官方件 + 增量"：
  - gold_props / gold_selinux 的"换成官方件" -> 已是官方字节，跳过；
  - prune 的 boringssl 32 位自检 rc、fstab 的 fstab.virtio* 在官方树里本就不存在 -> no-op
    （且官方 /vendor/bin 有 32 位自检件，该死循环从根上不存在）；
  - vintf 的 manifest.xml target-level 对齐 -> 官方无 manifest.xml，跳过。

入口 build-vendor.py 仅做参数解析后委托 build_vendor(dry_run=...)。
"""

import math
import os
import re
import shutil
import sys

from buildenv import (
    LINEAGE_SUPER_DIR, ROM_DIST, TMP_DIR, VENDOR_DIST, VENDOR_PATCH_DIR,
    VENDOR_STAGE_DIR, e2fs_tools,
)
from debugfs import DEBUGFS, E2FSCK, cat, run, run_command_file
import fstab
import gold_props
import gold_selinux
import gold_tree
import inject
import props
import prune
import sepolicy
import source_build
import vintf

RESIZE2FS = e2fs_tools()[2]
MKFS_EXT4 = os.path.join(os.path.dirname(DEBUGFS), "mkfs.ext4")

# lineage 底包 vendor 逻辑分区（virtio 内核匹配）作基座
VENDOR_BASE = os.path.join(LINEAGE_SUPER_DIR, "vendor_a.img")

WORK_DIR = os.path.join(TMP_DIR, "vendor")

# gold 覆盖树镜像大小：lineage 搬运 apex 约 101 MiB + 官方 verbatim 子树约 4 MiB
# + 图形栈别名约 48 MiB + CDH 注入件，再加 inode 表（每组 24576×256 B）与后续
# debugfs write 的富余。mkfs -d 会按内容自动 sizing，此处给显式大小以保证镜像内
# 有富余 inode/块（无需 resize）。
GOLD_VENDOR_SIZE_MIB = 320

# ueventd 视频节点规则：v4l2 HAL（android.hardware.media.v412）以 media 用户运行，
# 而内核 cdh_media.ko（virtio-media 驱动）创建的 /dev/video* 默认 0600 root:root，
# DAC 直接拒绝 open（早于 SELinux）。对齐像素链路：属主 media:camera。
# /dev/video* 必须允许 media 组读写，否则 enumerateSupportedPixelformats 全空、
# V4L2Decoder open("/dev/video0") 返回 EACCES（note13 实测 errno=13）。
# 本盘暂无媒体 HAL，规则先就位（幂等，无害）。
UEVENTD_VIDEO_RULE = "/dev/video*          0660    media   camera"
UEVENTD_VIDEO_MARKER = b"# cdh popsicle: virtio-media v4l2 video nodes"
UEVENTD_VIDEO_LABEL = "u:object_r:vendor_configs_file:s0"

# FRP 块设备授权：MIUI 16 的 PersistentDataBlockService 按
# ro.frp.pst=/dev/block/bootdevice/by-name/frp 打开 FRP 分区（该路径由
# build/vendor/patch/etc/init/cph_frp_symlink.rc 软链到未使用的 misc/vda3）。
# 但 /dev/block/vda3 默认 0600 root:root，system_server(uid 1000) 打开会 EACCES：
#   java.io.FileNotFoundException: /dev/block/bootdevice/by-name/frp: open failed:
#   EACCES (Permission denied) → pdb init 超时 → system_server FATAL（实测 2026-10-09）。
# 真机 FRP 分区即 root:system 0660 可读写，此处对齐。
UEVENTD_FRP_RULE = "/dev/block/vda3       0660    root    system"
UEVENTD_FRP_MARKER = b"# cdh popsicle: FRP block device (MIUI pdb)"

# Remote KeyMint 通道节点：/dev/hvc3 由 virtio_console.ko + ueventd 创建，lineage-23.2
# 基座 vendor ueventd.rc 无 /dev/hvc* 规则（note13 的 lineage-22 基座有 0666 system system，
# 故其 keymint/radio 未遇此墙）→ 节点落默认 0600 root:root，而服务以 nobody 运行，
# open() 直接 EACCES（SELinux 侧 keymaster_device 授权已齐、无 avc 拒绝）：
#   keymint-service.remote: Could not connect to keymaster: Permission denied → SIGABRT。
# ueventd 规则按字面量优于通配排序，本行只放开 hvc3，不动 hvc0/1（固件 console）。
UEVENTD_KEYMINT_RULE = "/dev/hvc3\t\t0666\tsystem\tsystem"
UEVENTD_KEYMINT_MARKER = b"# cdh popsicle: remote keymint channel /dev/hvc3"


def ueventd_video_cmds(work_img, tmp_dir=None):
    """读回 /etc/ueventd.rc，追加所需规则（video / FRP / keymint，逐条幂等），返回 debugfs 命令。

    与 fstab 同机制：基于工作镜像的「原始」内容拼接，命令序里只有最后一次 write
    生效——故所有规则在**一次** write 里补齐（否则后写的会覆盖先写的）。
    """
    tmp_dir = tmp_dir or os.path.join(TMP_DIR, "vendor")
    os.makedirs(tmp_dir, exist_ok=True)
    img = "/etc/ueventd.rc"
    data = cat(work_img, img)
    if data is None:
        print(f"[warn] 镜像内缺 {img}，跳过节点授权")
        return []
    need_video = UEVENTD_VIDEO_MARKER not in data
    need_frp = UEVENTD_FRP_MARKER not in data
    need_keymint = UEVENTD_KEYMINT_MARKER not in data
    if not need_video and not need_frp and not need_keymint:
        print("[ok] ueventd.rc 已含 video/FRP/keymint 规则，跳过")
        return []
    rule = b""
    if need_video:
        rule += b"\n" + UEVENTD_VIDEO_MARKER + b"\n" + \
            UEVENTD_VIDEO_RULE.encode() + b"\n"
    if need_frp:
        rule += b"\n" + UEVENTD_FRP_MARKER + b"\n" + \
            UEVENTD_FRP_RULE.encode() + b"\n"
    if need_keymint:
        rule += b"\n" + UEVENTD_KEYMINT_MARKER + b"\n" + \
            UEVENTD_KEYMINT_RULE.encode() + b"\n"
    tmp = os.path.join(tmp_dir, "ueventd.rc")
    with open(tmp, "wb") as f:
        f.write(data + rule)
    added = "+".join(n for n, need in (("video", need_video), ("FRP", need_frp),
                                       ("keymint", need_keymint))
                     if need)
    print(f"[ok] ueventd.rc 追加节点规则（{added}，幂等）")
    return [
        f"rm {img}",
        f"write {tmp} {img}",
        f"sif {img} mode 0x81a4",
        f"ea_set {img} security.selinux {UEVENTD_VIDEO_LABEL}",
    ]


def _estimate_new_inodes():
    """统计注入树将占用的新 inode 数（文件 + 子目录，保守计）。"""
    n = 0
    for tree in (VENDOR_PATCH_DIR, VENDOR_STAGE_DIR):
        if not os.path.isdir(tree):
            continue
        for root, dirs, files in os.walk(tree):
            n += len(dirs) + len(files)
    return n


def _ensure_vendor_capacity(work_img, needed_inodes):
    """扩展 vendor 工作镜像，保证注入可用 inode/块。

    lineage 基座空闲 inode 极少（note13 实测 29），而 ext4 的 inode 只随新增 block
    group 增长；注入内容多时 write 会报 "Could not allocate inode in ext2
    filesystem"。按整组补足（+ipg inode/组）。
    """
    res = run([DEBUGFS, "-R", "stats", work_img], capture_output=True, text=True)
    out = res.stdout

    def _int(key):
        m = re.search(rf"^{re.escape(key)}:\s+(\d+)", out, re.M)
        return int(m.group(1)) if m else 0

    free_inodes = _int("Free inodes")
    free_blocks = _int("Free blocks")
    block_size = _int("Block size") or 4096
    ipg = _int("Inodes per group") or 1
    bpg = _int("Blocks per group")
    blocks = _int("Block count")

    inode_groups = math.ceil(max(0, needed_inodes - free_inodes) / ipg)
    if inode_groups == 0:
        return
    new_blocks = blocks + inode_groups * bpg
    print(f"[..] vendor inode 不足（空闲 {free_inodes} < 需 {needed_inodes}），"
          f"resize2fs +{inode_groups} 组 -> {new_blocks} 块（空闲块 {free_blocks}）")
    with open(work_img, "rb+") as f:
        f.truncate(new_blocks * block_size)
    res2 = run([RESIZE2FS, work_img], capture_output=True, text=True)
    if res2.returncode not in (0, 1):
        print(res2.stderr)
        sys.exit(1)


def build_vendor_gold(dry_run=False):
    """gold 基座：vendor.img = 覆盖清单里的受控覆盖树（官方字节为底）。

    形态与 build-patch.py 同源：先把官方 verbatim + 搬运件铺到宿主 staging 树，一次
    `mkfs.ext4 -d` 导入，再回填官方 mode/uid/gid/SELinux 标签，最后跑与 lineage 期共用
    的下游 debugfs 流水线（注入树 + sepolicy 追加 + ueventd + build.prop）。
    """
    if not os.path.isdir(VENDOR_PATCH_DIR):
        print(f"[err] 注入树缺失: {VENDOR_PATCH_DIR}")
        sys.exit(1)
    stage = os.path.join(WORK_DIR, "gold-stage")
    work_img = os.path.join(WORK_DIR, "vendor.work.img")
    if dry_run:
        print(f"[..] (dry) gold 基座：staging={os.path.relpath(stage)} -> "
              f"mkfs.ext4 -d -> 回填 {len(gold_tree.GOLD_DIRS)} 个官方子树 + "
              f"{len(gold_tree.GENERATED_FILES)} 个官方件 + lineage 搬运件 + 注入树")
        return 0

    source_build.run_source_builds(dry_run=dry_run)
    os.makedirs(WORK_DIR, exist_ok=True)
    meta = gold_tree.stage_tree(stage)

    if os.path.exists(work_img):
        os.remove(work_img)
    run([MKFS_EXT4, "-q", "-F", "-b", "4096", "-d", stage, work_img,
         str(GOLD_VENDOR_SIZE_MIB * 1024 * 1024 // 4096)], check=True)
    print(f"[ok] mkfs.ext4 -d 导入覆盖树: {work_img}"
          f"（{GOLD_VENDOR_SIZE_MIB} MiB，{len(meta)} 个 inode）")

    if not run_command_file(work_img, gold_tree.stage_backfill_cmds(meta), WORK_DIR):
        print("[err] 官方 mode/uid/gid/SELinux 标签回填失败")
        sys.exit(1)

    # 下游流水线（顺序与 lineage 期一致；sepolicy 链读镜像「当前」内容 = 官方件）
    gold_props_note_off()
    # 注入树已在 staging 阶段原生落盘并由 backfill 打上 mode/uid/gid/标签，这里不再
    # 走 inject 的 debugfs 写入（同一批字节不留第二条路径）。
    # 只调一次：prune_cmds 会先 path_exists 再产出 rm，重复调用会把同一批 rm 命令
    # 排进同一个 debugfs 批处理，第二次那些文件已不存在 -> 整批注入报错中断。
    cmds = prune.prune_cmds(work_img)
    cmds += sepolicy.patch_commands(work_img, WORK_DIR)
    cmds += fstab.fstab_cmds(work_img)
    cmds += ueventd_video_cmds(work_img, WORK_DIR)
    cmds += vintf.vintf_cmds(work_img, WORK_DIR)
    bp_host = props.host_content(work_img, WORK_DIR)
    cmds += props.prop_cmds(work_img, bp_host)
    if not run_command_file(work_img, cmds, WORK_DIR):
        print("[err] debugfs 注入失败")
        sys.exit(1)
    print("[ok] debugfs 注入完成")

    _fsck_and_publish(work_img, strip_lost_found=True)
    return 0


def gold_props_note_off():
    """gold 基座下官方对齐步骤天然 no-op，只说明不执行。"""
    print("[..] 跳过 gold_props 属性上下文替换（/etc/selinux 已是官方 verbatim）")
    print("[..] 跳过 gold_selinux /etc/selinux 替换（同上）")


def _fsck_and_publish(work_img, dry_run=False, strip_lost_found=False):
    """e2fsck 修复（rc 1 = 已修复，视为成功）并发布 out/dist/rom/vendor.img。"""
    if not dry_run:
        res = run([E2FSCK, "-fy", work_img], capture_output=True)
        if res.returncode not in (0, 1):
            print(res.stderr.decode("utf8", "replace"))
            print(f"[err] e2fsck 失败: {res.returncode}")
            sys.exit(1)
        print("[ok] e2fsck 通过")
        if strip_lost_found:
            # mke2fs 建的 lost+found 会跟着进覆盖树，且删早了 e2fsck 还会重建
            # （"Lost+found directory is missing. Creating."），只能在 fsck 之后摘掉。
            # 官方 vendor 是 erofs，本就没有这个目录。
            run_command_file(work_img, ["rmdir /lost+found"], WORK_DIR)
    os.makedirs(ROM_DIST, exist_ok=True)
    if not dry_run:
        shutil.copy2(work_img, VENDOR_DIST)
    print(f"[ok] vendor.img -> {VENDOR_DIST}")


def build_vendor(dry_run=False):
    """lineage 基座路线（现状）：底包 vendor_a.img 副本 + 两次 debugfs 会话。

    会话一：官方对齐件先真实落盘（gold /etc/selinux 替换开关 + gold
    vendor_property_contexts 全量替换）——sepolicy 链在收集命令阶段读镜像「当前」
    内容，同会话命令尚未执行，必须分会话；
    会话二：注入树（inject）+ 裁剪（prune）+ sepolicy CIL 追加 + fstab 清理 +
    ueventd 视频节点 + build.prop 渲染修正（props）。
    """
    if gold_tree.gold_mode():
        return build_vendor_gold(dry_run=dry_run)
    if not os.path.isfile(VENDOR_BASE):
        print(f"[err] 基座 vendor_a.img 缺失: {VENDOR_BASE}（先 unpack lineage）")
        sys.exit(1)
    if not os.path.isdir(VENDOR_PATCH_DIR):
        print(f"[err] 注入树缺失: {VENDOR_PATCH_DIR}")
        sys.exit(1)

    # 0) 优先编译 source 子模块（daemon 等），产物就绪后再进入镜像重打包
    source_build.run_source_builds(dry_run=dry_run)

    os.makedirs(WORK_DIR, exist_ok=True)
    work_img = os.path.join(WORK_DIR, "vendor.work.img")
    if not dry_run:
        shutil.copy2(VENDOR_BASE, work_img)
    print(f"[ok] 基座副本: {VENDOR_BASE} -> {work_img}")

    # 注入前按注入树占用量扩展 inode/块（lineage 基座空闲 inode 极少；
    # gold /etc/selinux 替换件中镜像内不存在的新文件也占 inode）
    if not dry_run:
        _ensure_vendor_capacity(work_img,
                                _estimate_new_inodes() + gold_selinux.file_count())
    else:
        print(f"[..] (dry) 将校验 vendor inode 容量（注入树需 ~{_estimate_new_inodes()} inode）")

    # 注入树 + build.prop 渲染修正（一次 debugfs 会话）
    if dry_run:
        print(f"[..] (dry) gold /etc/selinux 替换开关={gold_selinux.ENABLED}；"
              f"将注入 vendor/patch 注入树并改 build.prop")
    else:
        # 官方 gold /etc/selinux 替换先真实落盘（单独一次 debugfs 会话）：sepolicy
        # 链在收集命令阶段读取镜像「当前」内容（同会话命令尚未执行）；若与它们
        # 同批，CDH CIL 段会叠在 lineage 策略上并成为最终写入者，替换被覆盖丢弃。
        # 默认关闭（CDH_GOLD_SELINUX=1 启用）：实测 gold 策略与 lineage 基座不兼容，
        # 启用后 boot 不完成（见 gold_selinux 模块「实测结论」）。
        if gold_selinux.ENABLED:
            if not run_command_file(work_img,
                                    gold_selinux.replace_cmds(work_img), WORK_DIR):
                print("[err] 官方 gold /etc/selinux 替换失败")
                sys.exit(1)
        else:
            print("[..] 跳过官方 gold /etc/selinux 替换（CDH_GOLD_SELINUX=1 可启用）")
        # vendor 属性上下文对齐真机（/dev/__properties__ 类型命名空间）：同样必须先
        # 真实落盘——sepolicy 链读镜像当前内容后统一追加。
        if not run_command_file(work_img, gold_props.prop_ctx_cmds(work_img), WORK_DIR):
            print("[err] 官方 vendor_property_contexts 对齐失败")
            sys.exit(1)
        cmds = inject.inject_cmds(work_img)
        cmds += prune.prune_cmds(work_img)
        cmds += sepolicy.patch_commands(work_img, WORK_DIR)
        cmds += fstab.fstab_cmds(work_img)
        cmds += ueventd_video_cmds(work_img, WORK_DIR)
        cmds += vintf.vintf_cmds(work_img, WORK_DIR)
        bp_host = props.host_content(work_img, WORK_DIR)
        cmds += props.prop_cmds(work_img, bp_host)
        if not run_command_file(work_img, cmds, WORK_DIR):
            print("[err] debugfs 注入失败")
            sys.exit(1)
        print("[ok] debugfs 注入完成")

    _fsck_and_publish(work_img, dry_run=dry_run)
    return 0
