"""popsicle-16 vendor sepolicy：向 /etc/selinux/vendor_sepolicy.cil 追加最小 CDH 声明。

内容（本盘精简版，机制对齐 note13-5G-15）：
  1. pioneer 段：guest 值守域（daemon 及其自研 adbd 的 shell/sync 子进程以
     seclabel u:r:pioneer:s0 运行，user/group root 且不降权）。该域需要完全 root
     语义：凡是它作为主体的判定都要放行，逐条 allow 枚举既写不全也易碎（vendor
     CIL 追加段还存在互相覆盖丢失的历史事故）。
  2. coredomain 属性区直读段（引导修复，承自 note13 实测）。
  3. 官方 gold vendor 属性类型段（gold_props.prop_types_cil，/dev/__properties__
     类型命名空间对齐真机）。

HAL 专属段（audio/radio/sensors/camera/.../chipinfo）不在本盘：各 HAL 在 bring-up
时经 hal_cil_chain 的同一机制追加（保持扩展点）。

pioneer 的 permissive 由**内核**承担（本盘 init 内嵌 secilc 不支持 CIL `permissive`
语句，Unknown keyword → init fatal）：
  * 内核补丁：kernel/source/security/selinux/ss/services.c (cdh_mark_pioneer_permissive)
  * 内核配置：kernel/build/lineage-kernel.config
    CONFIG_CDH_PIONEER_PERMISSIVE 在 security_load_policy() 解析完策略、发布前把
    pioneer 写进 policydb->permissive_map，等价于策略文件里的 "permissive pioneer"，
    且免疫策略追加段覆盖。

因此 pioneer 段只需保留 permissive **覆盖不到** 的两类规则，加上结构性类型声明：
  1. 启动链：源域是 init（换域 execve 的前置 process transition / file execute），
     permissive 只看源上下文 type，init 不是 pioneer，覆盖不到；
  2. 反向授权：源域是对端域、目标才是 pioneer（netd fwmark / system_server 管道 /
     媒体域 binder 回程），这些判定同样不因 pioneer 是 permissive 而放行；
  3. 类型/属性声明本身：init 的 `seclabel u:r:pioneer:s0` 若解析不到该类型会直接
     Invalid argument，故 (type pioneer)/(type pioneer_exec) 及其
     domain/exec_type/file_type/vendor_file_type 归属必须始终存在。

两条 vendor cil 约束（均实测自基座 vendor_sepolicy.cil）：
  * plat 类型在 vendor cil 中必须以「版本化影子名」引用（如 init_{vers} /
    shell_exec_{vers}），版本号运行时读 /etc/selinux/plat_sepolicy_vers.txt，不得
    硬编码。vendor 自有类型（如 mediacodec）未版本化，直接引用原名；
  * 属性（domain/exec_type/file_type/vendor_file_type）以原名关联，追加单成员
    typeattributeset 与基座的大集合语句按 union 语义合并。

entrypoint 永远只落 exec_type（plat 约束 neverallow domain ~exec_type:file entrypoint），
故 pioneer_exec / shell_exec_{vers} 仅授 entrypoint。

平台策略源 = **官方（Xiaomi）plat_sepolicy.cil**（CDH 架构 guest 平台 = 官方 super
零改动直拷，运行时 init 内嵌 secilc 以官方 plat 声明 class/权限；与 note13 的 AOSP 15
基线不同，AOSP 16 各家内核 classmap 已分叉，「各家一致」假设不成立）。构建期据此
做两件事：
  1. sanitize_vendor_cil：清理 lineage vendor 策略里官方类表没有的权限语句（实测
     netlink_route_socket 裸 nlmsg ×1 + nlmsg allowx ×2、system firmware_load ×1 ——
     目标内核 6.12.23 classmap 同样没有这些权限，内核永远不会对其判定，剔除/裁剪
     语义等价），否则开机 secilc "Failed to resolve permission" → InitFatalReboot；
  2. validate_cil 的 class/权限/名字全集改用官方 plat（名集 = 官方 plat +
     plat_pub_versioned.cil + vendor 基座 cil 声明全集，精确名匹配），片段内自声明
     的类型（pioneer/pioneer_exec 及 gold 属性类型）跳过；引用错名不再等到开机
     secilc 失败才发现。
"""

import os
import re
import shutil
import subprocess
import sys

from buildenv import SUPER_OUT, TMP_DIR, erofs_tool
from debugfs import cat
import gold_props

SELINUX_DIR = "/etc/selinux"
VERS_FILE = SELINUX_DIR + "/plat_sepolicy_vers.txt"
POLICY_FILE = SELINUX_DIR + "/vendor_sepolicy.cil"
PUB_VER_FILE = SELINUX_DIR + "/plat_pub_versioned.cil"
# 基座内该文件的原始标签（rm+write 新 inode 后须回填，debugfs write 不带 xattr）
POLICY_LABEL = "u:object_r:vendor_configs_file:s0"
MARKER = ";; cdh popsicle-16: pioneer"
# ---- coredomain 直读 vendor_default_prop（引导修复段，见 coredomain_vendor_prop_read_cil）----
VENDOR_PROP_READ_MARKER = ";; cdh popsicle-16: coredomain-vendor-prop-read"

# ---- HAL 段（自研虚拟 HAL；扩展点见 hal_cil_chain）----
# vibrator：lineage 基座无 vibrator HAL —— 官方 odm 声明 android.hardware.vibrator@2
# （IVibrator/default），而 lineage vendor 既无该服务进程、vendor_service_contexts
# 也无表项 → SystemServer 构造 VibratorManagerService 时 nativeGetVibratorIds() 阻塞
# （等不到 HAL 服务），主线程卡 65s 被 Watchdog 杀，boot_completed 永不置位
# （实测栈：VibratorManagerService.<init> → nativeGetVibratorIds）。
# 自研虚拟 HAL（build/vendor/source/android.hardware.vibrator）接管
# IVibrator/default 与 IVibratorManager/default。
VIBRATOR_MARKER = ";; cdh popsicle-16: vibrator-hal"
VSC = "/etc/selinux/vendor_service_contexts"
VSC_LABEL = "u:object_r:vendor_service_contexts_file:s0"
VIBRATOR_VSC_LINES = [
    "android.hardware.vibrator.IVibrator         u:object_r:hal_vibrator_service:s0",
    "android.hardware.vibrator.IVibratorManager  u:object_r:hal_vibrator_service:s0",
]

# IR（红外）：官方 /odm 声明 FEATURE_CONSUMER_IR，而 lineage vendor 无 IR 服务 →
# SystemServer 的 ConsumerIrService 抛
# "RuntimeException: FEATURE_CONSUMER_IR present, but no IR HAL loaded!" 启动即死。
# 已收编官方 vendor 的 android.hardware.ir-service.example
# （build/vendor/source/android.hardware.ir），故补服务名映射与 add/find 授权。
IR_MARKER = ";; cdh popsicle-16: ir-hal"
IR_VSC_LINES = [
    "android.hardware.ir.IConsumerIr             u:object_r:hal_ir_service:s0",
]

# Audio：自研 AIDL 服务（build/vendor/source/android.hardware.audio，代码承自
# note13-5G-15 / oriole-16）注册 core.IModule/{default,primary,r_submix} +
# core.IConfig/default + effect.IFactory/default。gold 基座的 hal_audio_default 是
# 给 QTI ADSP 音频写的，实测（out/tmp/patch/stage/root/etc/selinux/vendor_sepolicy.cil，
# 全量 allow 扫描）它对 plat 公共类型 **零** 授权：
#   - 没有一条 tcontext=hal_audio_service_202504 的 service_manager add/find
#     （80 条 hal_audio_default allow 里只有 vndbinder/servicemanager binder 和
#      QTI 自有 vendor 服务）→ 自研进程注册不出来，框架继续死等；
#   - 没有 /dev/ashmem（FMQ）与 /dev/snd（ALSA 输出）的 chr_file 授权
#     —— 真机走 ADSP 音轨，从不碰这两个节点。
# 客机实测（实例 44 root）：/dev/snd/{controlC0,pcmC0D0p,timer} = audio_device:s0、
# /dev/ashmem = ashmem_device:s0，两段都必要。
# neverallow 核对：官方 plat 里唯一一条 default_android_service 的 neverallow 是
# `(neverallow base_typeattr_239 default_android_service (service_manager (add find list)))`，
# 其中 base_typeattr_239 = (all)；它约束的是**未版本化的原名**，而 mapping/202504.cil
# 只有 `(typeattributeset default_android_service_202504 (default_android_service))`
# 一类填充、没有任何影子版 neverallow → 对影子类型授权不触 neverallow。
# v1/v2/v3 三个独立标记沿用 note13 的分段做法：增量追加、重复构建幂等。
AUDIO_MARKER = ";; cdh popsicle-16: audio-hal"
AUDIO_V2_MARKER = ";; cdh popsicle-16: audio-hal-v2-ashmem"
AUDIO_V3_MARKER = ";; cdh popsicle-16: audio-hal-v3-alsa"
# /system/etc/selinux/plat_service_contexts 逐实例覆盖 IModule/{default,r_submix}、
# IConfig/default、effect.IFactory/default，**没有 IModule/primary**（客机实测）。
# 这里按 note13 已实测的口径补接口级三行；它是否命中取决于 servicemanager 的
# contexts 查表（启动时一次性加载，vendor 侧未必生效），所以下面 audio_hal_cil 里
# 同时给未映射兜底类型 default_android_service_{vers} 补了 add/find。
AUDIO_VSC_LINES = [
    "android.hardware.audio.core.IModule         u:object_r:hal_audio_service:s0",
    "android.hardware.audio.core.IConfig         u:object_r:hal_audio_service:s0",
    "android.hardware.audio.effect.IFactory      u:object_r:hal_audio_service:s0",
]

# FRP 承载块设备：MIUI 16 pdb 按 ro.frp.pst 打开 FRP 分区，本盘把它软链到未使用的
# misc(vda3)（见 build/vendor/patch/etc/init/cph_frp_symlink.rc）。平台策略只给
# system_server 授了 frp_block_device，而该节点的类型是 misc_block_device：
# DAC 已由 ueventd 规则放开（0660 root:system），SELinux 侧此处补 allow（策略里带
# dontaudit，故日志无 AVC，表现为 open EACCES）。
FRP_MARKER = ";; cdh popsicle-16: frp-block-device"
FRP_BLK_PERMS = ["open", "read", "write", "ioctl", "getattr", "map", "lock",
                 "append"]

