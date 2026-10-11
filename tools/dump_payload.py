#!/usr/bin/env python3
"""popsicle-16 固件抽取工具：官方 OTA zip / payload.bin -> 分区镜像。

包装 payload-dumper-go（预编译二进制，版本锁定见 TOOL_VERSION），自动下载并固化到
build/tools/bin/payload-dumper-go：固定下载源 + sha256 校验，保证跨机、跨时间一致
（对齐 skills/build-toolchain 的"工具版本可控/新工具必须登记"约束）。

输入：roms/ 下的官方 OTA zip（A/B 全量包，内含 payload.bin）。payload-dumper-go 2.1.0
支持原地读 zip，不会先把 8G 的 payload.bin 拷出来（--payload-only 才落盘）。
输出：roms/<包名>/ 下平铺各分区镜像（<name>.img），与 note13-5G-15 的 unpack-roms.py
把分区镜像平铺到 roms/<解压目录>/ 的约定一致。

用法（可在任何目录）：
  python3 build/tools/dump_payload.py                 # 自动定位唯一的 roms/*.zip，解全部分区
  python3 build/tools/dump_payload.py --list          # 只列分区清单（不落盘）
  python3 build/tools/dump_payload.py -p boot,system  # 只解指定分区
  python3 build/tools/dump_payload.py --payload-only  # 只从 zip 抽出 payload.bin 与元数据
  python3 build/tools/dump_payload.py --input roms/xxx.zip --out /tmp/x --jobs 16
"""

import argparse
import glob
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

BUILD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # <device>/build
DEV_ROOT = os.path.dirname(BUILD_DIR)                                   # devices/<vendor>/<device>
ROMS_DIR = os.path.join(DEV_ROOT, "roms")
TOOL_BIN = os.path.join(BUILD_DIR, "tools", "bin", "payload-dumper-go")

TOOL_VERSION = "2.1.0"
DOWNLOAD_BASE = f"https://github.com/ssut/payload-dumper-go/releases/download/{TOOL_VERSION}"

# 平台 -> (asset 名, tarball sha256)；哈希取自官方 payload-dumper-go_sha256checksums.txt。
ASSETS = {
    ("darwin", "arm64"): ("payload-dumper-go_2.1.0_darwin_arm64.tar.gz",
                          "7f78ac6fe7f449c309ac82d85fa2303bb045fa729e0e975e3257c2ce4044715b"),
    ("darwin", "x86_64"): ("payload-dumper-go_2.1.0_darwin_amd64.tar.gz",
                           "c51f7d9c4a85ce38c3062e03cb9129a737b826c1b8a8080834bf7fcbd26d03ea"),
    ("linux", "x86_64"): ("payload-dumper-go_2.1.0_linux_amd64.tar.gz",
                          "bbbb53a71955c69272afdef7bc7e83a1bb1770f453d0c21dcaf61a3ba0463c11"),
    ("linux", "aarch64"): ("payload-dumper-go_2.1.0_linux_arm64.tar.gz",
                           "5743a39b48d0c4dab948ad6e5d9ff275e649528e5dfd2b7db4b6893093b70f6c"),
}

# OTA zip 内的元数据成员（--payload-only 时一并抽出，保留 zip 内相对路径）
PAYLOAD_MEMBERS = (
    "payload.bin",
    "payload_properties.txt",
    "care_map.pb",
    "apex_info.pb",
    "META-INF/com/android/metadata",
    "META-INF/com/android/metadata.pb",
    "META-INF/com/android/otacert",
)


def _platform_key():
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    return ("darwin" if sysname == "darwin" else "linux" if sysname == "linux" else sysname,
            {"aarch64": "aarch64"}.get(machine, machine))


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _human(nbytes):
    for unit in ("B", "K", "M", "G", "T"):
        if nbytes < 1024 or unit == "T":
            return f"{nbytes:.1f}{unit}" if unit != "B" else f"{nbytes}B"
        nbytes /= 1024.0


