#!/usr/bin/env python3
"""popsicle-16 内核编译 + 模块分发脚本。

流程：
  1. 编译内核与模块：调用 kernel/build/build-kernel.sh（复用本地 builder 镜像，
     产物落在 out/kernel/Image, out/kernel/modules/）。
  2. 内核模块归集到 out/dist/kernel/modules（供 build-vendor_dlkm.py 打包
     vendor_dlkm.img——其注入逻辑按产物目录内实际 .ko 发现）。

boot.img / vendor_boot.img 由独立脚本 build-boot.py / build-vendor-boot.py 打包，
super.img / vda.img 由 pack_super.py / pack-roms.py 组装；本脚本只负责内核侧产物。

用法（可在任何目录）：
  python3 build/scripts/build-kernel.py               # 编译内核/模块 + 归集模块
  python3 build/scripts/build-kernel.py --only-build  # 只编译内核/模块，不做模块归集
  python3 build/scripts/build-kernel.py --dry-run     # 只打印构建配置，不执行
"""

import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from buildenv import (  # noqa: E402
    KERNEL_MODULES_DIR, KERNEL_OUT, PROJECT_ROOT,
)

# 设备根（本文件位于 build/scripts/ 下，向上 3 级即设备根）
_DEVICE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 内核编译脚本与产物目录
KERNEL_BUILD_DIR = os.path.join(_DEVICE_ROOT, "kernel", "build")
KERNEL_BUILD_SCRIPT = os.path.join(KERNEL_BUILD_DIR, "build-kernel.sh")
KERNEL_IMAGE = os.path.join(KERNEL_OUT, "Image")
BUILD_MODULES = os.path.join(KERNEL_OUT, "modules")     # build-kernel.sh 产物
# 分发目录：内核模块归集到 out/dist/kernel/modules（buildenv.KERNEL_MODULES_DIR）

# 内核 builder 镜像；默认取本地 ubuntu 构建器（note13/pixel6-15 同款，缺失时导入 tar）。
# 可用 CDH_KERNEL_BUILDER 覆盖（build-kernel.sh 读取同名环境变量）。
BUILDER_IMAGE = os.environ.get("CDH_KERNEL_BUILDER",
                               "cloud-phone-hub-ubuntu-builder:latest")
UBUNTU_TAR = os.path.join(PROJECT_ROOT, "out", "docker", "ubuntu.tar")


def _file_ok(p):
    return os.path.isfile(p) and os.path.getsize(p) > 0


def _ensure_builder_image():
    """确保 builder 镜像就位；缺失时导入 out/docker/ubuntu.tar。"""
    if subprocess.run(
        ["docker", "image", "inspect", BUILDER_IMAGE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0:
        print(f"[ok] 复用内核 builder 镜像: {BUILDER_IMAGE}")
        return
    if not _file_ok(UBUNTU_TAR):
        print(f"[err] 缺少 builder 镜像 {BUILDER_IMAGE} 且无本地 tar: {UBUNTU_TAR}")
        sys.exit(1)
    print(f"[..] 导入 builder 镜像: {UBUNTU_TAR}")
    rc = subprocess.run(["docker", "load", "-i", UBUNTU_TAR])
    if rc.returncode != 0:
        print("[err] docker load 导入 builder 镜像失败")
        sys.exit(1)
    print("[ok] builder 镜像导入完成")


def build_kernel(dry_run=False):
    """调用 kernel/build/build-kernel.sh 编译内核与模块，产物落 out/kernel。"""
    if not os.path.isfile(KERNEL_BUILD_SCRIPT):
        print(f"[err] 缺少内核编译脚本: {KERNEL_BUILD_SCRIPT}")
        sys.exit(1)
    if not dry_run:
        _ensure_builder_image()
    env = dict(os.environ)
    env["CDH_KERNEL_BUILDER"] = BUILDER_IMAGE
    cmd = ["bash", os.path.basename(KERNEL_BUILD_SCRIPT)]
    if dry_run:
        cmd.append("--dry-run")
    print(f"==> 调用 {KERNEL_BUILD_SCRIPT}" + ("（--dry-run）" if dry_run else ""))
    rc = subprocess.run(cmd, cwd=KERNEL_BUILD_DIR, env=env)
    if rc.returncode != 0:
        print("[err] 内核/模块编译失败")
        sys.exit(rc.returncode)
    if dry_run:
        return None
    if not _file_ok(KERNEL_IMAGE):
        print(f"[err] 内核 Image 未生成: {KERNEL_IMAGE}")
        sys.exit(1)
    modules = [f for f in os.listdir(BUILD_MODULES) if f.endswith(".ko")]
    print(f"[ok] 内核编译完成: {KERNEL_IMAGE}")
    print(f"[ok] 模块 {len(modules)} 个: {', '.join(sorted(modules))}")
    return KERNEL_IMAGE


def sync_kernel_dist():
    """内核模块归集到 out/dist/kernel/modules（供 build-vendor_dlkm.py 使用）。"""
    os.makedirs(KERNEL_MODULES_DIR, exist_ok=True)
    copied = 0
    if os.path.isdir(BUILD_MODULES):
        for name in sorted(os.listdir(BUILD_MODULES)):
            if not name.endswith(".ko"):
                continue
            shutil.copy2(os.path.join(BUILD_MODULES, name),
                         os.path.join(KERNEL_MODULES_DIR, name))
            copied += 1
    print(f"[ok] kernel 分发归集完成: {KERNEL_MODULES_DIR}（模块 {copied} 个）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="popsicle-16 内核编译并归集模块")
    ap.add_argument("--only-build", action="store_true",
                    help="只编译内核/模块，不做模块归集")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印构建配置，不执行编译")
    args = ap.parse_args()

    build_kernel(dry_run=args.dry_run)
    if not args.dry_run and not args.only_build:
        sync_kernel_dist()
    return 0


if __name__ == "__main__":
    sys.exit(main())
