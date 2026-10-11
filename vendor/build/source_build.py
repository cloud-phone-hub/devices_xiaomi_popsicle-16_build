#!/usr/bin/env python3
"""build/vendor/source 下子构建脚本调度。

在 vendor.img 重打包前，先执行各 source 子模块的 build.py（如 daemon），
确保产物已编译/落盘。遍历 <设备根>/build/vendor/source 的每个一级子目录，
凡含 build.py 即依次执行（按目录名字母序）。

用法：
  python3 build/vendor/build/source_build.py
  python3 build/vendor/build/source_build.py --dry-run    # 只打印将执行的子构建
"""

import argparse
import os
import subprocess
import sys

# 本文件位于 build/vendor/build/ 下，向上 4 级为设备根
_DEVICE_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SOURCE_DIR = os.path.join(_DEVICE_ROOT, "build", "vendor", "source")


def run_source_builds(dry_run=False):
    if not os.path.isdir(SOURCE_DIR):
        print(f"[warn] source 目录不存在，跳过: {SOURCE_DIR}")
        return 0
    built = 0
    for entry in sorted(os.listdir(SOURCE_DIR)):
        build_py = os.path.join(SOURCE_DIR, entry, "build.py")
        if not os.path.isfile(build_py):
            continue
        built += 1
        if dry_run:
            print(f"[..] (dry) {sys.executable} {build_py} --dry-run")
            continue
        print(f"==> 子构建 {entry}: {build_py}")
        subprocess.run([sys.executable, build_py], check=True)
    print(f"[ok] source 子构建完成: {built} 个")
    return 0


def main():
    ap = argparse.ArgumentParser(description="build/vendor/source 子构建调度")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args = ap.parse_args()
    sys.exit(run_source_builds(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
