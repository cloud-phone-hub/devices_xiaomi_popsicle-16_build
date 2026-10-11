"""popsicle-16 gold vendor 基座：受控覆盖清单（单一数据源）+ 官方件读取/标号/规则生成。

事实前提（决定了本模块的形态）
------------------------------
vda 的 super = 官方 super 字节直拷（`build/scripts/pack_super.py` 零改动），所以
guest 里 `/vendor` 挂载点**本身就是官方 vendor_a.img（erofs）**。此前云机把
`/vendor/{etc,bin,lib,lib64}` 整目录重定向到 lineage 基座，等于整棵厂商 vendor 被
顶替；本模块把口径反过来：**厂商 vendor 为底，改动只允许是清单内的受控覆盖**。

清单之外的原则：能被"官方原字节"回答的路径一律不接管。于是
  - 目录枚举型内容只能整目录接管（apexd 扫 `/vendor/apex`、libvintf 扫
    `/vendor/etc/vintf/manifest`、InputReader 扫 `/vendor/usr/{keylayout,idc}`、
    kmod 走 `/vendor/lib/modules`）-> `GOLD_DIRS`/`CDH_DIRS`；其中官方有的
    （`GOLD_DIRS`）内容 = 官方子树 verbatim + CDH 增删改；
  - 其余按**单文件**接管 -> `FILE_OVERRIDES`；同目录其它文件仍是官方原件，
    因此目录枚举也保持官方口径；
  - `/build.prop`、`/ueventd.rc` 这类"官方字节 + 流水线改写"的路径，seed 阶段先落
    **官方字节**，再由既有 debugfs 流水线在其上改（props/sepolicy/ueventd 读到的
    就是官方内容）——改动是"官方件 + 受控增量"，不是 lineage 版本。

字节来源三档（seed 的三种动作）
  官方 verbatim   官方 erofs 直接读（`dump.erofs --cat` / `fsck.erofs --extract`）
  lineage 搬运    功能必需但官方没有的 lineage 内容（HAL APEX、图形后端字节）
  CDH 自产        两棵注入树（`build/vendor/patch` 与 `out/dist/vendor`）

内核侧对应物：重定向表由本清单**生成**（`emit_rules_header()` -> 内核
`fs/cdh_vendor_rules.h`），namei.c 不再手抄；`--check` 断言生成物与消费方一致。

模式开关 `CDH_VENDOR_BASE=gold|lineage`（默认 lineage，既有可引导链路不变）。
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys

from buildenv import (
    LINEAGE_SUPER_DIR, TMP_DIR, VENDOR_PATCH_DIR, VENDOR_STAGE_DIR,
    erofs_tool, load_baselines,
)
from debugfs import DEBUGFS, q, run
from inject import inject_files

# ---------------------------------------------------------------------------
# 模式
# ---------------------------------------------------------------------------

BASE = (os.environ.get("CDH_VENDOR_BASE") or "lineage").strip().lower()


def gold_mode():
    """当前是否以官方 gold vendor 为基座。"""
    if BASE not in ("gold", "lineage"):
        print(f"[err] CDH_VENDOR_BASE 取值非法: {BASE!r}（gold|lineage）")
        sys.exit(1)
    return BASE == "gold"


# ---------------------------------------------------------------------------
# 覆盖清单（vendor 相对路径，前导 /，不含 /vendor 前缀）
# ---------------------------------------------------------------------------

# 官方有的子树：整目录接管，内容 = 官方 verbatim + CDH 增删改。
# 判据只有一个：**消费方靠 readdir 发现内容**的目录。单文件接管在这类目录里等于
# 让官方 listing 决定可见性——CDH 新增的文件根本不会被枚举到（重定向只改写用户态
# 初始路径，readdir 出来的名字仍是官方目录里的名字）。
#   /apex                apexd 只扫这个目录（HAL APEX 激活的唯一入口）
#   /etc/selinux         init 用 opendir 收集 secilc 输入集（*.cil/*.ate/contexts）
#   /etc/vintf/manifest  libvintf 枚举片段（官方 137 件 + CDH 片段，同名者替换）
#   /etc/init            init 枚举 /vendor/etc/init/*.rc 全部读入（官方 154 件 + hw/）；
#                        只接管单文件则 cph_*.rc、netmgr.rc 永远不会被加载
#   /etc/permissions     PackageManagerService 对 /vendor/etc/permissions 做 scanDirLI
#                        （官方 52 件 + noRil/、sku_canoe/ 等子目录）
GOLD_DIRS = (
    "/apex",
    "/etc/init",
    "/etc/permissions",
    "/etc/selinux",
    "/etc/vintf/manifest",
)

# 官方没有/官方形态不可用的子树：内容全部由 CDH 侧提供（纯加法或形态改写）。
#   /usr                 官方 vendor 无此目录；InputReader 靠枚举读 keylayout/idc
#   /vendor_dlkm/lib     本盘自编译 ko（须与本盘 virtio 内核 vermagic 匹配）
#   /lib/modules         官方这里是 -> /vendor_dlkm/lib/modules 的符号链接；相对链接
#                        在 /mnt/vendor 内自重解析，正好落到上面的 ko 目录
CDH_DIRS = (
    "/usr",
    "/vendor_dlkm/lib",
)
CDH_LINKS = (
    ("/lib/modules", "../vendor_dlkm/lib/modules"),
)

# 官方 verbatim 后交给流水线改写的单文件。
GENERATED_FILES = (
    "/build.prop",        # props.prop_cmds 读镜像当前内容再渲染 baselines 声明
    "/etc/ueventd.rc",    # ueventd_video_cmds 读回后追加节点规则（ueventd 读 etc 下这份）
)

# 从 lineage 基座整目录搬运「官方没有、但功能必需」的文件（按名字差集）。
# /apex：lineage 的 HAL APEX（composer/minigbm gralloc/audio/power/sensors/memtrack/
# bootctrl/gatekeeper/health/clearkey/wpa_supplicant）。cas 官方已有 -> 用官方原件。
PULL_FROM_LINEAGE_DIRS = ("/apex",)

# 搬运黑名单：某个 lineage HAL APEX 由本设备的自产实现接管时，整块 APEX 不搬。
# com.android.hardware.audio.apex —— audio.core/audio.effect 改由
# build/vendor/source/android.hardware.audio（自研 AIDL 服务，随 vendor.img 落盘）提供。
# 为什么不能两者并存：APEX 激活后会贡献自己的 /etc/vintf 片段，与自产 manifest 对同一
# FqInstance（IModule/default、IConfig/default、effect.IFactory/default）重复声明，
# libvintf 合并片段时报 "Conflicting FqInstance" → 整份 device manifest 解析失败 →
# servicemanager "NULL VINTF MANIFEST"（本文件上方 GOLD_PRUNES 段已记录过同一条教训）。
# 为什么不能只靠 APEX 里的 example 实现（实例 44，build 46 实测）：它只注册出
# IConfig/default + IModule/r_submix，**不注册 IModule/default**，而 MIUI 的 audioserver
# 按 VINTF 认定 IModule/default 存在（FactoryHal: hasAidlHalService ... true）就死等，
# AudioService 在 system_server 主线程上被拖住 71s → Watchdog SIGABRT → 重启循环，
# sys.boot_completed 永不达成。自研实现补上 default/primary 并自带 primary_audio_policy
# 与引擎音量链（口径同 oriole-16 / note13-5G-15，已实测能开完机）。
PULL_FROM_LINEAGE_EXCLUDE = ("com.android.hardware.audio.apex",)

# 搬运进来的每个 lineage HAL APEX 被 apexd 激活后都会贡献自己的 VINTF 片段
# （挂载点 /bootstrap-apex/<apex>/etc/vintf/*.xml）。官方 vendor 侧对同一批 HAL 也有
# 声明 -> libvintf 合并片段时对同一 FqInstance 报
#   "Cannot add manifest fragment ...: HAL "..." has a conflict:
#    Conflicting FqInstance: ... Check whether or not multiple modules providing the
#    same HAL are installed"
# 并让**整份 device manifest 解析失败** -> servicemanager "NULL VINTF MANIFEST" ->
# keymint/secure_element SIGABRT、keystore2 崩、BootMonitor 周期重启
# （实例 20/21，2026-10-10 实测；冲突全集由比对"官方 137 片段+manifest_canoe.xml"
#  与"11 个搬运 APEX 的 /etc/vintf 片段"的 (format,name,fqname) 三元组得出）。
# 两侧只能留一侧：厂商实现要 TEE/ADSP/DPU/sensors PD，云机上起不来；lineage 那套是
# lineage 基座期已实测能引导到 launcher 的口径（见 project memory）。于是这里删掉
# **官方声明件**（staging 阶段不落盘，不进 meta/回填）。
# 只删片段、不删厂商 rc：这些 rc 里还挂着 on post-fs-data 的目录/权限动作（如
# hostapd.android.rc 建 /data/vendor/wifi、audiohalservice_qti.rc 的 audio ssr 触发），
# 整文件删会连坐；服务起不来时最多是一个可观测的注册失败，按实测再逐个决定。
# 例外：onekeymint 的 rc 一并删——它与我们自产的 Remote KeyMint 抢同一个
# IKeyMintDevice/default，而 keymint 在 keystore2 的启动关键路径上，不能留竞态。
GOLD_PRUNES = (
    # APEX: com.android.hardware.boot.grub.apex（IBootControl/default）
    "/etc/vintf/manifest/boot-service.qti.xml",
    # APEX: com.android.hardware.graphics.composer.drm_hwcomposer_upstream.apex
    # （IComposer/default；同文件还声明 QTI/Xiaomi 显示扩展 5 条，一并取消声明——
    #   云机无人实现，留着声明只会让客户端在 lazy start 上阻塞超时）
    "/etc/vintf/manifest/vendor.qti.hardware.display.composer-service3_v4.xml",
    # APEX: org.lineageos.device.gralloc.minigbm_upstream.apex（IAllocator/default）
    "/etc/vintf/manifest/vendor.qti.hardware.display.allocator-service.xml",
    # APEX: com.android.hardware.audio.apex（audio.core / audio.effect / bluetooth.audio）
    "/etc/vintf/manifest/manifest_audiocorehal_default.xml",
    "/etc/vintf/manifest/manifest_btaudiocoreservices_qti.xml",
    "/etc/vintf/manifest/audioeffectservice_qti.xml",
    "/etc/vintf/manifest/bluetooth_audio.xml",
    # APEX: com.android.hardware.drm.clearkey.apex（IDrmFactory/clearkey）
    "/etc/vintf/manifest/android.hardware.drm-service.clearkey.xml",
    # APEX: com.android.hardware.gatekeeper.nonsecure.apex（IGatekeeper/default）
    "/etc/vintf/manifest/android.hardware.gatekeeper-service-qti.xml",
    # APEX: com.android.hardware.memtrack.apex（IMemtrack/default）
    "/etc/vintf/manifest/memtrack_qti.xml",
    # APEX: com.android.hardware.power.apex（IPower/default、IPowerStats/default）
    "/etc/vintf/manifest/power.xml",
    "/etc/vintf/manifest/android.hardware.powerstats-service.xml",
    # APEX: com.android.hardware.sensors.apex（ISensors/default）
    "/etc/vintf/manifest/android.hardware.sensors-multihal.xml",
    # APEX: com.android.hardware.wpa_supplicant.mainline_common.apex
    # （IHostapd/default、ISupplicant/default）
    "/etc/vintf/manifest/android.hardware.wifi.hostapd.xml",
    "/etc/vintf/manifest/android.hardware.wifi.supplicant.xml",
    # APEX: com.google.cf.health.apex（IHealth/default）
    "/etc/vintf/manifest/android.hardware.health-service.qti.xml",
    # CDH 自产 Remote KeyMint（IKeyMintDevice/default）取代厂商 onekeymint
    "/etc/vintf/manifest/android.hardware.security.onekeymint-service-qti.xml",
    "/etc/init/android.hardware.security.onekeymint-service-qti.rc",
    # 显示链两条 rc 必须与上面的 manifest 一起删（onekeymint 先例的推广）。
    # 「只删片段、不删厂商 rc」的前提是厂商服务起不来、最多留一条可观测的注册失败；
    # 实例 29（2026-10-11，build 30）实测这个前提对显示链不成立 —— QTI composer
    # 与 allocator 都**起来了并抢赢了注册**：
    #   25.297 drm_hwc(pid 927)      注册 composer3.IComposer/default
    #   25.967 QTI composer(pid 813) 又注册同一个名字（servicemanager:
    #          "originally registered from PID 927 but it is being registered
    #           again from PID 813. Bad state?"）-> 后注册的 QTI 版生效
    #   24.776 QTI allocator(pid 812) 先注册 graphics.allocator.IAllocator/default；
    #          minigbm allocator 因缺 graphics_device 授权 exit(1) 反复重启，抢不回来
    # 两者的 service 段还都带 `onrestart restart surfaceflinger`，QTI 版一崩就把
    # SurfaceFlinger 一起带走 —— 正是"一直卡在 bootanim"的放大器。
    # 两个 rc 除 service 段外只有 QTI composer 的 on post-fs-data 对
    # /sys/class/mi_display/disp-DSI-{0,1}/* 的 chown/chmod（云机无此 sysfs 节点），
    # 整文件删无连带损失。
    "/etc/init/vendor.qti.hardware.display.composer-service.rc",
    "/etc/init/vendor.qti.hardware.display.allocator-service.rc",
    # 厂商 boot HAL 同样按 onekeymint/显示链的口径整文件删（实例 34，build 34 实测）：
    # 它的 manifest 片段早已在 GOLD_PRUNES 里（IBootControl/default 让给搬运的
    # com.android.hardware.boot.grub.apex，客机上 `IBootControl AIDL service running...`
    # 已确认可用），但 rc 还活着 —— 二进制 bootcontrol_init() 在云机拿不到 A/B misc
    # 后端，Check failed 直接 abort，init 每 5s 重启一次（10 分钟 297 次）。
    # rc 只有 service 段（class early_hal / user root，无 onrestart、无 on 触发的
    # 目录动作），删掉无连带损失。
    "/etc/init/android.hardware.boot-service.qti.rc",
    # soundtrigger3 的声明要单独取消，因为它卡死的是 system_server 主线程（实例 40，
    # build 44 实测 10:30:50）：QTI 的实现挂在 audiohalservice.qti 里、依赖 ADSP，VM 上
    # 永不注册；片段却把 ISoundTriggerHw/default 声明成设备提供，于是 servicemanager 对
    # 它走 lazy start（`Could not find 'aidl/android.hardware.soundtrigger3...'` ×N），
    # 而 SoundTriggerMiddlewareService$Lifecycle.onStart → SoundTriggerModule.attachToHal
    # → DefaultHalFactory.create 在 main thread 上等这个 binder，Watchdog 判定主线程
    # 卡住直接 SIGABRT system_server（pid 2234）→ zygote 退出 → system_server 重启循环，
    # sys.boot_completed 永不达成。删声明后 waitForDeclaredService 立刻返回 null，
    # middleware 走「HAL 不可用」分支（对照 oriole-16：无 soundtrigger3 HAL，
    # soundtrigger_middleware 照常发布、能开完机）。
    # 只删片段（当时的判断是「那个 rc 还挂着 audio ssr 的 on 触发动作」）；
    # 该判断在实例 42（build 45）被推翻，见下一条。
    "/etc/vintf/manifest/soundtrigger.qti.xml",
    # audiohalservice_qti.rc 必须整文件删——它与 lineage 的 audio APEX **同名 service**
    # 相撞（onekeymint/显示链/boot 先例的再一次推广，实例 42 实测）：
    #   /vendor/etc/init/audiohalservice_qti.rc:
    #       service vendor.audio-hal-aidl /vendor/bin/hw/audiohalservice.qti
    #   /apex/com.android.hardware.audio/etc/android.hardware.audio.service-aidl.example.rc:
    #       service vendor.audio-hal-aidl /apex/com.android.hardware.audio/bin/hw/
    #                                       android.hardware.audio.service-aidl.example
    # init 按名字去重，厂商 rc 赢 → 跑起来的是 audiohalservice.qti（依赖 ADSP，VM 上
    # 只注册 effect.IFactory/default，永远注册不出 core.IConfig/default），
    # APEX 的 example core HAL 从头到尾没被 start 过。于是 audioserver 死等
    #   Waited one second for android.hardware.audio.core.IConfig/default
    #   Could not find 'aidl/android.hardware.audio.core.IConfig/default' for ctl.interface_start
    # 而 A16/MIUI 的 AudioService 在 system_server **主线程**上同步等这个 binder
    # （boot 尾部停在 SystemServerTiming: StartAudioService，system_server 已重启一次），
    # 与 soundtrigger3 同一条 Watchdog SIGABRT 路径。
    # 对照 3a-16（能开完机）：它的 gold vendor /vendor/etc/init 里没有任何 audio rc，
    # 跑的就是 APEX 的 android.hardware.audio.service-aidl.example，注册
    # IConfig/default + IModule/{default,bluetooth,r_submix}。
    # 连带损失评估：该 rc 除 service 段外只有 `on property:vendor.audio.ssr.trigger=1`
    # 写 /sys/kernel/boot_adsp/ssr，和 `on boot` 对 /sys/kernel/snd_card/card_state、
    # /sys/bus/i2c/drivers/cs40l26/... 的 chown/chmod —— 云机无 ADSP/snd_card/cs40l26
    # 节点，整文件删无实际损失。
    "/etc/init/audiohalservice_qti.rc",
)

# 图形栈：路径名用厂商 soname，字节用 CDH 后端（Phase 1 的已知偏差，见
# build/docs/gpu.md §3.1）。左=接管路径（官方名），右=lineage 基座内字节源。
# 桥接层落地、厂商图形栈真跑起来时，整表删除即可（gpu.md §3.5/M0+）。
GRAPHICS_ALIAS = (
    ("/lib64/egl/libEGL_adreno.so", "/lib64/egl/libEGL_mesa.so"),
    ("/lib64/egl/libGLESv1_CM_adreno.so", "/lib64/egl/libGLESv1_CM_mesa.so"),
    ("/lib64/egl/libGLESv2_adreno.so", "/lib64/egl/libGLESv2_mesa.so"),
    ("/lib64/hw/gralloc.default.so", "/lib64/hw/gralloc.default.so"),
    ("/lib64/hw/vulkan.adreno.so", "/lib64/hw/vulkan.virtio.so"),
    ("/lib64/libgallium_dri.so", "/lib64/libgallium_dri.so"),
    ("/lib64/libminigbm_gralloc_upstream.so",
     "/lib64/libminigbm_gralloc_upstream.so"),
    # gralloc mapper 的 **passthrough/stable-C 实现**（官方 vendor 无此 inode）。
    # 实例 35（2026-10-11，build 35）实测 SF SIGABRT 环的直接成因：
    #   E Gralloc5: Failed to load mapper.minigbm_upstream.so
    #   F GraphicBufferMapper: gralloc-mapper is missing
    #   #03 /vendor/lib64/libui.so (android::GraphicBufferMapper::GraphicBufferMapper()+364)
    #   #08 libEGL_adreno.so (eglInitialize+268) -> SkiaGLRenderEngine::create
    # 后缀 `minigbm_upstream` 不是属性（实测 ro.hardware.gralloc/ro.hardware.mapper
    # 皆空，官方 rc 里那条 setprop 由缺失的 /vendor/bin/virtgpu_detect 触发、从未执行）：
    # gold libui 的符号引用指向 **lineage 侧同样存在** 的
    # aidl::android::hardware::graphics::allocator::IAllocator::getIMapperLibrarySuffix
    # ——它经 binder 问正在运行的 minigbm allocator 服务要后缀（llvm-readelf 实测该符号
    # 由 /vendor/lib64/android.hardware.graphics.allocator-V2-ndk.so 导出），
    # APEX 里的 minigbm_upstream 实现答 "minigbm_upstream"。随后 libui 走
    # `mapper/<后缀>` 的 passthrough 口径，dlopen /vendor/lib64/hw/mapper.<后缀>.so，
    # 而官方 hw 目录只有 mapper.qti.so（QTI 显示 mapper，解析不了 minigbm 句柄）。
    # 该件同时导出 AIMapper_loadIMapper（stable-C）与 HIDL_FETCH_IMapper（5.0
    # passthrough）两个入口，正是 gold libui 这条链要的形状；DT_NEEDED 17 件里
    # mapper@4.0/allocator-V2-ndk/libgralloctypes/libdmabufheap/libhidlbase/libutils/
    # libcutils/libbase/libc++ 在客机上已是 same_process_hal_file，
    # libminigbm_gralloc_upstream.so 由上面的别名提供，其余（liblog/libnativewindow/
    # libsync/libdl）按 sphal→runtime 的 links 解析——与已验证能加载的 mesa 别名同口径。
    ("/lib64/hw/mapper.minigbm_upstream.so",
     "/lib64/hw/mapper.minigbm_upstream.so"),
    # libdmabufheap：**换字节**，不是换标签。实例 36（2026-10-11，build 39）实测
    # mapper 的 passthrough 链在声明/标签/.so 三处补齐后推进到符号级失败：
    #   I servicemanager: Found mapper/minigbm_upstream in device VINTF manifest.
    #   E vndksupport: Could not load mapper.minigbm_upstream.so from sphal namespace:
    #        dlopen failed: cannot locate symbol
    #        "_ZN15BufferAllocator5AllocERKNSt3__1...Ic...EEEEmj"
    #        referenced by "/vendor/lib64/libminigbm_gralloc_upstream.so"
    #   E Gralloc5: Failed to load mapper.minigbm_upstream.so
    # 即 `BufferAllocator::Alloc(const std::string&, size_t, unsigned)` 这个 3 参重载
    # 官方件里没有：llvm-readelf 实测官方 /vendor/lib64/libdmabufheap.so（114792 B，
    # QTI 变体）只导出 `...Emjm`（4 参），lineage 的同名件（102360 B，AOSP 现版 +
    # ABI 兼容层）`Emj`/`Emjm` 都有。符号集对比（脚本 /tmp/gfxchk/closure_syms.py）：
    # 官方定义的 107 个符号 lineage **全部**定义（official-only = 0），lineage 另多 8 个
    # （DmabufHeapAlloc2 等新名 + 3 参重载），所以替换对既有消费者链接期中性。
    # 替换后同一脚本对 mapper+libminigbm 两个入口 BFS 出 36 个闭包成员，未解析符号
    # 只剩这一个，即本条是唯一残留。
    ("/lib64/libdmabufheap.so", "/lib64/libdmabufheap.so"),
)

# mesa 后端的 SP-HAL 闭包标签：接管路径 -> 上下文。
#
# 为什么必须换标签（实例 31，2026-10-11，build 33 实测）：SurfaceFlinger 起来即
# SIGABRT 循环（`init: Service 'surfaceflinger' (pid NNN) received signal 6`，
# abort message `couldn't find an OpenGL ES implementation`），唯一毒源是
#   avc: denied { read } comm="surfaceflinger"/"zygote"/"graphicsengine"
#        name="libui.so" tcontext=u:object_r:vendor_file:s0 tclass=file
#   vndksupport: Could not load vulkan.adreno.so from sphal namespace:
#        dlopen failed: library "libui.so" not found: needed by
#        /vendor/lib64/hw/vulkan.adreno.so in namespace sphal
# 机理：/linkerconfig/ld.config.txt 的 [vendor]（=sphal）段 `isolated = true`、
# permitted.paths 只有 /odm、/vendor、/system/vendor —— 平台 /system/lib64/libui.so
# 在该命名空间里根本不可见，DT_NEEDED 的 libui.so 只能在 /vendor/lib64 解析，
# 命中的却是厂商 inode（标签 vendor_file）。
#
# 换标签而不是加 allow：官方 plat_sepolicy.cil 有
#   (neverallow base_typeattr_480 vendor_file (file (execute open execute_no_trans)))
# 且 base_typeattr_480 = (and (coredomain) ...) —— coredomain（surfaceflinger/zygote）
# 永远拿不到 vendor_file 的 read/open/map，授权路线被 neverallow 封死。
# 反过来，同一份 cil 有
#   (allow base_typeattr_232 same_process_hal_file (file (read getattr map execute open)))
# base_typeattr_232 = (and (domain)(not (coredomain))) —— 所有非 coredomain 域对
# same_process_hal_file 本就有读/映射/执行，relabel 对既有消费者（QTI HAL、
# graphicsengine）**中性**；coredomain 侧走 appdomain/zygote/surfaceflinger 对
# same_process_hal_file 的既有授权 —— 这正是真机加载 /vendor/lib64/egl/*.so 的通路。
#
# 口径与厂商原生规则同源：官方 /vendor/etc/selinux/vendor_file_contexts（本盘 1518 行）
# 已为整个 SP-HAL 依赖闭包逐条点名 same_process_hal_file（libutils/libcutils/libbase/
# libhardware/libhidlbase/libion/libdmabufheap/libc++/libz/egl 与 hw/vulkan.adreno），
# 唯独没有 libui —— 真机的 Adreno 驱动不依赖 libui，是我们的 mesa 字节依赖它
# （llvm-readelf -d 实测：libEGL_mesa.so 与 vulkan.virtio.so 的 DT_NEEDED 含 libui.so）。
# 同理 libgallium_dri.so / libminigbm_gralloc_upstream.so 官方也没有这两个 inode，
# file_contexts 实算只能落到泛型 vendor_file，必须在这里点名。
#
# 实测闭包口径（脚本 /tmp/gfxchk/closure.py：以客机真值 BFS 出 sphal 四条搜索路径
# /odm/lib64、/vendor/lib64、/vendor/lib64/hw、/vendor/lib64/egl 下 8 个图形入口的
# 传递 DT_NEEDED 闭包，33 个成员逐个取真实 xattr）：只有下面 6 件不是
# same_process_hal_file。其余 NEEDED 中 liblog/libnativewindow/libsync/libvndksupport/
# libbinder_ndk 官方 vendor 里不存在（走 sphal→runtime 的 links 解析），libdrm/
# libgralloctypes/libdmabufheap/libc++/libz/libcutils/libhardware 客机上已是
# same_process_hal_file。
#
# 三条 allocator@{2,3,4}.0 是**官方 libui.so 自己的** DT_NEEDED（小米的 vendor libui
# 仍链 HIDL 传输层）：换标签换到 libui 就必然往下带一层，逐条试的话每轮都要重编内核，
# 所以一次把闭包跑全再落表。字节仍用官方 verbatim（不用 lineage 的 libui：MIUI vendor
# HAL 依赖小米在 libui 里加的符号，换字节是更大的面）。
GFX_SPHAL_LABELS = {
    "/lib64/libui.so": "u:object_r:same_process_hal_file:s0",
    "/lib64/libgallium_dri.so": "u:object_r:same_process_hal_file:s0",
    "/lib64/libminigbm_gralloc_upstream.so":
        "u:object_r:same_process_hal_file:s0",
    # mapper passthrough 件被 `android_load_sphal_library` 装进 **coredomain**
    # （surfaceflinger / zygote 预加载 / hal_camera_default 的客户端），官方没有这个
    # inode，file_contexts 实算只给泛型 vendor_file（上面的点名表就是为它准备的），
    # 而 coredomain 对 vendor_file 的 read/open/map 被 neverallow 封死。
    "/lib64/hw/mapper.minigbm_upstream.so":
        "u:object_r:same_process_hal_file:s0",
    # 字节换成 lineage 版（见 GRAPHICS_ALIAS 注释），标签与官方 inode 一致，点名一次
    # 免得依赖 file_contexts 正则实算。
    "/lib64/libdmabufheap.so": "u:object_r:same_process_hal_file:s0",
    "/lib64/android.hardware.graphics.allocator@2.0.so":
        "u:object_r:same_process_hal_file:s0",
    "/lib64/android.hardware.graphics.allocator@3.0.so":
        "u:object_r:same_process_hal_file:s0",
    "/lib64/android.hardware.graphics.allocator@4.0.so":
        "u:object_r:same_process_hal_file:s0",
}

# 官方 verbatim 字节 + CDH 换标签：官方有这个 inode、字节无可挑剔，只是标签挡住了
# sphal 命名空间的加载。接管它（进 file_overrides -> 内核重定向）后在 patch.img 里
# 落同一份字节、换 GFX_SPHAL_LABELS 的标签。
# 不能改用 bind mount：挂载点会进 /proc/mounts 形成"真机不可能有"的指纹，与
# namei.c 里选 /mnt/vendor 的整套反指纹理由冲突。
# 点名表里由 GRAPHICS_ALIAS 提供字节的（lineage 后端）不在这份清单里——它们官方没有
# inode，走别名落盘 + _file_meta 换标签。
GOLD_RELABEL = tuple(sorted(
    v for v in GFX_SPHAL_LABELS
    if v not in {dst for dst, _src in GRAPHICS_ALIAS}))

# 屏蔽类：路径不在 /vendor 下，指向不存在以令其 ENOENT（语义与 lineage 基座期一致）。
#   update_verifier 需要 A/B bootctrl 标记，缺它会主动 reboot 并阻塞 zygote-start
#   动作队列（引导死循环）。bootctrl 由 /apex 搬来的 lineage 件提供，本项留待实测决定去留。
BLOCKED = (
    "/system/bin/update_verifier",
)

# gold 基座下**解除**此前"指向不存在以屏蔽"的项：官方 vendor 策略与官方 odm 策略
# 配对，odm_sepolicy.cil 不再是毒源（那条屏蔽正是本设备周期重启环的成因）。
GOLD_UNBLOCKED = (
    "/odm/etc/selinux/odm_sepolicy.cil",
    "/odm/etc/build.prop",
)

# patch.img 挂在 /mnt/vendor：所有接管内容的运行态落点前缀。
PATCH_PREFIX = "/mnt/vendor"

WORK_DIR = os.path.join(TMP_DIR, "gold-seed")

# 内核规则表生成物（namei.c include 它；相对 kernel/source/fs）
RULES_HEADER_REL = os.path.join("fs", "cdh_vendor_rules.h")
_DEV_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
RULES_HEADER = os.path.join(_DEV_ROOT, "kernel", "source", RULES_HEADER_REL)
_NAMEI = os.path.join(_DEV_ROOT, "kernel", "source", "fs", "namei.c")


# ---------------------------------------------------------------------------
# 官方 vendor_a.img（erofs）读取原语
# ---------------------------------------------------------------------------

_FSCK = erofs_tool("fsck.erofs")
_DUMP = erofs_tool("dump.erofs")


def vendor_img():
    """官方 super 拆解出的 vendor_a.img。缺失即报错退出（先跑 unpack）。"""
    from gold_files import vendor_img as _v
    return _v()


_KIND_TYPE = {"regular file": 0o100000, "directory": 0o040000,
              "symlink file": 0o120000}

_STAT_CACHE = {}
_LS_CACHE = {}

# EROFS i_type：IFILETYPE_BIT=1（目录 bit2=2、符号链接 bit1=1），其余位是数据布局
# （固定 0、扩展 4、碎片 8、折叠 16 -> 普通文件可能是 1/5/9/17）。
_TYPE_DIR = 2
_TYPE_SYMLINK = 1


def _stat_raw(vpath):
    """dump.erofs --path=<vpath> 的 inode 头 -> dict；不存在返回 None。

    带缓存：清单审计要对同一官方镜像反复 stat 数百个路径。
    """
    key = (vendor_img(), vpath)
    if key in _STAT_CACHE:
        return _STAT_CACHE[key]
    res = subprocess.run([_DUMP, f"--path={vpath}", vendor_img()],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    text = res.stdout.decode("utf8", "replace")
    if "Path :" not in text:
        _STAT_CACHE[key] = None
        return None
    out = {}
    for ln in text.splitlines():
        if ln.startswith("Path :"):
            out["path"] = ln.split(":", 1)[1].strip()
        elif ln.startswith("Size:"):
            # "Size: 15319  On-disk size: 8192  regular file"
            # Size 是逻辑字节数（--cat 比对该值）；kind 在 On-disk size 之后
            m = re.match(r"Size:\s+(\d+)\s+On-disk size:\s+\d+\s+(.*)$", ln)
            if not m:
                continue
            out["size"] = int(m.group(1))
            out["kind"] = m.group(2).strip() or "regular file"
        elif ln.startswith("Uid:"):
            m = re.search(r"Uid:\s+(\d+)\s+Gid:\s+(\d+)\s+Access:\s+([0-7]{4})", ln)
            if m:
                out["uid"], out["gid"] = int(m.group(1)), int(m.group(2))
                out["perm"] = m.group(3)
    _STAT_CACHE[key] = out
    return out


def exists(vpath):
    """官方 vendor 内是否存在该路径。"""
    return _stat_raw(vpath) is not None


def stat(vpath):
    """官方 vendor 内该路径的 (mode, uid, gid, kind)；不存在返回 None。

    mode 含文件类型位（debugfs sif 需要的完整 mode）。
    """
    st = _stat_raw(vpath)
    if st is None:
        return None
    kind = st.get("kind", "regular file")
    return (_KIND_TYPE.get(kind, 0o100000) | int(st["perm"], 8),
            st["uid"], st["gid"], kind)


def read_file(vpath):
    """官方 vendor 内某文件的字节（erofs 解压输出）。字节数不符即报错退出。"""
    st = _stat_raw(vpath)
    if st is None:
        print(f"[err] 官方 vendor 无此文件: {vpath}")
        sys.exit(1)
    res = subprocess.run([_DUMP, "--cat", f"--path={vpath}", vendor_img()],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    data = res.stdout
    if len(data) != st["size"]:
        # dump.erofs --cat 对个别文件会短输出（实测 0 B）：宁可失败也不写坏字节
        print(f"[err] 官方件读取字节数不符 {vpath}: got {len(data)} want {st['size']}")
        sys.exit(1)
    return data


def ls(vrel):
    """官方 vendor 内某目录的名字列表（不含 . / ..）。"""
    res = subprocess.run([_DUMP, "--ls", f"--path={vrel or '/'}", vendor_img()],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    names = []
    for ln in res.stdout.decode("utf8", "replace").splitlines():
        parts = ln.split()
        if len(parts) == 3 and parts[0].isdigit() and parts[2] not in (".", ".."):
            names.append(parts[2])
    return names


def walk_subtree(vrel):
    """递归展开官方子树，产出 (img 内路径, stat 或 None)；符号链接不深入。"""
    for name in ls(vrel):
        p = f"{vrel.rstrip('/')}/{name}"
        st = stat(p)
        yield p, st
        if st and st[3] == "directory":
            yield from walk_subtree(p)


def extract_subtree(vrel, dst):
    """fsck.erofs 提取官方子树到宿主目录（**内容平铺**：vrel 的直接子项落在 dst 下）。

    幂等：以官方镜像 realpath/size/mtime 作 stamp，换 ROM 或重 unpack 后自动失效。
    """
    img = vendor_img()
    stamp = dst + ".stamp"
    want = (f"{os.path.realpath(img)} {os.path.getsize(img)} "
            f"{int(os.path.getmtime(img))} path={vrel}")
    if os.path.isdir(dst) and os.path.isfile(stamp):
        with open(stamp, encoding="utf8") as f:
            if f.read() == want:
                return dst
    print(f"[..] EROFS 提取官方 {vrel} -> {os.path.relpath(dst)}")
    tmp = dst + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    res = subprocess.run([_FSCK, f"--extract={tmp}", f"--path={vrel}", img],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if res.returncode != 0 or not os.listdir(tmp):
        print(res.stdout.decode("utf8", "replace"))
        print(f"[err] 官方 {vrel} 提取失败（rc={res.returncode}）")
        shutil.rmtree(tmp, ignore_errors=True)
        sys.exit(1)
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    os.rename(tmp, dst)
    with open(stamp, "w", encoding="utf8") as f:
        f.write(want)
    return dst


# ---------------------------------------------------------------------------
# lineage 基座（ext4）读取原语：功能必需但官方没有的内容从这里搬
# ---------------------------------------------------------------------------

_LINEAGE_VENDOR_IMG = os.path.join(LINEAGE_SUPER_DIR, "vendor_a.img")


def _lineage_img():
    if not os.path.isfile(_LINEAGE_VENDOR_IMG):
        print(f"[err] lineage 基座 vendor_a.img 缺失: "
              f"{os.path.relpath(_LINEAGE_VENDOR_IMG)}（先 unpack lineage）")
        sys.exit(1)
    return _LINEAGE_VENDOR_IMG


def lineage_read(vpath):
    """从 lineage 基座 ext4 vendor_a.img 读某文件字节。"""
    res = run([DEBUGFS, "-R", f"cat {vpath}", _lineage_img()],
              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    data = res.stdout
    if b"File not found" in data or not data:
        print(f"[err] lineage 基座无该文件: {vpath}")
        sys.exit(1)
    return data


def lineage_ls(vrel):
    """lineage 基座内某目录的文件名列表（不含 . / ..；不含子目录）。"""
    res = run([DEBUGFS, "-R", f"ls -l {vrel}", _lineage_img()],
              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    names = []
    for ln in res.stdout.decode("utf8", "replace").splitlines():
        parts = ln.split()
        # "  13  100644 (1)  0  0  68046848 22-Sep-2026 13:20 name"
        if len(parts) >= 8 and parts[0].isdigit() and "(1)" in ln:
            names.append(parts[-1])
    return names


# ---------------------------------------------------------------------------
# SELinux 标签：基线清单显式声明 > 官方 file_contexts 最长匹配
# ---------------------------------------------------------------------------
#
# 官方 vendor 的标签不是由 /vendor/etc/selinux/vendor_file_contexts 单独决定的：
# 构建期 file_contexts 是 **plat + vendor 合并**后按分区拆分的产物，通用兜底规则
# （`/(vendor|system/vendor)(/.*)? -> vendor_file`）留在 plat 侧，vendor 侧只有
# 专项细化（实测本盘 vendor_file_contexts 1518 行里 0 条 vendor_file/vendor_configs
# 兜底）。故标签口径 = 两份合并、模式串最长者胜、平局取后出现者（vendor 覆盖 plat）。

# libselinux 的 file_contexts 行：<路径正则> [类型标记] <上下文>；
# 类型标记 -l/-d/-s/-p/-c/-b，缺省=任意类型；上下文可为 <<none>>。
_TYPE_FLAG = {"-l": "symlink", "-d": "directory", "-s": "socket",
              "-p": "fifo", "-c": "char", "-b": "block"}
_KIND_FLAG = {"symlink file": "symlink", "directory": "directory",
              "regular file": None, "socket file": "socket",
              "fifo file": "fifo"}

# (标签, 分区镜像, 镜像内路径) —— 顺序即优先级，后者覆盖前者的平局
CONTEXTS_SOURCES = (
    ("plat_file_contexts", "system_a.img", "/system/etc/selinux/plat_file_contexts"),
    ("vendor_file_contexts", "vendor_a.img", "/etc/selinux/vendor_file_contexts"),
)


def contexts_file(name, img_rel, in_img):
    """把某份官方 file_contexts 取到宿主缓存（stamp 幂等），返回宿主路径。"""
    from buildenv import SUPER_OUT
    host = os.path.join(WORK_DIR, name)
    os.makedirs(WORK_DIR, exist_ok=True)
    stamp = host + ".stamp"
    img = os.path.join(SUPER_OUT, img_rel)
    if not os.path.isfile(img):
        print(f"[err] 官方 {img_rel} 缺失（先跑 unpack）")
        sys.exit(1)
    want = f"{os.path.realpath(img)} {os.path.getsize(img)} {int(os.path.getmtime(img))}"
    if os.path.isfile(host) and os.path.isfile(stamp) \
            and open(stamp, encoding="utf8").read() == want:
        return host
    res = subprocess.run([_DUMP, "--cat", f"--path={in_img}", img],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if not res.stdout:
        print(f"[err] 读取官方 {name} 失败: {in_img}")
        sys.exit(1)
    tmp = host + ".tmp"
    with open(tmp, "wb") as f:
        f.write(res.stdout)
    os.replace(tmp, host)
    with open(stamp, "w", encoding="utf8") as f:
        f.write(want)
    return host


_CONTEXTS = None
_BASELINE_LABELS = None
_META_LABELS = None


def _contexts_rules():
    """两份官方 file_contexts 合并后的规则序列（按优先级顺序编号）。"""
    global _CONTEXTS
    if _CONTEXTS is not None:
        return _CONTEXTS
    out = []
    order = 0
    for name, img_rel, in_img in CONTEXTS_SOURCES:
        with open(contexts_file(name, img_rel, in_img),
                  encoding="utf8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 3 and parts[1] in _TYPE_FLAG:
                    pattern, ftype, context = parts[0], _TYPE_FLAG[parts[1]], parts[2]
                elif len(parts) >= 2:
                    pattern, ftype, context = parts[0], None, parts[-1]
                else:
                    continue
                try:
                    cre = re.compile(pattern)
                except re.error:
                    continue
                order += 1
                out.append((cre, ftype, context, pattern, order))
    _CONTEXTS = out
    return out


def label_for(vpath, kind="regular file", meta=None):
    """官方口径 SELinux 标签。匹配不到直接报错退出，不静默兜底。

    file_contexts 语义取「全匹配、模式串最长者胜（平局取后出现者）」。错标签会让服务
    setexeccon/execute 被拒，比构建失败难查得多，所以宁可停下。

    官方 vendor 里没有的原生 inode（CDH 注入件）file_contexts 只能给出兜底泛型标签，
    此时改吃构建期基线 `vendor.selinux-meta`（与 inject 的 selinux-labels 同源异键，
    这里按 /vendor 绝对路径键入）。两条路都不通就是真错，直接退出。
    """
    global _BASELINE_LABELS, _META_LABELS
    subject = "/vendor" + vpath
    if _BASELINE_LABELS is None:
        labels = ((load_baselines() or {}).get("vendor", {})
                  .get("selinux-labels") or {})
        _BASELINE_LABELS = {"/" + k.lstrip("/"): v for k, v in labels.items()}
    if subject in _BASELINE_LABELS:
        return _BASELINE_LABELS[subject]
    if meta is not None:
        if _META_LABELS is None:
            entries = ((load_baselines() or {}).get("vendor", {})
                       .get("selinux-meta") or [])
            _META_LABELS = {e["path"]: e["context"] for e in entries}
        if subject in _META_LABELS:
            return _META_LABELS[subject]
    want = _KIND_FLAG.get(kind)
    best = None                      # (len(pattern), order, context)
    for cre, ftype, context, pattern, order in _contexts_rules():
        if ftype is not None and want is not None and ftype != want:
            continue
        if not cre.fullmatch(subject):
            continue
        key = (len(pattern), order)
        if best is None or key > (best[0], best[1]):
            best = (len(pattern), order, context)
    if best is None:
        print(f"[err] 官方 file_contexts（plat+vendor）无匹配: {subject}（{kind}）")
        sys.exit(1)
    return best[2]



# ---------------------------------------------------------------------------
# 清单组装
# ---------------------------------------------------------------------------

def cdh_tree_files():
    """两棵 CDH 注入树里的文件（接管路径 -> 宿主源文件）。"""
    out = {}
    for tree in (VENDOR_PATCH_DIR, VENDOR_STAGE_DIR):
        if not os.path.isdir(tree):
            continue
        for root, _dirs, files in os.walk(tree):
            for name in files:
                if name == ".DS_Store":
                    continue
                rel = os.path.relpath(os.path.join(root, name), tree)
                out["/" + rel.replace(os.sep, "/")] = os.path.join(root, name)
    return out


def graphics_alias_files():
    """图形栈别名（接管路径 -> 宿主字节源，取自 lineage 基座）。"""
    host_dir = os.path.join(WORK_DIR, "graphics")
    os.makedirs(host_dir, exist_ok=True)
    out = {}
    for dst, src in GRAPHICS_ALIAS:
        hp = os.path.join(host_dir, dst.lstrip("/").replace("/", "_"))
        if not os.path.isfile(hp):
            with open(hp, "wb") as f:
                f.write(lineage_read(src))
        out[dst] = hp
    return out


def pulled_lineage_files():
    """官方没有、但功能必需的 lineage 文件（按目录名字差集搬运）。"""
    out = {}
    for drel in PULL_FROM_LINEAGE_DIRS:
        gold_names = set(ls(drel))
        for name in lineage_ls(drel):
            if name in gold_names or name in PULL_FROM_LINEAGE_EXCLUDE:
                continue
            out[f"{drel.rstrip('/')}/{name}"] = ("lineage", drel.rstrip("/") + "/" + name)
    return out


def file_overrides():
    """单文件接管清单（排序去重）：生成件 + CDH 树 + 图形别名 + lineage 搬运件 + 换标签官方件。"""
    paths = set(GENERATED_FILES)
    paths.update(cdh_tree_files())
    paths.update(graphics_alias_files())
    paths.update(pulled_lineage_files())
    paths.update(GOLD_RELABEL)
    return tuple(sorted(p for p in paths
                        if not covered_by_dir(p)))


DIR_ENTRIES = GOLD_DIRS + CDH_DIRS + tuple(p for p, _t in CDH_LINKS)


def redirect_plan():
    """重定向计划：[(kind, vendor 相对路径)]，kind ∈ dir/file。

    dir = 前缀或其下任意路径都命中；file = 精确匹配。
    """
    plan = [("dir", p) for p in DIR_ENTRIES]
    plan += [("file", p) for p in file_overrides()]
    return plan


def covered_by_dir(vpath):
    """某文件路径是否已被目录条目接管（避免重复文件规则）。"""
    for d in DIR_ENTRIES:
        if d != "/" and vpath.startswith(d + "/"):
            return d
    return None


# ---------------------------------------------------------------------------
# seed：宿主 staging 树 + mke2fs -d 导入 + 官方 inode 元数据回填
# ---------------------------------------------------------------------------
#
# 形态与 build-patch.py 同源：官方 verbatim 有 387 个文件，逐条 `debugfs write` 既慢
# 又要在镜像里现建目录；改为先把内容铺到宿主 staging 树，一次 `mkfs.ext4 -d` 导入，
# 再用一批 `sif`/`ea_set` 回填官方 uid/gid/mode 与 SELinux 标签（mke2fs -d 不保留
# 属主，xattr 也无法从宿主 ext2 语义带过去）。
#
# 三档字节来源在此汇合，CDH 自产（两棵注入树）不在这里——那部分由 inject 在镜像建好
# 后按同一套 debugfs 会话写入，保持与 lineage 基座期一致的入口。


def _dir_meta(vpath):
    """目录 inode 的 meta：官方有则用官方（gid 2000 这类差异是真实的），无则标准 0755。"""
    st = stat(vpath)
    if st:
        mode, uid, gid, kind = st
        meta = False
    else:
        mode, uid, gid, kind = 0o040755, 0, 0, "directory"
        meta = True
    return (mode, uid, gid, label_for(vpath, kind, meta=meta), True)


def _file_meta(vpath, mode, uid, gid):
    """非官方件（lineage 搬运/图形别名）的 meta。

    标签优先 mesa 闭包点名表 `GFX_SPHAL_LABELS`（这些 inode 官方没有，file_contexts
    只会给泛型 vendor_file，而它们要被 coredomain 的 surfaceflinger/zygote 在 sphal
    命名空间里加载 —— 见常量注释），其次基线 `vendor.selinux-meta`，最后回落官方
    file_contexts 实算。
    """
    label = GFX_SPHAL_LABELS.get(vpath)
    if label is None:
        label = label_for(vpath, "regular file", meta=True)
    return (mode, uid, gid, label, False)


def _stage_file(dst, data):
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    tmp = dst + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)


def _drop_pruned(dst, drel):
    """按 GOLD_PRUNES 从 staging 子树剔除被取代的官方件，返回实际剔除的镜像内路径。

    必须在 meta 走盘之前做：走盘后每个落盘文件都进回填清单，删文件不删 meta 会让
    stage_backfill_cmds 对着不存在的 inode 下 sif。
    """
    dropped = []
    for vpath in GOLD_PRUNES:
        if not vpath.startswith(drel + "/"):
            continue
        host = os.path.join(dst, vpath[len(drel) + 1:])
        if os.path.islink(host) or os.path.isfile(host):
            os.remove(host)
            dropped.append(vpath)
            print(f"[..] 剔除被取代的官方声明: {vpath}")
    return dropped


def stage_tree(stage_dir):
    """把官方 verbatim + lineage 搬运 + CDH 注入树 + 符号链接铺进宿主 staging 树。

    vendor.img 因此是**完整的 /vendor 覆盖树**：清单内每一寸内容（官方字节、搬运件、
    CDH 自产）都在这棵树里一次落地，build-patch 只负责摊平成 /mnt/vendor 并拼上
    vendor_dlkm，不再对同一批字节走第二条写入路径。

    返回 {镜像内路径: (mode, uid, gid, label, is_dir)}，覆盖落地的每个 inode 及其各级
    父目录（含根 "/"），供 `mkfs.ext4 -d` 之后逐 inode 回填。
    """
    shutil.rmtree(stage_dir, ignore_errors=True)
    os.makedirs(stage_dir)
    meta = {"/": _dir_meta("/")}

    def _parents(vpath):
        for d in _ancestor_dirs(os.path.dirname(vpath)):
            meta.setdefault(d, _dir_meta(d))

    # 1) 官方子树 verbatim。先提到 WORK_DIR 缓存（fsck.erofs 平铺：子树直接子项落在
    #    缓存目录里，stamp 文件也在缓存旁），再整棵复制进 staging —— staging 树随后
    #    被 mkfs -d 原样导入，任何伴生文件（.stamp）都会变成镜像里的真实文件。
    prune_dropped = []
    for drel in GOLD_DIRS:
        cache = os.path.join(WORK_DIR, "extract" + drel.replace("/", "_"))
        extract_subtree(drel, cache)
        dst = stage_dir + drel
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(cache, dst, symlinks=True)
        prune_dropped += _drop_pruned(dst, drel)
        meta.setdefault(drel, _dir_meta(drel))
        _parents(drel)
        n = 0
        for root, dirs, files in os.walk(dst):
            rel = os.path.relpath(root, dst).replace(os.sep, "/")
            img_dir = drel if rel == "." else f"{drel}/{rel}"
            for name in dirs + files:
                # 形态以官方 inode 为准（os.walk 的 dir/file 划分按宿主 stype 分，
                # 符号链接指向目录时会被归进 dirs）
                vpath = f"{img_dir}/{name}"
                st = stat(vpath)
                if st is None:
                    print(f"[err] 提取出的 {vpath} 不在官方镜像里（提取脏了？）")
                    sys.exit(1)
                if st[3] == "directory":
                    meta.setdefault(vpath, _dir_meta(vpath))
                else:
                    meta[vpath] = (st[0], st[1], st[2],
                                   label_for(vpath, st[3]), False)
                    if st[3] == "regular file":
                        n += 1
                    else:
                        print(f"[..] 官方符号链接照搬: {vpath} -> "
                              f"{os.readlink(os.path.join(root, name))}")
        print(f"[ok] 官方子树 verbatim: {drel}（{n} 件）")
    missed = [p for p in GOLD_PRUNES if p not in prune_dropped]
    if missed:
        print(f"[warn] GOLD_PRUNES 有 {len(missed)} 项在官方树里没找到（官方基线变了？"
              f"核对名单）: {', '.join(missed)}")
    else:
        print(f"[ok] 剔除被取代的官方声明: {len(prune_dropped)} 件")

    # 2) 官方件 + 流水线改写的单文件（seed 官方字节，props/ueventd 在其上增量）
    for vpath in GENERATED_FILES:
        if covered_by_dir(vpath):
            continue
        st = stat(vpath)
        if st is None or st[3] != "regular file":
            print(f"[err] 官方 vendor 无普通文件 {vpath}（基座变了？需人工复核）")
            sys.exit(1)
        _stage_file(stage_dir + vpath, read_file(vpath))
        meta[vpath] = (st[0], st[1], st[2], label_for(vpath, st[3]), False)
        _parents(vpath)
        print(f"[ok] 官方件 verbatim: {vpath}")

    # 3) lineage 搬运件（官方没有、功能必需）。标签按官方 file_contexts 实算：
    #    /vendor/apex/*.apex 的官方规则是前缀式，对搬运名同样成立。
    pulled = pulled_lineage_files()
    for vpath, (_src, lpath) in sorted(pulled.items()):
        _stage_file(stage_dir + vpath, lineage_read(lpath))
        meta[vpath] = _file_meta(vpath, 0o100644, 0, 0)
        _parents(vpath)
    print(f"[ok] lineage 搬运件: {len(pulled)} 个（{', '.join(PULL_FROM_LINEAGE_DIRS)}）")

    # 3.5) 图形栈别名：接管路径是**厂商 soname**（官方 inode 存在则沿用其 mode/uid/gid），
    #      字节是 CDH 后端。清单里的 file 规则不会由 inject 写入，必须在这里落盘，
    #      否则内核重定向命中 /mnt/vendor/lib64/egl/… 直接 ENOENT。
    alias = graphics_alias_files()
    for vpath, host in sorted(alias.items()):
        with open(host, "rb") as f:
            _stage_file(stage_dir + vpath, f.read())
        st = stat(vpath)
        mode, uid, gid = st[:3] if st else (0o100644, 0, 0)
        meta[vpath] = _file_meta(vpath, mode, uid, gid)
        _parents(vpath)
    print(f"[ok] 图形栈别名: {len(alias)} 件（厂商 soname <- CDH 后端字节）")

    # 3.6) 官方件换标签：字节照抄官方 inode，只把 SELinux 标签换成
    #      GFX_SPHAL_LABELS（理由与实测证据见该常量注释）。
    for vpath in GOLD_RELABEL:
        st = stat(vpath)
        if st is None or st[3] != "regular file":
            print(f"[err] 官方 vendor 无普通文件 {vpath}（换标签清单失效）")
            sys.exit(1)
        _stage_file(stage_dir + vpath, read_file(vpath))
        meta[vpath] = (st[0], st[1], st[2], GFX_SPHAL_LABELS[vpath], False)
        _parents(vpath)
        print(f"[ok] 官方件换标签: {vpath} -> {GFX_SPHAL_LABELS[vpath]}"
              f"（字节 = 官方 verbatim，{st[0] & 0o777:o}）")

    # 4) CDH 符号链接（形态改写）：相对目标在 /mnt/vendor 内自重解析
    for vpath, target in CDH_LINKS:
        link = stage_dir + vpath
        os.makedirs(os.path.dirname(link), exist_ok=True)
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(target, link)
        st = stat(vpath)
        mode, uid, gid = (st[0], st[1], st[2]) if st else (0o120771, 0, 0)
        meta[vpath] = (mode, uid, gid, label_for(vpath, "symlink file"), False)
        _parents(vpath)
        print(f"[ok] 符号链接: {vpath} -> {target}")

    # 5) CDH 注入树原生落盘（gold 基座）：vendor.img 因此就是完整的 /vendor 覆盖树，
    #    build-patch 只做"摊平 + 拼 vendor_dlkm"，不再往 patch 镜像二次注入同一批字节。
    #    模式与标签直接复用 inject 的口径（两棵树的 exec 位、基线 selinux-labels），
    #    父目录走 _dir_meta：官方有的用官方 inode，没有的按 file_contexts 实算。
    n = 0
    for vpath, host, mode, label in inject_files():
        with open(host, "rb") as f:
            _stage_file(stage_dir + vpath, f.read())
        meta[vpath] = (mode, 0, 0, label, False)
        _parents(vpath)
        n += 1
    print(f"[ok] CDH 注入树原生落盘: {n} 件（模式/标签与 inject 同源）")
    return meta


def stage_backfill_cmds(meta, work_dir=None):
    """按 stage_tree 的 meta 产出回填命令：sif(mode/uid/gid) + ea_set -f 标签。

    标签写成值文件（label + NUL），与官方 inode 字节一致；debugfs 的 inline `ea_set`
    不补 NUL，libselinux 读到无结尾字符串会解析失败。
    """
    work_dir = work_dir or WORK_DIR
    os.makedirs(work_dir, exist_ok=True)
    val_dir = os.path.join(work_dir, "labels")
    os.makedirs(val_dir, exist_ok=True)
    cmds = []
    for vpath in sorted(meta, key=lambda p: (p.count("/"), p)):
        mode, uid, gid, label, _is_dir = meta[vpath]
        # sif mode 按十进制解析（0x 前缀会被 e2fsprogs 的部分版本按 base-0 猜），
        # 且字段含文件类型位，只能整值写入。
        target = q(vpath)
        cmds += [f"sif {target} mode {mode:d}",
                 f"sif {target} uid {uid}",
                 f"sif {target} gid {gid}"]
    for vpath in sorted(meta, key=lambda p: (p.count("/"), p)):
        label = meta[vpath][3]
        # 值文件名把非 [A-Za-z0-9._-] 一律折成 _（镜像里有带空格的路径，值文件路径
        # 自己也带空格就会在 debugfs 参数里被截断）；重名折叠概率极低，且 keyed by
        # 完整路径的 hash 后缀。
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", vpath) + \
            "-" + hashlib.sha1(vpath.encode("utf8")).hexdigest()[:8] + ".val"
        vf = os.path.join(val_dir, slug)
        with open(vf, "wb") as f:
            f.write(label.encode("utf8") + b"\x00")
        cmds.append(f"ea_set -f {vf} {q(vpath)} security.selinux")
    print(f"[..] 回填 {len(meta)} 个 inode 的官方 mode/uid/gid/标签")
    return cmds


def _ancestor_dirs(drel):
    """目录的各级父路径（不含根，debugfs mkdir 需自顶向下）。"""
    if not drel or drel == "/":
        return []
    parts = drel.strip("/").split("/")
    return ["/" + "/".join(parts[:i + 1]) for i in range(len(parts))]


# ---------------------------------------------------------------------------
# 内核规则表生成（单一数据源 -> C 数组）
# ---------------------------------------------------------------------------

def _c_entry(src, dst, is_dir):
    return (f'\t{{ "{src}", {len(src)}, "{dst}", {len(dst)}, '
            f'{"true" if is_dir else "false"} }},')


def redirect_rows():
    """(源绝对路径, 目标绝对路径, 是否目录条目) 全量序列，供 C 表与校验共用。"""
    rows = []
    for kind, rel in redirect_plan():
        if rel.startswith("/vendor_dlkm"):
            src, dst = rel, PATCH_PREFIX + rel
        else:
            src, dst = "/vendor" + rel, PATCH_PREFIX + rel
        rows.append((src, dst, kind == "dir"))
    for src in BLOCKED:
        rows.append((src, PATCH_PREFIX + src, False))
    return sorted(rows)


HEADER_BANNER = """/*
 * CDH vendor 受控覆盖重定向表 —— 生成物，请勿手改。
 * 数据源: devices/xiaomi/popsicle-16/build/vendor/build/gold_tree.py
 *         （python3 build/vendor/build/gold_tree.py --emit 重新生成，%d 条）
 * 语义与选型理由见 fs/namei.c 的 cdh_redirect_vendor_path()。
 * `gold_tree.py --check` 断言本表与覆盖清单逐条一致。
 */
