"""官方 gold vendor /vendor/etc/selinux 全量替换 lineage 基座版本。

lineage 基座的 vendor sepolicy 只覆盖 virtio 底座自身；gold（MIUI/QTI）的 vendor
策略含官方 vendor 服务域与上下文表，替换后 vendor 侧域/标签与官方 vendor 二进制
（系统侧 MIUI 服务、QTI HAL 桩）口径一致。

实现要点：
  * 源：官方 vendor_a.img（EROFS）经 gold_files 提取到 out/tmp 缓存的
    /etc/selinux 子树（无平铺产物可用）；
  * 目的：工作镜像 /etc/selinux/<同名文件>（rm + write 新 inode）；
  * meta：mode 0644 / uid:gid 0:0；SELinux 标签**沿用镜像内同路径现有标签**
    （路径与平台未变，平台 file_contexts 口径不变），镜像内没有的新文件
    （precompiled_sepolicy 及 .sha256）兜底 vendor_configs_file；
  * 时机：必须在 sepolicy 的 CDH CIL 追加链**之前真实落盘**——各 *_patch_commands
    在收集命令阶段读取镜像当前内容（同会话命令尚未执行），替换若与它们同批执行，
    CDH 段会叠在 lineage 策略上并成为最终写入者，替换被覆盖丢弃。
    vendor_builder 因此单独开一次 debugfs 会话执行本步骤。

兼容性：gold plat_sepolicy_vers.txt 与 lineage 基座一致（版本化类型影子同名）时，
替换后 CDH CIL 链的 vers 引用与 validate_cil 名字全集仍成立；gold 的
precompiled_sepolicy 因平台（lineage）与其构建平台不同，运行时 sha256 校验不过，
init 按既有路径从 .cil 现场编译（含 CDH 追加段），不会启用陈旧预编译件。

**实测结论（承自 note13 lineage-22 基座，2026-09-27；本平台未复测）**：整目录替换
后策略可加载、但 boot 不完成：
  * vendor build.prop 属性被拒（`Do not have permissions to set ...`，含
    ro.product.cpu.abilist/abilist32/abilist64 —— ABI 锁定失效）：gold 策略的
    vendor_init 属性 set 授权按 gold 属性上下文表给出，与 lineage 平台的
    plat/vendor 上下文组合不一致；
  * keystore2 起了但服务注册不出来（无 avc 记录，进程静默失败），vold 每 1s 等
    android.system.keystore2.IKeystoreService/default → vold 卡死、boot_completed
    永不置位。
故本模块默认**关闭**，仅在排障/专项适配时用 CDH_GOLD_SELINUX=1 启用。
"""

import os
import re
import subprocess
import sys

from debugfs import DEBUGFS, path_exists, run

import gold_files

IMG_DIR = "/etc/selinux"
# 开关：默认关闭（实测整目录替换在 lineage 基座上 boot 不完成，见模块说明 §实测结论）
ENABLED = os.environ.get("CDH_GOLD_SELINUX") == "1"
# 镜像内没有的同名文件（gold 新增件）兜底标签；平台 file_contexts 对
# /vendor/etc/selinux(/.*)? 的口径即 vendor_configs_file。
FALLBACK_LABEL = "u:object_r:vendor_configs_file:s0"
MODE_FILE = "0x81a4"  # 0644 regular file


def file_count():
    """gold /etc/selinux 文件数（替换新增 inode 数上界，供容量预估）。"""
    gold_dir = gold_files.selinux_dir()
    return sum(1 for n in os.listdir(gold_dir)
               if os.path.isfile(os.path.join(gold_dir, n)) and n != ".DS_Store")


def _ea_label(work_img, img_path):
    """读取镜像内现有 security.selinux 标签；缺失返回兜底标签。"""
    res = run([DEBUGFS, "-R", f"ea_get {img_path} security.selinux", work_img],
              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    m = re.search(r'= "([^"]*)"', res.stdout.decode("utf8", "replace"))
    if not m:
        return FALLBACK_LABEL
    val = m.group(1)
    val = re.sub(r"\\([0-7]{3})", lambda mm: chr(int(mm.group(1), 8)), val)
    return val.replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")


def replace_cmds(work_img):
    """产出官方 /etc/selinux 替换命令清单（rm/write/sif/ea_set）。"""
    gold_dir = gold_files.selinux_dir()
    cmds, replaced, added = [], [], []
    for name in sorted(os.listdir(gold_dir)):
        host = os.path.join(gold_dir, name)
        if not os.path.isfile(host) or name == ".DS_Store":
            continue
        img = f"{IMG_DIR}/{name}"
        exists = path_exists(work_img, img)
        label = _ea_label(work_img, img) if exists else FALLBACK_LABEL
        (replaced if exists else added).append(name)
        if exists:
            cmds.append(f"rm {img}")
        cmds += [
            f"write {host} {img}",
            f"sif {img} mode {MODE_FILE}",
            f"sif {img} uid 0",
            f"sif {img} gid 0",
            f"ea_set {img} security.selinux {label}",
        ]
    print(f"[ok] 官方 gold /etc/selinux 替换: 覆盖 {len(replaced)} 个 + 新增 "
          f"{len(added)} 个（新增: {', '.join(added) if added else '无'}）")
    return cmds
