#!/usr/bin/env python3
"""popsicle-16 QTI perf2 HAL 收编：官方 vendor.qti.hardware.perf2-hal-service + 依赖库。

背景：官方 MIUI 的 `system/etc/vintf/compatibility_matrix.device.xml` 把
`vendor.qti.hardware.perf2.IPerf/default` 列为**必需 HAL**（只读、不可改）。lineage
基座没有该服务，servicemanager 反复尝试 lazy 启动失败，于是
PackageManagerService.systemReady → android.util.BoostFramework.<init> →
com.qualcomm.qti.Performance.connectPerfServiceLocked →
IPerfManager$Stub$Proxy.setClientBinder 的 binder 调用**永久阻塞**，主线程 65s 后被
Watchdog 杀掉 → system_server 反复重启、每次都重放开机动画（实测 2026-10-09 界面卡死）。

只要该服务能注册即可解开阻塞（perf 功能本身在云机上无意义），故整件收编官方 vendor
的 HAL 二进制与其 7 个 QTI 依赖库（均在官方 vendor_a.img 内）。

产物（相对路径即镜像内 /vendor 路径）：
  bin/hw/vendor.qti.hardware.perf2-hal-service        (0755, vendor_hal_perf_default_exec)
  lib64/<7 个依赖库>                                   (vendor_file)
  etc/init/vendor.qti.hardware.perf2-hal-service.rc
  etc/vintf/manifest/vendor.qti.hardware.perf2.xml

SELinux：`vendor_hal_perf_*` 类型/域不在 lineage 策略里，由 sepolicy.perf_policy_cil
从官方 vendor_sepolicy.cil 逐行移植（同 plat 版本后缀 _202504），另补
vendor_service_contexts 映射（见 sepolicy.hal_vsc_lines）。

用法：
  python3 build/vendor/source/vendor.qti.hardware.perf2/build.py
  python3 build/vendor/source/vendor.qti.hardware.perf2/build.py --dry-run
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

# 本文件位于 build/vendor/source/vendor.qti.hardware.perf2/ 下，向上 5 级为设备根
_DEVICE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path.insert(0, os.path.join(_DEVICE_ROOT, "build", "scripts"))

from buildenv import (  # noqa: E402
    SUPER_OUT, VENDOR_STAGE_DIR, baselines_file, erofs_tool,
)

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_IMG = os.path.join(SUPER_OUT, "vendor_a.img")

BINARY = "vendor.qti.hardware.perf2-hal-service"
# 该二进制 NEEDED 的全部非系统库 + perf 链路的传递依赖（实测缺 libqti-util.so /
# libqti-perfd-client.so 时服务 CANNOT LINK、exit(1)，lazy 启动一直失败 → 客户端
# BoostFramework 永久阻塞）。QTI 这一族库在本盘别无他用，一次性收编避免逐轮试错。
LIBS = [
    "vendor.qti.snapdragonServices-V1-ndk.so",
    "vendor.qti.snapdragonServices.qape-V1-ndk.so",
    "libq-perflog.so",
    "vendor.qti.hardware.perf2-V1-ndk.so",
    "libqti-scenariod.so",
    "libperfconfig.so",
    "libqti-perfd.so",
    # 传递依赖（QTI perf 引擎一族）
    "libqti-util.so",
    "libqti-utils.so",
    "libqti-perfd-client.so",
    "libqti-qape-engine.so",
    "libqti-engine-loader.so",
    "libqti-lllite.so",
    "libqti-sslite.so",
    "libqti-s3tf.so",
    "libqti-qesdk-secure.so",
    "libqti-perfextn-intf.so",
    "libqti-jankrescue.so",
    "libqti-appclassifier.so",
]
RC_NAME = "vendor.qti.hardware.perf2-hal-service.rc"
VINTF_NAME = "vendor.qti.hardware.perf2.xml"

LABEL_BIN = "u:object_r:vendor_hal_perf_default_exec:s0"
LABEL_LIB = "u:object_r:vendor_file:s0"
LABEL_CONFIG = "u:object_r:vendor_configs_file:s0"


def erofs_extract(src_inner, dst, dry_run=False):
    """从官方 vendor_a.img 提取 src_inner 到 dst（幂等：先提临时再原子替换）。"""
    rel = os.path.relpath(dst, VENDOR_STAGE_DIR)
    if dry_run:
        print(f"[..] (dry) EROFS 提取 {src_inner} -> {rel}")
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    if os.path.exists(tmp):
        os.unlink(tmp)
    res = subprocess.run(
        [erofs_tool("fsck.erofs"), f"--extract={tmp}", f"--path={src_inner}",
         VENDOR_IMG],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if res.returncode != 0 or not os.path.isfile(tmp):
        print(res.stdout.decode("utf8", "replace"))
        print(f"[err] EROFS 提取失败: {src_inner}（rc={res.returncode}）",
              file=sys.stderr)
        if os.path.exists(tmp):
            os.unlink(tmp)
        sys.exit(1)
    os.chmod(tmp, 0o755 if dst.endswith(BINARY) else 0o644)
    os.replace(tmp, dst)
    print(f"    取 {src_inner} -> {rel}")


def write_asset(src_rel, dst, dry_run=False):
    src = os.path.join(MODULE_DIR, src_rel)
    if dry_run:
        print(f"[..] (dry) 写 {os.path.relpath(dst, VENDOR_STAGE_DIR)}")
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, encoding="utf-8") as f:
        content = f.read()
    with open(dst, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"    写 {os.path.relpath(dst, VENDOR_STAGE_DIR)}")


def register_labels(dry_run=False):
    path = baselines_file()
    if not path or not os.path.isfile(path):
        print(f"[err] 基线清单缺失: {path or 'None'}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    labels = doc.setdefault("vendor", {}).setdefault("selinux-labels", {})
    entries = {f"bin/hw/{BINARY}": LABEL_BIN}
    for lib in LIBS:
        entries[f"lib64/{lib}"] = LABEL_LIB
    entries[f"etc/init/{RC_NAME}"] = LABEL_CONFIG
    entries[f"etc/vintf/manifest/{VINTF_NAME}"] = LABEL_CONFIG
    missing = {k: v for k, v in entries.items() if labels.get(k) != v}
    if not missing:
        print("[..] baselines 标签已齐，跳过")
        return
    labels.update(missing)
    if dry_run:
        print(f"[..] (dry) baselines 将登记标签 {len(missing)} 条")
        return
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                               prefix=".baselines.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    print(f"[ok] baselines 登记标签 {len(missing)} 条")


def build(dry_run=False):
    if not os.path.isfile(VENDOR_IMG) and not dry_run:
        print(f"[err] 缺官方 vendor 镜像: {VENDOR_IMG}（先 unpack）", file=sys.stderr)
        sys.exit(1)

    print("==> 收编官方 perf2 HAL 二进制与依赖库")
    erofs_extract(f"/bin/hw/{BINARY}",
                  os.path.join(VENDOR_STAGE_DIR, "bin", "hw", BINARY),
                  dry_run=dry_run)
    for lib in LIBS:
        erofs_extract(f"/lib64/{lib}",
                      os.path.join(VENDOR_STAGE_DIR, "lib64", lib),
                      dry_run=dry_run)

    print("==> rc / vintf")
    write_asset(os.path.join("vendor", "etc", "init", RC_NAME),
                os.path.join(VENDOR_STAGE_DIR, "etc", "init", RC_NAME),
                dry_run=dry_run)
    write_asset(os.path.join("vendor", "etc", "vintf", "manifest", VINTF_NAME),
                os.path.join(VENDOR_STAGE_DIR, "etc", "vintf", "manifest",
                             VINTF_NAME),
                dry_run=dry_run)

    print("==> SELinux 标签登记")
    register_labels(dry_run=dry_run)

    print("[ok] perf2 HAL 收编完成")
    return 0


def main():
    ap = argparse.ArgumentParser(description="popsicle-16 perf2 HAL 收编")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args = ap.parse_args()
    sys.exit(build(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())