# QTI perf2 HAL：官方 system 的 compatibility_matrix.device.xml 将其列为必需 HAL，
# 而 lineage 基座既无该服务、也无任何 perf 策略类型 → 已收编官方 HAL
# （build/vendor/source/vendor.qti.hardware.perf2），其策略段（vendor_hal_perf* 类型/域/
# allow）从官方 vendor_sepolicy.cil 逐行移植（同为 plat 版本后缀 _202504，可直接对齐）。
PERF_MARKER = ";; cdh popsicle-16: perf2-hal"
PERF_VSC_LINES = [
    "vendor.qti.hardware.perf2.IPerf/default      u:object_r:vendor_hal_perf2_service:s0",
]

# Remote KeyMint HAL（承自 note13-5G-15）：guest 侧经 /dev/hvc3 与 host secure_env
# 通信（自研件见 build/vendor/source/android.hardware.keymint）。基座已提供大半：
# hal_keymint_default 域声明、init 换域（typetransition init_{vers}
# hal_keymint_default_exec process hal_keymint_default）、serialno_prop 与
# vendor_security_patch_level_prop 读、官方 plat_service_contexts 的 keymint 服务名
# 映射、官方 plat 的 hal_keymint_server→hal_keymint_service add/find。本段只补：
#   1. /dev/hvc3 设备类型 keymaster_device（官方 plat 与 lineage vendor 均无此类型，
#      自声明）+ hal_keymint_default 的 chr_file 读写；
#   2. kmsg 写（rc 的 `file /dev/kmsg w`）与 kmsg_debug(tmpfs) 写（v2）；
#   3. vendor_init 的 /dev/hvc3 wait stat 权限（init.virt.rc post-fs 门控，v2）；
#   4. system_prop 属性区读（v3：Configure 的 os_version/os_patchlevel，实例 15 实测）。
# 版本化影子填充说明（与 note13 的机制差异）：note13 基座的 plat_pub_versioned.cil
# 里 kmsg_device_{vers} 等影子属性为空、须手动填充；本盘官方 system 自带
# /system/etc/selinux/mapping/202504.cil，运行时 init 按 plat_sepolicy_vers.txt
# 自动加载并完成 `(typeattributeset kmsg_device_202504 (kmsg_device))` 等填充（实测
# 该文件含全部所需填充）。此处仍保留 note13 的显式填充语句：typeattributeset 为
# union 语义，重复填充无副作用，且不依赖官方 mapping 文件的存在性。
KEYMINT_MARKER = ";; cdh popsicle-16: keymint-hal"
KEYMINT_MARKER2 = ";; cdh popsicle-16: keymint-hal-v2"
KEYMINT_MARKER3 = ";; cdh popsicle-16: keymint-hal-v3"

# grub bootctrl HAL 读写 persist：官方策略里 hal_bootctl_default 只被授权到块设备
# （真机它直读 /dev/block/bootdevice/by-name/misc 的 slot 标记），本盘的 A/B 槽位状态
# 由 GRUB 的 grubenv_abootctrl 文件维护（服务来自 lineage 的
# com.android.hardware.boot.grub APEX，实测域为 hal_bootctl_default、uid 1000），
# 故需给它 /mnt/vendor/persist 的目录与文件权限。挂载点与其 vfat context= 同为
# mnt_vendor_file（见 build-patch.py VENDOR_SKELETON_DIRS 与 fstab.qcom 注释），
# 该类型在 lineage/gold 两基座都存在，且 plat 的
# `neverallow { coredomain -init -ueventd -vold } mnt_vendor_file:dir ...` 不涉及
# vendor 域 hal_bootctl_default，不触 neverallow。
BOOTCTL_PERSIST_MARKER = ";; cdh popsicle-16: bootctl-persist"

# 云机的三块盘全是 virtio-blk：vda（官方 super 直拷 + EFI/persist/misc/firmware 分区）、
# vdb（/data）、vdc（patch.img，挂 /mnt/vendor）。plat 的 file_contexts 把
# /dev/block/vd[a-z0-9]* 统一打成 vd_device（AOSP 为模拟器准备的类型，属 dev_type），
# 而真机侧没有任何 virtio 盘 —— 官方 gold 策略里 vd_device 只有一处类型声明、
# **零条 allow**（vendor_sepolicy.cil 里 grep 只命中 1 次即声明本身）。于是 gold 基座上
# 凡是碰盘的域全部 EACCES，lineage 基座反而没这问题（其策略带 virtio 授权）。
# 实例 28（2026-10-10，v5 内核）实测的 enforcing 拒绝：
#     vold  → vdb  blk_file { read write }   （/data，fstab 主行）
#     e2fs  → vda5 blk_file { read }         （mke2fs /metadata）
#     fsck  → vda4 blk_file { read write }   （fsck_msdos persist；/data 带 check 同域）
#     hal_bootctl_default → vda3 blk_file { getattr }（by-name/misc，每秒重试刷日志）
# 权限集照抄官方给 userdata/metadata_block_device 的那一条：
#     (allow vold userdata_block_device (blk_file (ioctl read write getattr lock
#         append map open watch watch_reads)))
# neverallow 只约束 appdomain/logd/netd/shell/ueventd/traced*/logpersist/recovery_*
# 对 dev_type 的 blk_file 访问，本段的源域（init/vold/fsck/e2fs/system_server/
# hal_bootctl_default）一个都不在其中。
VD_DEVICE_MARKER = ";; cdh popsicle-16: virtio-blk-device"
VD_BLK_PERMS = "ioctl read write getattr lock append map open watch watch_reads".split()

# 图形链：官方 gold 策略是给 QTI 自己的 composer/allocator 写的（SDM 走
# /dev/kgsl-3d0 + QTI dmabuf heap，不开 DRM 节点），而云机替换成的是 lineage 的
# drm_hwcomposer + minigbm gralloc（APEX 自 /mnt/vendor/apex 激活），两者都要打开
# /dev/dri/{card0,renderD128}。gold 的 vendor_file_contexts 把这两个节点都打成
# graphics_device（真机 renderD128 也是 graphics_device，不是 AOSP 的 gpu_device），
# 而 hal_graphics_allocator_default 在 gold 策略里对 graphics_device 零授权 ——
# 实例 29（2026-10-11，build 30）实测 enforcing 拒绝：
#     hal_graphics_allocator_default → graphics_device chr_file { read write }
#         （card0 与 renderD128 各一条，服务随即 exited with status 1，
#           init 每 ~10s 重启一次，IAllocator 一直握在 QTI allocator 手里）
#     hal_graphics_allocator_default → system_prop file { read }（1988 次）
#     hal_graphics_composer_default  → system_prop file { read }
# system_prop 是 plat 属性类型，覆盖 dev./hw./sys./net./ro.hw./service. 等前缀
# （plat_property_contexts:44-56）；minigbm 要读 ro.hardware.gralloc 一类键选后端，
# gold 表无此前缀 → 落 system_prop。gold_props 只复刻 **vendor 自有**属性类型的授权，
# plat 类型的这条缺口得在这里补。
# neverallow 核对：plat 里含 graphics_device 的 neverallow 只约束
# isolated_app/shell/untrusted_app；含 *_system_prop 的只约束 property_service set
# （dynamic_system_prop/bootanim_system_prop/exported3_system_prop），都不涉及本段。
GRAPHICS_MARKER = ";; cdh popsicle-16: virtio-graphics"
DRM_CHR_PERMS = "ioctl read write getattr lock append map open watch watch_reads".split()
PROP_READ_PERMS = "getattr open read map".split()

# virtio-gpu 的 **sysfs 子树**定标（实例 59，2026-10-15 实测）。/dev/dri 三个节点连
# 目录本身都已 gpu_device（dri_label_rewrite），Enforcing 下仍黑：SF(940) 与 launcher
# 各自带 llvmpipe-0..7，logcat 仍有 90 条 `W EGL-MAIN: Failed to open any DRM device`；
# 同一批进程里 minigbm 却能 `Loaded driver virtgpu_virgl on path /dev/dri/renderD128`
# —— 说明节点能开，卡在 mesa/libdrm **枚举** DRM 的那一步：libdrm 无 udev，走 sysfs
# 读 /sys/dev/char/<maj>:<min>/uevent 与 /sys/class/drm/*/device 来认总线/设备。
# 与 oriole-16（实例 17，基座 lineage）同路径对照，唯一的差别就是标签：
#     oriole   /sys/devices/pci0000:00/0000:00:01.0/drm/renderD128 = sysfs_gpu
#              /sys/dev/char/226:128（跟随软链）                    = sysfs_gpu
#     popsicle 同上两处                                             = sysfs
# 权限侧的缺口由此暴露：gold plat 给 appdomain/SF/bootanim 的是
#     (allow base_typeattr_257 sysfs_gpu (file (ioctl read getattr lock map open watch
#         watch_reads)))   base_typeattr_257 = appdomain - isolated_app_all
# 而对泛用 sysfs 只有写侧 neverallow、**没有任何读授权** —— 也就是说 sysfs_gpu 这个
# 标签本身就是 app 能读 GPU sysfs 的唯一通道，标签不对=读不到=枚举不出 DRM 设备。
# 口径来源：lineage 的 vendor 策略里就有这条 genfscon（oriole 基座
# vendor_sepolicy.cil:61 `(genfscon sysfs "/devices/pci0000:00/0000:00:01.0" ...
# sysfs_gpu)`，另有 :79/:80/:81 三个槽位与 /devices/platform/vkms），本盘 virtio-gpu
# 实测就落在 01.0（renderD128 的 realpath 一致），照抄这一条即可。
# dir/lnk_file 的 grant 也是 lineage vendor 段补的（gold plat 只有 sysfs_gpu:file，
# dir 侧仅 virtual_camera 有）：列目录、跟随 /sys/class/drm 下的软链都要 dir/lnk_file。
# neverallow 核对：官方 plat 与 sysfs_gpu 相关的 neverallow 为 0 条（grep 全文），
# sysfs 侧只有 write neverallow（base_typeattr_269）与 sysfs_net 的 app 读禁令，
# 本段全是 r_*_perms，不触线。
DRM_SYSFS_GPU_GENFS = ('(genfscon sysfs "/devices/pci0000:00/0000:00:01.0"'
                       ' (u object_r sysfs_gpu ((s0) (s0))))\n')
