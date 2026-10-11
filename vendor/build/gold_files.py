"""官方 vendor EROFS 对齐件提取缓存（gold_props / gold_selinux 共用入口）。

popsicle 的官方固件为 images 包：官方 vendor_a.img 是 EROFS（只读压缩镜像），
宿主侧不具备直接按普通目录读取的能力；本模块把 gold 对齐所需的 /etc/selinux
子树用 `fsck.erofs --extract` 提取到 out/tmp/gold-vendor/etc/selinux 缓存，
gold_props / gold_selinux 再以普通文件方式读取（与 note13 的 ROM_ROOT_DIR/
vendor_a/etc/selinux 平铺产物等价）。

缓存以标记文件（记录源镜像路径/大小/mtime）保证幂等：同 ROM 重复构建零成本，
换 ROM 或重新 unpack 后自动失效重提取。
"""

import os
import shutil
import subprocess
import sys

from buildenv import SUPER_OUT, TMP_DIR, erofs_tool

GOLD_CACHE_DIR = os.path.join(TMP_DIR, "gold-vendor")
SELINUX_SUBPATH = ("etc", "selinux")
_STAMP = ".source-stamp"
# 提取成功的最低要求：policy 主体与上下文表必须在（缺失说明提取失败/镜像不符）
_REQUIRED = ("vendor_sepolicy.cil", "vendor_property_contexts")


def vendor_img():
    """官方 super 拆解出的 vendor_a.img（金色 vendor 分区）。缺失即报错退出。"""
    img = os.path.join(SUPER_OUT, "vendor_a.img")
    if not os.path.isfile(img):
        print(f"[err] 官方 vendor_a.img 缺失: {img}（先跑 unpack 阶段）")
        sys.exit(1)
    return img


def selinux_dir():
    """确保官方 /etc/selinux 已提取到缓存，返回缓存内该目录的绝对路径。"""
    img = vendor_img()
    dst = os.path.join(GOLD_CACHE_DIR, *SELINUX_SUBPATH)
    stamp = os.path.join(GOLD_CACHE_DIR, _STAMP)
    want = (f"{os.path.realpath(img)} "
            f"{os.path.getsize(img)} {int(os.path.getmtime(img))}")
    if os.path.isdir(dst) and os.path.isfile(stamp):
        with open(stamp, encoding="utf-8") as f:
            if f.read() == want:
                return dst

    print(f"[..] EROFS 提取官方 /etc/selinux -> 缓存 {dst}")
    tmp = dst + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    res = subprocess.run(
        [erofs_tool("fsck.erofs"), f"--extract={tmp}",
         "--path=/etc/selinux", img],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    missing = [n for n in _REQUIRED if not os.path.isfile(os.path.join(tmp, n))]
    if res.returncode != 0 or missing:
        print(res.stdout.decode("utf8", "replace"))
        print(f"[err] 官方 /etc/selinux 提取失败（rc={res.returncode}，"
              f"缺: {', '.join(missing) or '无'}）")
        shutil.rmtree(tmp, ignore_errors=True)
        sys.exit(1)
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(tmp, dst)
    with open(stamp, "w", encoding="utf-8") as f:
        f.write(want)
    return dst
