#!/usr/bin/env python3
"""popsicle-16 Remote KeyMint HAL（android.hardware.security.keymint）落地。

背景：lineage 底包自带的 /vendor/bin/hw/android.hardware.security.keymint-service
是软件 keymint（AndroidKeyMintDevice，密钥材料在 guest 内存，落盘件为
libpuresoftkeymasterdevice.so）。本模块把它替换为 Cuttlefish 远程 keymint
（android.hardware.security.keymint-service.remote），经 /dev/hvc3 与 host
secure_env（KeymasterChannel）收发 keymaster 消息，密钥/证明能力全在 host。

方案（与 note13-5G-15 同款：单服务同名替换，避免双 keymint 争抢
IKeyMintDevice/default）：
  1. 编译：委托 cuttlefish/scripts/build-guest-keymint.sh（AIDL 生成 + 单发
     clang 链接 + llvm-readelf 校验的单一事实来源），产物直接以基线同名
     android.hardware.security.keymint-service 落盘 out/dist/vendor/bin/hw。
     A16 基线（lineage-23.2 声明 IKeyMintDevice v4）→ --keymint-aidl-version 4
     + standalone/remote_keymint_device_v4_compat.cpp 垫片（device-cuttlefish
     固定 commit 未实现 v4 新增的 setAdditionalAttestationInfo 纯虚方法）。
     guest 依赖源四件套（libbase/logging/binder-ndk-platform/boringssl 头）已
     vendor 进模块 source-workspace（自包含，不依赖网络与外部工作区）；
  2. rc：以基座 /etc/init/android.hardware.security.keymint-service.rc 为底，
     去 class early_hal、service 行尾加通道参数 /dev/hvc3、加 `file /dev/kmsg w`
     与 disabled（启动门控在 init.virt.rc post-fs，见 patch_rc docstring）；
  3. vintf：以基座 manifest 为底，换注释头并删除 RKP 组件块（RKP 注册前不声明）。

三条路径（/vendor/bin、/vendor/etc、/vendor/etc/vintf）全部命中内核 getname_flags
白名单 -> 运行时读到的都是 /mnt/vendor/...（patch.img），注入文件遮蔽基座同名件，
service vendor.keymint-default 由新二进制提供。

SELinux：二进制沿用基座文件标签 hal_keymint_default_exec（基座 vendor_file_contexts
已含该路径）/rc/xml 沿用 vendor_configs_file；/dev/hvc3 设备标签（keymaster_device）
与 hal_keymint_default 的 chr_file/kmsg 授权、vendor_init 的 wait 权限由
sepolicy.keymint_hal_cil* 追加（build/vendor/build/sepolicy.py，随 vendor 重打包
写入 vendor_sepolicy.cil + vendor_file_contexts）。

host 侧依赖：设备规格 FactoryTEE=true 时 daemon 拉起 secure_env 并把 socketpair
guest 端挂到 QEMU hvc3 chardev（core/host/daemon/qemu/qemu.go，dense 布局），
grub.cfg 注入 androidboot.vbmeta.*（build/scripts/pack-roms.py GRUB_VBMETA_*）。

用法：
  python3 build/vendor/source/android.hardware.keymint/build.py
  python3 build/vendor/source/android.hardware.keymint/build.py --dry-run
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

# 本文件位于 build/vendor/source/android.hardware.keymint/ 下，向上 5 级为设备根
_DEVICE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path.insert(0, os.path.join(_DEVICE_ROOT, "build", "scripts"))
sys.path.insert(0, os.path.join(_DEVICE_ROOT, "build", "vendor", "build"))

from buildenv import (  # noqa: E402
    LINEAGE_SUPER_DIR, OUT_DIR, PROJECT_ROOT, VENDOR_STAGE_DIR, baselines_file,
)
import deps  # noqa: E402   popsicle 自身 lineage super 的编译依赖组装

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
STANDALONE_DIR = os.path.join(MODULE_DIR, "standalone")
SOURCE_WORKSPACE = os.path.join(MODULE_DIR, "source-workspace")
COMMON_DIR = os.path.join(os.path.dirname(MODULE_DIR), "common")
BIONIC_DIR = os.path.join(COMMON_DIR, "bionic15")
# v4 垫片：device-cuttlefish 固定 commit 的 remote HAL 未实现 IKeyMintDevice v4
# 新增的纯虚方法 setAdditionalAttestationInfo（探针实测抽象类链接失败），
# 另附 shadow 头 standalone/guest/hals/keymint/remote/remote_keymint_device.h
# （standalone 是编译命令第一个 -I，遮蔽 device-cuttlefish 同名头）。
EXTRA_SOURCES = (
    os.path.join(STANDALONE_DIR, "remote_keymint_device_v4_compat.cpp"),
)

# 编译脚本（单一事实来源：AIDL 生成 + 链接 + readelf 校验）
COMPILE_SCRIPT = os.path.join(PROJECT_ROOT, "cuttlefish", "scripts",
                              "build-guest-keymint.sh")
# guest keymint 依赖源四件套（已 vendor 进模块 source-workspace；A15/A16 骨架
# 交叉验证过的 sparse clone，provenance 见 source-workspace/README 若有）
REQUIRED_SOURCE_DIRS = [
    "system-libbase/include",
    "system-logging/liblog/include",
    "frameworks-native/libs/binder/ndk/include_platform",
    "external-boringssl/src/include",
]
# 运行库（链接期输入；由 deps.py 从 lineage-23 super 组装，运行时由 guest
# 自身 /vendor/lib64 + /system/lib64 提供）
RUNTIME_LIBS = [
    "libc++.so", "libbase.so", "libbinder_ndk.so", "libcrypto.so",
    "libkeymaster_messages.so", "liblog.so",
]

# 链接期运行库目录（deps.prepare_runtime_libs 落盘处）与编译工作目录
WORK_DIR = os.path.join(OUT_DIR, "tmp", "keymint-build")
BASE_DIR = os.path.join(OUT_DIR, "tmp", "keymint-base")
DEBUGFS = deps.DEBUGFS
VENDOR_A_IMG = os.path.join(LINEAGE_SUPER_DIR, "vendor_a.img")
BASE_RC_INNER = "/etc/init/android.hardware.security.keymint-service.rc"
BASE_VINTF_INNER = "/etc/vintf/manifest/android.hardware.security.keymint-service.xml"

# 落盘（相对 VENDOR_STAGE_DIR 即镜像内 /vendor 路径）
BIN_NAME = "android.hardware.security.keymint-service"
DEST_BIN = os.path.join(VENDOR_STAGE_DIR, "bin", "hw", BIN_NAME)
DEST_RC = os.path.join(VENDOR_STAGE_DIR, "etc", "init",
                       "android.hardware.security.keymint-service.rc")
DEST_VINTF = os.path.join(VENDOR_STAGE_DIR, "etc", "vintf", "manifest",
                          "android.hardware.security.keymint-service.xml")

# 新增/遮蔽文件的 SELinux 标签（二进制沿用基座 file_contexts 路径标签）
LABELS = {
    f"bin/hw/{BIN_NAME}": "u:object_r:hal_keymint_default_exec:s0",
    "etc/init/android.hardware.security.keymint-service.rc":
        "u:object_r:vendor_configs_file:s0",
    "etc/vintf/manifest/android.hardware.security.keymint-service.xml":
        "u:object_r:vendor_configs_file:s0",
}

# rc 改写：基座 service 行尾追加通道参数 /dev/hvc3（Cuttlefish 基线编号，
# posix_channel 打开该通道与 host secure_env 收发 keymaster 消息）
RC_SERVICE = b"service vendor.keymint-default " \
             b"/vendor/bin/hw/android.hardware.security.keymint-service"
KEYMINT_CHANNEL_ARG = b" /dev/hvc3"
KMSG_LINE = b"    file /dev/kmsg w"

# vintf 改写：换注释头 + 删除 RKP 组件块（RKP 注册前不声明）
VINTF_HEADER = "<!-- popsicle-16 Remote KeyMint实际能力清单：RKP注册前不声明。 -->"
RKP_FQ = b"IRemotelyProvisionedComponent/default"


def resolve_runtime_lib_dir(arg, dry_run=False):
    """--runtime-lib-dir 参数 > env CDH_RUNTIME > deps（popsicle lineage-23 super）。"""
    if arg:
        return arg
    if os.environ.get("CDH_RUNTIME"):
        return os.environ["CDH_RUNTIME"]
    return deps.prepare_runtime_libs(dry_run=dry_run)


def _missing(what, path):
    print(f"[err] {what} 缺失: {path}", file=sys.stderr)
    sys.exit(1)


def check_prereqs(runtime_lib_dir):
    """编译前置校验：编译脚本、依赖源工作区、bionic/standalone、运行库。"""
    if not os.path.isfile(COMPILE_SCRIPT):
        _missing("keymint 编译脚本", COMPILE_SCRIPT)
    for sub in REQUIRED_SOURCE_DIRS:
        if not os.path.isdir(os.path.join(SOURCE_WORKSPACE, sub)):
            _missing("依赖源目录", os.path.join(SOURCE_WORKSPACE, sub))
    for f in ("cdh_crtbegin_dynamic.cpp", "cdh_crtend_android.S",
              "cdh_android_availability.cpp"):
        if not os.path.isfile(os.path.join(BIONIC_DIR, f)):
            _missing("bionic15 启动件", os.path.join(BIONIC_DIR, f))
    if not os.path.isdir(runtime_lib_dir):
        _missing("运行库目录", runtime_lib_dir)
    for lib in RUNTIME_LIBS:
        if not os.path.isfile(os.path.join(runtime_lib_dir, lib)):
            _missing("运行库", os.path.join(runtime_lib_dir, lib))


def compile_remote(dry_run=False, runtime_lib_dir=None):
    """委托 build-guest-keymint.sh 编译远程 keymint，产物直接落成基线同名。"""
    argv = [
        "bash", COMPILE_SCRIPT,
        "--out", DEST_BIN,
        "--runtime-lib-dir", runtime_lib_dir,
        "--standalone-dir", STANDALONE_DIR,
        "--source-workspace", SOURCE_WORKSPACE,
        "--bionic-dir", BIONIC_DIR,
        "--keymint-aidl-version", "4",
        "--build-dir", WORK_DIR,
    ]
    argv += [arg for src in EXTRA_SOURCES for arg in ("--extra-source", src)]
    if dry_run:
        print("[..] (dry)", " ".join(argv))
        return
    res = subprocess.run(argv)
    if res.returncode != 0:
        print(f"[err] keymint 编译失败（exit={res.returncode}）", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(DEST_BIN):
        _missing("keymint 编译产物", DEST_BIN)


def dump_base(dry_run=False):
    """从 lineage 基座 vendor_a.img dump 基座 rc/vintf（debugfs 直取）。

    基座与 guest 运行时一致：patch.img 由 lineage vendor 重打包而来，注入件
    以同名遮蔽（注：debugfs dump 打 "Operation not permitted while changing
    ownership" 警告属正常，文件内容仍完整写出）。
    """
    if not os.path.isfile(VENDOR_A_IMG):
        _missing("lineage vendor 镜像（先 unpack-roms）", VENDOR_A_IMG)
    paths = {}
    for inner, name in ((BASE_RC_INNER, "base.rc"),
                        (BASE_VINTF_INNER, "base.xml")):
        dst = os.path.join(BASE_DIR, name)
        paths[name] = dst
        if dry_run:
            print(f"[..] (dry) dump {name} <- {VENDOR_A_IMG}:{inner}")
            continue
        os.makedirs(BASE_DIR, exist_ok=True)
        if os.path.isfile(dst):
            os.unlink(dst)
        subprocess.run([DEBUGFS, "-R", f"dump -p {inner} {dst}", VENDOR_A_IMG],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
            _missing(f"基座文件（{inner}）", VENDOR_A_IMG)
        print(f"[ok] 基座 dump: {name} <- vendor_a.img:{inner}"
              f"（{os.path.getsize(dst)} B）")
    return paths["base.rc"], paths["base.xml"]


def patch_rc(base: bytes) -> bytes:
    """基座 rc → 去 early_hal + 通道参数 + kmsg + disabled（门控在 init.virt.rc）。

    基座：
      service vendor.keymint-default /vendor/bin/hw/android.hardware.security.keymint-service
          class early_hal
          user nobody

    启动时机（note13 实测同款竞态）：本盘 virtio_console 是内核模块
    （kernel/build/build-kernel.sh `--module VIRTIO_CONSOLE`），/dev/hvc3 由
    vendor.dlkm_loader（post-fs 触发）加载模块后出现；class early_hal 早于它
    → open("/dev/hvc3") 失败 → LOG(FATAL) → exit status 1。
    故去 early_hal 改 disabled，启动门控移到 init.virt.rc：
    `on post-fs` 内 start dlkm_loader 之后 `wait /dev/hvc3 30` + start。
    """
    lines = base.split(b"\n")
    body = []
    inserted = 0
    for line in lines:
        fields = line.split()
        if len(fields) >= 3 and fields[0] == b"file" and \
                fields[1] == b"/dev/kmsg" and fields[2] == b"w":
            continue  # 基座已含 kmsg 行时去重
        if fields and fields[0] == b"class" and fields[1] == b"early_hal":
            continue  # 去 early_hal 类（见 docstring 竞态说明）
        body.append(line)
        if line.startswith(RC_SERVICE):
            body.pop()
            body.append(line + KEYMINT_CHANNEL_ARG)
            body.append(KMSG_LINE)
            body.append(b"    disabled")
            inserted += 1
    if inserted != 1:
        print(f"[err] keymint rc service 声明命中 {inserted} 处（期望 1）",
              file=sys.stderr)
        sys.exit(1)
    return b"\n".join(body) + b"\n"


def patch_vintf(base: bytes) -> bytes:
    """基座 vintf：去 4 行注释头 -> 换基线头 -> 删 RKP 组件块。"""
    lines = base.split(b"\n")
    if len(lines) >= 4 and lines[0] == b"<!--" and lines[3] == b"-->":
        lines = lines[4:]
    lines = [VINTF_HEADER.encode("utf-8")] + lines
    idx = next((i for i, l in enumerate(lines) if RKP_FQ in l), -1)
    if idx >= 0:
        start = idx
        while b"<hal" not in lines[start]:
            start -= 1
        end = idx
        while b"</hal>" not in lines[end]:
            end += 1
        lines = lines[:start] + lines[end + 1:]
    return b"\n".join(lines)


def _write(dest, data, mode):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    os.chmod(dest, mode)
    print(f"    写 {os.path.relpath(dest, VENDOR_STAGE_DIR)} ({len(data)} B)")


def write_rc_vintf(dry_run=False):
    """dump 基座 rc/vintf，改写后落盘（遮蔽基座同名件）。"""
    base_rc_path, base_vintf_path = dump_base(dry_run=dry_run)
    if dry_run:
        print(f"[..] (dry) 将改写并写入 "
              f"{os.path.relpath(DEST_RC, VENDOR_STAGE_DIR)} / "
              f"{os.path.relpath(DEST_VINTF, VENDOR_STAGE_DIR)}")
        return
    with open(base_rc_path, "rb") as f:
        rc = patch_rc(f.read())
    with open(base_vintf_path, "rb") as f:
        vintf = patch_vintf(f.read())
    _write(DEST_RC, rc, 0o644)
    _write(DEST_VINTF, vintf, 0o644)


def register_labels(baselines_path, dry_run=False):
    """登记新增文件的 SELinux 标签进 baselines vendor.selinux-labels（幂等）。"""
    if not baselines_path or not os.path.isfile(baselines_path):
        _missing("基线清单", baselines_path)
    with open(baselines_path, encoding="utf-8") as f:
        doc = json.load(f)
    labels = doc.setdefault("vendor", {}).setdefault("selinux-labels", {})
    missing = {k: v for k, v in LABELS.items() if labels.get(k) != v}
    if not missing:
        print("[..] baselines 标签已齐，跳过")
        return
    labels.update(missing)
    if dry_run:
        print(f"[..] (dry) baselines 将登记标签 {len(missing)} 条")
        return
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(baselines_path),
                               prefix=".baselines.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, baselines_path)
    except BaseException:
        os.unlink(tmp)
        raise
    print(f"[ok] baselines 登记标签 {len(missing)} 条")


def build(dry_run=False, runtime_lib_dir=None):
    runtime_lib_dir = resolve_runtime_lib_dir(runtime_lib_dir, dry_run=dry_run)
    check_prereqs(runtime_lib_dir)

    print(f"==> 编译远程 keymint（AIDL v4，{os.path.relpath(COMPILE_SCRIPT, _DEVICE_ROOT)}）")
    compile_remote(dry_run=dry_run, runtime_lib_dir=runtime_lib_dir)

    print("==> rc / vintf（基座 dump + 改写）")
    write_rc_vintf(dry_run=dry_run)

    print("==> SELinux 标签登记")
    register_labels(baselines_file(), dry_run=dry_run)

    print("[ok] Remote KeyMint HAL 落盘完成")
    return 0


def main():
    ap = argparse.ArgumentParser(description="popsicle-16 Remote KeyMint HAL 构建")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    ap.add_argument("--runtime-lib-dir", default=None,
                    help="链接期运行库目录（默认 deps.py 组装 popsicle lineage-23 "
                         "super；env CDH_RUNTIME 或本参数可覆盖）")
    args = ap.parse_args()
    sys.exit(build(dry_run=args.dry_run, runtime_lib_dir=args.runtime_lib_dir))


if __name__ == "__main__":
    main()