SYSFS_GPU_DIR_PERMS = "ioctl read getattr lock open watch watch_reads search".split()
SYSFS_GPU_FILE_PERMS = "ioctl read getattr lock map open watch watch_reads".split()
# vendor 自有/公共属性名不带版本后缀（appdomain 在 plat_pub_versioned 里就是
# `(typeattribute appdomain)`，hal_graphics_* 是 gold vendor 类型），其余走 _{vers} 影子。
DRM_SYSFS_GPU_DOMAINS_PLAT = ("surfaceflinger", "bootanim", "gpuservice", "system_server")
DRM_SYSFS_GPU_DOMAINS_UNVERSIONED = (
    "appdomain", "hal_graphics_allocator_default", "hal_graphics_composer_default")


# RenderEngine 的 JIT 可执行内存（实例 37，2026-10-11，build 40 实测）。
# mapper 链修好后 SF 第一次跑到创建上下文/画首帧，随即在 RenderEngine 线程 SIGSEGV：
#   Fatal signal 11 (SIGSEGV), code 2 (SEGV_ACCERR), fault addr 0x7c5100d000
#       in tid ... (RenderEngine), pid ... (surfaceflinger)
#   Cause: trying to execute non-executable memory.
#   #00 pc 0  <anonymous:7c5100d000>
#   #01..#05 pc 0x153eea0/0x153eb44/0x1466548/0x1460ea0/0x1460a00  /vendor/lib64/libgallium_dri.so
#   #16 SkiaGLRenderEngine::flushAndSubmit  #19 Cache::primeShaderCache
# 同一时刻的 AVC 只有这一条（同一 pid 连刷 276 次）：
#   avc: denied { execmem } for scontext=u:r:surfaceflinger:s0
#        tcontext=u:r:surfaceflinger:s0 tclass=process permissive=0
# 即 mesa 的 llvmpipe JIT 拿不到可执行页（映射成功但没 PROT_EXEC），跳进去就是
# 跳到不可执行内存。gold 官方策略没有这条是因为真机走 Adreno —— GPU 侧编译着色器，
# CPU 侧从不 execmem；lineage 基座（3a-16 / sm-s9380-16 的 vendor_sepolicy.cil）本来
# 就带这四条，来源是 cuttlefish 自己的
# device/google/cuttlefish/shared/swiftshader/sepolicy/{surfaceflinger,bootanim}.te，
# 这里按同一口径移植（只取 coredomain 的两个域）。
# neverallow 核对：官方 plat 的 execmem neverallow 只有
# perfetto / system_server / traced / traced_probes 四条，surfaceflinger 与 bootanim
# 都不在其中；zygote / appdomain / app_zygote 官方 plat 本就授予（32604/9376/11816 行），
# 故 app 侧不需要追加。system_server 有 neverallow，不碰。
VFC = "/etc/selinux/vendor_file_contexts"
VFC_LABEL = "u:object_r:vendor_configs_file:s0"
KEYMINT_FC_LINES = [
    "# cdh popsicle-16: remote keymint /dev/hvc3",
    "/dev/hvc3  u:object_r:keymaster_device:s0",
]

# mesa SP-HAL 闭包里 gold vendor_file_contexts 未覆盖的三件（实例 30，2026-10-11，
# build 32 实测）。云机图形后端是 lineage 的 mesa（GRAPHICS_ALIAS 把厂商 soname 换成
# mesa 字节），它会被**加载进 coredomain 进程**：libEGL_adreno.so / libGLESv2_adreno.so
# 进 SurfaceFlinger 与每个用 GL 的 app，libminigbm_gralloc_upstream.so 是 mapper 的
# stable-C 实现、同样进 SF/app 进程。coredomain 读 /vendor 下的 so 必须是
# same_process_hal_file —— plat 有
#     (neverallow (and (domain) (not (coredomain))) ... )   ← 不含本场景
#     (neverallow base_typeattr_480 vendor_file (file (... execute open execute_no_trans)))
#     base_typeattr_480 = (and (coredomain) ...)
# 即 coredomain 对 vendor_file 的 execute/open 是 neverallow，只能靠改标签解决，
# 不能给 surfaceflinger 直接授权。实例 30 的实测拒绝正是这一条：
#     avc: denied { read } for comm="surfaceflinger" name="libui.so" dev="dm-5"
#          scontext=u:r:surfaceflinger:s0 tcontext=u:object_r:vendor_file:s0 tclass=file
#     -> libEGL "couldn't find an OpenGL ES implementation" -> SF SIGABRT
# gold 表已把闭包其余各件标成 same_process_hal_file（libEGL_adreno/libGLESv2_adreno/
# libGLESv1_CM_adreno/hw.vulkan.adreno/hw.gralloc.default/libdrm/libz/libc++/libcutils/
# libbase/libhardware/libdmabufheap/libgralloctypes/libutils），只漏 mesa 独有的这三件。
# 反向安全性：same_process_hal_file 属 vendor_file_type，且 plat 有
#     (allow base_typeattr_232 same_process_hal_file (file (read getattr map execute open)))
#     base_typeattr_232 = (and (domain) (not (coredomain)))
# 即全部 vendor 域照旧可读可执行 —— 对 libui.so 这种厂商 HAL 也在用的库，改标签不
# 会削掉任何既有访问。
GRAPHICS_FC_MARKER = "# cdh popsicle-16: mesa SP-HAL closure"
GRAPHICS_FC_LINES = [
    GRAPHICS_FC_MARKER,
    r"/(vendor|system/vendor)/lib(64)?/libui\.so u:object_r:same_process_hal_file:s0",
    r"/(vendor|system/vendor)/lib(64)?/libgallium_dri\.so"
    " u:object_r:same_process_hal_file:s0",
    r"/(vendor|system/vendor)/lib(64)?/libminigbm_gralloc_upstream\.so"
    " u:object_r:same_process_hal_file:s0",
    r"/(vendor|system/vendor)/lib(64)?/android\.hardware\.graphics\.allocator"
    r"@2\.0\.so u:object_r:same_process_hal_file:s0",
    r"/(vendor|system/vendor)/lib(64)?/android\.hardware\.graphics\.allocator"
    r"@3\.0\.so u:object_r:same_process_hal_file:s0",
    r"/(vendor|system/vendor)/lib(64)?/android\.hardware\.graphics\.allocator"
    r"@4\.0\.so u:object_r:same_process_hal_file:s0",
]

# /dev/dri/* 标签口径（实例 54/55，2026-10-15 实测）。gold（MIUI 官方 vendor）的
# vendor_file_contexts 把三个 DRM 节点逐条点名成 graphics_device（本盘 1172/1242/1243
# 行：card0、controlD64、renderD128），因为真机 GLES 走 /dev/kgsl-3d0，DRM 只服务显示；
# AOSP/lineage 口径 /dev/dri/* 是 gpu_device（oriole 基座 251/254/262 行）。云机的 GLES
# 是 mesa(libEGL) 在**进程内** open renderD128，标签不对就开不了：
#     W EGL-MAIN: Failed to open any DRM device
# mesa 打不到 DRM 就静默回落 llvmpipe（客机 CPU 渲染），宿主侧 scanout 纹理永远收不到
# 帧、VNC 全黑。实测对照：
#   · 实例 54：guest 每个 GL 进程都有该报错 + 24~40 个 llvmpipe 线程 + VNC 纯色帧；
#     运行期 chcon 两节点成 gpu_device 后 SF 的该报错消失（暴露下一条
#     `failed to create DRI image from FD`），证实标签就是闸门。
#   · oriole-16（基座为 lineage）：该报错 0 条、gpu_device 拒绝 0 条、VNC 出完整画面。
# 权限侧不用加：plat 有
#   (allow base_typeattr_257 gpu_device (chr_file (ioctl read write getattr lock append
#       map open watch watch_reads)))       base_typeattr_257 = appdomain - isolated_app_all
# 即全部 app 域 + surfaceflinger/system_server/mediaswcodec/gpuservice/bootanim 等
# 本就随 plat 拿到 gpu_device；graphics_device 侧的 neverallow 只约束
# isolated_app/shell/untrusted_app（改标签反而把它们放行）。
# 为什么不能用 ueventd 规则第 5 列定标（试过，实例 55 失败）：Android 16 的 ueventd
# 已删除 file_contexts 列，只认 no_fnm_pathname ——
#   ueventd.rc: 680: invalid option 'u:object_r:gpu_device:s0', only no_fnm_pathname
# 故只能改这张表本身（gold 字节在此文件上已由 KEYMINT/GRAPHICS 段改写，非 verbatim）。
DRI_LABEL_FROM = b"u:object_r:graphics_device:s0"
DRI_LABEL_TO = b"u:object_r:gpu_device:s0"

# /dev/dri **目录**本身也要定标（实例 56，2026-10-15 实测）。节点改成 gpu_device 后
# 画面仍黑：SF/每个 GL 进程照旧起 llvmpipe 线程（/proc/<sf>/task = llvmpipe-0..7），
# VNC 仍纯色帧；而 setenforce 0 + 重启后该报错归零、launcher 无 llvmpipe 线程
# => 还有一个 dontaudit（所以 logcat 里看不到）的 enforcing 拒绝。dmesg 抓到了：
#   avc: denied { read } for  scontext=u:r:platform_app:s0:c512,c768
#        tcontext=u:object_r:device:s0 tclass=dir permissive=0  app=com.android.systemui
#   （×159）
# mesa 枚举 DRM 走 drmGetDevices2()，要先 **列目录** /dev/dri 再逐个 open 节点；
# 目录拿不到 read 就打不开 DRM → 静默回落 llvmpipe。gold 的 vendor_file_contexts 没有
# /dev/dri 目录表项，devtmpfs 建的目录继承父目录标签 = device；plat 只给
# (allow domain device:dir (search))，device:dir read 只给了 crosvm/hal_camera/
# device_as_webcam/slideshow/system_server —— 都不含 appdomain/surfaceflinger。
# lineage 口径把目录点名成 gpu_device（本盘 oriole 基座 262 行 `/dev/dri  gpu_device`），
# 实测 oriole-16 实例 17：`ls -ldZ /dev/dri` = u:object_r:gpu_device:s0（同机制生效），
# 且 plat 给 appdomain/SF 的 gpu_device:dir 里带 read/open/search。
# 机制：ueventd 建节点时会对路径逐级 MakeDirector + selinux_android_restorecon，
# restorecon 读的正是这张 vendor_file_contexts（ueventd.rc 里没有标签列，见上）。
# 追加的是目录字面量（不是 /dev/dri/.*）：libselinux 判据 = 非通配前缀最长者胜、
# 同长度时字面量胜过正则，与既有三条节点行不冲突。gold plat 无 neverallow 约束
# device:dir/gpu_device:dir 的这段（platdiff：lineage-only 14 条、gold-only 0 条）。
DRI_DIR_LABEL_LINE = b"/dev/dri\t\tu:object_r:gpu_device:s0"

