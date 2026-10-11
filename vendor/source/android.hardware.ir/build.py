#!/usr/bin/env python3
"""popsicle-16 IR（红外）HAL 收编：官方 android.hardware.ir-service.example 整件搬运。

背景：官方 MIUI 由 /odm/etc/permissions/android.hardware.consumerir.xml 声明
FEATURE_CONSUMER_IR，而 SystemServer 的 ConsumerIrService 在「特性存在但取不到
android.hardware.ir.IConsumerIr/default」时直接抛

    java.lang.RuntimeException: FEATURE_CONSUMER_IR present, but no IR HAL loaded!

→ system_server 启动即死、反复重启，boot_completed 永不置位（实测 2026-10-09）。

lineage virtio vendor 只带策略、没有服务：/vendor/bin/hw 无 IR 服务、vendor VINTF 无声明、
vendor_service_contexts 无映射；而 hal_ir_default / hal_ir_service_* / file_contexts 均已存在。
官方 vendor_a.img 里的 android.hardware.ir-service.example 是 AOSP AIDL 示例实现
（本就不驱动真实硬件，适合云机），故整件收编 + 补 rc/VINTF/VSC 映射。

产物（相对路径即镜像内 /vendor 路径）：
  bin/hw/android.hardware.ir-service.example   (0755, hal_ir_default_exec)
  lib64/android.hardware.ir-V1-ndk.so          (vendor_file；lineage vendor 无此库)
  etc/init/android.hardware.ir-service.example.rc
  etc/vintf/manifest/android.hardware.ir-service.example.xml

用法：
  python3 build/vendor/source/android.hardware.ir/build.py
  python3 build/vendor/source/android.hardware.ir/build.py --dry-run
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

# 本文件位于 build/vendor/source/android.hardware.ir/ 下，向上 5 级为设备根
_DEVICE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path.insert(0, os.path.join(_DEVICE_ROOT, "build", "scripts"))

from buildenv import (  # noqa: E402
    SUPER_OUT, VENDOR_STAGE_DIR, baselines_file, erofs_tool,
)

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
# 官方 vendor 分区镜像（unpack 阶段自官方 super 拆出，与 guest 运行时一致）
VENDOR_IMG = os.path.join(SUPER_OUT, "vendor_a.img")

BINARY = "android.hardware.ir-service.example"
AIDL_LIB = "android.hardware.ir-V1-ndk.so"
RC_NAME = "android.hardware.ir-service.example.rc"
VINTF_NAME = "android.hardware.ir-service.example.xml"

LABEL_BIN = "u:object_r:hal_ir_default_exec:s0"
LABEL_LIB = "u:object_r:vendor_file:s0"
LABEL_CONFIG = "u:object_r:vendor_configs_file:s0"


def erofs_extract(src_inner, dst, dry_run=False):
    """从官方 vendor_a.img 提取 src_inner 到 dst（dst 为该文件最终路径，幂等）。

    fsck.erofs 对已存在的目标会拒绝（rc=1），故先提到临时路径再原子替换。
    """
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
    os.chmod(tmp, 0o755)
    os.replace(tmp, dst)
    print(f"    取 {src_inner} -> {rel}")


def write_asset(src_rel, dst, dry_run=False):
    """把模块内 vendor/ 下的源文件拷到 staging。"""
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
    """bin/lib64/rc/vintf 的 SELinux 标签登记进 baselines（vendor_builder 据此 ea_set）。"""
    path = baselines_file()
    if not path or not os.path.isfile(path):
        print(f"[err] 基线清单缺失: {path or 'None'}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    labels = doc.setdefault("vendor", {}).setdefault("selinux-labels", {})
    entries = {
        f"bin/hw/{BINARY}": LABEL_BIN,
        f"lib64/{AIDL_LIB}": LABEL_LIB,
        f"etc/init/{RC_NAME}": LABEL_CONFIG,
        f"etc/vintf/manifest/{VINTF_NAME}": LABEL_CONFIG,
    }
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

    print("==> 收编官方 IR HAL 二进制与 AIDL 库")
    bin_dst = os.path.join(VENDOR_STAGE_DIR, "bin", "hw", BINARY)
    lib_dst = os.path.join(VENDOR_STAGE_DIR, "lib64", AIDL_LIB)
    erofs_extract(f"/bin/hw/{BINARY}", bin_dst, dry_run=dry_run)
    erofs_extract(f"/lib64/{AIDL_LIB}", lib_dst, dry_run=dry_run)
    if not dry_run:
        os.chmod(bin_dst, 0o755)

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

    print("[ok] IR HAL 收编完成")
    return 0


def main():
    ap = argparse.ArgumentParser(description="popsicle-16 IR HAL 收编")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args = ap.parse_args()
    sys.exit(build(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())