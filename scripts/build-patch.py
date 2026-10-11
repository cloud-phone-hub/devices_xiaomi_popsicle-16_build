#!/usr/bin/env python3
"""构建 patch.img：修补后的 vendor/vendor_dlkm 内容打包为 ext4，供 vdc 挂载到 /mnt/vendor。

结构：patch.img（ext4）的 **root 就是 vendor 树**（运行态 /mnt/vendor/etc、
/mnt/vendor/lib64 …），root 下另有 vendor_dlkm/ 子树、官方 odm build.prop 落点
（odm/etc/build.prop，内核重定向 /odm/etc/build.prop 的命中目标）与少量自供目录
（VENDOR_SKELETON_DIRS：EFI/persist/shared 三个 mount_all 挂载点 + mi_ext 分区挂载点）。
内容、mode/uid/gid、security.selinux 标签全部自 **out/dist/rom 下 build-vendor /
build-vendor_dlkm 产物**（lineage 基座 + CDH 注入 + 自编译内核模块）逐文件提取
（debugfs stat/ea_get），与源分区逐字节一致（标签含 NUL 结尾），保证与"修补后 vendor
直接作 /vendor"的运行态等价。odm build.prop 例外：自官方 odm_a.img 提取 verbatim
（基线 length+sha256 校验），固定 meta 写入。

mke2fs -d 不保留 uid/gid（实测按宿主当前用户），故回填分两步：
  1. mke2fs -d 导入内容与 mode；
  2. debugfs -w 批量 sif uid/gid + ea_set security.selinux（-f 值文件，含 NUL）。

依赖：先跑 build-vendor / build-vendor_dlkm（产物 out/dist/rom/vendor.img、
out/dist/rom/vendor_dlkm.img 就位）。

用法（可在任何目录）：
  python3 build/scripts/build-patch.py              # 构建 out/dist/rom/patch.img
  python3 build/scripts/build-patch.py --dry-run    # 只打印不执行
  python3 build/scripts/build-patch.py --size 512   # 指定 patch.img 大小（MiB）
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from buildenv import (  # noqa: E402
    PATCH_DIST, SUPER_OUT, TMP_DIR, VENDOR_DIST, VENDOR_DLKM_DIST,
)
from buildenv import e2fs_tools, erofs_tool, load_baselines  # noqa: E402

# gold 基座的重定向表/屏蔽清单由 gold_tree 单一来源给出（覆盖清单 -> 内核表 -> patch 根
# 落地校验），这里只消费，不再手抄。
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "vendor", "build")))
import gold_tree  # noqa: E402

GOLD = gold_tree.gold_mode()

DEBUGFS, E2FSCK, _RESIZE2FS = e2fs_tools()

# 源镜像：out/dist/rom 下修补后产物（build-vendor / build-vendor_dlkm）
VENDOR_SRC = VENDOR_DIST
VENDOR_DLKM_SRC = VENDOR_DLKM_DIST

# patch.img 内布局。vdc 挂在 /mnt/vendor，所以 **vendor 树的内容直接落在镜像根**
# （运行态即 /mnt/vendor/etc、/mnt/vendor/lib64 …），vendor_dlkm 树落在 vendor_dlkm/
# 子目录（重定向 /vendor_dlkm/lib -> /mnt/vendor/vendor_dlkm/lib）。
# (patch 内子目录，"" = 镜像根；源镜像)
PATCH_TREES = (
    ("", VENDOR_SRC),
    ("vendor_dlkm", VENDOR_DLKM_SRC),
)

# 镜像根（= /mnt/vendor）下不许被 vendor 树自带内容占掉的名字：vendor_dlkm 是另一棵
# 源树的落点，其余四个是 VENDOR_SKELETON_DIRS 自供的目录。
ROOT_FORBIDDEN = ("vendor_dlkm", "EFI", "persist", "shared", "mi_ext")

# "指向不存在以屏蔽"的重定向，其落点必须真的不存在，否则屏蔽会反过来变命中：
#   /system/bin/update_verifier -> /mnt/vendor/system/bin/update_verifier
#       （缺 A/B bootctrl HAL 时会主动 reboot，形成引导死循环）
#   /odm/etc/selinux/odm_sepolicy.cil（仅 lineage 基座）
#       （官方 odm 策略引用 Qualcomm/厂商专属类型，与 lineage virtio vendor 策略不兼容，
#        secilc 解析失败会让 init 直接重启）
# gold 基座下官方 vendor 策略与官方 odm 策略配对，odm 那条屏蔽已解除（见
# gold_tree.GOLD_UNBLOCKED），/odm 直读官方分区，patch 根里不该再有 odm 子树。
# vendor 树自带一个空的 odm/ 目录，无妨；里面长出 sepolicy 才致命。
MUST_ABSENT = ("system/bin/update_verifier",)
if not GOLD:
    MUST_ABSENT = ("odm/etc/selinux/odm_sepolicy.cil",) + MUST_ABSENT

# 官方 odm /etc/build.prop 的 patch 内落点（内核重定向 /odm/etc/build.prop 的命中目标，
# 见 build-patch 步骤 2.5）。重定向无回退语义：目标缺失即 ENOENT，官方 odm 属性整体
# 丢失；故本文件必须收编（官方件 verbatim，基线校验，无需 note13 式 ABI 改写）。
ODM_PROP_PATH = "/odm/etc/build.prop"

WORK_DIR = os.path.join(TMP_DIR, "patch")
DEFAULT_SIZE_MIB = 384

# 目录/文件默认标签（源镜像缺 xattr 时兜底；正常应全部命中）
FALLBACK_LABEL = "u:object_r:vendor_file:s0"

# 自供条目（无源镜像 inode）的固定 meta：目录用 ROOT_META，odm build.prop 用基线 label。
ROOT_META = (0o040755, 0, 0, "u:object_r:vendor_file:s0")


def _ctx(type_):
    return f"u:object_r:{type_}:s0"


# —— /mnt/vendor 下必须自带的目录 ——
# vdc 以 first_stage_mount 挂在 /mnt/vendor，会盖掉 first_stage_init 在 /mnt tmpfs 上建
# 的那个空 /mnt/vendor。两件事因此必须烘进镜像：
#   1. fstab 里 /mnt/vendor/{EFI,persist,shared} 由二阶段 mount_all 挂载，挂载点得先
#      存在 —— 只读分区上 fs_mgr 的 mkdir 会 EROFS，挂载直接失败（persist 里是
#      grubenv，GRUB 的槽位与 android_theme 都靠它）。
#   2. /mnt/vendor/mi_ext 是真 mi_ext 分区（super 内 mi_ext_a，erofs）的挂载点：boot
#      ramdisk 一阶段 fstab 在 vdc 之后挂 mi_ext 到这里（见 build-boot.py FIRSTSTAGE_FSTAB），
#      init 随即原生加载其 /etc/build.prop 并解析 /etc/init（ro.mi.os.* / ro.ai.os.* 等
#      属性与 init.miui.mi_ext.rc 全部来自真分区内容）。
# 标签取「运行态策略里真的存在的那个类型」，而不是照抄 note13：
#   /mnt/vendor(/.*)? -> mnt_vendor_file（plat file_contexts），EFI/shared 用它；
#   persist 也用 mnt_vendor_file —— note13 抄来的 persist_file 在本盘会失效：
#   gold 基座的策略 = 官方 plat_sepolicy.cil + 官方 vendor_sepolicy.cil，两边都
#   **没有** persist_file 这个类型（MIUI/Android16 已把它并入 vendor_persist_*），
#   xattr 落不上 → 目录变 unlabeled → init 的 mounton 被拒 → /mnt/vendor/persist
#   挂不上（实例 22 实测 "Context u:object_r:persist_file:s0 is not valid
#   (left unmapped)" + "avc: denied { mounton } ... tcontext=u:object_r:unlabeled:s0"）。
#   真机上该挂载点本身就没有专门条目（落到 /mnt/vendor(/.*)? = mnt_vendor_file），
#   且官方 vendor 策略里 `(allow init_202504 mnt_vendor_file_202504:dir (mounton))`
#   已就位，故选 mnt_vendor_file：lineage/gold 两基座都合法。
# mi_ext 对齐真机实测（ls -Z /mi_ext 全树 system_file），与挂载源根标签一致以过 mounton。
# EFI/persist 挂上后的可见标签由 vfat 的 context= 决定，这里的底层标签只参与 init 的
# mounton 检查，与现状一致。
#
# /mnt 本身仍是 first_stage_init 挂的可写 tmpfs：init.rc `on init` 的 mkdir、zygote 的
# fs_prepare_dir("/mnt/user/0")、vold 的 pass_through/runtime/installer 视图、netmgr 的
# /mnt/run/adb_net 全部照旧可写，不需要任何骨架复刻。
VENDOR_SKELETON_DIRS = (
    # (相对镜像根的路径, 含类型位的 mode, uid, gid, SELinux 类型)
    ("EFI",        0o040755, 0,    0,    "mnt_vendor_file"),
    ("persist",    0o040755, 0,    0,    "mnt_vendor_file"),
    ("shared",     0o040770, 1000, 1000, "mnt_vendor_file"),
    ("mi_ext",     0o040755, 0,    0,    "system_file"),
)


def _skeleton_meta():
    """骨架表 -> {patch 内绝对路径: (mode, uid, gid, label, is_dir)}。"""
    meta = {}
    for rel, mode, uid, gid, type_ in VENDOR_SKELETON_DIRS:
        meta["/" + rel] = (mode, uid, gid, _ctx(type_), True)
    return meta


SKELETON_META = _skeleton_meta()


def _q(img_path):
    """debugfs 参数引号：镜像里有带空格的路径（/vendor/usr/idc/QEMU Virtio Tablet.idc），
    不引会被按空白截断，stat/ea_get 静默取不到值而走兜底。"""
    return f'"{img_path}"' if re.search(r"\s", img_path) else img_path


def _run(cmd, **kw):
    print("  + " + " ".join(cmd))
    return subprocess.run(cmd, **kw)


def _stat_meta(img, path):
    """debugfs stat 取 (mode, uid, gid)；失败返回 (0o100644, 0, 0)。

    mode 为**含文件类型位的完整 mode**（如 regular+0755 = 0o100755）：
    debugfs stat 的 "Mode:" 只含权限位，"Type:" 给类型；sif mode 字段上限 65535
    且按十进制解析，故须拼完整 mode 后以十进制写入（0o100755 = 33261）。
    """
    res = _run([DEBUGFS, "-R", f"stat {_q(path)}", img],
               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    perms = uid = gid = None
    ftype = ""
    for raw in res.stdout.decode("utf8", "replace").splitlines():
        line = raw.strip()
        # Mode/User/Group 均可能位于行中（如 "Inode: 106 ... Mode: 0644 ..."、
        # "User: 0   Group: 0   Project: 0"），一律用 "xxx:" in line 独立匹配
        if "Type:" in line:
            m = re.search(r"Type:\s*(\w+)", line)
            if m:
                ftype = m.group(1)
        if "Mode:" in line:
            m = re.search(r"Mode:\s+(\d+)", line)
            if m:
                perms = int(m.group(1), 8)
        if "User:" in line:
            m = re.search(r"User:\s+(\d+)", line)
            if m:
                uid = int(m.group(1))
        if "Group:" in line:
            m = re.search(r"Group:\s+(\d+)", line)
            if m:
                gid = int(m.group(1))
    if perms is None or uid is None or gid is None:
        print(f"[warn] stat 解析失败: {img} {path}，按 0644/0/0 兜底")
        return 0o100644, 0, 0
    type_bits = {"regular": 0o100000, "directory": 0o040000,
                 "symlink": 0o120000, "block": 0o060000, "char": 0o020000,
                 "fifo": 0o010000, "socket": 0o140000}.get(ftype, 0o100000)
    return perms | type_bits, uid, gid


def _ea_label(img, path):
    """debugfs ea_get 取 security.selinux 标签；缺失返回 FALLBACK_LABEL。"""
    res = _run([DEBUGFS, "-R", f"ea_get {_q(path)} security.selinux", img],
               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    text = res.stdout.decode("utf8", "replace")
    m = re.search(r'= "([^"]*)"', text)
    if not m:
        print(f"[warn] 缺 security.selinux: {img} {path}，兜底 {FALLBACK_LABEL}")
        return FALLBACK_LABEL
    val = m.group(1)
    # 反转义 \ooo（如 \000 -> NUL）
    val = re.sub(r"\\([0-7]{3})", lambda mm: chr(int(mm.group(1), 8)), val)
    val = val.replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")
    return val


def _rdump(img, dest):
    """debugfs rdump 整树解出（内容/mtime；不保留属主/权限/xattr）。"""
    os.makedirs(dest, exist_ok=True)
    res = _run([DEBUGFS, "-R", f"rdump / {dest}", img],
               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    # rdump 尾部 "Operation not permitted while changing ownership" 是 macOS 非 root 提示，忽略
    err = res.stderr.decode("utf8", "replace")
    if "File not found" in err or "Error" in err:
        print(err)
        sys.exit(1)


def _collect(stage_root):
    """遍历宿主 staging 树，返回 [(img 内路径, 宿主路径, 是否目录)]。"""
    entries = []
    for base, dirs, files in os.walk(stage_root):
        for d in dirs:
            hp = os.path.join(base, d)
            entries.append((os.path.relpath(hp, stage_root), hp, True))
        for f in files:
            hp = os.path.join(base, f)
            entries.append((os.path.relpath(hp, stage_root), hp, False))
    return sorted(entries)


def _baseline_odm_build_prop():
    """自基线清单取 odm /etc/build.prop 基线件：(img_path, length, sha256, label)。"""
    e = (load_baselines() or {}).get("odm", {}).get("baseline-build-prop")
    if not e:
        print("[err] 基线清单缺少 odm.baseline-build-prop（build/baselines/<build-id>.json）")
        sys.exit(1)
    return (e["path"], e["length"], e["sha256"], e["label"])


def _official_odm_build_prop(img_path, length, sha256, root):
    """自官方 odm_a.img 提取基线件，长度+sha256 校验后写入 patch root 的 odm 落点。"""
    odm_img = os.path.join(SUPER_OUT, "odm_a.img")
    if not os.path.isfile(odm_img):
        print(f"[err] 官方 odm_a.img 缺失: {odm_img}（先 unpack）")
        sys.exit(1)
    res = subprocess.run([erofs_tool("dump.erofs"), f"--path={img_path}", "--cat", odm_img],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if res.returncode != 0 or not res.stdout:
        print(f"[err] 官方 odm 缺少基线件: {img_path}")
        sys.exit(1)
    data = res.stdout
    if len(data) != length or hashlib.sha256(data).hexdigest() != sha256:
        print(f"[err] 官方基线件校验失败 {img_path}: len={len(data)}"
              f" sha256={hashlib.sha256(data).hexdigest()}"
              f"（期望 {length} / {sha256}）")
        sys.exit(1)
    dst = os.path.join(root, ODM_PROP_PATH.lstrip("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(data)


def build_patch(dry_run=False, size_mib=DEFAULT_SIZE_MIB):
    for name, src in PATCH_TREES:
        if not os.path.isfile(src):
            print(f"[err] 源镜像缺失: {src}")
            sys.exit(1)
    odm_bp_path, odm_len, odm_sha, odm_label = (
        _baseline_odm_build_prop() if not GOLD else (None,) * 4)
    odm_meta = (0o100644, 0, 0, odm_label)
    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(PATCH_DIST), exist_ok=True)
    work_img = os.path.join(WORK_DIR, "patch.work.img")
    stage = os.path.join(WORK_DIR, "stage")

    if dry_run:
        print(f"[..] (dry) patch.img -> {PATCH_DIST}（{size_mib} MiB，"
              f"含 {[n or '<镜像根>=vendor' for n, _ in PATCH_TREES]} 子树"
              f"{'' if GOLD else f' + 官方 odm {odm_bp_path}'}）")
        return 0

    # 1. rdump 两棵源树到 staging
    raw = {}
    for name, src in PATCH_TREES:
        key = name or "root"
        dest = os.path.join(stage, "raw_" + key)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        print(f"[..] rdump {src} -> {dest}")
        _rdump(src, dest)
        raw[key] = dest

    # 2. 组装 patch root：vendor 树内容摊到镜像根（= /mnt/vendor），vendor_dlkm 树落到
    #    root/vendor_dlkm（= /mnt/vendor/vendor_dlkm）。
    root = os.path.join(stage, "root")
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    for name, _src in PATCH_TREES:
        key = name or "root"
        dst = os.path.join(root, name) if name else root
        os.makedirs(dst, exist_ok=True)
        for entry in os.listdir(raw[key]):
            shutil.move(os.path.join(raw[key], entry), os.path.join(dst, entry))
    # 镜像根摊平后，我们自己要放上去的目录名不能被 vendor 树自带内容占掉。
    _tree_dirs = {n for n, _ in PATCH_TREES if n}
    for bad in ROOT_FORBIDDEN:
        if bad in _tree_dirs:            # vendor_dlkm 是我们自己放上去的，不算冲突
            continue
        if os.path.lexists(os.path.join(root, bad)):
            print(f"[err] vendor 树自带 /{bad}，与镜像根布局冲突（需人工复核）")
            sys.exit(1)

    # 2.3 vendor 树自带的 odm 若是绝对符号链接（lineage 约定 /vendor/odm -> /odm；官方
    #     vendor_a 亦为 /odm -> /odm 形态），摊平到镜像根后，它会让屏蔽落点
    #     /mnt/vendor/odm/etc/selinux/odm_sepolicy.cil 在 path_walk 内解析回官方 /odm
    #     （符号链接目标不经过 getname_flags 重定向），"指向不存在以屏蔽"反过来命中
    #     官方厂商策略 -> secilc "Failed to resolve allow statement" -> InitFatalReboot
    #     引导死循环。换成真实空目录即恢复 ENOENT 语义。
    odm_link = os.path.join(root, "odm")
    if os.path.islink(odm_link):
        tgt = os.readlink(odm_link)
        os.unlink(odm_link)
        os.makedirs(odm_link)
        print(f"[..] odm 符号链接（-> {tgt}）替换为真实空目录")

    # 2.4 把 /vendor/lib/modules 由软链接改为真实目录（直接放模块副本，仅 lineage 基座）。
    #     lineage 基座这里是 -> /vendor_dlkm/lib/modules 的**绝对**软链接；内核
    #     getname_flags 只改写用户态初始路径，软链接目标在 path_walk 内解析不经过重定向，
    #     会落到官方 vendor_dlkm 分区读到厂商 ko（Exec format error）。改为真实目录后，
    #     重定向直接命中本目录，不依赖软链接/内核特例。
    #     gold 基座的清单条目本身就是官方形态的**相对**软链接（-> ../vendor_dlkm/lib/
    #     modules），摊平到镜像根后在 /mnt/vendor 内自重解析，既不越界也不重复占块，
    #     故保持链接形态；check_patch_root 会验证它不是断链。
    v_mods = os.path.join(root, "lib", "modules")
    if not GOLD and os.path.islink(v_mods):
        d_mods = os.path.join(root, "vendor_dlkm", "lib", "modules")
        print(f"[..] 替换软链接 {v_mods} -> 真实目录（源: {d_mods}）")
        os.unlink(v_mods)
        if os.path.isdir(d_mods):
            shutil.copytree(d_mods, v_mods)
        else:
            os.makedirs(v_mods)

    # 2.5 官方 odm build.prop 收编（仅 lineage 基座：内核重定向 /odm/etc/build.prop 的
    #     命中目标）。重定向规则无回退语义：目标不存在即 ENOENT，官方 odm 属性（整机
    #     身份、ro.product.property_source_order=odm,vendor,...——全分区唯一定义处）整体
    #     丢失，合并属性源序退回 AOSP 默认，与真机行为分叉。故收编官方件 verbatim：
    #     官方已是纯 64 位 abilist 声明（abilist32 空），无需 note13 式 ABI 改写，
    #     长度+sha256 基线校验后原样写入（meta 见 plan：0644/0:0/vendor_file）。
    #     gold 基座下该重定向已解除，/odm 直读官方分区，patch 根里不该出现 odm 子树。
    if not GOLD:
        _official_odm_build_prop(odm_bp_path, odm_len, odm_sha, root)
        print(f"[..] 收编官方 odm {odm_bp_path} -> {ODM_PROP_PATH}")

    # 2.6 /mnt/vendor 下必须自带的目录（三个 mount_all 挂载点 + mi_ext 分区挂载点）
    for rel, *_rest in VENDOR_SKELETON_DIRS:
        if os.path.lexists(os.path.join(root, rel)):
            print(f"[err] 骨架与 vendor 树内容冲突: /{rel}（基座变了？需人工复核）")
            sys.exit(1)
    for rel, *_rest in VENDOR_SKELETON_DIRS:
        os.makedirs(os.path.join(root, rel), exist_ok=True)
    print(f"[..] /mnt/vendor 骨架 {len(VENDOR_SKELETON_DIRS)} 目录")

    # 2.7 屏蔽落点必须真的不存在（见 MUST_ABSENT 说明）：odm_sepolicy.cil 命中会让
    #     secilc 解析失败、init 直接重启；update_verifier 命中会形成引导死循环。
    #     用 exists（跟随符号链接）而不是 lexists，避免绝对符号链接把检查骗过去。
    for rel in MUST_ABSENT:
        if os.path.exists(os.path.join(root, rel)):
            print(f"[err] 屏蔽落点竟存在: /mnt/vendor/{rel}（会反噬，需人工复核）")
            sys.exit(1)
    print(f"[..] 屏蔽落点校验通过（{len(MUST_ABSENT)} 项均不存在）")

    # 2.8 gold 基座：内核重定向表由覆盖清单生成，而 patch 根是它唯一的运行态答源。
    #     逐条断言表里每个命中目标在本树里存在且形态正确（屏蔽项断言缺席）。
    if GOLD:
        gold_tree.check_patch_root(root)

    # 3. 逐路径从源镜像取元数据（mode/uid/gid/label）
    plan = []  # (patch 内路径, 源镜像, 源镜像内路径, 是否目录)
    # vendor 树摊在镜像根上，os.walk(root) 会连带走到后面几步放进去的自供子树，
    # 这些顶层名字一律跳过（各自有独立 plan 条目）。
    _skip_top = _tree_dirs | {"odm"} | {k.lstrip("/").split("/", 1)[0]
                                        for k in SKELETON_META}
    for name, src in PATCH_TREES:
        prefix = "/" + name if name else ""
        # os.walk 不含树根本身：vendor 树的根就是镜像根 "/"，vendor_dlkm 是 "/vendor_dlkm"
        plan.append((prefix + "/" if name else "/", src, "/", True))
        for rel, _hp, is_dir in _collect(os.path.join(root, name) if name else root):
            if not name and rel.split("/", 1)[0] in _skip_top:
                continue
            patch_path = f"{prefix}/{rel}"
            if patch_path in SKELETON_META:
                continue
            if not name and not GOLD and rel == "lib/modules":
                # 步骤 2.4 已把该路径从符号链接替换为真实目录；源镜像里它仍是符号链接，
                # 回填若取源 inode 类型位会把实目录改坏（sif mode 覆盖 inode 类型）。
                # 固定套用目录 ROOT_META（0o040755 / 0:0 / vendor_file）。
                # gold 基座保留官方相对软链接形态，直接套源 inode，无需特例。
                plan.append((patch_path, None, None, True))
                continue
            plan.append((patch_path, src, f"/{rel}", is_dir))
    # 官方 odm build.prop 落点（/odm 子树不来自 vendor 树，全部自供 meta：
    # 目录 ROOT_META、文件 odm_meta）。gold 基座下 /odm 不接管，整棵子树退场。
    if not GOLD:
        plan.append(("/odm", None, None, True))
        plan.append(("/odm/etc", None, None, True))
        plan.append((ODM_PROP_PATH, None, None, False))
    # /mnt/vendor 骨架（三个 mount_all 挂载点 + mi_ext 分区挂载点）：一律固定 meta
    for patch_path, (_mode, _uid, _gid, _label, is_dir) in sorted(SKELETON_META.items()):
        plan.append((patch_path, None, None, is_dir))

    # 4. mke2fs -d 导入内容与 mode（uid/gid 由 sif 回填）
    if os.path.exists(work_img):
        os.remove(work_img)
    size_bytes = size_mib * 1024 * 1024
    # mkfs.ext4 末尾参数是块数（非字节）；显式 -b 4096 使大小与 size_mib 一致
    _run([os.path.join(os.path.dirname(DEBUGFS), "mkfs.ext4"),
          "-q", "-F", "-b", "4096", "-d", root, work_img, str(size_bytes // 4096)])
    print(f"[ok] mke2fs -d 导入 {len(plan)} 项 -> {work_img}（{size_mib} MiB）")

    # 5. debugfs -w 单会话回填 uid/gid + security.selinux（含 NUL 结尾）
    cmds = []
    labels = {}
    for patch_path, src, img_path, is_dir in plan:
        if src is None:                     # 骨架/自供文件：固定 meta
            if patch_path in SKELETON_META:
                mode, uid, gid, label = SKELETON_META[patch_path][:4]
            elif patch_path == ODM_PROP_PATH:
                mode, uid, gid, label = odm_meta
            else:
                mode, uid, gid, label = ROOT_META
        else:
            mode, uid, gid = _stat_meta(src, img_path)
            label = _ea_label(src, img_path)
        labels[patch_path] = label
        target = _q(patch_path)
        cmds += [
            f"sif {target} mode {mode}",       # mode 已是含类型位的十进制 int
            f"sif {target} uid {uid}",
            f"sif {target} gid {gid}",
        ]
    # 标签统一用 -f 值文件（label + NUL 结尾，与源字节一致）
    val_files = []
    cmd_file = os.path.join(WORK_DIR, "patch-debugfs.cmd")
    with open(cmd_file, "w") as f:
        # 先写 sif（mode/uid/gid），再写 ea_set（标签）
        for c in cmds:
            f.write(c + "\n")
        for patch_path in [p for p, _s, _i, _d in plan]:
            # 值文件名折掉空白与斜杠：镜像里有带空格的路径，值文件路径自己带空格会在
            # debugfs 参数里被截断（同名折叠用 hash 后缀兜住）
            slug = re.sub(r"[^A-Za-z0-9._-]", "_", patch_path) +                 "-" + hashlib.sha1(patch_path.encode("utf8")).hexdigest()[:8]
            vf = os.path.join(WORK_DIR, "selinux" + slug + ".val")
            if patch_path not in val_files:
                # 恰好一个 NUL 结尾：`_ea_label` 已把源里的 \000 还原进字符串，再补一次
                # 就成了双 NUL。内核只剥一个尾部 NUL，多出的那个会留在 context 里。
                with open(vf, "wb") as fv:
                    fv.write(labels[patch_path].rstrip("\x00").encode("utf8") + b"\x00")
                val_files.append(patch_path)
            f.write(f"ea_set -f {vf} {_q(patch_path)} security.selinux\n")
    res = _run([DEBUGFS, "-w", "-f", cmd_file, work_img],
               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    err = res.stderr.decode("utf8", "replace")
    if res.returncode != 0 or re.search(
            r"Could not|Couldn't|File not found|No such file|exceeds|Usage:|error",
            err, re.I):
        print(err)
        print("[err] debugfs 回填标签/属主失败")
        sys.exit(1)
    print(f"[ok] 回填 {len(plan)} 项 uid/gid/标签")

    # 6. e2fsck 校验
    res = _run([E2FSCK, "-fy", work_img], capture_output=True)
    if res.returncode not in (0, 1):
        print(res.stderr.decode("utf8", "replace"))
        print(f"[err] e2fsck 失败: {res.returncode}")
        sys.exit(1)
    print("[ok] e2fsck 通过")

    # 7. 输出
    shutil.copy2(work_img, PATCH_DIST)
    print(f"[ok] patch.img -> {PATCH_DIST}（{os.path.getsize(PATCH_DIST) // 2**20} MiB）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="popsicle-16 patch.img 构建（vdc 挂载到 /mnt/vendor）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    ap.add_argument("--size", type=int, default=DEFAULT_SIZE_MIB,
                    help="patch.img 大小（MiB，默认 %(default)s）")
    args = ap.parse_args()
    sys.exit(build_patch(dry_run=args.dry_run, size_mib=args.size))


if __name__ == "__main__":
    main()