# gralloc mapper 的 passthrough 服务名映射（实例 35，2026-10-11，build 35 实测）。
# libui 拿到后缀后经 servicemanager 查 `mapper/<后缀>` 的声明，本盘该名字在 gold 的
# vendor_service_contexts 里**没有表项** -> 落 plat 的 default_android_service ->
#   avc: denied { find } pid=<SF> name=mapper/minigbm_upstream
#        scontext=u:r:surfaceflinger:s0 tcontext=u:object_r:default_android_service:s0
#        tclass=service_manager
#   ServiceManagerCppClient: Failed to get isDeclared for mapper/minigbm_upstream:
#        Status(-1, EX_SECURITY): 'SELinux denied for service.'
# 类型 hal_graphics_mapper_service 官方 vendor 策略已有（小米自己的
# hal_miface/hal_mseccam/hal_misecmedia 就在 find 它），缺的只是这一行名字映射。
# 客户端授权不用新增：plat 有
#   (allow hal_graphics_allocator_client hal_graphics_mapper_service (service_manager (find)))
# 且 plat 把 surfaceflinger / bootanim / cameraserver / mediaserver / system_server
# 以及 (and (appdomain) ...) 都放进了 hal_graphics_allocator_client —— 本段只改
# service_contexts（运行期由 servicemanager 读取的文本表，不是镜像 xattr），
# 不碰任何 allow，也不触 neverallow。
MAPPER_VSC_LINES = [
    "mapper/minigbm_upstream                      "
    "u:object_r:hal_graphics_mapper_service:s0",
]

# 运行态平台策略源：官方（Xiaomi）system 分区（unpack 自官方 super 拆出；与 guest
# 运行时一致——super 零改动直拷）。宿主 dump.erofs 读官方 system 报
# "invalid de[0].nameoff"（实测 2026-10-09），改用 fsck.erofs --extract --path
# 提取（与 build-patch 读官方 odm 的 erofs 工具链同一来源）。
OFFICIAL_PLAT_IMG = os.path.join(SUPER_OUT, "system_a.img")
OFFICIAL_PLAT_XDIR = os.path.join(TMP_DIR, "vendor", "official_plat")

# ---- 服务 execv 前置检查权限集（init_{vers} -> pioneer_exec:file）----
_INIT_EXEC_P = "ioctl read open execute getattr map".split()


def _allow(target, ttype, cls, perms):
    """渲染一条 allow；权限 set 化去重排序（输出稳定、天然无重复权限）。"""
    body = " ".join(sorted(set(perms)))
    return f"(allow {target} {ttype} ({cls} ({body})))\n"


def pioneer_cil(vers: str) -> str:
    """生成 pioneer 追加段（vers = plat 策略版本后缀，如 202504）。

    内核已把 pioneer 类型置入 permissive_map（内核开关 CONFIG_CDH_PIONEER_PERMISSIVE），
    凡以 pioneer 为主体的 AVC 判定一律放行，故本段只保留 permissive **覆盖不到** 的
    两类规则：
      1. 启动链：源域是 init（换域 exec 的前置 process transition / file execute）；
      2. 反向授权：源域是对端域、目标才是 pioneer（netd / system_server / 媒体域），
         这些判定不因 pioneer 是 permissive 而放行。
    另有两条 pioneer -> {pioneer_exec,shell_exec}:file entrypoint 属结构性声明，内核侧
    已覆盖，保留以保证「内核开关关掉时仍能起服务」。
    """
    stmts = [
        "(type pioneer)\n",
        "(type pioneer_exec)\n",
        "(typeattributeset domain (pioneer))\n",
        "(typeattributeset exec_type (pioneer_exec))\n",
        "(typeattributeset file_type (pioneer_exec))\n",
        "(typeattributeset vendor_file_type (pioneer_exec))\n",
        # ---- 启动链：源域 init，permissive 覆盖不到 ----
        # init 换域拉起 pioneer 服务（noatsecure/siginh/rlimitinh 消 transition 噪音）
        _allow(f"init_{vers}", "pioneer", "process",
               "transition noatsecure siginh rlimitinh".split()),
        _allow(f"init_{vers}", "pioneer_exec", "file", _INIT_EXEC_P),
        # ---- 结构性 entrypoint（内核侧已覆盖，保留兜底）----
        _allow("pioneer", "pioneer_exec", "file", ["entrypoint"]),
        _allow("pioneer", f"shell_exec_{vers}", "file", ["entrypoint"]),
        # ---- 反向授权：源域为对端域，permissive 覆盖不到 ----
        # fwmarkd（netd 域）经 SCM_RIGHTS 持有 pioneer 的 socket fd 写 fwmark：
        # 缺 use 时 connect 在 fwmark 客户端路径被拒（avc denied { use }
        # scontext=netd tcontext=pioneer tclass=fd，note13 实测）。
        _allow(f"netd_{vers}", "pioneer", "fd", ["use"]),
        # netd InetDiag（destroySockets）要读写 pioneer 的 socket 才能完成销毁，
        # 否则每 0.5s 刷 avc denied（note13 实测）；udp 同理预留。
        # setopt：fwmarkd（binder 线程）给 connect 路径上 pioneer 的 socket 打
        # SO_MARK，缺它 connect 直接 EACCES（note13 实测，2117 次 avc 全是它）
        _allow(f"netd_{vers}", "pioneer", "tcp_socket",
               ["getattr", "getopt", "setattr", "setopt", "read", "write"]),
        _allow(f"netd_{vers}", "pioneer", "udp_socket", ["getattr", "getopt", "setattr", "read", "write"]),
        # 投屏媒体链路反向授权（note13 实测）: binder 回程（reply 携带 binder
        # 对象 / 服务回调）由对端域发起 transfer/call。缺 mediaswcodec_{vers} →
        # pioneer:binder transfer 时 Codec2 AIDL "software" 取 IConfigurable 的
        # transaction 直接 FAILED_TRANSACTION，CHECK abort（且被 vendor dontaudit
        # 吞掉 avc，logcat 无痕迹）。
        # mediacodec 是 vendor 自有类型（lineage vendor cil 未版本化声明）；
        # mediaswcodec 是 plat 公共类型（版本化影子），swcodec 软编解码进程二选一
        # 实跑，两个域都授。
        *(_allow(src, "pioneer", cls, perms)
          for src in ("mediacodec", f"mediaswcodec_{vers}",
                      f"mediaextractor_{vers}", f"mediaserver_{vers}",
                      f"audioserver_{vers}", f"cameraserver_{vers}",
                      f"surfaceflinger_{vers}", f"system_server_{vers}",
                      f"servicemanager_{vers}", f"hwservicemanager_{vers}")
          for cls, perms in (("binder", ["transfer", "call"]),
                             ("fd", ["use"]))),
        # 自研 adbd 的 shell/sync 服务子进程继承 pioneer 域（daemon 是 root 常驻服务，
        # 子进程不降权）。system_server 需把命令输出写回子进程的 stdio 管道
        # （fifo_file）、并读写它的 socketpair（unix_stream_socket），缺任一条时
        # `dumpsys`/`settings`/`pm` 一律 FAILED_TRANSACTION（note13 实测 avc：
        # comm="dumpsys" tclass=fifo_file denied { write }；
        # comm="cmd" tclass=unix_stream_socket denied { read write }）。
        _allow(f"system_server_{vers}", "pioneer", "fifo_file",
               ["read", "write", "getattr", "ioctl"]),
        _allow(f"system_server_{vers}", "pioneer", "unix_stream_socket",
               ["read", "write", "getattr", "ioctl", "getopt", "setopt", "shutdown"]),
    ]
    return f"{MARKER}: pioneer minimal declaration (kernel permissive).\n" + "".join(stmts)


_RE_CLASS = re.compile(r"\(class\s+(\S+)\s+\(([^)]*)\)\)")
_RE_COMMON = re.compile(r"\(common\s+(\S+)\s+\(([^)]*)\)\)")
_RE_CLASSCOMMON = re.compile(r"\(classcommon\s+(\S+)\s+(\S+)\)")
_RE_ALLOW = re.compile(r"\(allow\s+(\S+)\s+(\S+)\s+\((\S+)\s+\(([^)]*)\)\)\)")
_RE_TYPE = re.compile(r"\(type\s+(\S+)\)")
_RE_TYPEATTR = re.compile(r"\(typeattribute\s+(\S+)\)")
# allow 家族（同一权限语法）：用于平台兼容清理整行解析
_RE_ALLOW_FAMILY = re.compile(
    r"\((allow|dontaudit|auditallow|neverallow)\s+(\S+)\s+(\S+)\s+\((\S+)\s+\(([^)]*)\)\)\)")
# allowx：权限值可含嵌套括号（(range 0x… 0x…)），贪婪匹配到行尾
_RE_ALLOWX_FULL = re.compile(
    r"\(allowx\s+(\S+)\s+(\S+)\s+\((\S+)\s+(\S+)\s+\((.+)\)\)\)\s*$")
# 内核支持的 xperm 家族（CIL allowx 的 op 名）：6.12.23 avtab.h 仅
# AVTAB_XPERMS_IOCTLFUNCTION/IOCTLDRIVER；nlmsg（netlink 消息类型过滤）是更新
# 内核线特性，本盘内核没有。
KERNEL_XPERM_OPS = {"ioctl"}

_decl_cache = None
_name_cache = None


