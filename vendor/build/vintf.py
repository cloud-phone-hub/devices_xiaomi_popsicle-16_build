"""vendor VINTF 设备 manifest 修正：target-level 对齐平台版本（官方 A16 FCM）。

CDH 架构下设备 VINTF manifest = lineage 基座 manifest.xml + lineage 片段
（/vendor/etc/vintf/manifest/*.xml，无 target-level）+ 官方 odm 片段
（/odm/etc/vintf/manifest/*.xml，读官方 odm 分区）。libvintf 合并片段要求片段
target-level 与基座**完全一致**（VintfObject::addDirectoryManifests →
HalManifest::addAll，不等即 UNKNOWN_ERROR），而官方 odm 的
nfc2-service-nxp.xml 带 target-level="202504"（官方 Android 16 FCM），lineage
virtio 基座却仍是 target-level="202404"（device/virt/virtio-common/configs/vintf/
manifest_target-level-202404.xml 的固有 pin）→ 整个设备 manifest 合并失败
（"Cannot add manifest fragment ... Conflicting target-level: 202404 vs. 202504"
→ NULL VINTF MANIFEST）→ keymint 等 HAL 不注册 → keystore2 起不来 → 引导卡死
（实测 2026-10-09）。

修复方向选"改自家基座"而非屏蔽官方片段：官方 odm 片段保持原样可见；基座
manifest.xml 本就是 lineage 生成件（非官方内容），且其自身 `<sepolicy><version>`
已是 202504，target-level 的 202404 是该配置文件的存量 pin。对齐值取
/etc/selinux/plat_sepolicy_vers.txt（与运行时同一文件，官方平台版本，两者同用
FCM YYYYMM 版本号）。
"""

import os
import re

from debugfs import cat, path_exists

MANIFEST = "/etc/vintf/manifest.xml"
VERS_FILE = "/etc/selinux/plat_sepolicy_vers.txt"
MANIFEST_LABEL = "u:object_r:vendor_configs_file:s0"
MODE = "0x81a4"  # 100644
_RE_LEVEL = re.compile(r'target-level="(\d{6})"')


def vintf_cmds(work_img, tmp_dir=None):
    """把 /etc/vintf/manifest.xml 的 target-level 对齐平台版本，返回 debugfs 命令。

    幂等：已等于平台版本则跳过；无 target-level 属性则告警跳过（不猜不改）。
    """
    if not path_exists(work_img, MANIFEST):
        print(f"[warn] 镜像内缺 {MANIFEST}，跳过 VINTF target-level 对齐")
        return []
    data = cat(work_img, MANIFEST)
    vers = cat(work_img, VERS_FILE)
    if data is None or vers is None:
        print(f"[warn] 读不到 {MANIFEST} 或 {VERS_FILE}，跳过 VINTF target-level 对齐")
        return []
    vers = vers.decode("ascii", "ignore").strip()
    text = data.decode("utf-8", "replace")
    m = _RE_LEVEL.search(text)
    if not m:
        print(f"[warn] {MANIFEST} 无 target-level 属性，跳过对齐")
        return []
    if m.group(1) == vers:
        print(f"[ok] VINTF target-level 已对齐 {vers}，跳过")
        return []
    old = m.group(1)
    text = _RE_LEVEL.sub(rf'target-level="{vers}"', text, count=1)
    tmp_dir = tmp_dir or os.path.dirname(work_img)
    os.makedirs(tmp_dir, exist_ok=True)
    tmp = os.path.join(tmp_dir, "vintf_manifest.xml")
    with open(tmp, "w") as f:
        f.write(text)
    print(f"[ok] VINTF target-level {old} -> {vers}（对齐官方 odm 片段，"
          f"见模块 docstring）")
    return [
        f"rm {MANIFEST}",
        f"write {tmp} {MANIFEST}",
        f"sif {MANIFEST} mode {MODE}",
        f"sif {MANIFEST} uid 0",
        f"sif {MANIFEST} gid 0",
        f"ea_set {MANIFEST} security.selinux {MANIFEST_LABEL}",
    ]
