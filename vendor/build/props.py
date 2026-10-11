"""vendor build.prop 渲染修正（属性声明收敛到 baselines，避免硬编码漂移）。

对齐 note13-5G-15 的 baselines vendor.build-prop-sets / build-prop-unsets：
云机侧与官方 vendor_a/build.prop 的差异（abi 口径、身份属性、图形后端键的
取舍等）全部声明在 baselines JSON，本模块只负责应用：
  sets：["key", "value"] 覆盖或追加；unsets："key" 删除所有匹配行。

本模块不判断具体键的语义（那是基线数据的事），只做机械的 set/unset 应用。
"""

import json
import os

from buildenv import baselines_file  # noqa: E402
from debugfs import cat, stat_meta  # noqa: E402


def _build_prop_edits():
    """读取 baselines vendor.build-prop-sets / build-prop-unsets。

    sets：["key", "value"] 覆盖或追加；unsets："key" 删除所有匹配行。
    """
    with open(baselines_file(), encoding="utf-8") as f:
        doc = json.load(f)
    v = doc.get("vendor", {})
    sets = [tuple(e) for e in v.get("build-prop-sets", []) if len(e) >= 2]
    unsets = list(v.get("build-prop-unsets", []))
    return sets, unsets


def host_content(work_img, work_dir):
    """读回镜像内 /build.prop，应用 baselines 声明的 set/unset 并写为宿主文件。"""
    data = cat(work_img, "/build.prop")
    if data is None:
        lines = []
    else:
        lines = data.decode("utf-8", "replace").splitlines()

    prop_sets, prop_unsets = _build_prop_edits()

    # 1) unset：删除所有以 "key=" 开头的行
    for key in prop_unsets:
        lines = [ln for ln in lines if not ln.startswith(key + "=")]

    # 2) set：已有键覆盖，缺省追加
    for k, v in prop_sets:
        matched = False
        for i, ln in enumerate(lines):
            if ln.startswith(k + "="):
                lines[i] = f"{k}={v}"
                matched = True
                break
        if not matched:
            lines.append(f"{k}={v}")

    tmp = os.path.join(work_dir, "build.prop")
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    return tmp


def prop_cmds(work_img, bp_host):
    """build.prop 注入 debugfs 命令清单（重写 + 把原 inode 的 mode/属主还回去）。

    rm+write 会新建 inode，mode/uid/gid 全落默认值；真机 /vendor/build.prop 是
    0600 root:root，硬编 0644 就成了一处可证伪的模式差异，故先取回原值再写回。
    """
    cmds = []
    orig = stat_meta(work_img, "/build.prop")
    if orig:
        cmds.append("rm /build.prop")
    mode, uid, gid = orig or (0o100644, 0, 0)
    cmds += [
        f"write {bp_host} /build.prop",
        f"sif /build.prop mode {mode}",
        f"sif /build.prop uid {uid}",
        f"sif /build.prop gid {gid}",
        "ea_set /build.prop security.selinux u:object_r:vendor_file:s0",
    ]
    return cmds
