#!/usr/bin/env python3
"""android.hardware.audio —— 自研 audio HAL 依赖准备（deps，自包含）。

本模块完全自包含，不依赖任何其他 vendor 模块的产物（不复制、不软链）。负责两类依赖：

1. 链接期 ABI 运行库：沿用仓库共享的 build/vendor/build/deps.py 研磨逻辑，
   来源全部为 popsicle-16 自己的 lineage-23 super（vendor_a / system_a），
   组装到 <out>/dist/vendor-compile-dep/（本模块只链接 libc++/libbinder_ndk，运行期
   由 guest 自身的 /vendor/lib64 + /system/lib64 提供）。

2. 编译期三方代码（AOSP platform 头 + audio reference 实现 + AIDL 快照）：按
   tag android-15.0.0_r36 逐子目录从 Gitiles `+archive` 取到本模块
   <out>/tmp/audio-ndk-work/platform-source（已有内容即跳过，幂等）。包含 system 侧头
   （libfmq 模板 / cutils / utils / libbase / libsystem）+ hardware/interfaces
   （audio/aidl、common/aidl、common/fmq 的 aidl_api 快照与 reference 实现）
   + system/hardware/interfaces（android.media.audio.common 稳定类型）。

3. AIDL 依赖：构建期由 prepare_aosp() 从 platform-source 展开到本模块 aidl/aosp/
   （6 个版本包 <rel>/<version>/ 含 .hash + framework 稳定类型目录）。aidl/aosp
   不入库（.gitignore），是可再生的构建输入。

用法：
  python3 .../android.hardware.audio/deps.py            # 校验依赖齐全
  python3 .../android.hardware.audio/deps.py --prepare  # 缺失时获取
  python3 .../android.hardware.audio/deps.py --force    # 重建 platform-source 与 aidl/aosp
  python3 .../android.hardware.audio/deps.py --abi      # 研磨 ABI 运行库（幂等）
  python3 .../android.hardware.audio/deps.py --check    # 只校验
"""

import argparse
import os
import shutil
import subprocess
import sys

# 本文件位于 build/vendor/source/android.hardware.audio/ 下，向上 5 级为设备根
_DEVICE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path.insert(0, os.path.join(_DEVICE_ROOT, "build", "scripts"))
sys.path.insert(0, os.path.join(_DEVICE_ROOT, "build", "vendor", "build"))

from buildenv import OUT_DIR  # noqa: E402
import deps as _shared_deps  # noqa: E402  共享运行库研磨（build/vendor/build/deps.py）

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- 构建期缓存目录（中间产物，不进源码树）----
WORK_DIR = os.path.join(OUT_DIR, "tmp", "audio-ndk-work")
AIDL_DIR = os.path.join(MODULE_DIR, "aidl")
# 仓库内维护的官方 AIDL 展开源（编译 -I 根，随库固化，含 .hash）
AOSP_DIR = os.path.join(AIDL_DIR, "aosp")

# ---- platform-source：本模块自有 AOSP 三方源码目录（<out>/tmp 下，不进源码树）----
# 编译只需要 AOSP 的 system 侧头（libfmq 模板 / cutils / utils / libbase / libsystem）、
# audio reference 实现与官方 aidl_api 快照，以及 framework 稳定类型。
# 缺哪个子目录就按 tag 单独取哪个（Gitiles `+archive/<tag>/<子目录>.tar.gz`：
# 一次 HTTP GET 拿到该子树，无 git、无 partial clone，弱网下可靠且体积可控）。
# 已有本机快照时直接把内容放到本目录即可，逐个 rel 幂等跳过。
PLATFORM_SOURCE = os.path.join(WORK_DIR, "platform-source")

AOSP_TAG = "android-15.0.0_r36"
GITILES_ARCHIVE_BASE = "https://android.googlesource.com/platform"
# platform-source 需要的 AOSP 子目录（相对各仓根，逐目录取）
PLATFORM_RELS = [
    "system/core/libcutils", "system/core/libutils", "system/core/libsystem",
    "system/libbase", "system/libfmq", "system/logging/liblog",
    # audio HAL 接口 aidl_api 快照（6 版本包中 4 个在此）+ default/ reference 实现
    "hardware/interfaces/audio/aidl",
    # android.hardware.common@2 / android.hardware.common.fmq@1（FMQ 基类）
    "hardware/interfaces/common/aidl", "hardware/interfaces/common/fmq",
    # framework 稳定类型 android.media.audio.common（audio.core/common 全量 import 它；
    # AOSP 官方文档指向 /system/hardware/interfaces/media/aidl，
    # frameworks/av 下实测无此包——tag android-15.0.0_r36 与 android-16.0.0_r1 都没有）
    "system/hardware/interfaces/media/aidl",
]

