#!/usr/bin/env python3
"""vibrator HAL 官方 AIDL 依赖准备（deps）。

vibrator 的接口定义由 AOSP hardware/interfaces 提供，属外部官方依赖：
  * aidl/aosp/ —— 仓库内维护的「官方 AIDL 展开源」（编译 -I 根）：
      android.hardware.vibrator@2（9 个 .aidl + .hash，无跨包 import），随代码固化；
  * out/tmp/vibrator-ndk-work/aidl-cache-api/ —— 官方 aidl_api 快照的下载缓存
    （中间产物，不进源码树）：由 `deps.py --import <快照目录>` 一次性导入。

获取策略（prepare 判定）：
  * aidl/aosp 就绪（.hash 非空）且非 --force：直接复用，不联网不生成；
  * 否则按 CDH_AIDL_AOSP（在线 URL / 本地目录）→ 下载缓存 的顺序获取并展开。
    缓存的 aidl_api 形态约定为 <cache>/<包名>/<版本>/android/... + .hash。

用法：
  python3 build/vendor/source/android.hardware.vibrator/deps.py            # 检查就绪
  python3 build/vendor/source/android.hardware.vibrator/deps.py --prepare  # 缺失时获取
  python3 build/vendor/source/android.hardware.vibrator/deps.py --force    # 强制重建
  python3 build/vendor/source/android.hardware.vibrator/deps.py --import <快照目录>
"""

import argparse
import os
import shutil
import sys

# 本文件位于 build/vendor/source/android.hardware.vibrator/ 下，向上 5 级为设备根
_DEVICE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path.insert(0, os.path.join(_DEVICE_ROOT, "build", "scripts"))

from buildenv import OUT_DIR  # noqa: E402

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
AIDL_DIR = os.path.join(MODULE_DIR, "aidl")
AOSP_DIR = os.path.join(AIDL_DIR, "aosp")               # 编译 -I 根（随库固化）
CACHE_DIR = os.path.join(OUT_DIR, "tmp", "vibrator-ndk-work", "aidl-cache-api")

AOSP_TAG = "android-16.0.0_r1"

# <aidl 包名> -> (<展开相对路径>, <AIDL 版本>)
PACKAGES = {
    "android.hardware.vibrator": ("android/hardware/vibrator", 2),
}


def _hash_path(root, pkg):
    return os.path.join(root, pkg, str(PACKAGES[pkg][1]), ".hash")


def _aosp_ready():
    """aidl/aosp 是否就绪：包 .hash 存在且非空。"""
    return all(
        (lambda h: os.path.isfile(h) and os.path.getsize(h) > 0)(
            os.path.join(AOSP_DIR, rel, ".hash"))
        for rel, _ver in PACKAGES.values())


def _inspect(path):
    """判断目录形态：'aidl_api'（<pkg>/<ver>/android/...）或 'flat'（<rel>/...）。"""
    if not os.path.isdir(path):
        raise RuntimeError(f"AIDL 源不存在: {path}")
    if all(os.path.isfile(_hash_path(path, pkg)) for pkg in PACKAGES):
        return "aidl_api", path
    if all(os.path.isfile(os.path.join(path, rel, ".hash"))
           for rel, _ver in PACKAGES.values()):
        return "flat", path
    raise RuntimeError(
        f"AIDL 源形态不识别（缺 <包名>/<版本>/.hash 或 <rel>/.hash）: {path}")


def _pick_acquire_source():
    env = os.environ.get("CDH_AIDL_AOSP", "").strip()
    if env:
        return _inspect(os.path.expanduser(env))
    if os.path.isdir(CACHE_DIR):
        return _inspect(CACHE_DIR)
    raise RuntimeError(
        "无法获取官方 AIDL：请设置 CDH_AIDL_AOSP，或先运行 "
        "`deps.py --import <官方 aidl_api 快照目录>` 导入下载缓存")


