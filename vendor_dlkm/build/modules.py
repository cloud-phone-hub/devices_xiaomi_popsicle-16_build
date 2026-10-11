"""vendor_dlkm 内核模块注入：产物发现 + 索引重建 + debugfs 命令清单/回读校验。

模块清单不硬编码：以产物目录内实际 .ko 为准（popsicle 内核产物含 virtio_console/
rfkill/cfg80211/mac80211/mac80211_hwsim/libarc4/cdh_virt_wifi/cdh_goodix_fingerprint/
cdh_cpu_probe/cdh_device_info/cdh_media），索引按各 .ko modinfo 的
name/depends/alias 生成，保证与内核模块加载器一致（沿用 note13-5G-15 的
debugfs 原地补丁思路）。
"""

import os
import re
import subprocess
import sys

from buildenv import KERNEL_MODULES_DIR
from debugfs import DEBUGFS, cat, path_exists, run

# 镜像内模块目录：lineage 基座无 kver 子目录，模块平铺 /lib/modules
MOD_DIR_IN_IMG = "/lib/modules"
# lineage modules.dep 使用 /vendor_dlkm/lib/modules/ 作为前缀（note13 实测口径，
# lineage-23 底包同族；如底包不同，按 depot 回读结果调整）
DEP_PREFIX = "/vendor_dlkm/lib/modules"

# SELinux label：vendor_dlkm 分区 .ko 统一 vendor_file 域
MODULE_SELABEL = "u:object_r:vendor_file:s0"

# 注入前先 rm 的基座测试/kunit/tipc 模块：基座几乎写满，需先释放 inode/块，才能为
# 新增 .ko 与索引分配（对齐 note13 reclaim 思路；rm 前以 debugfs stat 探测，
# 列表与基座实际内容差异自动跳过）。
RECLAIM_MODULES = [
    "clk-gate_test.ko",
    "clk_kunit_helpers.ko",
    "clk-test.ko",
    "dev_addr_lists_test.ko",
    "ext4-inode-test.ko",
    "fat_test.ko",
    "hid-uclogic-test.ko",
    "iio-test-format.ko",
    "input_test.ko",
    "kheaders.ko",
    "kunit-example-test.ko",
    "kunit-test.ko",
    "kunit.ko",
    "lib_test.ko",
    "of_kunit_helpers.ko",
    "platform-test.ko",
    "regmap-kunit.ko",
    "regmap-ram.ko",
    "regmap-raw-ram.ko",
    "soc-topology-test.ko",
    "soc-utils-test.ko",
    "time_test.ko",
    "tipc.ko",
    "tipc_diag.ko",
]