def _official_plat_text() -> str:
    """提取官方 plat_sepolicy.cil 文本（EROFS system 分区的 /etc/selinux）。

    fsck.erofs --extract --path 每次全量重提（~5MB，亚秒级），避免缓存陈旧；
    提取失败即报错退出（校验数据不允许静默降级）。两个实测坑（2026-10-09）：
      * 目标目录必须不存在（fsck.erofs 自建该目录，预先存在会静默不产出）；
      * path 必须用 system-as-root 的真实路径 /system/etc/selinux —— 镜像内
        /etc 是符号链接，fsck.erofs 沿链接查找会报 "invalid de[0].nameoff /
        failed to lookup"，提取不到。
    """
    if not os.path.isfile(OFFICIAL_PLAT_IMG):
        print(f"[err] 官方 system_a.img 缺失: {OFFICIAL_PLAT_IMG}（先 unpack）")
        sys.exit(1)
    shutil.rmtree(OFFICIAL_PLAT_XDIR, ignore_errors=True)
    parent = os.path.dirname(OFFICIAL_PLAT_XDIR)
    os.makedirs(parent, exist_ok=True)
    res = subprocess.run(
        [erofs_tool("fsck.erofs"), f"--extract={OFFICIAL_PLAT_XDIR}",
         "--path=/system/etc/selinux", OFFICIAL_PLAT_IMG],
        capture_output=True, text=True)
    cil = os.path.join(OFFICIAL_PLAT_XDIR, "plat_sepolicy.cil")
    if res.returncode != 0 or not os.path.isfile(cil):
        print(f"[err] 官方 plat 提取失败: {res.stdout.strip()} {res.stderr.strip()}")
        sys.exit(1)
    with open(cil, encoding="utf-8", errors="replace") as f:
        return f.read()


def plat_class_perms() -> dict:
    """解析官方 plat_sepolicy.cil 的 (class)/(common)/(classcommon) 声明，
    返回 {class: 合法权限集}；读不到即报错退出（校验数据不允许静默降级）。"""
    global _decl_cache
    if _decl_cache is not None:
        return _decl_cache
    blob = _official_plat_text()
    classes, commons, links = {}, {}, {}
    for raw in blob.splitlines():
        s = raw.strip()
        m = _RE_CLASS.match(s)
        if m:
            classes[m[1]] = set(m[2].split())
            continue
        m = _RE_COMMON.match(s)
        if m:
            commons[m[1]] = set(m[2].split())
            continue
        m = _RE_CLASSCOMMON.match(s)
        if m:
            links[m[1]] = m[2]
    _decl_cache = {c: own | commons.get(links.get(c), set())
                   for c, own in classes.items()}
    return _decl_cache


def _read_cil(img, path) -> str:
    blob = cat(img, path)
    if not blob:
        print(f"[err] 读不到策略声明: {path} @ {img}")
        sys.exit(1)
    return blob.decode("utf-8", "replace")


def _cil_names(text: str) -> set:
    """(type X) / (typeattribute X) 声明名集合。
    正则不误伤: (typetransition/typeattributeset 无 type 后空白，不匹配)。"""
    return {m[1] for m in _RE_TYPE.finditer(text)} | \
           {m[1] for m in _RE_TYPEATTR.finditer(text)}


def plat_name_set() -> set:
    """官方 plat cil 的 type/typeattribute 名全集。版本化影子的 typeattribute 声明
    在 plat_pub_versioned.cil（vendor 侧文件），由调用方经 _cil_names 解析后并入。"""
    global _name_cache
    if _name_cache is None:
        _name_cache = _cil_names(_official_plat_text())
    return _name_cache


# 类型名可含连字符（gold 属性类型，如 vendor_ctl_port-bridge_prop）：字符集漏掉
# '-' 会把片段内自声明的连字符类型当未知名字误报。
_TYPE_DECL_RE = re.compile(r"^\(type\s+([a-z0-9_\- ]+?)\s*\)\s*$", re.M)


def _declared_types(cil: str) -> set:
    """收集 CIL 片段里 (type X) 自声明的类型名。

    这些类型随本片段一起写进 vendor_sepolicy.cil，编译期必定存在，故 validate_cil
    对它们跳过「名字全集」存在性检查（此前用硬编码白名单，新增类型会误报）。
    """
    names = set()
    for group in _TYPE_DECL_RE.findall(cil):
        names.update(group.split())
    return names


def sanitize_vendor_cil(text: str, table: dict) -> str:
    """按官方 plat 类表清理 vendor CIL（运行态平台兼容，构建期一次完成）。

    背景：运行态 init 内嵌 secilc 以**官方 plat 策略**声明 class/权限，而 lineage
    vendor 策略是随更新内核线（6.12.81-line）生成的，含官方类表没有的权限；任何一条
    解析失败即 secilc 报 "Failed to resolve permission" → InitFatalReboot。本盘内核
    6.12.23 classmap 同样没有这些权限（nlmsg 于 netlink_route_socket、firmware_load
    于 system），内核永远不会对其判定，剔除/裁剪语义等价。

    规则：
      * allow 家族（allow/dontaudit/auditallow/neverallow）：class 不在官方类表 →
        整条删除；部分权限不在（`-perm` 否定式不参与判定）→ 仅剔除缺失权限，全缺则
        整条删除；
      * allowx：op 不在内核 xperm 家族（KERNEL_XPERM_OPS，nlmsg 即此类）或 class
        未知 → 整条删除；
      * 无法整行解析的语句原样保留（不做过删）。

    输出与输入行序一致，未改动行逐字节保留。
    """
    out, dropped, pruned = [], [], []
    for ln in text.splitlines(keepends=True):
        s = ln.strip()
        if s.startswith(("(allow ", "(dontaudit ", "(auditallow ", "(neverallow ")):
            m = _RE_ALLOW_FAMILY.fullmatch(s)
            if m is None:
                out.append(ln)
                continue
            kind, src, tgt, cls, perm_s = m.groups()
            perms = perm_s.split()
            if cls not in table:
                dropped.append(f"{kind} {src} {tgt} (class {cls} 未知)")
                continue
            miss = [p for p in perms if not p.startswith("-") and p not in table[cls]]
            if not miss:
                out.append(ln)
                continue
            keep = [p for p in perms if p.startswith("-") or p in table[cls]]
            if not keep:
                dropped.append(f"{kind} {src} {tgt} ({cls}: {' '.join(miss)})")
                continue
            pruned.append(f"{kind} {src} {tgt} ({cls}: {' '.join(miss)})")
            eol = ln[len(ln.rstrip("\r\n")):]
            out.append(f"({kind} {src} {tgt} ({cls} ({' '.join(keep)})))" + eol)
            continue
        if s.startswith("(allowx "):
            m = _RE_ALLOWX_FULL.fullmatch(s)
            if m is None:
                out.append(ln)
                continue
            src, tgt, op, cls, _vals = m.groups()
            if op not in KERNEL_XPERM_OPS or cls not in table:
                dropped.append(f"allowx {src} {tgt} (op {op} class {cls})")
                continue
            out.append(ln)
            continue
        out.append(ln)
    for d in dropped:
        print(f"[..] 平台兼容剔除: {d}")
    for p in pruned:
        print(f"[..] 平台兼容裁剪: {p}")
    if dropped or pruned:
        print(f"[ok] vendor CIL 平台兼容清理：剔除 {len(dropped)} 条、"
              f"裁剪 {len(pruned)} 条（官方 plat 类表）")
    else:
        print("[ok] vendor CIL 平台兼容检查通过（无语句需清理）")
    return "".join(out)


def validate_cil(cil: str, name_universe: set) -> None:
    """逐条 allow 校验: class/perm 合法性（按平台声明）、源/目标名存在性
    （name_universe = plat cil + plat_pub_versioned.cil + vendor 基座 cil 的
    声明全集，精确名匹配）与冗余（完全重复语句、同一 (src,tgt,class) 拆分授权）。
    违规即报错退出。"""
    valid = plat_class_perms()
    names = plat_name_set() | set(name_universe)
    # 本段自有类型/标签（片段内 (type X) 声明，如 pioneer/gold 属性类型）
    # + CIL 关键字目标，不做存在性检查
    skip = {"self"} | _declared_types(cil)
    seen_stmt, seen_pair, errs, n = set(), set(), [], 0
    for ln in cil.splitlines():
        s = ln.strip()
        if not s.startswith("(allow "):
            continue
        n += 1
        m = _RE_ALLOW.fullmatch(s)
        if not m:
            errs.append(f"语句格式不可解析: {s}")
            continue
        src, tgt, cls, perms = m[1], m[2], m[3], m[4].split()
        if s in seen_stmt:
            errs.append(f"重复语句: {s}")
            continue
        seen_stmt.add(s)
        if cls not in valid:
            errs.append(f"未知 class {cls}: {s}")
            continue
        bad = sorted(set(perms) - valid[cls])
        if bad:
            errs.append(f"非法权限 {cls}:{bad} -> {tgt}")
        for name in (src, tgt):
            if name in skip or name in names:
                continue
            errs.append(f"未知 type/attribute {name}: {s}")
        key = (src, tgt, cls)
        if key in seen_pair:
            errs.append(f"拆分冗余授权 ({src} {tgt} {cls}): {s}")
        seen_pair.add(key)
    if errs:
        for e in errs:
            print(f"[err] sepolicy 校验: {e}")
        sys.exit(1)
    print(f"[ok] CIL 校验通过（{n} 条 allow，平台 {len(valid)} class、"
          f"{len(names)} type/attr 声明）")


def coredomain_vendor_prop_read_cil(vers: str) -> str:
    """coredomain 服务直读 vendor_default_prop 属性区（引导修复段）。

    note13 实测：lmkd/keystore2 读取 /dev/__properties__/u:object_r:vendor_default_prop:s0
    被拒（avc denied { read } tclass=file）后 20ms 内 SIGABRT —— keystore2 起不来导致
    vold 等 IKeystoreService 卡死、boot 永不完成。授权直读该属性区文件即可让服务正常
    启动。风险：若 plat 对 coredomain→vendor_default_prop 有 neverallow，本段会导致
    init 编译策略失败（引导极早失败，可立即发现回退）。
    """
    return (
        f"{VENDOR_PROP_READ_MARKER}\n"
        + _allow(f"lmkd_{vers}", f"vendor_default_prop_{vers}", "file",
                 ["read", "getattr", "map", "open"])
        + _allow(f"keystore_{vers}", f"vendor_default_prop_{vers}", "file",
                 ["read", "getattr", "map", "open"])
        # 诊断：crash_dump 默认被拒 ptrace/dac_override（note13 实测 avc denied），
        # tombstone 永远写不出来，崩溃根因不可见。授权后 abort 栈可落盘定位。
        + _allow(f"crash_dump_{vers}", "domain", "process", ["ptrace"])
        + _allow(f"crash_dump_{vers}", "self", "capability", ["dac_override"])
    )


