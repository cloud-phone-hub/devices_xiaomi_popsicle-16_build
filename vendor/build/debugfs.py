"""debugfs 工具层：宿主工具定位 + 镜像内文件探测/读取/命令会话。

被 props / selinux / inject 与编排层（vendor_builder）共享；debugfs 单命令失败
常仍返回 0，须扫描 stderr 错误标记（run_command_file 内实现）。
"""

import os
import re
import subprocess

from buildenv import e2fs_tools

DEBUGFS, E2FSCK, _RESIZE2FS = e2fs_tools()


def run(cmd, **kw):
    print("  + " + " ".join(cmd))
    return subprocess.run(cmd, **kw)


def q(img_path):
    """debugfs 命令行里的路径参数：含空白必须加引号。

    镜像里确有带空格的文件名（/vendor/usr/idc/QEMU Virtio Tablet.idc）。debugfs 按
    空白切参数，未引时 `sif … mode …` 会把 "QEMU Virtio Tablet.idc" 截成 "QEMU"，
    报 "Usage:"（stderr 扫描抓不到）或指向不存在的值文件，属主/模式/标签静默不生效。
    """
    return f'"{img_path}"' if re.search(r"\s", img_path) else img_path


def path_exists(work_img, img_path):
    """debugfs stat 探测镜像内路径是否存在。"""
    res = run([DEBUGFS, "-R", f"stat {q(img_path)}", work_img],
              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    blob = (res.stdout + res.stderr).lower()
    return not (b"not found" in blob or b"no such" in blob)


# debugfs stat 的 "Type:" -> inode 文件类型位
_TYPE_BITS = {"regular": 0o100000, "directory": 0o040000, "symlink": 0o120000,
              "char": 0o020000, "block": 0o060000, "fifo": 0o010000,
              "socket": 0o140000}


def stat_meta(work_img, img_path):
    """镜像内该路径的 (含类型位的完整 mode, uid, gid)；取不到返回 None。

    `sif mode` 写的是整个 i_mode，类型位缺失会把文件变成 0 类型 inode，故须把
    debugfs 分开给出的 "Type:" 与 "Mode:" 拼起来。重写文件前先取它，才能把原有
    mode/属主原样还回去（/vendor/build.prop 真机是 0600 root:root）。
    """
    res = run([DEBUGFS, "-R", f"stat {q(img_path)}", work_img],
              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if b"File not found" in res.stdout + res.stderr:
        return None
    perms = uid = gid = None
    ftype = ""
    for raw in res.stdout.decode("utf8", "replace").splitlines():
        line = raw.strip()
        # 三个字段可能同行也可能分行（"Inode: 28 Type: regular Mode: 0644 ..."、
        # "User: 0   Group: 0   Project: 0"），逐行独立匹配
        m = re.search(r"Type:\s*(\w+)", line)
        if m:
            ftype = m.group(1)
        m = re.search(r"Mode:\s+(\d+)", line)
        if m:
            perms = int(m.group(1), 8)
        m = re.search(r"User:\s+(\d+)", line)
        if m:
            uid = int(m.group(1))
        m = re.search(r"Group:\s+(\d+)", line)
        if m:
            gid = int(m.group(1))
    if perms is None or uid is None or gid is None:
        return None
    return (_TYPE_BITS.get(ftype, 0o100000) | perms, uid, gid)


def cat(work_img, img_file):
    """debugfs cat 某文件并去掉 banner 行。不存在返回 None。"""
    res = run([DEBUGFS, "-R", f"cat {q(img_file)}", work_img],
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