def _copy_pkg_to_aosp(kind, src_root, pkg, rel, version):
    """把某包从源展开到 aidl/aosp。成功返回 True。"""
    if kind == "aidl_api":
        pkg_root = os.path.join(src_root, pkg, str(version))
        src = os.path.join(pkg_root, rel)
        if not os.path.isdir(src):
            return False
        for dirpath, _dirs, files in os.walk(src):
            for name in files:
                if not name.endswith(".aidl"):
                    continue
                s = os.path.join(dirpath, name)
                # 相对 pkg_root 去掉 <包名>/<版本> 层级，落盘 <rel>/...（编译 -I 根形态）
                d = os.path.join(AOSP_DIR, os.path.relpath(s, pkg_root))
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy2(s, d)
        h = os.path.join(pkg_root, ".hash")
        if os.path.isfile(h):
            os.makedirs(os.path.join(AOSP_DIR, rel), exist_ok=True)
            shutil.copy2(h, os.path.join(AOSP_DIR, rel, ".hash"))
        return True
    src = os.path.join(src_root, rel)
    if not os.path.isdir(src):
        return False
    for dirpath, _dirs, files in os.walk(src):
        for name in files:
            if not (name.endswith(".aidl") or name == ".hash"):
                continue
            s = os.path.join(dirpath, name)
            d = os.path.join(AOSP_DIR, os.path.relpath(s, src_root))
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
    return True


def acquire(force=False, quiet=False):
    """从官方源重建 aidl/aosp（destructive：清空后重写）。"""
    kind, src_root = _pick_acquire_source()
    if not quiet:
        print(f"[deps] AIDL 获取源: {kind} {src_root}")
    if os.path.isdir(AOSP_DIR):
        shutil.rmtree(AOSP_DIR)
    os.makedirs(AOSP_DIR, exist_ok=True)
    missing = [pkg for pkg, (rel, ver) in PACKAGES.items()
               if not _copy_pkg_to_aosp(kind, src_root, pkg, rel, ver)]
    if missing:
        raise RuntimeError(f"AIDL 包缺失: {missing}")
    if not quiet:
        print(f"[deps] aidl/aosp 已重建（{len(PACKAGES)} 包，tag {AOSP_TAG}）")


def prepare(force=False, quiet=False):
    """确保 aidl/aosp 就绪。已就绪（且非 --force）直接复用；否则获取并展开。"""
    if not force and _aosp_ready():
        if not quiet:
            print("[deps] aidl/aosp 已就绪，复用仓库源")
        return
    acquire(force=force, quiet=quiet)


def import_cache(src):
    """把官方 aidl_api 快照导入下载缓存（幂等：缺失才复制；CDH_AIDL_REIMPORT 强制）。"""
    src = os.path.expanduser(src)
    kind, root = _inspect(src)
    if kind != "aidl_api":
        raise RuntimeError(f"缓存导入仅支持 aidl_api 形态快照: {src}")
    os.makedirs(CACHE_DIR, exist_ok=True)
    imported = 0
    for pkg in PACKAGES:
        h = _hash_path(CACHE_DIR, pkg)
        if os.path.isfile(h) and not os.environ.get("CDH_AIDL_REIMPORT"):
            continue
        d = os.path.join(CACHE_DIR, pkg)
        if os.path.isdir(d):
            shutil.rmtree(d)
        shutil.copytree(os.path.join(root, pkg), d)
        imported += 1
    print(f"[deps] 已导入 {imported} 包官方快照 -> {CACHE_DIR}")
    return imported


def check():
    """只校验 aidl/aosp 就绪，不获取不写。"""
    problems = []
    for pkg, (rel, _ver) in PACKAGES.items():
        h = os.path.join(AOSP_DIR, rel, ".hash")
        if not (os.path.isfile(h) and os.path.getsize(h) > 0):
            problems.append(f"{pkg}: 缺 {h}")
    if problems:
        print("[deps] aidl/aosp 未就绪：")
        for p in problems:
            print("  -", p)
        return 1
    print(f"[deps] aidl/aosp 就绪 ({len(PACKAGES)} 包)")
    return 0


def main():
    ap = argparse.ArgumentParser(description="vibrator HAL AIDL 官方依赖准备")
    ap.add_argument("--prepare", action="store_true",
                    help="aidl/aosp 缺失时从官方源获取并展开")
    ap.add_argument("--force", action="store_true", help="强制重新获取并重建 aidl/aosp")
    ap.add_argument("--import", dest="import_src", metavar="PATH",
                    help="把官方 aidl_api 快照导入下载缓存（out/tmp/vibrator-ndk-work）")
    args = ap.parse_args()
    try:
        if args.import_src:
            import_cache(args.import_src)
            return 0
        if args.prepare or args.force:
            prepare(force=args.force)
            return 0
        return check()
    except RuntimeError as e:
        print(f"[deps] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