# ---- AIDL 包：aidl/aosp 下官方 aidl_api 展开源（版本 + .hash）----
# aidl_api/<pkg>/<version>/ 含 <pkg 路径>/ 与 .hash；版本与 build.py 原
# AIDL_PACKAGES 保持一致（二进制接口一致）。rel 相对 platform-source 的
# hardware/interfaces/（对应 PLATFORM_RELS 里的 audio/aidl、common/aidl、common/fmq）。
AIDL_HW_PREFIX = "hardware/interfaces"
AIDL_PACKAGES = [
    ("common/aidl/aidl_api/android.hardware.common", "2"),
    ("common/fmq/aidl/aidl_api/android.hardware.common.fmq", "1"),
    ("audio/aidl/aidl_api/android.hardware.audio.common", "2"),
    ("audio/aidl/aidl_api/android.hardware.audio.core", "2"),
    ("audio/aidl/aidl_api/android.hardware.audio.core.sounddose", "1"),
    ("audio/aidl/aidl_api/android.hardware.audio.effect", "2"),
]
# framework 稳定类型（无版本）：android.media.audio.common，
# AOSP 位置 = system/hardware/interfaces/media/aidl（见上 PLATFORM_RELS 注释）
FRAMEWORK_TYPES_DIRS = [
    "system/hardware/interfaces/media/aidl",  # android/media/audio/common/*
]


def aosp_ready():
    """aidl/aosp 是否就绪：所有版本化包 .hash 非空，且 framework 稳定类型就位。"""
    for rel, version in AIDL_PACKAGES:
        hash_path = os.path.join(AOSP_DIR, rel, version, ".hash")
        if not (os.path.isfile(hash_path) and os.path.getsize(hash_path) > 0):
            return False
    for rel in FRAMEWORK_TYPES_DIRS:
        types_root = os.path.join(AOSP_DIR, rel)
        if not any(name.endswith(".aidl")
                   for _root, _dirs, files in os.walk(types_root)
                   for name in files):
            return False
    return True


