"""vendor 注入文件的 SELinux 标签（基线清单 vendor.selinux-labels）。

标签自官方 gold images dump 后外置到 build/baselines/<build-id>.json（原
.xttar 伴生声明已删除）；换 ROM = 更新基线 JSON，代码零改动。标签缺失直接
报错退出，不静默回退，避免注入出错误标签。
"""

import sys

from buildenv import load_baselines


def patch_labels():
    """返回基线标签映射（键 = 注入树内相对路径）；清单缺失时报错退出。"""
    baselines = load_baselines()
    labels = (baselines or {}).get("vendor", {}).get("selinux-labels")
    if not labels:
        print("[err] 基线清单缺失或缺少 vendor.selinux-labels")
        sys.exit(1)
    return labels
