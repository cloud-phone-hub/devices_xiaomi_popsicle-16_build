"""debugfs 工具层：宿主工具定位 + 镜像内文件探测/读取/命令会话。

被 modules / props 与编排层（vendor_dlkm_builder）共享；debugfs 单命令失败
常仍返回 0，须扫描 stderr 错误标记（run_command_file 内实现）。
与 build/vendor/build/debugfs.py 同构，vendor_dlkm 树内自持。
"""

import os
import re
import subprocess

from buildenv import e2fs_tools

DEBUGFS, E2FSCK, _RESIZE2FS = e2fs_tools()


def run(cmd, **kw):
    print("  + " + " ".join(cmd))
    return subprocess.run(cmd, **kw)


def path_exists(work_img, img_path):
    """debugfs stat 探测镜像内路径是否存在。"""
    res = run([DEBUGFS, "-R", f"stat {img_path}", work_img],
              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    blob = res.stdout + res.stderr
    return not (b"File not found" in blob or b"not found" in blob)


def cat(work_img, img_file):
    """debugfs cat 某文件内容（banner 走 stderr，stdout 即文件内容）；不存在返回 None。"""
    res = run([DEBUGFS, "-R", f"cat {img_file}", work_img],
              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if b"File not found" in res.stdout + res.stderr:
        return None
    return res.stdout


def run_command_file(work_img, commands, work_dir):
    """将 debugfs 命令清单写成命令文件后以 -w -f 执行，并扫描 stderr 错误标记。"""
    cmd_file = os.path.join(work_dir, "debugfs.cmd")
    os.makedirs(work_dir, exist_ok=True)
    with open(cmd_file, "w") as f:
        for c in commands:
            f.write(c + "\n")
    res = run([DEBUGFS, "-w", "-f", cmd_file, work_img],
              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err = res.stderr.decode("utf8", "replace")
    if b"File not found" in res.stdout + res.stderr:
        # 写入阶段 rm/write 目标不应出现 "not found"，视为命令失败
        print(err)
        return False
    fail_re = re.compile(
        r"(Could not allocate|Could not|: (No such file|File not found|Permission denied"
        r"|Failed|Invalid|not a directory|can't|error))",
        re.IGNORECASE,
    )
    if fail_re.search(err):
        for ln in err.splitlines():
            ln = ln.strip()
            if not ln.startswith("debugfs "):
                print(ln)
        return False
    if res.returncode not in (0,):
        print(err)
        return False
    return True
