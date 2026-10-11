#!/usr/bin/env python3
"""popsicle-16（Xiaomi SM8850，HyperOS OS3.x / Android 16）构建全流程编排脚本（main）。

按顺序串联 build/scripts/ 下的 python 脚本（与 buildenv.STAGES 一致）：

  unpack       -> unpack-roms.py       解官方 images 包（sparse super 转 raw + 逻辑分区拆解）
                                        + 解 lineage UTM 底包（vda raw/qcow2 -> GPT 分区）
  kernel       -> build-kernel.py      编译内核/模块（GKI android16-6.12 + CDH 补丁）
  boot         -> build-boot.py        以 lineage boot 链为基底 + 编译 Image 打包 boot/init_boot
  vendor_boot  -> build-vendor-boot.py 处理 vendor_boot（cmdline/fragment fstab 清理）
  vendor       -> build-vendor.py      提取依赖 + 编译 vendor 子构建 + 重打包 vendor.img
  vendor_dlkm  -> build-vendor_dlkm.py 用内核模块产物打包 vendor_dlkm.img
  patch        -> build-patch.py       组装 patch.img（ext4，vdc 挂载 /mnt/vendor）
  super        -> pack_super.py        官方 super.img 零改动搬运（raw 产物）
  pack         -> pack-roms.py         组装 vda.img + data.qcow2（最终盘集）

依赖关系：
  - unpack 先于其余阶段（vendor 需 lineage super/，boot 需 lineage boot 链）；
  - kernel 先于 boot（boot 需 out/kernel/Image）与 vendor（vendor_dlkm 需模块产物）；
  - patch 需 vendor/vendor_dlkm 产物；super 零改动仅需 unpack 的 raw；
  - pack 最后（需各阶段产物 + lineage vda 底）。

用法（可在任何目录）：
  python3 build/main.py                    # 全流程
  python3 build/main.py --skip-unpack      # 跳过 unpack（已解好时）
  python3 build/main.py --only kernel      # 只跑指定阶段
  python3 build/main.py --from vendor      # 从指定阶段起跑
  python3 build/main.py --dry-run          # 只打印将执行的命令
  python3 build/main.py --force-pack       # pack 阶段加 --force
  python3 build/main.py --device-info      # 只打印机型 TSV 行（供 just device list）
  python3 build/main.py --build-id ID      # 选择 manifest.json 固件条目（导出 CDH_*）
"""

import argparse
import json
import os
import subprocess
import sys

BUILD_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(BUILD_DIR, "scripts")

_manifest_path = os.path.join(BUILD_DIR, "manifest.json")