def _modinfo(ko_path):
    """用 strings 提取 .modinfo 段 name=/depends=/vermagic=。返回 (name, deps, ver)。"""
    out = subprocess.run(["strings", ko_path],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    text = out.stdout.decode("latin1", "replace")
    m_name = re.search(r"\bname=(\S+)", text)
    m_deps = re.search(r"\bdepends=(\S*)", text)
    m_ver = re.search(r"\bvermagic=(\S+)", text)
    name = m_name.group(1) if m_name else None
    deps = [d for d in m_deps.group(1).split(",") if d] if m_deps else []
    ver = m_ver.group(1) if m_ver else None
    return name, deps, ver


def _modinfo_aliases(ko_path):
    """用 strings 提取 .modinfo 段所有 alias= 行。"""
    out = subprocess.run(["strings", ko_path],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    text = out.stdout.decode("latin1", "replace")
    return [m.group(1) for m in re.finditer(r"\balias=(\S+)", text)]


def _cat_lines(work_img, img_file):
    """读镜像内文本文件（缺失返回空列表），按行去空白。"""
    data = cat(work_img, img_file)
    if data is None:
        return []
    return [ln for ln in data.decode("utf8", "replace").splitlines() if ln]


def _write_host(work_dir, tmp_name, lines):
    """把索引内容写成宿主临时文件，返回绝对路径。"""
    p = os.path.join(work_dir, tmp_name)
    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")
    return p


def discover_modules():
    """扫描内核模块产物目录，返回 [(fn, src, name, deps)]；未就位返回 []。"""
    if not (os.path.isdir(KERNEL_MODULES_DIR) and os.listdir(KERNEL_MODULES_DIR)):
        return []
    kois = []
    for fn in sorted(os.listdir(KERNEL_MODULES_DIR)):
        src = os.path.join(KERNEL_MODULES_DIR, fn)
        if not fn.endswith(".ko") or not (os.path.isfile(src) and os.path.getsize(src) > 0):
            continue
        name, deps, ver = _modinfo(src)
        if not name:
            name = fn[:-3]
        print(f"[..] {fn}: modinfo name={name}, vermagic={ver}")
        kois.append((fn, src, name, deps))
    return kois


def inject_cmds(work_img, kois, work_dir):
    """生成注入 debugfs 命令清单：先 rm 基座测试模块腾 inode/块，再写 .ko 与四类索引。

    只读基座索引后返回命令，由编排层合并为一次 debugfs 会话执行。
    """
    img_load = f"{MOD_DIR_IN_IMG}/modules.load"
    img_dep = f"{MOD_DIR_IN_IMG}/modules.dep"
    img_alias = f"{MOD_DIR_IN_IMG}/modules.alias"
    img_opts = f"{MOD_DIR_IN_IMG}/modules.options"

    # 基座 modules.dep 中仍存在的模块名（用于过滤缺失依赖）+ 注入覆盖/回收剔除
    base_names = {l.split("/")[-1].removesuffix(".ko")
                  for l in _cat_lines(work_img, img_dep)}
    inject_fns = {fn for fn, _, _, _ in kois}
    reclaimed = {fn[:-3] for fn in RECLAIM_MODULES}
    names = [name for _, _, name, _ in kois]
    available = set(names) | {
        n for n in base_names if f"{n}.ko" not in inject_fns and n not in reclaimed
    }

    # modules.load：全量重建（virtio 云手机场景自编译模块均需加载；依赖自动解析）
    load_host = _write_host(work_dir, "modules.load", names)

    # modules.dep：全量重建，依赖按 modinfo depends= 解析并过滤不存在项
    dep_lines = []
    for _fn, _src, name, deps in kois:
        resolved = [f"{DEP_PREFIX}/{d}.ko" for d in deps if d in available]
        dep_lines.append(f"{DEP_PREFIX}/{name}.ko"
                         + (": " + " ".join(resolved) if resolved else ":"))
    dep_host = _write_host(work_dir, "modules.dep", dep_lines)

    # modules.alias：保留基座既有条目 + 注入模块自带的 modinfo alias（去重）
    alias_lines = list(_cat_lines(work_img, img_alias))
    seen = set(alias_lines)
    for _fn, src, _name, _deps in kois:
        for a in _modinfo_aliases(src):
            if a not in seen:
                alias_lines.append(a)
                seen.add(a)
    alias_host = _write_host(work_dir, "modules.alias", alias_lines)

    # modules.options：基座无此文件；保留既有条目，并追加 cdh_media 的加载参数。
    # allow_userptr=1 是 Android Codec2 走 USERPTR 队列的前提（见 virtio_media
    # README）：驱动默认关闭该参数时会把 V4L2_BUF_CAP_SUPPORTS_USERPTR 清位，而
    # DMABUF 恒被拒，v4l2_codec2 解码器因「输入队列既不支持 DMABUF 也不支持
    # USERPTR」直接拒绝启动。libmodprobe 的 Insmod() 会把此处选项作为
    # finit_module 的 args 下发（libmodprobe_ext.cpp），故写在这里即随模块加载生效。
    opts_lines = list(_cat_lines(work_img, img_opts))
    opts_lines.append("options cdh_media allow_userptr=1")
    opts_host = _write_host(work_dir, "modules.options", opts_lines)

    cmds = []
    for fn in RECLAIM_MODULES:
        img_path = f"{MOD_DIR_IN_IMG}/{fn}"
        if path_exists(work_img, img_path):
            cmds.append(f"rm {img_path}")

    plan = [(src, f"{MOD_DIR_IN_IMG}/{fn}") for fn, src, _, _ in kois] + [
        (load_host, img_load),
        (dep_host, img_dep),
        (alias_host, img_alias),
        (opts_host, img_opts),
    ]
    for src, img_path in plan:
        if path_exists(work_img, img_path):
            cmds.append(f"rm {img_path}")
        cmds += [
            f"write {src} {img_path}",
            f"sif {img_path} mode 0x81a4",
            f"sif {img_path} uid 0",
            f"sif {img_path} gid 0",
            f"ea_set {img_path} security.selinux {MODULE_SELABEL}",
        ]
    return cmds


def verify(work_img, kois, work_dir):
    """回读校验：ko 用 dump+cmp 严格比对，不一致即报错退出。"""
    ok = True
    for fn, src, _name, _deps in kois:
        dump = os.path.join(work_dir, f"readback_{fn}")
        target = f"{MOD_DIR_IN_IMG}/{fn}"
        r = run([DEBUGFS, "-R", f"dump {target} {dump}", work_img],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode != 0 or not os.path.isfile(dump):
            print(f"    回读缺失: {target}")
            ok = False
            continue
        if open(src, "rb").read() != open(dump, "rb").read():
            print(f"    回读内容不匹配: {target}")
            ok = False
        os.remove(dump)
    if not ok:
        print("[err] 回读校验失败")
        sys.exit(1)
    print("[ok] 回读校验通过")
