"""popsicle-16（Xiaomi SM8850，HyperOS OS3.x / Android 16）构建环境。

各阶段脚本（build-* / unpack-roms / pack_super / pack-roms / vendor* / vendor_dlkm*）
统一从这里读取：
  - 构建选择：CDH_BUILD_ID（build-id）决定固件（roms/<build-id>.tgz）与基线件；
  - 路径/命名：设备根、out/dist 布局、lineage 底包、版本目录等全部在此推导，
    可被 CDH_* 环境变量覆盖，脚本不硬编码绝对路径；
  - 宿主工具：debugfs / e2fsck / resize2fs / qemu-img / mtools / baselines。

与 note13-5G-15 的差异（设备事实）：
  - 官方固件为 images 包（tgz 解出 <factory-dir>/images/ 平铺分区镜像），
    super.img 为 **sparse** 格式（unpack 阶段先转 raw）；
  - 官方只读分区为 **erofs**（vendor/system/product/odm/...），
    vendor 阶段实际以 lineage 底包 ext4 vendor_a.img 为基座；
  - lineage 底包为 UTM virtio 虚机，Android 16 基线 lineage-23；
    vda 载体为 raw（.img 直通）或旧版 qcow2（qemu-img 转换），见 lineage_vda_member。

工具解析优先级（_tool / _any_tool）：
  1. 环境变量直指（E2FSPROGS_<NAME> / QEMU_IMG）
  2. brew e2fsprogs sbin / erofs-utils / lz4 / Android SDK emulator 等常用安装位置
  3. PATH（shutil.which）
"""

import glob
import json
import os
import re
import shutil

# 宿主工具常用安装位置（macOS brew e2fsprogs / erofs-utils / lz4 / Android SDK）
_E2FSPROGS_SBIN = "/opt/homebrew/opt/e2fsprogs/sbin"
_EROFS_UTILS_BIN = "/opt/homebrew/opt/erofs-utils/bin"
_LZ4_BIN = "/opt/homebrew/opt/lz4/bin"
_QEMU_CANDIDATES = (
    os.path.expanduser("~/Library/Android/sdk/emulator/qemu-img"),
    "/opt/homebrew/bin/qemu-img",
    "/usr/local/bin/qemu-img",
)


def env(name, default):
    """读取环境变量，未设置或为空时回退 default。"""
    return os.environ.get(name) or default


def _tool(name):
    """定位 e2fsprogs 工具；env 覆盖 > brew sbin > PATH，缺失时抛异常。"""
    cand = os.environ.get("E2FSPROGS_" + name.upper())
    if cand and os.path.isfile(cand):
        return cand
    brew = os.path.join(_E2FSPROGS_SBIN, name)
    if os.path.isfile(brew):
        return brew
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(
        f"缺少 e2fsprogs 工具: {name}（brew install e2fsprogs）")


def e2fs_tools():
    """返回 (debugfs, e2fsck, resize2fs) 绝对路径元组。"""
    return _tool("debugfs"), _tool("e2fsck"), _tool("resize2fs")


def erofs_tool(name):
    """定位 erofs-utils 工具（fsck.erofs / dump.erofs / mkfs.erofs）。"""
    brew = os.path.join(_EROFS_UTILS_BIN, name)
    if os.path.isfile(brew):
        return brew
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(
        f"缺少 erofs-utils 工具: {name}（brew install erofs-utils）")


def lz4_tool():
    """定位 lz4 命令（vendor_boot fragment 编解码）。"""
    brew = os.path.join(_LZ4_BIN, "lz4")
    if os.path.isfile(brew):
        return brew
    found = shutil.which("lz4")
    if found:
        return found
    raise FileNotFoundError("缺少 lz4（brew install lz4）")


_MTOOLS_BIN = "/opt/homebrew/opt/mtools/bin"