#ifndef _CDH_VENDOR_RULES_H
#define _CDH_VENDOR_RULES_H

struct cdh_redirect_rule {
	const char *prefix;	/* 命中的源绝对路径 */
	size_t plen;
	const char *target;	/* 替换成的目标前缀 */
	size_t tlen;
	bool dir;			/* true: 该前缀或其下任意路径；false: 仅精确匹配 */
};

/*
 * 规则源路径的第二个字符集合（"/vendor..." -> 'v'，"/system..." -> 's'）。
 * getname_flags 是全局热路径，先用它把不可能命中的名字一次挡掉，再扫表。
 * 由生成器按实际规则算出，清单新增顶层目录时自动变宽。
 */
static const char cdh_rule_first_chars[] = { %s };

static const struct cdh_redirect_rule cdh_redirect_rules[] = {
%s
};

#endif /* _CDH_VENDOR_RULES_H */
"""


def emit_rules_header(write=True):
    """按清单生成内核规则表头文件，返回 (路径, 条数)。"""
    rows = redirect_rows()
    body = "\n".join(_c_entry(s, d, is_dir) for s, d, is_dir in rows)
    gate = ", ".join(f"'{c}'" for c in sorted({s[1] for s, _d, _i in rows}))
    text = HEADER_BANNER % (len(rows), gate, body)
    if write:
        os.makedirs(os.path.dirname(RULES_HEADER), exist_ok=True)
        old = None
        if os.path.isfile(RULES_HEADER):
            with open(RULES_HEADER, encoding="utf8") as f:
                old = f.read()
        if old == text:
            print(f"[ok] 内核规则表已最新: {RULES_HEADER_REL}（{len(rows)} 条）")
            return RULES_HEADER, len(rows)
        with open(RULES_HEADER, "w", encoding="utf8") as f:
            f.write(text)
        print(f"[ok] 生成内核规则表: {RULES_HEADER_REL}（{len(rows)} 条）")
    return RULES_HEADER, len(rows)


# ---------------------------------------------------------------------------
# 与内核规则表的一致性校验
# ---------------------------------------------------------------------------

_RULE_RE = re.compile(
    r"\{\s*\"([^\"]*)\"\s*,\s*\d+\s*,\s*\"([^\"]*)\"\s*,\s*\d+\s*,\s*(true|false)\s*\}",
    re.S)


def kernel_rules():
    """解析内核侧规则表（优先生成头文件；未生成时回退解析 namei.c 内联表）。

    返回 {源绝对路径: 是否目录条目}。
    """
    text = ""
    if os.path.isfile(RULES_HEADER):
        with open(RULES_HEADER, encoding="utf8") as f:
            text = f.read()
    if os.path.isfile(_NAMEI):
        with open(_NAMEI, encoding="utf8", errors="replace") as f:
            text += "\n" + f.read()
    out = {}
    for m in _RULE_RE.finditer(text):
        out[m.group(1)] = m.group(3) == "true"
    return out


def check(strict=True):
    """断言清单与内核规则表逐条一致，并汇报官方存在性。"""
    want = {}
    for src, _dst, is_dir in redirect_rows():
        want[src] = is_dir
    kr = kernel_rules()
    problems = []
    for src in sorted(want):
        if src not in kr:
            problems.append(f"内核缺规则: {src}（dir={want[src]}）")
        elif kr[src] != want[src]:
            problems.append(f"内核规则类型不符: {src}"
                            f"（期望 dir={want[src]} 实际 dir={kr[src]}）")
    for src in sorted(kr):
        if src not in want:
            problems.append(f"内核多余规则: {src}（清单未声明）")
    counts = {"override": 0, "additive": 0}
    for _kind, rel in redirect_plan():
        if rel.startswith("/vendor_dlkm"):
            continue
        counts["override" if exists(rel) else "additive"] += 1
    if problems:
        for p in problems:
            print(f"[err] 覆盖清单/内核不一致: {p}")
        if strict:
            sys.exit(1)
        return False
    print(f"[ok] 清单 {len(want)} 条与内核 rules[] 一致"
          f"（官方同名覆盖 {counts['override']}、加法 {counts['additive']}）")
    return True


def check_patch_root(root_dir):
    """断言 patch.img 的镜像根（宿主 staging 目录）能答上每一条重定向规则。

    表由本模块出、树由 build-patch 组装，两处代码。重定向没有回退语义：命中目标
    缺失就是 ENOENT，而"表里有、树里没有"在构建期完全静默，只能在真机上看成
    "库加载失败/目录空"。屏蔽项（BLOCKED）反过来必须真的缺席，否则屏蔽变命中。
    """
    blocked = {PATCH_PREFIX + p for p in BLOCKED}
    rows = redirect_rows()
    problems = []
    for src, dst, is_dir in rows:
        rel = dst[len(PATCH_PREFIX):].lstrip("/")
        path = os.path.join(root_dir, rel) if rel else root_dir
        if dst in blocked:
            if os.path.lexists(path):
                problems.append(f"屏蔽项在 patch 根里存在: {dst}（屏蔽反噬）")
            continue
        if not os.path.lexists(path):
            problems.append(f"规则目标缺失: {src} -> {dst}")
        elif (os.path.isdir if is_dir else os.path.isfile)(path) is not True:
            problems.append(f"规则目标形态不符: {dst}"
                            f"（期望{'目录' if is_dir else '文件'}）")
    if problems:
        for p in problems:
            print(f"[err] patch 根与重定向表不符: {p}")
        sys.exit(1)
    print(f"[ok] patch 根覆盖重定向表全部 {len(rows)} 条"
          f"（{len(BLOCKED)} 项屏蔽落点已验证缺席）")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def report():
    plan = redirect_plan()
    trees = cdh_tree_files()
    alias = graphics_alias_files()
    pulled = pulled_lineage_files()
    print(f"官方镜像: {os.path.relpath(vendor_img())}")
    print(f"\n目录接管 {len(DIR_ENTRIES)} 条：")
    for d in GOLD_DIRS:
        print(f"  {d:24s} 官方 verbatim，条目={len(ls(d))}")
    for d in CDH_DIRS:
        print(f"  {d:24s} CDH 内容（官方={'有' if exists(d) else '无'}）")
    for l, t in CDH_LINKS:
        print(f"  {l:24s} 符号链接 -> {t}（官方={'有' if exists(l) else '无'}）")
    print(f"\n文件接管 {len([1 for k, _p in plan if k == 'file'])} 条：")
    for _k, p in plan:
        if _k != "file":
            continue
        src = ("官方件" if p in GENERATED_FILES else
               "CDH 注入树" if p in trees else
               "图形别名" if p in alias else
               "lineage 搬运" if p in pulled else "?")
        print(f"  {p:60s} 官方={'覆盖' if exists(p) else '加法'}  {src}")
    print(f"\n屏蔽: {', '.join(BLOCKED)}")
    print(f"解除屏蔽（官方原件生效）: {', '.join(GOLD_UNBLOCKED)}")
    print("\n标签（官方 file_contexts plat+vendor 合并口径，逐条实算）：")
    groups = {}
    for kind, p in plan:
        if p.startswith("/vendor_dlkm"):
            kind_lbl, label = "regular file", None
        else:
            st = stat(p)
            kind_lbl = st[3] if st else "regular file"
            label = label_for(p, kind_lbl)
        if label is None:
            continue
        groups.setdefault(label, []).append(p)
    for label in sorted(groups):
        print(f"  {label:52s} x{len(groups[label])}")
        if label == "<<none>>":
            print("      ! <<none>> 需要 xattr 的路径：见上，需人工确认")
    # 目录接管内还会 seed 官方子树的其它 inode，全递归算一遍标签（缺失即报错退出）：
    # 这些路径不逐条进清单，readdir 直接吃官方 listing，标签错了运行态才炸。
    extra = 0
    for d in GOLD_DIRS:
        for p, st in walk_subtree(d):
            if st is None:
                print(f"[err] 官方子树里的 {p} stat 取不到（镜像变了？）")
                sys.exit(1)
            label_for(p, st[3])
            extra += 1
    print(f"  （另 {extra} 个官方子树 inode 标签均可算出，含目录/符号链接）")



def main():
    args = sys.argv[1:]
    if "--emit" in args:
        emit_rules_header()
        return
    if "--check" in args:
        sys.exit(0 if check() else 1)
    if "--report" in args:
        report()
        return
    print("用法: gold_tree.py [--report|--emit|--check]")


if __name__ == "__main__":
    main()