def vibrator_hal_cil(vers: str) -> str:
    """vibrator HAL 段：hal_vibrator_default 注册 IVibrator/IVibratorManager default。

    lineage 基座无 vibrator HAL，自研服务以 hal_vibrator_default 域注册
    android.hardware.vibrator.IVibrator/default 与 IVibratorManager/default，
    需 service_manager add/find（plat 公共类型用版本化影子 hal_vibrator_service_{vers}）。
    vibrator HAL 不使用 FMQ，故无 ashmem 授权。
    """
    return (
        f"{VIBRATOR_MARKER}\n"
        + _allow("hal_vibrator_default", f"hal_vibrator_service_{vers}",
                 "service_manager", ["add", "find"])
        + _allow(f"system_server_{vers}", f"hal_vibrator_service_{vers}",
                 "service_manager", ["find"])
    )


def ir_hal_cil(vers: str) -> str:
    """IR HAL 段：hal_ir_default 注册 android.hardware.ir.IConsumerIr/default。"""
    return (
        f"{IR_MARKER}\n"
        + _allow("hal_ir_default", f"hal_ir_service_{vers}",
                 "service_manager", ["add", "find"])
        + _allow(f"system_server_{vers}", f"hal_ir_service_{vers}",
                 "service_manager", ["find"])
    )


def audio_hal_cil(vers: str) -> str:
    """audio AIDL HAL 注册段（承自 note13-5G-15）：service_manager add/find。

    自研服务以 hal_audio_default 域注册 core.IModule/{default,primary,r_submix} 与
    IConfig/default、effect.IFactory/default。gold 基座对 hal_audio_default 零
    hal_audio_service 授权（判据见 AUDIO_MARKER 注释），缺 add 则注册被拒、框架
    按 VINTF 认定存在而死等 -> MIUI AudioService 在 system_server 主线程上同步等
    binder -> Watchdog 71s SIGABRT 重启循环。
    default_android_service_{vers} 那两条是 contexts 映射未生效时的兜底：未映射的
    AIDL 服务名落 default_android_service，注册方需 add、audioserver 需 find，
    少一条就是 getService 返回 null（note13 实例实测 avc denied {find}）。
    """
    return (
        f"{AUDIO_MARKER}: allow audio HAL service add/find\n"
        + _allow("hal_audio_default", f"hal_audio_service_{vers}",
                 "service_manager", ["add", "find"])
        + _allow("hal_audio_default", f"default_android_service_{vers}",
                 "service_manager", ["add"])
        + _allow(f"audioserver_{vers}", f"default_android_service_{vers}",
                 "service_manager", ["find"])
        + _allow(f"audioserver_{vers}", f"hal_audio_service_{vers}",
                 "service_manager", ["find"])
    )


def audio_hal_cil_v2(vers: str) -> str:
    """audio v2 增量段：/dev/ashmem（FMQ）。

    自研 IModule 的 StreamDescriptor 走 AidlMessageQueue，必须 open /dev/ashmem；
    被拒则 fd=-1 -> mmap EBADF -> framework 侧 openOutput 失败（note13 实例 59 实测
    avc denied {open} hal_audio_default ashmem_device chr_file）。
    """
    return (
        f"{AUDIO_V2_MARKER}: allow audio HAL FMQ /dev/ashmem\n"
        + _allow("hal_audio_default", f"ashmem_device_{vers}", "chr_file",
                 "ioctl read write getattr map open".split())
    )


def audio_hal_cil_v3(vers: str) -> str:
    """audio v3 增量段：/dev/snd（QEMU 声卡）。

    ModuleImpl 用原生 ALSA ioctl/write 直连 /dev/snd/pcmC0D0p，把 AudioFlinger 的
    burst 背压锚到真实声卡时钟（否则播放时钟偏快、在线视频倍速快进）；节点类型
    audio_device（客机实测 /dev/snd/* 全为该类型），plat 公共类型按影子引。
    """
    return (
        f"{AUDIO_V3_MARKER}: allow audio HAL /dev/snd (QEMU 声卡)\n"
        + _allow("hal_audio_default", f"audio_device_{vers}", "chr_file",
                 "ioctl read write getattr map open".split())
    )


def frp_pdb_cil(vers: str) -> str:
    """FRP 承载块设备段：system_server（pdb）读写 misc_block_device。

    本盘没有 frp 分区，pdb 经 ro.frp.pst 打开的是软链到 misc(vda3) 的节点
    （类型 misc_block_device），平台策略只允许 system_server 访问 frp_block_device。
    """
    return (
        f"{FRP_MARKER}\n"
        + _allow(f"system_server_{vers}", "misc_block_device", "blk_file",
                 list(FRP_BLK_PERMS))
    )


def perf_policy_cil(vers: str) -> str:
    """perf2 HAL 段（手写最小面，按平台 neverallow 精确构造）。

    官方整段搬运不可行：lineage 侧缺兄弟域、且平台 neverallow 规定
    `(and (domain) (not (vendor_hal_perf_client vendor_hal_perf_server)))` 不得
    add/find vendor_hal_perf2_service —— 即 system_server 必须先被并入
    vendor_hal_perf_client、本 HAL 域并入 vendor_hal_perf_server，才允许注册与查找。
    这里只声明服务启动/注册/查找所需的最小集合。
    """
    return (
        f"{PERF_MARKER}\n"
        # 类型与域
        "(type vendor_hal_perf_default)\n"
        "(type vendor_hal_perf_default_exec)\n"
        "(type vendor_hal_perf2_service)\n"
        "(typeattribute vendor_hal_perf_server)\n"
        "(typeattribute vendor_hal_perf_client)\n"
        "(roletype object_r vendor_hal_perf_default)\n"
        "(roletype object_r vendor_hal_perf_default_exec)\n"
        "(roletype object_r vendor_hal_perf2_service)\n"
        # neverallow 豁免集：本 HAL 域=server，system_server=client
        "(typeattributeset domain (vendor_hal_perf_default))\n"
        "(typeattributeset vendor_hal_perf_server (vendor_hal_perf_default))\n"
        f"(typeattributeset vendor_hal_perf_client (system_server_{vers}))\n"
        # init 启动该服务
        f"(typetransition init_{vers} vendor_hal_perf_default_exec process"
        " vendor_hal_perf_default)\n"
        + _allow(f"init_{vers}", "vendor_hal_perf_default", "process",
                 ["transition", "siginh", "rlimitinh", "noatsecure"])
        + _allow(f"init_{vers}", "vendor_hal_perf_default_exec", "file",
                 ["read", "getattr", "map", "execute", "open"])
        + _allow("vendor_hal_perf_default", "vendor_hal_perf_default_exec",
                 "file", ["read", "getattr", "map", "execute", "open",
                          "entrypoint"])
        # 注册 / 查找（neverallow 豁免已就位）
        + _allow("vendor_hal_perf_default", "vendor_hal_perf2_service",
                 "service_manager", ["add", "find"])
        + _allow(f"system_server_{vers}", "vendor_hal_perf2_service",
                 "service_manager", ["find"])
        # 与 servicemanager 通 binder（注册路径）
        + _allow("vendor_hal_perf_default", f"servicemanager_{vers}", "binder",
                 ["call", "transfer"])
        + _allow(f"servicemanager_{vers}", "vendor_hal_perf_default", "binder",
                 ["transfer"])
        + _allow("vendor_hal_perf_default", f"servicemanager_{vers}", "fd",
                 ["use"])
    )


def keymint_hal_cil(vers: str) -> str:
    """Remote KeyMint 段（v1）：/dev/hvc3 设备类型 + hal_keymint_default 授权。

    keymaster_device 为新增设备类型（/dev/hvc3 的 file_contexts 标签目标）：
    dev_type 归属供 ueventd 创建节点时打标；官方 plat 与 lineage vendor 均无该
    类型，故自声明（两处均不声明 -> 运行时 secilc 无重复声明冲突，实测）。
    serialno_prop_{vers} 读：attestation 取 serial number（基座 vendor cil 已含
    同名授权，此处保留为幂等冗余）。kmsg 写：rc 插 `file /dev/kmsg w` 后
    LOG(FATAL)/CHECK 取证日志可落内核日志。
    """
    return (
        f"{KEYMINT_MARKER}: remote keymint hvc3 chardev + serialno/kmsg\n"
        "(type keymaster_device)\n"
        "(roletype object_r keymaster_device)\n"
        "(typeattributeset dev_type (keymaster_device))\n"
        + _allow("hal_keymint_default", "keymaster_device", "chr_file",
                 "getattr ioctl open read write".split())
        + _allow("hal_keymint_default", f"serialno_prop_{vers}", "file",
                 "read getattr map open".split())
        # 显式填充版本化影子（官方 mapping/202504.cil 亦有同名填充，union 无副作用）
        + f"(typeattributeset kmsg_device_{vers} (kmsg_device))\n"
        + _allow("hal_keymint_default", f"kmsg_device_{vers}", "chr_file",
                 ["write"])
    )


def keymint_hal_cil_v2(vers: str) -> str:
    """Remote KeyMint 增量段（v2）：wait stat 权限 + kmsg_debug(tmpfs) 写。

    1) init.virt.rc 门控 `wait /dev/hvc3 30` 由 vendor_init 域执行（vendor rc），
       stat 该节点缺 getattr 会连续 avc denied 直到 wait 超时失败（note13 实测
       812 次）——授权消除拖慢与噪音。
    2) keymint 的 LOG 走 /dev/kmsg_debug（目标标签实为 tmpfs）：缺 write 则
       LOG(FATAL)/CHECK 取证日志全丢。
    """
    return (
        f"{KEYMINT_MARKER2}: hvc3 wait stat + kmsg_debug(tmpfs) write\n"
        + _allow(f"vendor_init_{vers}", "keymaster_device", "chr_file",
                 ["getattr"])
        + f"(typeattributeset tmpfs_{vers} (tmpfs))\n"
        + _allow("hal_keymint_default", f"tmpfs_{vers}", "chr_file",
                 ["write"])
    )


