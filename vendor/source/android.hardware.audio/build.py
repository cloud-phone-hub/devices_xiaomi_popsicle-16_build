#!/usr/bin/env python3
"""popsicle-16 audio HAL 自研服务构建（本机 NDK 工具链 + 官方 AIDL 展开源）。

背景：VM 里没有 ADSP，官方 gold vendor 的 audiohalservice.qti 起不来（实例 42 实测
只注册 effect.IFactory/default，core.IConfig/IModule 永远等不到）；换 lineage 的
audio APEX example HAL 又只注册 IConfig/default + IModule/r_submix，**从不注册
IModule/default**（实例 44 实测），audioserver 便在 AudioService/MediaRouter 的
主线程同步等 HAL，71s 后 Watchdog SIGABRT 杀掉 system_server，boot_completed 不达标。
本方案沿用 note13 的自研服务：实现只依赖 libbinder_ndk/libc++/liblog，
按 VINTF 声明注册 IModule/default + /primary + /r_submix + IConfig/default。

源码布局（本模块内，对齐 android.hardware.media.v412）：
  aidl/aosp/   官方 AIDL 展开源（硬件接口 aidl_api 快照：audio.common/core/
               effect/sounddose + hardware.common/fmq，各含版本目录 + .hash；
               system/hardware/interfaces/media/aidl 下是 android.media.audio.common
               稳定类型）——由 deps.py 从 platform-source 展开，不入库；
  deps.py      依赖准备：platform-source（按 tag 从 Gitiles +archive 逐子目录取）+
               aidl/aosp 展开 + 链接期 ABI 运行库（本机 lineage-23 super）；
  src/         自研 C++ 实现（ConfigImpl / ModuleImpl / EffectFactoryImpl + 两个 main）。

工具链（env 可覆盖；默认与本机实测一致）：
  CDH_AIDL     Android SDK build-tools aidl      （默认 36.1.0）
  CDH_CLANG    宿主 clang（默认 NDK r29 自带 clang 21，不再依赖 brew llvm@22）
  CDH_LLD      宿主 ld.lld（默认 NDK r29 自带 ld.lld）
  CDH_NDK      NDK r29                           （默认 SDK ndk/29.0.14206865）
  CDH_BINDER   platform libbinder_ndk_cpp 头     （默认 platforms/android-36）
  CDH_RUNTIME  运行库目录；默认由 deps.py 从本机 lineage-23 super 组装

编译期公共组件（binder 平台头 / bionic crt / libcxx __config_site）仍随包放在
build/vendor/source/common/ 下；AOSP platform 头改走 deps.py 的 platform-source。

产物（相对路径即镜像内 /vendor 路径；rc/vintf/policy 为 vendor/ 目录源文件拷贝）：
  bin/hw/android.hardware.audio.service-aidl.example        (0755, hal_audio_default_exec)
  bin/hw/android.hardware.audio.effect.service-aidl.example (同上，可选)
  etc/init/android.hardware.audio.service-aidl.example.rc   (0644, vendor_configs_file)
  etc/vintf/manifest/android.hardware.audio.service-aidl.xml      (同 libs)
  etc/vintf/manifest/android.hardware.bluetooth.audio.xml         (同上)
  etc/primary_audio_policy_configuration.xml                (0644, vendor_configs_file)

用法：
  python3 build/vendor/source/android.hardware.audio/build.py
  python3 build/vendor/source/android.hardware.audio/build.py --dry-run
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile

# 本文件位于 build/vendor/source/android.hardware.audio/ 下，向上 5 级为设备根
_DEVICE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path.insert(0, os.path.join(_DEVICE_ROOT, "build", "scripts"))
sys.path.insert(0, os.path.join(_DEVICE_ROOT, "build", "vendor", "build"))

from buildenv import (  # noqa: E402
    OUT_DIR, VENDOR_STAGE_DIR, baselines_file,
)

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

# 模块级 deps.py（对齐 v412：platform-source 复用/拉取 + ABI 运行库研磨）
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "audio_deps", os.path.join(MODULE_DIR, "deps.py"))
audio_deps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audio_deps)
# 公共编译组件（binder 平台头/bionic crt/libcxx config），
# 本模块随包自包含于 build/vendor/source/common/，不引用其他设备目录。
COMMON_DIR = os.path.join(os.path.dirname(MODULE_DIR), "common")
SRC_DIR = os.path.join(MODULE_DIR, "src")
WORK_DIR = audio_deps.WORK_DIR  # out/tmp/audio-ndk-work（与 deps.py 同源）

# 目标
TARGET = "aarch64-linux-android35"
SERVICES = {
    "android.hardware.audio.service-aidl.example": ["audio_service_main.cpp"],
    "android.hardware.audio.effect.service-aidl.example": ["effect_service_main.cpp"],
}
RC_NAME = "android.hardware.audio.service-aidl.example.rc"
RUNNING_ONLY_VINTF = "android.hardware.audio.service-aidl.xml"
VINTF_NAMES = [RUNNING_ONLY_VINTF]

# 运行时链接库（全部来自实机/基座，只参与链接；运行期由设备解析）
RUNTIME_LIBS = ["libc++.so", "libbinder_ndk.so"]
EXTRA_LIBS = ["-llog", "-ldl", "-lm", "-lc"]

LABEL_BIN = "u:object_r:hal_audio_default_exec:s0"
LABEL_CONFIG = "u:object_r:vendor_configs_file:s0"

# AIDL 根：官方 aidl_api 展开源（含版本目录 + .hash），由 deps.py 从 platform-source 展开
AIDL_ROOT = audio_deps.AOSP_DIR
# 版本化包 + framework 稳定类型：单一来源在 deps.py（避免两处漂移）
AIDL_PACKAGES = audio_deps.AIDL_PACKAGES
FRAMEWORK_TYPES_DIRS = audio_deps.FRAMEWORK_TYPES_DIRS


def env(name, default):
    return os.environ.get(name) or default


def ndk_clang_rt_dir(prebuilt):
    """NDK clang 资源目录 lib/clang/<ver>，glob 探测避免写死版本号。"""
    base = os.path.join(prebuilt, "lib", "clang")
    cands = sorted(d for d in glob.glob(os.path.join(base, "*"))
                   if os.path.isdir(d))
    if not cands:
        raise RuntimeError(f"NDK clang 资源目录缺失: {base}")
    return cands[-1]


def toolchain(dry_run=False):
    ndk = env("CDH_NDK", os.path.expanduser(
        "~/Library/Android/sdk/ndk/29.0.14206865"))
    # NDK 自带工具链：clang/lld/sysroot/builtins 同源，不依赖宿主 brew llvm
    prebuilt = os.path.join(ndk, "toolchains", "llvm", "prebuilt",
                            "darwin-x86_64")
    # AOSP platform 头（libfmq 模板、cutils/utils/libbase）走 deps.py platform-source
    platform_source = audio_deps.prepare_platform_source(
        audio_deps.PLATFORM_SOURCE, dry_run=dry_run)
    # AIDL 编译根 aidl/aosp（6 版本包 + framework 稳定类型）从 platform-source 展开
    audio_deps.prepare_aosp(platform_source, dry_run=dry_run)
    return {
        "aidl": env("CDH_AIDL",
                    os.path.expanduser(
                        "~/Library/Android/sdk/build-tools/36.1.0/aidl")),
        "clang": env("CDH_CLANG", os.path.join(prebuilt, "bin", "clang")),
        "lld": env("CDH_LLD", os.path.join(prebuilt, "bin", "ld.lld")),
        "sysroot": os.path.join(prebuilt, "sysroot"),
        "clang_rt_dir": ndk_clang_rt_dir(prebuilt),
        "binder": env("CDH_BINDER",
                      os.path.expanduser(
                          "~/Library/Android/sdk/platforms/android-36/"
                          "optional/libbinder_ndk_cpp")),
        # 以下编译期组件随包放在 build/vendor/source/common/（自包含）
        "binder_platform": os.path.join(COMMON_DIR, "ndk-binder-platform"),
        "bionic": os.path.join(COMMON_DIR, "bionic15"),
        "libcxx_config": os.path.join(COMMON_DIR, "libcxx-platform"),
        "platform_source": platform_source,
        # 运行库目录：默认由 audio_deps 从 **本机** lineage-23 super 组装。
        # 可用 env CDH_RUNTIME 显式覆盖（仅排查用）。
        "runtime_lib_dir": env("CDH_RUNTIME", None) or
        audio_deps.prepare_abi(dry_run=dry_run),
    }


def run(argv, dry_run=False):
    if dry_run:
        print("[..] (dry)", " ".join(argv))
        return 0
    res = subprocess.run(argv, capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stdout, file=sys.stderr)
        print(res.stderr, file=sys.stderr)
        sys.exit(1)
    return 0


def _collect_aidl(root):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if name.endswith(".aidl"):
                out.append(os.path.join(dirpath, name))
    return out


def codegen(tc, gen_src, gen_hdr, dry_run):
    """按 v412 方式生成 NDK 绑定：每个版本化包读 .hash，用 --version/--hash。

    -I 根：AIDL_ROOT（aosp）之外，还需把每个版本包目录 + framework 类型目录
    加入 -I，供包间 import（android.media.audio.common 被 audio.core 等引用）。
    """
    os.makedirs(gen_src, exist_ok=True)
    os.makedirs(gen_hdr, exist_ok=True)
    fw_dirs = [os.path.join(AIDL_ROOT, rel) for rel in FRAMEWORK_TYPES_DIRS]
    snap_dirs = [os.path.join(AIDL_ROOT, rel, ver) for rel, ver in AIDL_PACKAGES]
    inputs = []
    for rel, version in AIDL_PACKAGES:
        base = os.path.join(AIDL_ROOT, rel, version)
        pkg_inputs = _collect_aidl(base)
        inputs.extend(pkg_inputs)
        hash_path = os.path.join(base, ".hash")
        if not os.path.isfile(hash_path):
            print(f"[err] 缺 AIDL 包 .hash: {hash_path}", file=sys.stderr)
            sys.exit(1)
        with open(hash_path, encoding="ascii") as f:
            pkg_hash = f.read().split()[-1]
        argv = [tc["aidl"], "--lang=ndk", "--structured", "--stability=vintf",
                "--min_sdk_version=35",
                f"--version={version}", f"--hash={pkg_hash}",
                "-I", AIDL_ROOT] + [f"-I{d}" for d in snap_dirs + fw_dirs] \
                + ["--out=" + gen_src, "--header_out=" + gen_hdr] + pkg_inputs
        run(argv, dry_run=dry_run)
        print(f"    {rel} (v{version}, hash {pkg_hash[:12]})")
    # framework 稳定类型（无版本）：android.media.audio.common
    for rel in FRAMEWORK_TYPES_DIRS:
        fw_inputs = _collect_aidl(os.path.join(AIDL_ROOT, rel))
        if not fw_inputs:
            continue
        argv = [tc["aidl"], "--lang=ndk", "--structured", "--stability=vintf",
                "--min_sdk_version=35",
                "-I", AIDL_ROOT] + [f"-I{d}" for d in snap_dirs + fw_dirs] \
                + ["--out=" + gen_src, "--header_out=" + gen_hdr] + fw_inputs
        run(argv, dry_run=dry_run)
        print(f"    {rel} (framework stable)")


def platform_extras(tc):
    """AOSP platform 头（libfmq 模板等）——用 -isystem 避免其自带警告触发 -Werror。"""
    ps = tc["platform_source"]
    return ["-isystem", os.path.join(ps, "system", "libfmq", "include"),
            "-isystem", os.path.join(ps, "system", "libfmq", "base"),
            "-isystem", os.path.join(ps, "system", "core", "libcutils", "include"),
            "-isystem", os.path.join(ps, "system", "core", "libutils", "include"),
            # 本模块的 log/log.h shim（NDK 真 android/log.h；不用 liblog_stub 以免遮蔽）
            "-isystem", os.path.join(MODULE_DIR, "platform-shim"),
            "-isystem", os.path.join(ps, "system", "libbase", "include"),
            "-isystem", os.path.join(ps, "system", "core", "libsystem", "include")]


def compile_one(tc, gen_hdr, src, obj, dry_run):
    argv = [
        tc["clang"], "--target=" + TARGET, "--sysroot=" + tc["sysroot"],
        "-std=c++17", "-fPIC", "-fno-exceptions",
        "-fno-rtti", "-Wall", "-Wextra", "-Werror",
        "-Wno-unused-parameter",
        "-I" + gen_hdr, "-I" + tc["binder"], "-I" + tc["binder_platform"],
        "-nostdinc++", "-I" + tc["libcxx_config"],
        "-isystem", os.path.join(tc["sysroot"], "usr", "include", "c++", "v1"),
    ]
    argv.extend(platform_extras(tc))
    argv.extend(["-c", src, "-o", obj])
    if dry_run:
        return
    res = subprocess.run(argv, capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stdout, file=sys.stderr)
        print(res.stderr, file=sys.stderr)
        sys.exit(1)


def collect_gen_cpps(gen_src):
    cpps = []
    for root, _dirs, files in os.walk(gen_src):
        for f in sorted(files):
            if f.endswith(".cpp"):
                cpps.append(os.path.join(root, f))
    return sorted(cpps)


def link_service(tc, gen_hdr, objs, crtbegin, crtend, runtime_lib_dir,
                 out_bin, dry_run):
    # aarch64 LSE/atomic 完备实现（__aarch64_ldadd* 等）来自 NDK compiler-rt builtins
    builtins = os.path.join(tc["clang_rt_dir"], "lib", "linux",
                            "libclang_rt.builtins-aarch64-android.a")
    argv = [
        tc["clang"], "--target=" + TARGET, "--sysroot=" + tc["sysroot"],
        "--ld-path=" + tc["lld"], "-nostdlib", "-fno-stack-protector",
        "-mno-outline-atomics", "-mbranch-protection=standard",
        "-Wl,--dynamic-linker,/system/bin/linker64", "-Wl,-z,relro",
        "-Wl,-z,now", "-std=c++17", "-fPIE", "-pie",
        "-I" + gen_hdr, "-I" + tc["binder"], "-I" + tc["binder_platform"],
        "-nostdinc++", "-I" + tc["libcxx_config"],
        "-isystem", os.path.join(tc["sysroot"], "usr", "include", "c++", "v1"),
        crtbegin,
    ]
    argv.extend(objs)
    argv.extend([crtend, "-L" + runtime_lib_dir])
    argv.extend(os.path.join(runtime_lib_dir, lib) for lib in RUNTIME_LIBS)
    argv.extend(EXTRA_LIBS)
    argv.extend([builtins, "-o", out_bin])
    run(argv, dry_run=dry_run)


def validate_bin(tc, out_bin):
    res = subprocess.run([tc["lld"], "--file-header", out_bin],
                         capture_output=True, text=True)
    return res.returncode == 0


def write_rc(dst):
    """拷贝 init rc 到 staging。源文件位于模块内 vendor/etc/init/ 下。"""
    src = os.path.join(MODULE_DIR, "vendor", "etc", "init", RC_NAME)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, encoding="utf-8") as f:
        content = f.read()
    with open(dst, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"    写 {os.path.relpath(dst, VENDOR_STAGE_DIR)}")


def write_vintf(dst):
    """拷贝 VINTF manifest 到 staging。源文件位于模块内 vendor/etc/vintf/manifest/ 下。"""
    src = os.path.join(MODULE_DIR, "vendor", "etc", "vintf", "manifest",
                       os.path.basename(dst))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, encoding="utf-8") as f:
        content = f.read()
    with open(dst, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"    写 {os.path.relpath(dst, VENDOR_STAGE_DIR)}")


def write_policy(dst):
    """复制 primary audio policy 覆盖文件（REMOTE_SUBMIX 走 primary 模块）到 staging。
    源文件位于模块内 vendor/etc/ 下（vendor 分区内容，镜像 /vendor/etc 路径）。"""
    src = os.path.join(MODULE_DIR, "vendor", "etc", "primary_audio_policy_configuration.xml")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, encoding="utf-8") as f:
        content = f.read()
    with open(dst, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"    写 {os.path.relpath(dst, VENDOR_STAGE_DIR)}")


# 小米/MTK 默认引擎硬编码读取 /vendor/etc/audio_policy_engine_configuration.xml
# （含 xi:include 的 product_strategies / stream_volumes / default_stream_volumes）。
# 缺这套文件时引擎回退"默认音量配置"，导致音量组 Min/Max=0、音量键无效。
ENGINE_CONFIG_XMLS = (
    "audio_policy_engine_configuration.xml",
    "audio_policy_engine_product_strategies.xml",
    "audio_policy_engine_stream_volumes.xml",
    "audio_policy_engine_default_stream_volumes.xml",
)


def write_engine_configs(dst_dir):
    """拷贝 MTK 引擎音量配置链到 staging 的 /vendor/etc（镜像小米 system_ext 原版）。"""
    for name in ENGINE_CONFIG_XMLS:
        src = os.path.join(MODULE_DIR, "vendor", "etc", name)
        dst = os.path.join(dst_dir, name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, encoding="utf-8") as f:
            content = f.read()
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"    写 {os.path.relpath(dst, VENDOR_STAGE_DIR)}")


def register_labels(baselines_path, dry_run=False):
    if not baselines_path or not os.path.isfile(baselines_path):
        print(f"[err] 基线清单缺失: {baselines_path or 'None'}", file=sys.stderr)
        sys.exit(1)
    with open(baselines_path, encoding="utf-8") as f:
        doc = json.load(f)
    labels = doc.setdefault("vendor", {}).setdefault("selinux-labels", {})
    entries = {
        f"bin/hw/{name}": LABEL_BIN for name in SERVICES
    }
    entries[f"etc/init/{RC_NAME}"] = LABEL_CONFIG
    for v in VINTF_NAMES:
        entries[f"etc/vintf/manifest/{v}"] = LABEL_CONFIG
    entries[f"etc/primary_audio_policy_configuration.xml"] = LABEL_CONFIG
    for name in ENGINE_CONFIG_XMLS:
        entries[f"etc/{name}"] = LABEL_CONFIG
    missing = {k: v for k, v in entries.items() if labels.get(k) != v}
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


def build(dry_run=False):
    tc = toolchain(dry_run=dry_run)
    gen_src = os.path.join(WORK_DIR, "gen", "src")
    gen_hdr = os.path.join(WORK_DIR, "gen", "hdr")
    obj_dir = os.path.join(WORK_DIR, "obj")
    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(obj_dir, exist_ok=True)

    print("==> AIDL 代码生成")
    if not dry_run:
        # 清掉旧生成产物，保证无残留
        for d in (gen_src, gen_hdr):
            if os.path.isdir(d):
                for root, _dirs, files in os.walk(d):
                    for f in files:
                        os.unlink(os.path.join(root, f))
    codegen(tc, gen_src, gen_hdr, dry_run)

    gen_cpps = collect_gen_cpps(gen_src)
    src_cpps = [f for f in sorted(os.listdir(SRC_DIR)) if f.endswith(".cpp")]
    print(f"==> 编译 {len(gen_cpps)}+{len(src_cpps)} 个源文件")

    all_srcs = gen_cpps + [os.path.join(SRC_DIR, f) for f in src_cpps]
    objs = []
    for i, src in enumerate(all_srcs):
        obj = os.path.join(obj_dir, f"o{i}.o")
        compile_one(tc, gen_hdr, src, obj, dry_run)
        objs.append(obj)

    crtbegin = os.path.join(tc["bionic"], "cdh_crtbegin_dynamic.cpp")
    crtend = os.path.join(tc["bionic"], "cdh_crtend_android.S")

    # 业务实现库（两个服务共享）：ConfigImpl / ModuleImpl / EffectFactoryImpl
    # + virtual DAC 流实现（VdacStream/VdacCommon）+ FMQ 支撑（FmqSupport，
    #   替代 libfmq.so/libcutils.so 的 ashmem/native_handle/logError/EventFlag）
    shared_business = {"ConfigImpl.cpp", "ModuleImpl.cpp", "EffectFactoryImpl.cpp",
                       "VdacStream.cpp", "VdacCommon.cpp", "FmqSupport.cpp",
                       "AlsaPcm.cpp"}
    # 每个服务还需要自己对应的 main 对象
    main_obj = {}
    for name, mains in SERVICES.items():
        obj = os.path.join(obj_dir, "main_" + name + ".o")
        compile_one(tc, gen_hdr, os.path.join(SRC_DIR, mains[0]), obj, dry_run)
        main_obj[name] = obj

    print("==> 链接服务二进制")
    for name, _mains in SERVICES.items():
        out_bin = os.path.join(VENDOR_STAGE_DIR, "bin", "hw", name)
        os.makedirs(os.path.dirname(out_bin), exist_ok=True)
        service_objs = [
            o for o, src in zip(objs, all_srcs)
            if os.path.basename(src) in shared_business or src in gen_cpps
        ]
        service_objs.append(main_obj[name])
        link_service(tc, gen_hdr, service_objs, crtbegin, crtend,
                     tc["runtime_lib_dir"], out_bin, dry_run)
        if not dry_run:
            os.chmod(out_bin, 0o755)
            print(f"    写 {os.path.relpath(out_bin, VENDOR_STAGE_DIR)}")

    print("==> rc / vintf / policy")
    write_rc(os.path.join(VENDOR_STAGE_DIR, "etc", "init", RC_NAME))
    for v in VINTF_NAMES:
        write_vintf(os.path.join(VENDOR_STAGE_DIR, "etc", "vintf", "manifest", v))
    write_policy(os.path.join(VENDOR_STAGE_DIR, "etc",
                              "primary_audio_policy_configuration.xml"))
    write_engine_configs(os.path.join(VENDOR_STAGE_DIR, "etc"))

    print("==> SELinux 标签登记")
    register_labels(baselines_file(), dry_run=dry_run)

    print("[ok] 自研 audio HAL 构建完成")
    return 0


def main():
    ap = argparse.ArgumentParser(description="popsicle-16 自研 audio HAL 构建")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args = ap.parse_args()
    sys.exit(build(dry_run=args.dry_run))


if __name__ == "__main__":
    main()