def ensure_tool(allow_unpinned=False):
    """确保 build/tools/bin/payload-dumper-go 在位且可执行；缺失则下载固化并返回路径。

    二进制已在位时直接复用（不联网）。要强制重下，删掉 tools/bin/ 下的文件再跑。
    """
    if os.path.isfile(TOOL_BIN) and os.access(TOOL_BIN, os.X_OK):
        return TOOL_BIN

    key = _platform_key()
    asset = ASSETS.get(key)
    if not asset:
        print(f"[error] 未登记平台 {key[0]}/{key[1]} 的 payload-dumper-go 预编译包"
              f"（已登记: {', '.join(f'{k[0]}/{k[1]}' for k in ASSETS)}）", file=sys.stderr)
        print("        可自行放置二进制到 tools/bin/，或补登记 ASSETS 后重跑", file=sys.stderr)
        sys.exit(1)
    name, want_sha = asset
    if not want_sha and not allow_unpinned:
        print(f"[error] 平台 {key[0]}/{key[1]} 无锁定 sha256，拒绝下载（可加 --allow-unpinned-download）",
              file=sys.stderr)
        sys.exit(1)

    url = f"{DOWNLOAD_BASE}/{name}"
    print(f"[tool] 下载 payload-dumper-go {TOOL_VERSION} ({name})")
    print(f"       {url}")
    os.makedirs(os.path.dirname(TOOL_BIN), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pdg-") as tmp:
        tgz = os.path.join(tmp, name)
        req = urllib.request.Request(url, headers={"User-Agent": "cloud-phone-hub-build"})
        with urllib.request.urlopen(req) as resp, open(tgz, "wb") as out:
            shutil.copyfileobj(resp, out)
        got_sha = _sha256_file(tgz)
        if want_sha and got_sha != want_sha:
            print(f"[error] sha256 校验失败\n        期望 {want_sha}\n        实际 {got_sha}", file=sys.stderr)
            sys.exit(1)
        print(f"[tool] sha256 {got_sha} 校验通过" if want_sha else f"[tool] sha256 {got_sha}（未锁定，已跳过校验）")
        with tarfile.open(tgz, "r:gz") as tf:
            member = tf.getmember("payload-dumper-go")
            src = tf.extractfile(member)
            with open(TOOL_BIN, "wb") as out:
                shutil.copyfileobj(src, out)
    os.chmod(TOOL_BIN, 0o755)
    if platform.system() == "Darwin":
        # 清 quarantine 扩展属性，避免 Gatekeeper 拦截下载来的二进制
        subprocess.run(["xattr", "-c", TOOL_BIN], check=False)
    return TOOL_BIN


def resolve_input(arg_input):
    """定位输入包：--input 指定，或 roms/ 下唯一的 .zip。"""
    if arg_input:
        path = os.path.abspath(arg_input)
        if not os.path.isfile(path):
            print(f"[error] 输入不存在: {path}", file=sys.stderr)
            sys.exit(1)
        return path
    zips = sorted(glob.glob(os.path.join(ROMS_DIR, "*.zip")))
    if len(zips) == 1:
        return zips[0]
    if not zips:
        print(f"[error] {ROMS_DIR} 下没有 .zip，用 --input 指定固件包", file=sys.stderr)
        sys.exit(1)
    print(f"[error] {ROMS_DIR} 下有多个 zip，请用 --input 指定：", file=sys.stderr)
    for z in zips:
        print(f"        {os.path.relpath(z, DEV_ROOT)}", file=sys.stderr)
    sys.exit(1)


def default_out_dir(input_path):
    """输出目录约定：xxx.zip -> <同目录>/xxx/；其余（payload.bin 等）-> 其所在目录。"""
    if input_path.endswith(".zip"):
        return input_path[: -len(".zip")]
    return os.path.dirname(input_path)


def list_partitions(tool, input_path, quiet=False):
    """返回 [(分区名, 大小KB)]，机器可读输出按 'name:size' 解析（按内容自动识别 zip/bin）。"""
    res = subprocess.run([tool, "-l", "-m", "-q", input_path],
                         capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[error] 列举分区失败:\n{res.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    parts = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, size = line.partition(":")
        try:
            parts.append((name, int(size)))
        except ValueError:
            continue
    if not quiet:
        print(f"[list] {len(parts)} 个分区（大小 KB）")
        for name, size in parts:
            print(f"       {name:<20} {_human(size * 1024):>10}")
    return parts


def dump_partitions(tool, input_path, out_dir, partitions, jobs, verify):
    """调用 payload-dumper-go 解分区到 out_dir。partitions 为空表示全部。"""
    cmd = [tool, "-o", out_dir]
    if jobs:
        cmd += ["-c", str(jobs)]
    if partitions:
        cmd += ["-p", ",".join(partitions)]
    if not verify:
        cmd += ["-no-verify"]
    cmd.append(input_path)
    print(f"[dump] {' '.join(cmd)}")
    os.makedirs(out_dir, exist_ok=True)
    rc = subprocess.call(cmd)
    if rc != 0:
        print(f"[error] payload-dumper-go 退出码 {rc}", file=sys.stderr)
        sys.exit(rc)


def extract_payload_only(input_path, out_dir):
    """只从 zip 抽出 payload.bin 与元数据（保留 zip 内相对路径），不解析分区。"""
    if not zipfile.is_zipfile(input_path):
        print(f"[error] --payload-only 需要 OTA zip 输入: {input_path}", file=sys.stderr)
        sys.exit(1)
    os.makedirs(out_dir, exist_ok=True)
    written = []
    with zipfile.ZipFile(input_path) as zf:
        names = set(zf.namelist())
        for member in PAYLOAD_MEMBERS:
            if member not in names:
                continue
            dst = os.path.join(out_dir, member)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst) and os.path.getsize(dst) == zf.getinfo(member).file_size:
                print(f"[skip] {member} 已存在且大小一致")
                continue
            print(f"[zip ] {member}")
            with zf.open(member) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 22)
            written.append(member)
    if written:
        print(f"[zip ] 已抽出 {len(written)} 个成员 -> {out_dir}")


def main():
    ap = argparse.ArgumentParser(description="popsicle-16 固件抽取（OTA zip / payload.bin -> 分区镜像）")
    ap.add_argument("--input", help="OTA zip 或 payload.bin；缺省取 roms/ 下唯一的 .zip")
    ap.add_argument("--out", help="输出目录；缺省 roms/<包名>/")
    ap.add_argument("-l", "--list", action="store_true", help="只列分区清单，不落盘")
    ap.add_argument("-p", "--partitions", help="只解指定分区（逗号分隔，如 boot,system）")
    ap.add_argument("-c", "--jobs", type=int, default=0, help="并发数（缺省用工具默认 10）")
    ap.add_argument("--payload-only", action="store_true", help="只从 zip 抽出 payload.bin 与元数据")
    ap.add_argument("--no-verify", action="store_true", help="跳过输出镜像 sha256 校验（不推荐）")
    ap.add_argument("--force", action="store_true", help="忽略已解出的分区，强制重解")
    ap.add_argument("--allow-unpinned-download", action="store_true", help="允许下载未登记 sha256 的平台包")
    args = ap.parse_args()

    input_path = resolve_input(args.input)
    print(f"[input] {input_path}  ({_human(os.path.getsize(input_path))})")
    out_dir = os.path.abspath(args.out) if args.out else default_out_dir(input_path)

    if args.payload_only:
        extract_payload_only(input_path, out_dir)
        return

    tool = ensure_tool(allow_unpinned=args.allow_unpinned_download)

    if args.list:
        list_partitions(tool, input_path)
        return

    wanted = [p.strip() for p in args.partitions.split(",")] if args.partitions else []
    known = dict(list_partitions(tool, input_path, quiet=True))
    if wanted:
        unknown = [p for p in wanted if p not in known]
        if unknown:
            print(f"[error] 分区不存在: {', '.join(unknown)}", file=sys.stderr)
            print(f"        可用: {', '.join(known)}", file=sys.stderr)
            sys.exit(1)
        targets = wanted
    else:
        targets = list(known)

    # 幂等：已解出的分区跳过（payload-dumper-go 对 -p 结果整批重解，8G 重跑代价高）
    if args.force:
        todo = list(targets)
    else:
        todo = [p for p in targets if not os.path.isfile(os.path.join(out_dir, f"{p}.img"))]
    print(f"[plan ] 输出 {out_dir}")
    print(f"[plan ] 目标 {len(targets)} 个分区：已解 {len(targets) - len(todo)}，待解 {len(todo)}")
    if not todo:
        print("[done] 全部目标分区已就位，无需重解（--force 可强制重解）")
        return

    dump_partitions(tool, input_path, out_dir, todo, args.jobs, verify=not args.no_verify)

    print("[done] 产物：")
    total = 0
    for name in targets:
        path = os.path.join(out_dir, f"{name}.img")
        if os.path.isfile(path):
            size = os.path.getsize(path)
            total += size
            print(f"       {name:<20} {_human(size):>10}  {os.path.relpath(path, DEV_ROOT)}")
    print(f"       共 {_human(total)}，目录 {out_dir}")


if __name__ == "__main__":
    main()