def keymint_hal_cil_v3(vers: str) -> str:
    """Remote KeyMint 增量段（v3）：system_prop 属性区读（实例 15 实测补）。

    guest remote keymint 的 Initialize() 起手两件事是 Configure(os_version,
    os_patchlevel)：`keymaster_configuration.cpp` 读 ro.build.version.release 与
    ro.build.version.security_patch，本盘两者都落 plat 的 system_prop 区
    （实测 avc：`denied { read } ... name="u:object_r:system_prop:s0"
    scontext=u:r:hal_keymint_default:s0`，早期 3 次）。该域来自 lineage 基座
    （其软件 keymint 不读 plat 属性，故基座只授 serialno_prop 与
    vendor_security_patch_level_prop），缺 system_prop 读时 GetProperty 返回空串
    → GetOsVersion/GetOsPatchlevel 退化为 0：host secure_env 仍接受 Configure
    （不崩、服务照常注册），但密钥派生与 attestation 的 osVersion/
    osPatchlevel 字段失真，故必须补。

    system_prop_{vers} 影子由官方 mapping/202504.cil 已填充
    （`(typeattributeset system_prop_202504 (system_prop))`），perm 集与基座
    serialno_prop 授权同形（getattr map open read）；权限落在类型上即覆盖
    /dev/__properties__ 内该上下文区域文件。
    property_socket 写不在本段：实例 14 那条 sock_file write 拒绝是崩溃路径产物
    （HAL 源码内无 SetProperty），实例 15/16 正常运行时不复现。

    实测（实例 16，2026-10-10）：hal_keymint_default 作源域的 avc denied 归零，
    四个 AIDL 实例照常注册，keystore2 仍认 KeyMint v400 TRUSTED_ENVIRONMENT。
    """
    return (
        f"{KEYMINT_MARKER3}: system_prop read (os_version/os_patchlevel)\n"
        + f"(typeattributeset system_prop_{vers} (system_prop))\n"
        + _allow("hal_keymint_default", f"system_prop_{vers}", "file",
                 "read getattr map open".split())
    )


def bootctl_persist_cil(vers: str) -> str:
    """grub bootctrl 段：hal_bootctl_default 读写 /mnt/vendor/persist 的 grubenv。

    实例 22（2026-10-10，gold 基座）实测：
        GrubBootControlUtil: Check failed: mFd.ok()
            Failed to open or create /mnt/vendor/persist/grubenv_abootctrl
        init: Service 'vendor.boot-default' (pid 368) received signal 6
    随后 vold 永久拿不到 android.hardware.boot.IBootControl/default，引导停在
    /data 之前。该服务起手就 open(O_CREAT|O_RDWR) 这个文件，打不开即 CHECK 失败
    自杀，没有回退路径。

    dir 权限集含 add_name/remove_name/rename/unlink：grub 的 save_env 走「写临时
    文件再替换」，只授读写会留下半截 env。file 权限集含 append/map/unlink/rename。
    目标类型 mnt_vendor_file 是挂载点与 vfat context= 共用的标签（见 build-patch.py
    VENDOR_SKELETON_DIRS 与 patch/etc/fstab.qcom 的注释）。

    还有一条 init 侧的 filesystem relabelto + relabelfrom：vfat 挂 context= 时内核
    selinux_set_mnt_opts 要求挂载域对**该 context 的类型**持 filesystem:relabelto，
    对**被替换掉的旧标签**（vfat 无 xattr，旧标签 = 挂载点自身标签）持 relabelfrom，
    官方 plat 只把 relabelto 授给 contextmount_type（本盘 = {oemfs app_fusefs}），
    mnt_vendor_file 不在其中。实例 23/26（2026-10-10，gold 基座）逐条实测：
        实例 23（只改了挂载点标签、未授权）：
            avc: denied { relabelto } for comm="init" scontext=u:r:init:s0
                tcontext=u:object_r:mnt_vendor_file:s0 tclass=filesystem permissive=0
        实例 26（补了 relabelto 之后）：
            avc: denied { relabelfrom } for comm="init" scontext=u:r:init:s0
                tcontext=u:object_r:mnt_vendor_file:s0 tclass=filesystem permissive=0
    两次都表现为同一句
        __mount(source=/dev/block/by-name/persist,target=/mnt/vendor/persist,
            type=vfat)=-1,during time: 0 ms: Permission denied
    （挂载失败后 fs_mgr 转去跑 fsck_msdos，又撞上 fsck 域无 vd_device:blk_file 的
    旧账——实例 13 lineage 基座里同一批 fsck 拒绝只发生在 firmware 分区上，persist
    首挂即成功、根本走不到 fsck，故不单独授权，补完 relabel* 即自然消失。）
    不触 neverallow：plat 那条 `neverallow all {fs_type -contextmount_type}:filesystem
    relabelto` 限定在 fs_type（文件系统类型 vfat/ext4/...），mnt_vendor_file 是文件
    类型、不在 fs_type 里；另两条含 relabelfrom/relabelto 的 neverallow 分别把源域限定
    为 domain∖{apexd init kernel recovery update_engine vold zygote ...}（init 已被排除）
    与 appdomain，均不涉及。lineage 基座一并适用（其 persist context= 已随之改为
    mnt_vendor_file，见 patch/etc/fstab.qcom 注释）。

    最后是 context= 挂载自身要求的 self associate + init 侧的完整 filesystem 权限集。
    实例 27（2026-10-10，v5 内核，relabelto/relabelfrom 均已授）实测：
        avc: denied { associate } for comm="init" scontext=u:object_r:mnt_vendor_file:s0
            tcontext=u:object_r:mnt_vendor_file:s0 tclass=filesystem permissive=0
    relabel* 只解决「把挂载点旧标签换成新 context」，换完之后内核还要校验
    「该文件类型能否与此文件系统类型配对」（selinux_sb_finish_set_mnt_opts），缺这条
    一样 Permission denied。官方策略里所有 context= 挂载的文件类型都带这一条，写法统一
    为 (allow <type> self (filesystem (associate)))：firmware_file / bt_firmware_file /
    postinstall_file / vendor_qmcs_file / vendor_soccp_file / vendor_spunvm_file，以及
    (allow fs_type self ...) 的批量版。

    实例 28（补完 self associate 之后）实测下一条：
        avc: denied { mount } for comm="init" name="/" dev="vda4" ino=1
            scontext=u:r:init:s0 tcontext=u:object_r:mnt_vendor_file:s0 tclass=filesystem
    官方 plat 把这一整组只授给 fs_type（base_typeattr_755）：
        (allow init base_typeattr_755 (filesystem (mount remount unmount getattr
            relabelfrom associate quotamod quotaget watch)))
    mnt_vendor_file 是文件类型、不在 fs_type 里，所以整套都缺。这里按官方同一条照抄
    （外加 contextmount_type 才有的 relabelto），不再逐条试错——每次试错是一整轮
    「重编 vendor→patch→super→pack + 起实例」，成本远高于多授几条同域同类的权限。
    """
    return (
        f"{BOOTCTL_PERSIST_MARKER}: hal_bootctl_default rw on mnt_vendor_file\n"
        + _allow("hal_bootctl_default", f"mnt_vendor_file_{vers}", "dir",
                 "create open read write getattr setattr lock search add_name "
                 "remove_name rename unlink ioctl watch watch_reads".split())
        + _allow("hal_bootctl_default", f"mnt_vendor_file_{vers}", "file",
                 "create open read write getattr setattr lock append map unlink "
                 "rename ioctl watch watch_reads".split())
        + _allow(f"init_{vers}", f"mnt_vendor_file_{vers}", "filesystem",
                 "mount remount unmount getattr relabelfrom relabelto associate "
                 "quotamod quotaget watch".split())
        + _allow(f"mnt_vendor_file_{vers}", "self", "filesystem", ["associate"])
    )


def vd_device_cil(vers: str) -> str:
    """virtio-blk 段：把官方对 userdata/metadata_block_device 的授权平移到 vd_device。

    源域取「引导期真正会打开云机磁盘」的那几个（判据见 VD_DEVICE_MARKER 注释）：
      * init —— fs_mgr 挂 /data(vdb)、/metadata(vda5)、misc(vda3)；
      * vold —— 元数据加密/检查点，直接读写 vdb；
      * fsck / e2fs —— /data 带 check、/metadata 首启要 mke2fs；
      * system_server —— PersistentDataBlock 经 ro.frp.pst 打开的软链最终落在 vda3
        （gold 基座里该节点是 vd_device，不是 lineage 基座的 misc_block_device，
        故 FRP_MARKER 那一条在 gold 上不生效，这里补同一权限）；
      * hal_bootctl_default —— 官方 QTI boot 服务反复 stat by-name/misc(vda3)；只读，
        不给写，避免它真去改槽位标记（A/B 状态由 grub 的 grubenv_abootctrl 维护）。
    """
    return (
        f"{VD_DEVICE_MARKER}: virtio-blk (vda/vdb/vdc) 访问授权\n"
        + "".join(
            _allow(f"{dom}_{vers}", f"vd_device_{vers}", "blk_file",
                   list(VD_BLK_PERMS))
            for dom in ("init", "vold", "fsck", "e2fs", "system_server")
        )
        + _allow("hal_bootctl_default", f"vd_device_{vers}", "blk_file",
                 ["ioctl", "read", "getattr", "open"])
    )


def graphics_cil(vers: str) -> str:
    """图形链段：drm_hwcomposer / minigbm gralloc 在 gold 策略下缺的 DRM 与属性授权。

    源域与权限集判据见 GRAPHICS_MARKER 注释。hal_graphics_{allocator,composer}_default
    在 gold 基座是 **vendor 自有**类型（mapping/202504.cil 里没有它们的
    _{vers} 影子，plat_sepolicy.cil 全文零命中），故不带版本后缀；surfaceflinger /
    graphics_device / gpu_device / system_prop 是 plat 类型，必须走 _{vers} 影子。
    两个 DRM 类型都授：本文件现在由 dri_label_rewrite 把 gold 的 /dev/dri/* 改写成
    AOSP/lineage 的 gpu_device 口径（判据见 DRI_LABEL_FROM 注释），但 hal 域仍可能被
    其它 gold 表项带进 graphics_device，两条都留着就不必为标签口径再跑一轮。
    """
    return (
        f"{GRAPHICS_MARKER}: drm_hwcomposer / minigbm gralloc 授权\n"
        + "".join(
            _allow(dom, f"graphics_device_{vers}", "chr_file", list(DRM_CHR_PERMS))
            + _allow(dom, f"gpu_device_{vers}", "chr_file", list(DRM_CHR_PERMS))
            + _allow(dom, f"system_prop_{vers}", "file", list(PROP_READ_PERMS))
            for dom in ("hal_graphics_allocator_default",
                        "hal_graphics_composer_default")
        )
        + _allow(f"surfaceflinger_{vers}", f"graphics_device_{vers}", "chr_file",
                 list(DRM_CHR_PERMS))
        + _allow(f"surfaceflinger_{vers}", f"gpu_device_{vers}", "chr_file",
                 list(DRM_CHR_PERMS))
        # mesa llvmpipe JIT 的可执行页（判据见 RENDER_EXECMEM 注释）
        + _allow(f"surfaceflinger_{vers}", "self", "process", ["execmem"])
        + _allow(f"bootanim_{vers}", "self", "process", ["execmem"])
        # virtio-gpu 的 sysfs 子树：mesa/libdrm 枚举 DRM 设备要读它（判据见
        # DRM_SYSFS_GPU_GENFS 注释）
        + DRM_SYSFS_GPU_GENFS
        + "".join(
            _allow(dom, f"sysfs_gpu_{vers}", "dir", list(SYSFS_GPU_DIR_PERMS))
            + _allow(dom, f"sysfs_gpu_{vers}", "file", list(SYSFS_GPU_FILE_PERMS))
            + _allow(dom, f"sysfs_gpu_{vers}", "lnk_file", list(SYSFS_GPU_FILE_PERMS))
            for dom in (list(DRM_SYSFS_GPU_DOMAINS_UNVERSIONED)
                        + [f"{d}_{vers}" for d in DRM_SYSFS_GPU_DOMAINS_PLAT])
        )
    )


