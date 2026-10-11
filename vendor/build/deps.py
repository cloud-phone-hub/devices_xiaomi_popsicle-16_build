#!/usr/bin/env python3
"""popsicle-16 vendor 重打包的编译依赖准备（deps）。

职责：为自研 HAL（vibrator/sensors 等 NDK + AIDL 交叉编译）准备**链接期**运行库目录。
这些 .so 仅作编译期 -L/链接输入（决定 DT_NEEDED/SONAME 与链接期符号检查），
运行时仍由 guest 自身的 /system/lib64（官方 MIUI A16）提供。

来源是 popsicle 自己的 lineage-23 底座（ext4 逻辑分区，debugfs 直取）：
  * libc++.so / libbase.so / libcrypto.so / libkeymaster_messages.so
      <- lineage-rom/lineage-23/super/vendor_a.img 的 /lib64/
  * libbinder_ndk.so / liblog.so
      <- lineage-rom/lineage-23/super/system_a.img 的 /system/lib64/
（lineage 的 system_a.img 是嵌套布局：根下另有 system/ 目录。）

组装到 <out>/dist/vendor-compile-dep/，幂等：缺哪个补哪个，已存在即跳过。

用法：
  python3 build/vendor/build/deps.py            # 准备并打印目录
  python3 build/vendor/build/deps.py --check    # 只校验不准备
"""

import argparse
import os
import subprocess
import sys

# buildenv 位于 build/scripts；独立执行本模块时自行插入（vendor_builder 调用时已存在）
DEVICE_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(DEVICE_ROOT, "build", "scripts"))

from buildenv import DIST_DIR, LINEAGE_SUPER_DIR  # noqa: E402

DEBUGFS = os.environ.get(
    "CDH_DEBUGFS", "/opt/homebrew/opt/e2fsprogs/sbin/debugfs")

VENDOR_A_IMG = os.path.join(LINEAGE_SUPER_DIR, "vendor_a.img")
SYSTEM_A_IMG = os.path.join(LINEAGE_SUPER_DIR, "system_a.img")

# 链接期运行库 -> (源镜像, 镜像内路径)
RUNTIME_LIBS = {
    "libc++.so": (VENDOR_A_IMG, "/lib64/libc++.so"),
    "libbase.so": (VENDOR_A_IMG, "/lib64/libbase.so"),
    "libcrypto.so": (VENDOR_A_IMG, "/lib64/libcrypto.so"),
    "libkeymaster_messages.so": (VENDOR_A_IMG, "/lib64/libkeymaster_messages.so"),
    "libbinder_ndk.so": (SYSTEM_A_IMG, "/system/lib64/libbinder_ndk.so"),
    "liblog.so": (SYSTEM_A_IMG, "/system/lib64/liblog.so"),
}

COMPILE_DEP_DIR = os.path.join(DIST_DIR, "vendor-compile-dep")


def _path_exists(img, inner):
    res = subprocess.run([DEBUGFS, "-R", f"stat {inner}", img],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return res.returncode == 0


def _dump_lib(name, src_img, inner, dry_run=False):
    """debugfs dump 单个库到目标目录（幂等）。返回是否就绪。"""
    dst = os.path.join(COMPILE_DEP_DIR, name)
    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
        return True
    if dry_run:
        print(f"[..] (dry) 提取 {name} <- {os.path.basename(src_img)}:{inner}")
        return True
    if not os.path.isfile(src_img):
        print(f"[warn] 缺 lineage 镜像: {src_img}（跳过 {name}）", file=sys.stderr)
        return False
    res = subprocess.run([DEBUGFS, "-R", f"dump -p {inner} {dst}", src_img],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 or not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        print(f"[warn] 提取失败（镜像内无此项？）: {os.path.basename(src_img)}:{inner}",
              file=sys.stderr)
        return False
    print(f"[ok] 运行库: {name} <- {os.path.basename(src_img)}:{inner}")
    return True


def prepare_runtime_libs(dry_run=False):
    """准备链路编译所需运行库目录，返回目录路径（幂等）。"""
    os.makedirs(COMPILE_DEP_DIR, exist_ok=True)
    missing = [lib for lib in RUNTIME_LIBS
               if not (os.path.isfile(os.path.join(COMPILE_DEP_DIR, lib))
                       and os.path.getsize(os.path.join(COMPILE_DEP_DIR, lib)) > 0)]
    if not missing:
        return COMPILE_DEP_DIR
    print(f"[..] 依赖研磨: 补齐 {len(missing)} 个链接期运行库（来源: lineage-23 底座）")
    for lib in RUNTIME_LIBS:
        if lib in missing:
            img, inner = RUNTIME_LIBS[lib]
            _dump_lib(lib, img, inner, dry_run=dry_run)
    announced = [lib for lib in RUNTIME_LIBS
                 if os.path.isfile(os.path.join(COMPILE_DEP_DIR, lib))]
    if not announced and not dry_run:
        print(f"[err] 运行库一个都没准备成功，检查 {LINEAGE_SUPER_DIR}",
              file=sys.stderr)
        sys.exit(1)
    return COMPILE_DEP_DIR


def main():
    ap = argparse.ArgumentParser(description="popsicle-16 vendor 编译依赖准备")
    ap.add_argument("--check", action="store_true", help="只校验不准备")
    args = ap.parse_args()
    if args.check:
        missing = [lib for lib in RUNTIME_LIBS
                   if not os.path.isfile(os.path.join(COMPILE_DEP_DIR, lib))]
        if missing:
            print(f"[err] 运行库缺失: {', '.join(missing)}（先跑 deps.py）",
                  file=sys.stderr)
            sys.exit(1)
        print(f"[ok] 运行库齐全: {COMPILE_DEP_DIR}")
        return 0
    prepare_runtime_libs(dry_run=False)
    print(f"[ok] 运行库目录: {COMPILE_DEP_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())