def mtools_tool(name):
    """定位 mtools 命令（mtype/mcopy，EFI FAT 分区读写）；brew 位置 > PATH。"""
    brew = os.path.join(_MTOOLS_BIN, name)
    if os.path.isfile(brew):
        return brew
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"缺少 mtools {name}（brew install mtools）")


def _qemu_img():
    """定位 qemu-img（QEMU_IMG 环境变量优先，其次常见安装位置，最后 PATH）。"""
    cand = os.environ.get("QEMU_IMG")
    if cand and os.path.isfile(cand):
        return cand
    for p in _QEMU_CANDIDATES:
        if os.path.isfile(p):
            return p
    found = shutil.which("qemu-img")
    if found:
        return found
    raise FileNotFoundError("缺少 qemu-img（可用 QEMU_IMG 环境变量指定）")


def any_tool(names, suffixes=()):
    """依次在 PATH 与常见位置查找任一命令，返回首个可执行绝对路径或 None。"""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        for suf in suffixes:
            cand = os.path.join(suf, name)
            if os.path.isfile(cand):
                return cand
    return None


def baselines_file():
    """定位 build/baselines/<build-id>.json（基线件清单）。

    优先 CDH_BASELINES_JSON；其次 CDH_BUILD_ID 对应文件；否则取 baselines/ 目录
    内唯一 JSON（单 ROM 设备）。无法确定时返回 None。
    """
    p = os.environ.get("CDH_BASELINES_JSON")
    if p:
        return p if os.path.isfile(p) else None
    build_dir = os.path.dirname(os.path.abspath(__file__))
    baselines_dir = os.path.join(os.path.dirname(build_dir), "baselines")
    build_id = os.environ.get("CDH_BUILD_ID")
    if build_id:
        p = os.path.join(baselines_dir, f"{build_id}.json")
        return p if os.path.isfile(p) else None
    entries = sorted(glob.glob(os.path.join(baselines_dir, "*.json")))
    return entries[0] if len(entries) == 1 else None


def load_baselines():
    """读取基线清单 JSON；文件缺失或解析失败时返回 None。"""
    p = baselines_file()
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# =====================================================================
# 设备事实与路径（单一数据源，env 优先，其次 manifest.json 推导）
# =====================================================================

# 本文件位于 build/scripts/ 下，向上 3 级即设备根；再向上 3 级为项目根
_DEV_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_DEV_ROOT)))

ROMS_DIR = os.path.join(_DEV_ROOT, "roms")          # 官方固件输入（非源码）
OUT_DIR = os.path.join(_DEV_ROOT, "out")            # 设备编译产物