def _fetch_rel(platform_root, rel, force=False):
    """按 tag 用 Gitiles +archive 取一个 AOSP 子目录到 platform-source（幂等）。

    tar 包内条目相对所请求的子目录，解到 <platform_root>/<rel> 即还原原路径。
    """
    dst = os.path.join(platform_root, rel)
    if os.path.isdir(dst) and os.listdir(dst) and not force:
        return dst
    url = f"{GITILES_ARCHIVE_BASE}/+archive/refs/tags/{AOSP_TAG}/{rel}.tar.gz"
    staging = dst + f".fetch-{os.getpid()}"
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    os.makedirs(staging, exist_ok=True)
    print(f"[..] 拉取 {rel} ({AOSP_TAG})")
    res = subprocess.run(["curl", "-sL", "--fail", "-o", "src.tar.gz", url],
                         cwd=staging, capture_output=True, text=True)
    tar_path = os.path.join(staging, "src.tar.gz")
    if res.returncode != 0 or not os.path.isfile(tar_path):
        print(f"[err] 下载失败: {url}\n  {res.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    res = subprocess.run(["tar", "xzf", "src.tar.gz", "-C", staging],
                         capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[err] 解包失败 {url}: {res.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    os.unlink(tar_path)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(staging, dst)
    return dst


def prepare_platform_source(platform_root, dry_run=False, force=False):
    """确保 platform-source 就位：本模块自有目录，缺哪个子目录取哪个。"""
    if dry_run:
        print(f"[..] (dry) 确保 platform-source 就位（{len(PLATFORM_RELS)} 个子目录）")
        return platform_root
    os.makedirs(platform_root, exist_ok=True)
    fetched = 0
    for rel in PLATFORM_RELS:
        dst = os.path.join(platform_root, rel)
        existed = os.path.isdir(dst) and os.listdir(dst)
        _fetch_rel(platform_root, rel, force=force)
        if not existed:
            fetched += 1
    print(f"[ok] platform-source 就位: {platform_root}"
          + (f"（本次新取 {fetched} 个子目录）" if fetched else ""))
    return platform_root


def prepare_aosp(platform_root, dry_run=False, force=False):
    """从 platform-source 展开本模块的 AIDL 编译根 aidl/aosp（真实复制，非软链）。

    aidl/aosp 不入库（.gitignore），构建期由此函数从 platform-source 生成：
      * 版本化包：platform-source/hardware/interfaces/<rel>/<version>/（含 .hash）
        -> aidl/aosp/<rel>/<version>/，build.py 逐包读 .hash 传 aidl --version/--hash；
      * framework 稳定类型：platform-source/<FRAMEWORK_TYPES_DIRS>
        -> aidl/aosp/<同路径>，供 audio.core/common 的 android.media.audio.common.* import。
    已就绪则复用（幂等）；--force 清空重建。
    """
    if dry_run:
        print(f"[..] (dry) 展开 aidl/aosp（{len(AIDL_PACKAGES)} 版本包 + "
              f"{len(FRAMEWORK_TYPES_DIRS)} framework 类型目录）")
        return AOSP_DIR
    if aosp_ready() and not force:
        print(f"[..] aidl/aosp 已就绪，复用: {AOSP_DIR}")
        return AOSP_DIR
    if os.path.isdir(AOSP_DIR):
        shutil.rmtree(AOSP_DIR)
    for rel, version in AIDL_PACKAGES:
        src = os.path.join(platform_root, AIDL_HW_PREFIX, rel, version)
        dst = os.path.join(AOSP_DIR, rel, version)
        if not os.path.isfile(os.path.join(src, ".hash")):
            print(f"[err] platform-source 缺 AIDL 版本包: {src}", file=sys.stderr)
            sys.exit(1)
        shutil.copytree(src, dst)
    for rel in FRAMEWORK_TYPES_DIRS:
        src = os.path.join(platform_root, rel)
        dst = os.path.join(AOSP_DIR, rel)
        if not os.path.isdir(src):
            print(f"[err] platform-source 缺 framework 类型目录: {src}", file=sys.stderr)
            sys.exit(1)
        shutil.copytree(src, dst)
    print(f"[ok] aidl/aosp 已展开（{len(AIDL_PACKAGES)} 包 + framework 类型）: {AOSP_DIR}")
    return AOSP_DIR


def prepare_abi(dry_run=False):
    """链接期 ABI 运行库（libc++/libbinder_ndk，来源本机 lineage-23 super）。"""
    if dry_run:
        print("[..] (dry) 研磨链接期 ABI 运行库（共享 deps）")
        return _shared_deps.prepare_runtime_libs(dry_run=True)
    return _shared_deps.prepare_runtime_libs(dry_run=False)


def check():
    ok = True
    if not aosp_ready():
        print("[deps] aidl/aosp 未就绪：版本包 .hash 缺失/为空，或 framework 稳定类型为空")
        ok = False
    else:
        print(f"[deps] aidl/aosp 就绪（{len(AIDL_PACKAGES)} 包 + framework 类型）")
    platform_root = PLATFORM_SOURCE
    missing = [rel for rel in PLATFORM_RELS
               if not (os.path.isdir(os.path.join(platform_root, rel))
                       and os.listdir(os.path.join(platform_root, rel)))]
    if missing:
        print(f"[deps] platform-source 缺 {len(missing)} 个子目录: "
              + ", ".join(missing))
        ok = False
    else:
        print(f"[deps] platform-source 就绪（{len(PLATFORM_RELS)} 个子目录）")
    if ok:
        print("[deps] audio HAL 依赖齐全")
        return 0
    return 1


def main():
    ap = argparse.ArgumentParser(description="audio HAL 依赖准备")
    ap.add_argument("--prepare", action="store_true", help="缺失时获取")
    ap.add_argument("--force", action="store_true", help="强制重建")
    ap.add_argument("--abi", action="store_true", help="研磨链接期 ABI 运行库")
    ap.add_argument("--check", action="store_true", help="只校验依赖齐全")
    args = ap.parse_args()
    if args.check:
        return check()
    if args.prepare or args.force or args.abi:
        prepare_abi()
        if args.prepare or args.force:
            root = prepare_platform_source(PLATFORM_SOURCE, force=args.force)
            prepare_aosp(root, force=args.force)
        return 0
    return check()


if __name__ == "__main__":
    sys.exit(main())