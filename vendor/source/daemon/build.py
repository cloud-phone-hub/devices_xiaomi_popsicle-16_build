#!/usr/bin/env python3
"""popsicle-16 guest 守护进程：编译 + 落盘 /vendor/bin/daemon。

编译源：core/guest/daemon（Rust workspace，[[bin]] 产物名 guest-daemon）。
复用 core 模块的 guest-main（NDK + Rust 交叉编译 aarch64-linux-android），
不重复实现工具链发现/链接环境；本脚本只做「编排 + 落盘」。

产物按 cph_daemon.rc 的 service 声明（/vendor/bin/daemon）放到
<设备根>/out/dist/vendor/bin/daemon。

用法：
  python3 build/vendor/source/daemon/build.py
  python3 build/vendor/source/daemon/build.py --dry-run   # 只打印将执行的动作
"""

import argparse
import os
import shutil
import subprocess
import sys

# 本文件位于 build/vendor/source/daemon/ 下，向上 5 级为设备根；构建入口与常量复用共享模块
_DEVICE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path.insert(0, os.path.join(_DEVICE_ROOT, "build", "scripts"))

from buildenv import DIST_DIR, PROJECT_ROOT  # noqa: E402

GUEST_MAIN = os.path.join(PROJECT_ROOT, "build", "core", "guest-main")
SRC_BINARY = os.path.join(PROJECT_ROOT, "out", "core", "guest", "daemon", "guest-daemon")
VENDOR_BIN_DIR = os.path.join(DIST_DIR, "vendor", "bin")
DEST_BINARY = os.path.join(VENDOR_BIN_DIR, "daemon")


def build_guest_daemon(dry_run=False):
    # 1) 编译（guest-main build 幂等：idempotent 拉取 external + cargo 交叉编译）
    if dry_run:
        print(f"[..] (dry) {GUEST_MAIN} build")
    else:
        print(f"==> {GUEST_MAIN} build")
        subprocess.run(["bash", GUEST_MAIN, "build"], check=True)

    if not os.path.isfile(SRC_BINARY):
        print(f"[err] 未找到编译产物: {SRC_BINARY}（先 core guest build 或修复 NDK）", file=sys.stderr)
        sys.exit(1)

    # 2) 落盘为 /vendor/bin/daemon
    os.makedirs(VENDOR_BIN_DIR, exist_ok=True)
    if not dry_run:
        shutil.copy2(SRC_BINARY, DEST_BINARY)
        os.chmod(DEST_BINARY, 0o755)
    print(f"[ok] /vendor/bin/daemon -> {DEST_BINARY}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="popsicle-16 guest 守护进程构建")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args = ap.parse_args()
    sys.exit(build_guest_daemon(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