def _manifest():
    """读取 build/manifest.json（固件/构建声明），缺失或解析失败返回 {}。"""
    try:
        with open(os.path.join(_DEV_ROOT, "build", "manifest.json"),
                  encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _manifest_build():
    builds = (_manifest().get("build") or [])
    return builds[0] if builds else {}


# ---- 构建选择：CDH_BUILD_ID 优先，其次 manifest.json 首个 build ----
BUILD_ID = env("CDH_BUILD_ID", _manifest_build().get("build-id") or "")

# Android 版本（主/主.次，取自 manifest build 的 android-version，如 16.0.0）
_ANDROID_VERSION = _manifest_build().get("android-version") or ""
OS = _ANDROID_VERSION.split(".")[0] if _ANDROID_VERSION else "16"
_ANDROID_MINOR = ".".join(_ANDROID_VERSION.split(".")[:2]) if _ANDROID_VERSION else "16.0"

# 固件版本（OS3.0.319.0.WPBCNXM，取自 build-id；CDH_OS_VERSION 可覆盖）
_OS_VER_RE = re.compile(r"OS\d+(?:\.\d+)*\.[A-Z0-9]+")


def _os_version():
    cand = os.environ.get("CDH_OS_VERSION")
    if cand:
        return cand
    m = _OS_VER_RE.search(BUILD_ID)
    return m.group(0) if m else ""


OS_VERSION = _os_version()

# 版本目录：out/device/<name>/<os>/<os_version 小写>（多设备共用的项目级 out/device）
VERSION_DIR = os.path.join(PROJECT_ROOT, "out", "device",
                           "popsicle-16", OS, OS_VERSION.lower())

# ---- 官方固件输入（images 包）----
# ROM_TGZ = roms/<build-id>.tgz（CDH_ROM_TGZ 可覆盖）
ROM_TGZ = env("CDH_ROM_TGZ",
              os.path.join(ROMS_DIR, f"{BUILD_ID}.tgz") if BUILD_ID else "")
# ROM_ROOT_DIR = tgz 内层目录（约定 popsicle_images_<OS版本>_<android主.次>，CDH_ROM_DIR 可覆盖）
_ROM_VER_RE = re.compile(r"^(popsicle_images_OS\d+(?:\.\d+)*\.[A-Z0-9]+)_")


def _rom_root_dir():
    cand = os.environ.get("CDH_ROM_DIR")
    if cand:
        return cand
    m = _ROM_VER_RE.match(BUILD_ID)
    if m:
        return os.path.join(ROMS_DIR, f"{m.group(1)}_{_ANDROID_MINOR}")
    raise RuntimeError(
        f"无法从 build-id 推导 ROM 目录: {BUILD_ID!r}（可设 CDH_ROM_DIR 指定）")


ROM_ROOT_DIR = _rom_root_dir()

# 官方分区镜像目录（popsicle images 包布局：<ROM_ROOT_DIR>/images/ 平铺）
IMAGES_DIR = env("CDH_IMAGES_DIR", os.path.join(ROM_ROOT_DIR, "images"))

# 关键镜像（官方固件平铺在 IMAGES_DIR）
SUPER_IMG = os.path.join(IMAGES_DIR, "super.img")           # sparse 格式（unpack 转 raw）
BOOT_IMG = os.path.join(IMAGES_DIR, "boot.img")             # GKI 纯内核（无 ramdisk）
INIT_BOOT_IMG = os.path.join(IMAGES_DIR, "init_boot.img")   # 通用 ramdisk（含 /init）
VENDOR_BOOT_IMG = os.path.join(IMAGES_DIR, "vendor_boot.img")
VBMETA_IMG = os.path.join(IMAGES_DIR, "vbmeta.img")
# 官方 super.img 逻辑分区拆解结果（unpack-roms.py super 任务平铺到这里）
SUPER_OUT = os.path.join(ROM_ROOT_DIR, "super")
# 官方 super.img 转 raw 的产物（pack_super / pack-roms 的零改动基座）
SUPER_RAW = os.path.join(SUPER_OUT, "super.raw.img")

# ---- lineage 底包（UTM 虚机，同 pixel6-15/note13 的 vda->分区 模式）----
# 解析顺序：CDH_LINEAGE_ZIP > manifest lineage-zip（存在时）> lineage-rom/*.zip 唯一件
LINEAGE_ZIP = env("CDH_LINEAGE_ZIP", "")
if not LINEAGE_ZIP:
    _man_zip = _manifest_build().get("lineage-zip") or ""
    if _man_zip:
        _cand = os.path.join(PROJECT_ROOT, "lineage-rom", _man_zip)
        if os.path.isfile(_cand):
            LINEAGE_ZIP = _cand
if not LINEAGE_ZIP:
    _zips = sorted(glob.glob(os.path.join(PROJECT_ROOT, "lineage-rom", "*.zip")))
    # 多底包共存时优先 lineage-23（本设备基线），避免字母序误选旧版
    _pref = [z for z in _zips if "lineage-23" in os.path.basename(z)]
    _zips = _pref or _zips
    LINEAGE_ZIP = _zips[0] if _zips else ""
# 底包内 vda 成员候选：lineage-23.2 起为 raw .img（直通拷贝，无需 qemu-img），
# 旧版为 qcow2（qemu-img convert）。
LINEAGE_VDA_MEMBERS = (
    "LineageOS_on_arm64.utm/Data/vda.img",
    "LineageOS_on_arm64.utm/Data/vda.qcow2",
)
LINEAGE_VDA_MEMBER = LINEAGE_VDA_MEMBERS[0]


def lineage_vda_member(names):
    """在 zip 成员名单中探测底包 vda 载体名（决定直通/转换）；未找到返回 None。"""
    for m in LINEAGE_VDA_MEMBERS:
        if m in names:
            return m
    return None
LINEAGE_OUT_DIR = env("CDH_LINEAGE_DIR",
                      os.path.join(PROJECT_ROOT, "lineage-rom", "lineage-23"))
# lineage 底包 super 逻辑分区（vendor/vendor_dlkm/odm 基座，virtio 内核匹配）
LINEAGE_SUPER_DIR = os.path.join(LINEAGE_OUT_DIR, "super")
# lineage 底包 vda 内 boot 链分区（build-boot / build-vendor-boot 基座）
LINEAGE_BOOT_IMG = os.path.join(LINEAGE_OUT_DIR, "boot_a.img")
LINEAGE_INIT_BOOT_IMG = os.path.join(LINEAGE_OUT_DIR, "init_boot_a.img")
LINEAGE_VENDOR_BOOT_IMG = os.path.join(LINEAGE_OUT_DIR, "vendor_boot_a.img")

# ---- 输出布局 ----
DIST_DIR = os.path.join(OUT_DIR, "dist")
ROM_DIST = os.path.join(DIST_DIR, "rom")        # 分发镜像（boot/vendor/super/...）
TMP_DIR = os.path.join(OUT_DIR, "tmp")          # 调试临时（不用 /tmp）
KERNEL_OUT = os.path.join(OUT_DIR, "kernel")    # 内核编译产物（Image/modules/config）
KERNEL_MODULES_DIR = os.path.join(DIST_DIR, "kernel", "modules")  # 模块归集
VENDOR_OUT = os.path.join(OUT_DIR, "vendor", "bin", "hw")         # HAL 产物
KERNEL_SRC = os.path.join(_DEV_ROOT, "kernel", "source")          # 内核源码

# ---- vendor patch 注入树 ----
VENDOR_PATCH_DIR = os.path.join(_DEV_ROOT, "build", "vendor", "patch")
# vendor 注入的第二棵树：source 子模块落盘的 /vendor 内容（out/dist/vendor，
# 相对路径即镜像内 /vendor 路径，如 bin/daemon）。由 inject 一并注入。
VENDOR_STAGE_DIR = os.path.join(DIST_DIR, "vendor")

# ---- 分发/最终产物 ----
VENDOR_DIST = os.path.join(ROM_DIST, "vendor.img")                # build-vendor 产物
VENDOR_DLKM_DIST = os.path.join(ROM_DIST, "vendor_dlkm.img")      # build-vendor_dlkm 产物
SUPER_DIST = os.path.join(ROM_DIST, "super.img")                  # pack_super 产物（raw）
BOOT_DIST = os.path.join(ROM_DIST, "boot.img")                    # build-boot 产物（写 vda boot_a/boot_b）
INIT_BOOT_DIST = os.path.join(ROM_DIST, "init_boot.img")          # build-boot 产物（ramdisk 载体，若 lineage 底包有）
VENDOR_BOOT_DIST = os.path.join(ROM_DIST, "vendor_boot.img")      # build-vendor-boot 产物（写 vda vendor_boot_a/_b）
PATCH_DIST = os.path.join(ROM_DIST, "patch.img")                  # vdc 载入的 lineage vendor/vendor_dlkm 打包镜像
VERSION_VDA = os.path.join(VERSION_DIR, "vda.img")                # pack-roms 产物
VERSION_DATA = os.path.join(VERSION_DIR, "data.qcow2")            # pack-roms 数据盘

# ---- 构建阶段 ----
STAGES = ["unpack", "kernel", "boot", "vendor_boot",
          "vendor", "vendor_dlkm", "patch", "super", "pack"]