def hal_vsc_lines() -> list:
    """全部自研/收编 HAL 需要追加的 vendor_service_contexts 映射行。"""
    return (list(VIBRATOR_VSC_LINES) + list(IR_VSC_LINES) + list(AUDIO_VSC_LINES)
            + list(PERF_VSC_LINES) + list(MAPPER_VSC_LINES))


def hal_vsc_missing(vsc: bytes) -> list:
    """工作镜像 vendor_service_contexts 里尚缺的 HAL 映射行。"""
    text = vsc.decode("utf-8", "replace")
    return [line for line in hal_vsc_lines() if line.split()[0] not in text]


def hal_cil_chain(merged: str, vers: str, names=None) -> str:
    """按固定顺序补齐缺失的 CDH CIL 段
    （pioneer + coredomain 属性区直读 + 官方 gold vendor 属性类型段）。

    各 *_patch_commands 都基于工作镜像里的「原始」vendor_sepolicy.cil 拼接（debugfs
    命令此刻尚未执行），而命令序列里只有最后一次 write 生效 —— 所以最后调用的那个
    函数必须把所有前序段一并写出，否则会把它们覆盖丢失。新增 HAL 段时只需在这里加
    一行，再由该 HAL 的 *_patch_commands 调用本函数即可（note13 机制）。
    names = 本策略已声明的类型/属性名全集（供 gold prop types 段过滤源域不存在的授权）。
    """
    cil = ""
    if MARKER not in merged:
        cil += pioneer_cil(vers)
    if VENDOR_PROP_READ_MARKER not in merged:
        cil += coredomain_vendor_prop_read_cil(vers)
    if gold_props.MARKER not in merged:
        cil += gold_props.prop_types_cil(vers, names or set())
    if VIBRATOR_MARKER not in merged:
        cil += vibrator_hal_cil(vers)
    if IR_MARKER not in merged:
        cil += ir_hal_cil(vers)
    if AUDIO_MARKER not in merged:
        cil += audio_hal_cil(vers)
    if AUDIO_V2_MARKER not in merged:
        cil += audio_hal_cil_v2(vers)
    if AUDIO_V3_MARKER not in merged:
        cil += audio_hal_cil_v3(vers)
    if FRP_MARKER not in merged:
        cil += frp_pdb_cil(vers)
    if PERF_MARKER not in merged:
        cil += perf_policy_cil(vers)
    if KEYMINT_MARKER not in merged:
        cil += keymint_hal_cil(vers)
    if KEYMINT_MARKER2 not in merged:
        cil += keymint_hal_cil_v2(vers)
    if KEYMINT_MARKER3 not in merged:
        cil += keymint_hal_cil_v3(vers)
    if BOOTCTL_PERSIST_MARKER not in merged:
        cil += bootctl_persist_cil(vers)
    if VD_DEVICE_MARKER not in merged:
        cil += vd_device_cil(vers)
    if GRAPHICS_MARKER not in merged:
        cil += graphics_cil(vers)
    return cil


def dri_label_rewrite(vfc: bytes):
    """把 vendor_file_contexts 里 /dev/dri 的标签口径改成 lineage 的 gpu_device。

    两件事：1) 就地改写三条节点行的 graphics_device（不追加同名行）；
    2) 补 /dev/dri **目录**字面量（gold 没有该行，目录留 device -> app/SF 列不了目录，
    mesa 打不开 DRM）。判据与实测见 DRI_LABEL_FROM / DRI_DIR_LABEL_LINE 注释。
    返回 (新字节, 命中行数)。
    """
    out = []
    hit = 0
    has_dir = False
    for line in vfc.split(b"\n"):
        if line.startswith(b"/dev/dri/") and DRI_LABEL_FROM in line:
            line = line.replace(DRI_LABEL_FROM, DRI_LABEL_TO)
            hit += 1
        if line.split(b"\t")[0].split(b" ")[0] == b"/dev/dri":
            has_dir = True
        out.append(line)
    if not has_dir:
        out.append(DRI_DIR_LABEL_LINE)
        hit += 1
    return b"\n".join(out), hit


def patch_commands(work_img, tmp_dir=None) -> list:
    """补齐 vendor_sepolicy.cil 的各 CDH 段，返回 debugfs 命令清单。

    幂等：基座副本每次从 lineage vendor_a.img 重新拷贝，正常不会已含标记；
    若已含（重复构建同一 work.img）则跳过不重复追加。
    """
    vers = cat(work_img, VERS_FILE)
    if not vers:
        print(f"[err] 读不到 {VERS_FILE}")
        sys.exit(1)
    vers = vers.decode("ascii", "ignore").strip()

    orig = cat(work_img, POLICY_FILE)
    if orig is None:
        print(f"[err] 读不到 {POLICY_FILE}")
        sys.exit(1)
    orig_text = orig.decode("utf-8", "replace")
    # 运行态平台兼容清理（官方 plat 类表）：secilc 以官方 plat 解析权限，
    # lineage 策略里官方类表没有的权限语句必须先剔除，否则开机 InitFatalReboot。
    merged = sanitize_vendor_cil(orig_text, plat_class_perms())
    # 名字全集: vendor 基座自有类型（mediacodec 等，未版本化）
    #          + plat_pub_versioned.cil（plat 公共类型的版本化 typeattribute 影子）
    #          + 官方 plat（plat_name_set 内部缓存）
    name_universe = _cil_names(merged) | \
        _cil_names(_read_cil(work_img, PUB_VER_FILE))
    cil = hal_cil_chain(merged, vers, name_universe)
    tmp_dir = tmp_dir or os.path.join(TMP_DIR, "vendor")
    os.makedirs(tmp_dir, exist_ok=True)
    cmds = []
    if cil or merged != orig_text:
        validate_cil(cil, name_universe)
        tmp = os.path.join(tmp_dir, "vendor_sepolicy.cil")
        with open(tmp, "wb") as f:
            f.write(merged.encode("utf-8") + cil.encode("utf-8"))
        print(f"[ok] vendor_sepolicy.cil 追加 CDH 段（plat vers {vers}，"
              f"+{len(cil)} B）")
        cmds += [
            f"rm {POLICY_FILE}",
            f"write {tmp} {POLICY_FILE}",
            f"ea_set {POLICY_FILE} security.selinux {POLICY_LABEL}",
        ]
    else:
        print("[ok] vendor_sepolicy.cil 各段齐全，跳过")

    # 自研 HAL 的 vendor_service_contexts 映射：缺了服务名会落到 default 类型，
    # 服务 add/find 被拒、框架找不到 HAL（vibrator 即因此卡死 SystemServer 主线程）。
    # 与 CIL 段同为追加工件，命令序列里保持在后。
    vsc = cat(work_img, VSC)
    if vsc is None:
        print(f"[err] 读不到 {VSC}")
        sys.exit(1)
    missing = hal_vsc_missing(vsc)
    if not missing:
        print("[ok] vendor_service_contexts 已含自研 HAL 映射，跳过")
    else:
        blob = ("\n# cdh popsicle-16: AIDL HAL services\n"
                + "\n".join(missing) + "\n").encode("utf-8")
        tmp = os.path.join(tmp_dir, "vendor_service_contexts")
        with open(tmp, "wb") as f:
            f.write(vsc + blob)
        print(f"[ok] vendor_service_contexts 追加 {len(missing)} 条 HAL 映射")
        cmds += [
            f"rm {VSC}",
            f"write {tmp} {VSC}",
            f"ea_set {VSC} security.selinux {VSC_LABEL}",
        ]

    # /dev/hvc3 设备标签（Remote KeyMint 通道）：基座 vendor_file_contexts 的正则
    # `/dev/hvc[0-9]+ -> console_device` 会命中 hvc3；追加字面量行（libselinux
    # 按「最长非元字符 stem」优先，字面量胜正则）让 ueventd 以 keymaster_device
    # 打标。与 CIL/VSC 同为追加工件，命令序列里保持在后。
    vfc = cat(work_img, VFC)
    if vfc is None:
        print(f"[err] 读不到 {VFC}")
        sys.exit(1)
    vfc, dri_hit = dri_label_rewrite(vfc)
    if dri_hit:
        print(f"[ok] vendor_file_contexts {dri_hit} 条 /dev/dri(含目录) -> gpu_device")
    fc_blob = b""
    if b"u:object_r:keymaster_device:s0" in vfc:
        print("[ok] vendor_file_contexts 已含 keymaster_device 标签，跳过")
    else:
        fc_blob += ("\n" + "\n".join(KEYMINT_FC_LINES) + "\n").encode("utf-8")
        print("[ok] vendor_file_contexts 追加 /dev/hvc3 -> keymaster_device")
    if GRAPHICS_FC_MARKER.encode("utf-8") in vfc:
        print("[ok] vendor_file_contexts 已含 mesa SP-HAL 标签，跳过")
    else:
        fc_blob += ("\n" + "\n".join(GRAPHICS_FC_LINES) + "\n").encode("utf-8")
        print(f"[ok] vendor_file_contexts 追加 {len(GRAPHICS_FC_LINES) - 1} 条 "
              "same_process_hal_file 标签")
    if fc_blob or dri_hit:
        tmp = os.path.join(tmp_dir, "vendor_file_contexts")
        with open(tmp, "wb") as f:
            f.write(vfc + fc_blob)
        cmds += [
            f"rm {VFC}",
            f"write {tmp} {VFC}",
            f"ea_set {VFC} security.selinux {VFC_LABEL}",
        ]
    return cmds
