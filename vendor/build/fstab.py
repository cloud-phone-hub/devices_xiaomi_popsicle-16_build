"""vendor fstab：删除 lineage 基座残留的 fstab.virtio*。

云机 fstab 改由 vendor patch 树提供 /etc/fstab.qcom（真机 gold /vendor/etc/fstab.qcom
为基准 + 云机特有着地：vdc/vdb/persist vfat 等；含 mi_ext 挂载、/mi_ext bind 与
overlay 全套），文件名与 ro.hardware=qcom（build-vendor-boot.py 改的
androidboot.hardware）一致。

lineage 基座自带的 fstab.virtio / fstab.virtio.gsi.sda / fstab.virtio.gsi.vdc 已无人
读取（旧逻辑是删 odm 行后重写，现整份由 fstab.qcom 取代），留着只会在 /vendor/etc
里暴露非真机文件（真机 /vendor/etc 只有 fstab.qcom/fstab.emmc/fstab.enableswap）。
"""

from debugfs import path_exists

# lineage 基座内要删除的 fstab.virtio 系列
FSTAB_NAMES = ("fstab.virtio", "fstab.virtio.gsi.sda", "fstab.virtio.gsi.vdc")


def fstab_cmds(work_img):
    """产出删除基座 fstab.virtio* 的 debugfs 命令清单（存在才删）。"""
    cmds = []
    for name in FSTAB_NAMES:
        img = "/etc/" + name
        if path_exists(work_img, img):
            cmds.append(f"rm {img}")
            print(f"[..] 删除基座 fstab: {img}（改由 fstab.qcom 承担）")
    return cmds
