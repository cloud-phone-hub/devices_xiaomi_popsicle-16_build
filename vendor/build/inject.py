"""vendor 注入树 -> 接管条目（路径/宿主源/模式/标签）与 debugfs 注入命令。

注入两棵树，相对路径即镜像内 /vendor 路径：
  1. build/vendor/patch（seasoning）：bin/logcat-dump、etc/init/*.rc、
     etc/permissions/*.xml 等，默认文件模式 0644。
  2. out/dist/vendor（source 子模块落盘）：bin/daemon 等编译产物，可执行文件
     模式 0755。

SELinux 标签统一按基线 vendor.selinux-labels 回填（键 = 注入树内相对路径），
缺失即报错退出。staging 树缺失/为空时静默跳过（产物可未编译）。

条目口径只有一个来源 `inject_files()`：lineage 基座用它拼 debugfs 命令，gold 基座
用它把同一批字节原生铺进 staging 树（同一份字节不许有第二套模式/标签判断）。
"""

import os
import sys

from buildenv import VENDOR_PATCH_DIR, VENDOR_STAGE_DIR
from debugfs import path_exists, q
from selinux import patch_labels

# 常规数据文件（patch 树）
MODE_FILE = 0o100644      # 0x81a4
# 可执行文件（staging 树），如 /vendor/bin/daemon
MODE_EXEC = 0o100755      # 0x8175
# patch 树内需可执行位的脚本（init service 直接 exec，如 bin/netmgr_setup）
PATCH_EXEC = {"bin/netmgr_setup", "bin/dlkm_loader_mainline",
              "bin/tablet2multitouch"}
# staging 树内需要常规数据模式（0644）的文件：staging 默认按可执行产物打 0755，
# 但落盘的配置文件/共享库带可执行位会与基座原模式不一致（rc/xml 是配置文件：
# 真机 /vendor/etc/init/*.rc、/vendor/etc/vintf/manifest/*.xml 均为 0644）。
STAGE_FILE = {
    "etc/init/android.hardware.security.keymint-service.rc",
    "etc/vintf/manifest/android.hardware.security.keymint-service.xml",
}

TREES = ((VENDOR_PATCH_DIR, MODE_FILE), (VENDOR_STAGE_DIR, MODE_EXEC))


def inject_files():
    """两棵注入树的全部文件 -> [(接管路径, 宿主源文件, 含类型位 mode, SELinux 标签)]。"""
    labels = patch_labels()
    out = []
    for tree_dir, mode in TREES:
        if not os.path.isdir(tree_dir):
            continue
        for root, _dirs, files in os.walk(tree_dir):
            rel = os.path.relpath(root, tree_dir).replace(os.sep, "/")
            for name in sorted(files):
                if name == ".DS_Store":
                    continue
                host = os.path.join(root, name)
                rel_key = "" if rel == "." else f"{rel}/{name}"
                label = labels.get(rel_key)
                if not label:
                    print(f"[err] 基线清单缺少 SELinux 标签: {rel_key}")
                    sys.exit(1)
                file_mode = MODE_EXEC if rel_key in PATCH_EXEC else mode
                if mode == MODE_EXEC and rel_key in STAGE_FILE:
                    file_mode = MODE_FILE
                out.append(("/" + rel_key, host, file_mode, label))
    return out


def inject_cmds(work_img):
    """lineage 基座：把注入条目逐条写进既有镜像（mkdir/rm/write/sif/ea_set）。"""
    entries = inject_files()
    cmds = []
    dirs = set()
    for img, host, file_mode, label in entries:
        d = os.path.dirname(img)
        if d not in dirs:
            dirs.add(d)
            if not path_exists(work_img, d):
                cmds.append(f"mkdir {q(d)}")
        target = q(img)
        if path_exists(work_img, img):
            cmds.append(f"rm {target}")
        cmds += [
            f"write {host} {target}",
            f"sif {target} mode {file_mode:#x}",
            f"sif {target} uid 0",
            f"sif {target} gid 0",
            f"ea_set {target} security.selinux {label}",
        ]
    print(f"[..] 注入树: {len(entries)} 个文件"
          f" -> {VENDOR_PATCH_DIR} + {VENDOR_STAGE_DIR}")
    return cmds