def _load_manifest():
    """读取 manifest.json（声明 roms/ 下支持的固件与关键镜像），返回 dict。

    仅用于打印设备名与可编译 build-id；文件缺失或解析失败时返回 None 优雅降级。
    """
    try:
        with open(_manifest_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _print_device_info(manifest):
    if not manifest:
        print("[warn] 缺少或无法解析 manifest.json，跳过设备信息")
        return
    dev = manifest.get("device", {})
    builds = manifest.get("build", [])
    print(f"  设备: {dev.get('name') or dev.get('codename') or '未知'}")
    ids = [b.get("build-id") for b in builds if b.get("build-id")]
    if ids:
        print(f"  可编译 build-id: {', '.join(ids)}")


def _device_info_lines(manifest):
    """机型 TSV 行（供 just device list 收集），每个 build-id 一行机型：
    <vendor>/<device>\\t<显示名>\\t<build-id>
    无 build 条目时输出一行空 build-id，保证设备仍可被列出。"""
    dev = manifest.get("device", {}) if manifest else {}
    builds = manifest.get("build", []) if manifest else []
    name = dev.get("name") or dev.get("codename") or "unknown"
    vendor = os.path.basename(os.path.dirname(os.path.dirname(BUILD_DIR)))  # xiaomi
    device = os.path.basename(os.path.dirname(BUILD_DIR))                   # popsicle-16
    ids = [b.get("build-id") for b in builds if b.get("build-id")]
    if not ids:
        ids = [""]
    return [f"{vendor}/{device}\t{name}\t{i}" for i in ids]


def _select_build(manifest, build_id):
    """按 --build-id 选择 manifest build 条目。

    返回 (entry, err)：err 非 None 时致命；entry 为 None 且无 err 表示
    manifest 无 build 条目或多条目未指定 --build-id（阶段回退内置默认固件）。
    未指定 --build-id 且恰有一个条目时自动选中。
    """
    builds = (manifest or {}).get("build", [])
    if build_id:
        for b in builds:
            if b.get("build-id") == build_id:
                return b, None
        ids = [b.get("build-id") for b in builds]
        return None, f"未知 build-id: {build_id}（可选: {', '.join(ids)}）"
    if len(builds) == 1:
        return builds[0], None
    return None, None


def _export_build_env(entry):
    """把选中 build 条目导出为 CDH_* 环境变量。

    各阶段脚本与 vendor 子构建统一从环境读取固件选择（单一数据源 =
    manifest.json build 条目），独立运行脚本时各脚本回退内置默认值。
    """
    build_id = entry.get("build-id", "")
    pixel6 = os.path.dirname(BUILD_DIR)                    # devices/xiaomi/popsicle-16
    roms = os.path.join(pixel6, "roms")
    lineage_rom = os.path.abspath(
        os.path.join(pixel6, "..", "..", "..", "lineage-rom"))
    pairs = {
        "CDH_BUILD_ID": build_id,
        "CDH_VERSION": entry.get("version", ""),
        "CDH_FACTORY_ZIP": (
            os.path.join(roms, entry["factory-zip"]) if entry.get("factory-zip") else ""),
        "CDH_FACTORY_DIR": (
            os.path.join(roms, entry["factory-dir"]) if entry.get("factory-dir") else ""),
        "CDH_LINEAGE_ZIP": (
            os.path.join(lineage_rom, entry["lineage-zip"]) if entry.get("lineage-zip") else ""),
        "CDH_LINEAGE_DIR": (
            os.path.join(lineage_rom, entry["lineage-dir"]) if entry.get("lineage-dir") else ""),
        "CDH_BASELINES_JSON": (
            os.path.join(BUILD_DIR, "baselines", f"{build_id}.json") if build_id else ""),
    }
    for k, v in pairs.items():
        if v:
            os.environ[k] = v
            print(f"  {k}={v}")


# 阶段：短名 -> (脚本文件名, 追加参数)
STAGE_SCRIPTS = [
    ("unpack", "unpack-roms.py", []),
    ("kernel", "build-kernel.py", []),
    ("boot", "build-boot.py", []),
    ("vendor_boot", "build-vendor-boot.py", []),
    ("vendor", "build-vendor.py", []),
    ("vendor_dlkm", "build-vendor_dlkm.py", []),
    ("patch", "build-patch.py", []),
    ("super", "pack_super.py", []),
    ("pack", "pack-roms.py", []),
]
STAGE_NAMES = [s[0] for s in STAGE_SCRIPTS]


def _stage_index(name):
    try:
        return STAGE_NAMES.index(name)
    except ValueError:
        print(f"[err] 未知阶段: {name}（可选: {', '.join(STAGE_NAMES)}）")
        sys.exit(2)


def _run_script(script, extra, dry_run):
    path = os.path.join(SCRIPTS_DIR, script)
    cmd = [sys.executable, path] + extra
    print("  + " + " ".join(cmd))
    if dry_run:
        return 0
    rc = subprocess.run(cmd)
    if rc.returncode != 0:
        print(f"[err] {script} 执行失败 (rc={rc.returncode})")
        sys.exit(rc.returncode)
    return 0


def main():
    ap = argparse.ArgumentParser(description="popsicle-16 构建全流程编排")
    ap.add_argument("--skip-unpack", action="store_true", help="跳过 unpack（已解好时）")
    ap.add_argument("--only", metavar="STAGE", help=f"只跑指定阶段（{', '.join(STAGE_NAMES)}）")
    ap.add_argument("--from", dest="from_stage", metavar="STAGE", help="从指定阶段起跑（含）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    ap.add_argument("--force-pack", action="store_true", help="pack 阶段加 --force")
    ap.add_argument("--device-info", action="store_true",
                    help="只打印机型 TSV 行（vendor/device、显示名、build-id，每机型一行）后退出，"
                         "供 just device list 收集")
    ap.add_argument("--build-id", metavar="ID",
                    help="选择 manifest.json 中的固件条目，并导出 CDH_* 环境变量驱动各阶段")
    args = ap.parse_args()

    if args.device_info:
        for line in _device_info_lines(_load_manifest()):
            print(line)
        return 0

    manifest = _load_manifest()
    entry, err = _select_build(manifest, args.build_id)
    if err:
        print(f"[err] {err}")
        sys.exit(2)
    if entry:
        bid = entry.get("build-id", "")
        print(f"==> 选择固件 build-id: {bid}"
              + ("" if args.build_id else "（自动选中唯一条目）"))
        _export_build_env(entry)
    else:
        print("[warn] manifest.json 无 build 条目（或多条目未指定 --build-id），"
              "各阶段使用内置默认固件")

    _print_device_info(manifest)

    stages = list(STAGE_SCRIPTS)
    if args.skip_unpack:
        stages = [s for s in stages if s[0] != "unpack"]
    if args.only:
        _stage_index(args.only)  # 校验阶段名
        stages = [s for s in stages if s[0] == args.only]
        if not stages:
            print(f"[err] 阶段 {args.only} 已被 --skip-unpack 移除")
            sys.exit(2)
    elif args.from_stage:
        # --skip-unpack 已删掉 unpack，stages 与 STAGE_NAMES 索引错位；
        # 必须按名字在过滤后的 stages 里定位。
        _stage_index(args.from_stage)
        names = [s[0] for s in stages]
        if args.from_stage not in names:
            print(f"[err] 阶段 {args.from_stage} 已被 --skip-unpack 移除")
            sys.exit(2)
        stages = stages[names.index(args.from_stage):]

    if args.force_pack:
        stages = [(n, s, a + ["--force"]) if n == "pack" else (n, s, a)
                  for n, s, a in stages]

    if not stages:
        print("[err] 没有要执行的阶段")
        sys.exit(2)

    print(f"==> 执行 {len(stages)} 个阶段: {', '.join(n for n, _, _ in stages)}")
    for name, script, extra in stages:
        print(f"==> [{name}] {script}")
        _run_script(script, extra, args.dry_run)
    print("[ok] 全流程完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
