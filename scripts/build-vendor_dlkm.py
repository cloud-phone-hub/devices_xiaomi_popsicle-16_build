#!/usr/bin/env python3
"""popsicle-16 vendor_dlkm.img 重打包——薄入口。

具体实现见 build/vendor_dlkm/build/vendor_dlkm_builder.py；此处仅解析参数并委托。
  1. 以 lineage vendor_dlkm_a.img 为基座（virtio 内核匹配）
  2. 内核模块产物（out/dist/kernel/modules/）就位时 debugfs 注入 /lib/modules
     并重建索引；未就位时退化为基座直通
  3. e2fsck -fy 重打包 -> out/dist/rom/vendor_dlkm.img

用法：
  python3 build/scripts/build-vendor_dlkm.py
  python3 build/scripts/build-vendor_dlkm.py --dry-run   # 只打印将执行的动作与命令
"""

import argparse
import os
import sys

# 本文件位于 build/scripts/ 下，向上 1 级即为 build/（含 vendor_dlkm/build/）
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_DLKM_BUILD_DIR = os.path.join(
    os.path.dirname(_SCRIPTS_DIR), "vendor_dlkm", "build")
sys.path.insert(0, VENDOR_DLKM_BUILD_DIR)

from vendor_dlkm_builder import build_vendor_dlkm  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="popsicle-16 vendor_dlkm.img 重打包")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args = ap.parse_args()
    sys.exit(build_vendor_dlkm(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
