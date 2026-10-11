# 厂商图形栈原样运行方案（Adreno UMD → 桥接 virtio-gpu）

- 设备: Xiaomi popsicle-16（HyperOS OS3.0.319.0.WPBCNXM / Android 16 / SM8850）
- 宿主: macOS (darwin, QEMU + hvf)，Apple M4
- 状态: **设计稿（M0 未通过前不承诺实施）**（2026-10-10）
- 关联: `../note13-5G-15/build/doc/vendor-so-identity.md`（身份面三路线 A/B/C 与严格验收档）

---

## 0. 目标与硬约束

**硬约束（本方案的第一原则）**

1. **不修改厂商任何 `.so` 的字节**。不用 interposer、不用壳、不 patch、不重编。
2. **图形栈全部使用厂商 gold 原件**：`libEGL_adreno.so` / `libGLESv2_adreno.so` /
   `vulkan.adreno.so` / `libgsl.so` / `libllvm-glnext.so` 等必须是被真正 dlopen 的那一份。
3. 仅使用官方镜像包作为输入（`roms/popsicle_images_OS3.0.319.0.WPBCNXM_16.0/`）。

**目标**：guest 里跑**真 Adreno 用户态驱动**，它产出的 GPU 命令流经桥接层翻译后由宿主执行，
从而同时拿到「身份真实」与「能出画面」。

**代价的本质**（与 interposer 路线的根本区别）：
interposer 是**改口**——上报一个字符串，兑现与否无关；本方案是**兑现**——真驱动会按 Adreno
硬件能力表上报 ES 3.2 / Vulkan 1.3 / AEP / tessellation / `VK_QCOM_*`，App 一旦调用，
桥接层就必须**真能执行**。做不到不是指纹问题，是渲染错误。这句话是本方案全部风险的来源。

---

## 1. 取证基线（全部取自本仓库官方包，2026-10-10）

### 1.1 属性面（gold `vendor_a.img:/build.prop`）

```
ro.hardware.egl=adreno                 ro.hardware.vulkan=adreno
ro.board.platform=canoe                ro.hwui.use_vulkan=true
ro.gfx.driver.1=com.qualcomm.qti.gpudrivers.canoe.api36
vendor.gralloc.disable_ubwc=0          vendor.gralloc.enable_snapalloc=1
debug.egl.hw=0
```

> 这些值本方案**全部保留**（不需要改口）。注意 `ro.gfx.driver.1` 见 §1.2 的澄清。

### 1.2 驱动 APK 是 stub —— 真正被加载的 so 在 `/vendor/lib64`

```
/vendor/app/com.qualcomm.qti.gpudrivers.canoe.api36/com.qualcomm.qti.gpudrivers.canoe.api36.apk
  8503 B；内部仅 4 项：assets/allowlist.txt(0) assets/sphal_libraries.txt(0)
  resources.arsc(40) AndroidManifest.xml(1856)   —— 无任何 .so
```

**结论**：本盘的 Adreno 驱动不是 APK 投递形态，运行期加载对象就是 gold `/vendor/lib64/**` 原件。
→ 「完全使用厂商图形栈 so」的文件清单因此是明确、可 md5 校验的一批（§1.3）。

### 1.3 需要进入加载路径的厂商件（gold 实测尺寸）

| 文件 | 字节 | 角色 |
|---|---|---|
| `/lib64/egl/libEGL_adreno.so` | 83 880 | EGL 子驱动入口（`DT_NEEDED: libEGL_adreno.so`，内含 "Adreno"） |
| `/lib64/egl/eglSubDriverAndroid.so` | 165 800 | QC EGL subdriver 转发层 |
| `/lib64/egl/libGLESv1_CM_adreno.so` | — | ES1 入口 |
| `/lib64/egl/libGLESv2_adreno.so` | 4 734 664 | ES2/3 前端（主体） |
| `/lib64/hw/vulkan.adreno.so` | 4 461 504 | Vulkan ICD（由 `ro.hardware.vulkan=adreno` 命中） |
| `/lib64/libgsl.so` | 2 138 536 | **GPU UMD**（PM4 构建 + timestamp/fence + NPU 路径） |
| `/lib64/libllvm-glnext.so` | 2 025 552 | GLSL→Adreno 微码编译器（专有 IR） |
| `/lib64/libadreno_utils.so` | 134 152 | 核参数/工具 |
| `/lib64/hw/mapper.qti.so` | 114 920 | gralloc 6.x buffer 元数据解码 |
| `/lib64/hw/gralloc.default.so`（gold 版） | 60 816 | 分配器（**与基座 minigbm 互斥，见 §3.6**） |

Loader 选型前置事实：gold `/lib64/egl/` **没有单库 `libGLES_adreno.so`** → `Loader.cpp:684`
必走三件套退回路径 → 厂商 EGL 链可被完整命中（不需要任何壳）。

### 1.4 UMD 要求的硬件侧接口（`strings libgsl.so` 取证）

```
设备节点：/dev/kgsl-3d0  /dev/kgsl-2d0  /dev/kgsl-2d1  /dev/dri/renderD128  /dev/dri/%s  /dev/ion
sysfs    ：/sys/class/kgsl/kgsl-3d0/snapshot      /sys/kernel/debug/kgsl/proc/%d/mem
           /sys/kernel/debug/dri/0                /dev/xiaomi-fp（无关，属指纹）
heap     ：qcom,system  qcom,adsp
依赖      ：libsync.so  libdmabufheap.so  libnativewindow.so  libcutils.so  libbinder_ndk.so
```

ioctl 面（UMD 内字符串直证，即我们要满足的 ABI）：

```
IOCTL_KGSL_RINGBUFFER_ISSUEIBCMDS      IOCTL_KGSL_CMDSTREAM_READTIMESTAMP[_CTXTID]
IOCTL_KGSL_DEVICE_WAITTIMESTAMP[_CTXTID]  IOCTL_KGSL_TIMESTAMP_EVENT
IOCTL_KGSL_CMDSTREAM_FREEMEMONTIMESTAMP[_CTXTID]
```

### 1.5 UMD 形态：UMDK（这是本方案可行的根本前提）

```
自研命令流：CP_DRAW_INDX_OFFSET / CP_DRAW_INDIRECT / CP_DRAW_AUTO / CP_SET_DRAW_STATE
            显存寻址以 gpuAddr 表述（"is not valid gpuAddr: 0x%x"）
自带自检：PM4CC "User mode driver cannot write to CP registers / RBBM registers"
          "No WFI before non-context register write"  "Must submit increasing timestamps"
          "CLEAR_LRZ_RESOURCE bit MUST be set only in BR pipe"  "BV %u | BR %u" 计数一致
寄存器模型：vmmRB_MRT_BLEND_CNTL_0..7 / vmmSP_BLEND_CNTL（Adreno 寄存器位域定义内嵌于 UMD）
```

→ **命令包由用户态直接生成，KMD 只做 buffer / issue / timestamp / fence / 地址空间**。
所以本方案不需要 MMIO 设备模型、不需要 GMU 电源时序仿真——需要的是**假 KMD + 命令流翻译器**。

### 1.6 核代际

```
vendor_dlkm：msm_kgsl.ko（GPU KMD）、msm_drm.ko（6 598 352 B，strings 只见 DSI/framebuffer/iova，
             未见 adreno/gmu 标识 → 该 ko 承载显示，不承载 3D）
             governor_msm_adreno_ro.ko / governor_msm_adreno_tz.ko / governor_gpubw_mon.ko
             gpu_freq_stats.ko / msm-mmrm.ko / msm_hw_fence.ko / msm-eva.ko
vendor 固件：gen80000_gmu.bin  gen80200_gmu.bin  gen80900_gmu.bin
```

`gen8xxxx` 命名指向 Adreno **gen8 / a8xx** 代际（确切 chip_id 待真机核实，见 §8）。
显示 HAL：`/bin/hw/vendor.qti.hardware.display.{composer,allocator,color,demura}-service`
+ `vendor.xiaomi.hardware.displayfeature_aidl-service`。

### 1.7 决定性负面事实：厂商栈没有虚拟化出口

```
在 libgsl.so 中检索 virtio / gvirt / hab / venus / virgl / paravirt → 0 命中
（唯一近似命中是寄存器名 vmmRB_* / vmmSP_*）
```

→ **不存在任何"现成桥接点"**。QC 云/半虚拟化驱动（gVirt 一类）不在这份 ROM 里；gfxstream 的
模拟器驱动是 QC 为模拟器单独编译的另一份件，也不是这里被禁改的这批。本方案必须自造接口。

---

## 2. 与 note13 既有结论的关系

`../note13-5G-15/build/doc/vendor-so-identity.md` §2 已写下硬约束原文：

> 「真机 GL 是 MTK/Mali，依赖 MTK 内核驱动（`/dev/mali0`、ion、ged/pq）与 MTK gralloc/composer HAL；
> 云机为 virtio-gpu。因此"名字+md5 一致"与"实际能渲染"在同一进程内很难兼得——本方案只解前者。」

本方案就是**去正面解那句"很难兼得"**：不改 so，而是把驱动下面缺失的硬件侧补齐。
与 note13 三路线的对应关系：

| note13 路线 | 本方案里的位置 |
|---|---|
| **A 收窄重定向** | **必要前置**（§3.1）：GL/Vulkan/gralloc 子路径不重定向，让厂商原件真的被 dlopen |
| B loader 壳 | **禁用**——壳意味着我方产出 .so，违反硬约束 1 |
| C 整体 gold vendor 底 | 本方案只把 C 收窄到**图形子树**，不重走整机 gold 基座 |

---

## 3. 架构

```
 ┌ guest ─────────────────────────────────────────────────────────┐
 │ App / SurfaceFlinger                                            │
 │   → libEGL.so(plat) → libEGL_adreno.so → eglSubDriverAndroid   │
 │   → libGLESv2_adreno.so / vulkan.adreno.so                      │
 │   → libgsl.so(UMD，未改) ── 生成 PM4 IB + gpuAddr              │
 │        │ ioctl(IOCTL_KGSL_* / DRM)                              │
 │   ② cdh-kgsl.ko（假 KMD，本方案自研的唯一内核件）              │
 │        │ ③ 通道（私有 virtio 设备 / virtio-gpu 自定义 capset）  │
 └────────┼───────────────────────────────────────────────────────┘
          ▼
 ┌ host ──────────────────────────────────────────────────────────┐
 │ ④ 翻译器：PM4 → 宿主渲染                                        │
 │    包解析器由 Mesa freedreno 的核 XML **代码生成**（不可手写）  │
 │    状态重建 → drawbatch；shader 从编译入口截标准源（§4.1）      │
 │    完成后回填 timestamp → 经 ③ 唤醒 ② 的等待者                 │
 │    落地方式二选一：宿主 Vulkan（MoltenVK）／复用 venus 通道     │
 └────────────────────────────────────────────────────────────────┘
```

### 3.1 路径让渡（不改 so 的"让 so 生效"）

现网事实：内核 CDH 重定向把 `/vendor/lib64`（含 `egl/`、`hw/`）指到 lineage 的
`/mnt/vendor/lib64`，因此厂商 GL 件虽然**在盘上**、却**永不被加载**。本步把图形子路径从
重定向白名单里摘出，使其落回官方 gold（路径名 = 真机、字节 = 真机）。

- 让渡范围：`/vendor/lib64/egl/**`、`/vendor/lib64/hw/vulkan.adreno.so`、
  `/vendor/lib64/{libgsl.so,libllvm-glnext.so,libadreno_utils.so,libllvm-qgl.so,...}`
  （全集以真机 maps 为准，见 §8 取证项 T5）
- 构建期断言：让渡清单每条的 `length + sha256` 与 gold 对齐（复用
  `vendor/source/android.hardware.gpu/config/gpu.json` 里 `scan-libs` 的契约校验写法），
  不符即失败退出——厂商固件换版必须炸在构建期，不能静默进镜像。
- 与整机重定向的关系：其余 `/vendor/**` 继续走 `/mnt/vendor`（lineage 基座），否则系统起不来
  （note13 §10.4 同结论）。

### 3.2 假 KMD `cdh-kgsl.ko`

必须满足的最小 ABI（按 §1.4/§1.5 取证逐条实现，不多做）：

| 面 | 内容 |
|---|---|
| 节点 | `/dev/kgsl-3d0`（+ `kgsl-2d0/1` 空桩）、`/dev/dri/renderD128`（若走 DRM 路径） |
| buffer | `FD_TO_ID`/`QUERYMEMORY` 等：以 dmabuf 后端分配，导出 **自定 GPU VA**（不做真 SMMU），维护 VA→guest 页表映射供翻译器 deref |
| 提交 | `RINGBUFFER_ISSUEIBCMDS` → 取出 IB 字节流 + 本次 VA 快照，推入 ③；分配**严格递增** timestamp（UMD 自检会校验） |
| 时序 | `CMDSTREAM_READTIMESTAMP` / `DEVICE_WAITTIMESTAMP` / `TIMESTAMP_EVENT`：等待者挂到 eventfd；翻译器执行完毕即推进 |
| 频率/电源 | `/sys/class/kgsl/kgsl-3d0/{gpulvl,devfreq/...}`、`governor_msm_adreno_*` 的读面桩 |
| 取证面 | `/sys/class/kgsl/kgsl-3d0/snapshot`、`/sys/kernel/debug/kgsl/proc/%d/mem` |
| heap | 供 `qcom,system` / `qcom,adsp`（`msm-mmrm.ko` 的替身或最小 dmabuf-heaps 实现） |
| fence | `libsync` 侧 dma-fence/sync_file 导出，跨端可 poll |

### 3.3 通道

两个候选，倾向前者：

- **私有 virtio 设备**：语义自由（IB 流 + VA 表 + fence 句柄 + 完成中断），buffer 走 dmabuf 零拷贝。
- **virtio-gpu 自定义 capset**：蹭现成 virtqueue 与 rutabaga 资源模型，但要把 GPU 对象塞进它的
  resource 语义，容易在 buffer 生命周期上打架。

### 3.4 翻译器（本项目唯一真正的新代码）

- **包解析**：`CP_SET_PIPELINE_SHARE_OBJECT` / `CP_LOAD_STATE6` / `CP_SET_DRAW_STATE` /
  `CP_DRAW_INDX_OFFSET` / `CP_EVENT_WRITE(WAIT_TIMESTAMP)` 等，**由核 XML 代码生成**状态机；
  手写不可维护（包数量与位域按代际变化）。
- **状态重建**：把 state6 blob 还原成顶点输入/光栅/混合/深度模板/FS/VS 绑定 → 映射到宿主管线。
- **tile 语义展平**：Adreno 的 BV/BR 分 binning、LRZ、on-chip memspace 在宿主侧**全部丢弃/展平**
  （它们是优化，不是语义）。
- **执行模型**：准同步（提交后尽快完成并推进 timestamp），否则进 ADRV hang/recover 路径。

### 3.5 shader —— 本方案的成败手

`libllvm-glnext.so` 产出 **Adreno IL 微码**；开源界（Freedreno/Turnip）只**生成**微码、
**没有解释器**，逆向反编译也不成气候。因此**唯一现实做法是不消费它的产物**：

- **Vulkan 路径（可行）**：`vkCreateShaderModule` 的 `pCode` 就是**标准 SPIR-V**，在翻译层完整可见
  → 直接交宿主编译。这也是**必须先做 Vulkan、GL 缓做**的原因（§4.1）。
- **GL 路径（重）**：源在进 glnext 之前是 GLSL，但那是 QC 私有调用面（glnext 内部 ABI、
  按 `GL66` 一类版本号变化），要拿到"源/程序对象"就得理解该私有 ABI，且不保证拿得到
  compute/geometry 等着色器类型。**列为 M4，明确不承诺。**

### 3.6 buffer 与 display —— 「完全用厂商栈」必然拖出的连带项

| 子系统 | 冲突 | 本方案选择 |
|---|---|---|
| gralloc | 厂商 `gralloc.default.so`(gold) + `mapper.qti.so` 与基座 minigbm **互斥**（一个进程只能有一份 gralloc） | 走厂商 qti gralloc（符合硬约束），分配落到 §3.2 的 heap；`vendor.gralloc.disable_ubwc=1` 关压缩（**改属性，不改 so**） |
| UBWC/AFHC | 宿主不懂压缩布局 | 同上，属性关；剩余 snapalloc 语义在翻译器里按线性处理 |
| composer | QTI composer 绑真 `msm_drm`/DSI，而显示设备是 virtio-gpu | **推荐：composer 保留基座 virtio HWC，只把 GL/Vulkan 栈换成厂商件**（图形栈 ≠ 显示栈，见 §9 未决 Q2） |
| VINTF | 合成 vendor 的 manifest 目前声明 lineage/virtio HAL | 图形相关条目需与"厂商件真在跑"自洽（否则 App 侧查询会自相矛盾） |
| sepolicy | 厂商图形件带 `same_process_hal_file` 等标签；新节点要类型 | 由 `vendor/build/sepolicy.py` 追加段落地（note13 已有同类先例：pioneer/keymint 段） |

---

## 4. 关键设计决策

**D1 Vulkan 先行，GL 缓做。** 判据来自 §3.5：SPIR-V 是标准输入，GL 侧要啃私有编译 ABI。
现代检测面与新 App 也主要看 Vulkan。

**D2 扩展与能力「只减不加」的纪律在本方案里升级为「做不到就别让它被调用」。**
即使全部做完，宿主是 M4（GL 4.1 / MoltenVK 的 VK 子集），Adreno 上报的 `VK_KHR_*`/`VK_QCOM_*`
与宿主可用集合的**差集就是必须真实现的部分**。也就是说：**模拟器做完并不能消掉能力封顶**，
只是把矛盾从"字符串谎报"变成"功能缺失"。这一点必须作为验收的一部分，而不是当作可后补细节。

**D3 「不改 so」用构建期断言保证，而不是靠自觉。** 让渡清单每条 length+sha256 契约校验（§3.1）。
任何一次真机固件换版都会让构建炸掉，这是期望行为。

**D4 属性/sysfs/内核件不算修改 so。** 保留全部厂商图形值（`ro.hardware.egl=adreno` 等，§1.1），
这与当前 `out/dist/rom` 里"egl=mesa/vulkan=virtio/gralloc=minigbm"的临时抢先值**互斥**——
本方案启动即须回退那三键（见 `build/baselines/*.json` 的 `vendor.build-prop-sets` 与其 note 说明）。

---

## 5. 硬墙清单（每条带判据）

| # | 墙 | 缓解 | M0 判据 |
|---|---|---|---|
| W1 | **gen8/a8xx 的 PM4 + state6 文档覆盖度** | 用 Mesa freedreno 的核 XML 生成解析器 | **生死**：XML 覆盖率不足以生成解析器 → 整案止损 |
| W2 | 着色器微码 | 只走"截标准源"；GL 侧不做 | Vulkan 能拿到 SPIR-V 即通过 |
| W3 | timestamp/fence 时序 + UMD 自检 PM4CC | 准同步执行 + 递增 ts；自检由 UMD 自己跑（我们不必实现，但**必须让它认为合法**） | M1 中 `eglInitialize`/`vkCreateInstance` 不失败 |
| W4 | UBWC/AFHC/LRZ/tile/on-chip | 属性关压缩 + 翻译器展平 | 出现无法旁路的压缩耦合则回退 |
| W5 | 宿主能力封顶（§D2） | 无解，只能界定范围 | 差集清单必须量化出来 |
| W6 | composer/DRM/HWC 连带 | 显示栈保留基座（§3.6） | 未定，见 §9 Q2 |
| W7 | 性能（QEMU 侧逐包解析）+ 长期维护（每次换版重做语义核对） | batch 级并行；先接受低于 venus 直通 | M3 实测帧率 |

---

## 6. 里程碑与止损判据

| 阶段 | 交付 | 现实估时 | 通过判据 | 不通过动作 |
|---|---|---|---|---|
| **M0** | 可行性侦察（**不写代码**）：gen8 XML 覆盖度、chip_id、宿主 VK 能力差集 | 1–2 周 | 三项齐备且 W5 差集可接受 | **整案止损，回落 interposer 身份层** |
| M1 | §3.1 路径让渡 + §3.2 假 KMD 过探针 | 2–3 周 | UMD 在 open/query/ioctl 阶段不再失败，`eglInitialize` 成功 | 回到 §1.4 取证补 ABI |
| M2 | ③④ 打通：一个 clear + 一个三角形 | 1–2 月 | 宿主出像素、timestamp 正常推进 | 检查 W3/W4 |
| M3 | dEQP-VK 子集 + 目标 App 主界面 | 3–6 月 | 与真机逐帧 diff 达可用水位 | 长尾修 |
| M4 | GL 路径（glnext + 微码替代） | **不承诺** | — | — |

**合计 M0→M3 ≈ 6–12 人月**，M3 后是 App 兼容长尾。作为对照：note13 的 interposer 身份层是
**天级**工作量，代价是 §D2 那三处必须留真值（version/GLSL/扩展）。

---

## 7. 验收方法（复用现成工具，不新造）

身份/文件面（观测件已在 `core/development/device-checker/`）：

1. 真机与云机各跑一次 `hardware.gles`（触发图形栈加载）。
2. 同一 `/api/run` 下发 `['proc.vendor_so','proc.vendor_so_md5']`。
3. 比对：**图形 so 集合是否就是厂商那一套**（不该再出现 `lib*_mesa.so` /
   `vulkan.virtio.so` / `minigbm` 条目）、逐条 md5 是否 = gold。
   本方案的通过标准比 note13 更硬：**不是"看起来像"，而是"maps 里就是它"**。

功能/正确性面：

- `dumpsys SurfaceFlinger` 的 GLES 行 = 真机串（由驱动自己报出，非我们写入）。
- `vulkaninfo` JSON 逐字段比对（deviceName/vendorID/deviceID/apiVersion/driverName/driverID）。
- dEQP-VK 子集通过率；目标 App 与真机同帧截图 diff。
- 稳定性：连续 boot 无 `sys.init.updatable_crashing`、无 `Linux version` 多次出现。

---

## 8. 前置取证清单（不做则本方案不成立）

| # | 取什么 | 为什么 | 手段 |
|---|---|---|---|
| T1 | SM8850 GPU 的 **chip_id / 核号 / GMU 版本** | 决定用哪套 XML、以及 UMD 走 kgsl 还是 DRM 分支 | 真机 `/sys/class/kgsl/kgsl-3d0/gpu_model`、`debugfs` |
| T2 | 真机 **kgsl ioctl 序列**（一次启动 + 一次 App 渲染的调用与参数） | 这就是假 KMD 的 ABI 规格书 | 真机 strace/ftrace（`msm_kgsl` tracepoints） |
| T3 | 真机 `vulkaninfo` JSON + `dumpsys SurfaceFlinger` GLES 行 + GL 扩展全表 | 定义"必须兑现"的能力集（§D2/W5） | device-checker `hardware.gles` + `hardware.vulkan` |
| T4 | `msm_kgsl.ko` 的 ioctl 表与结构体（本次 `dump.erofs --cat` 拿到 0 B，**未取证**） | §3.2 的接口边界 | 宿主 debugfs/erofs 提取成功后逐 ioctl 反推 |
| T5 | 真机某典型 App 进程 maps 里的**全部图形 so 路径** | §3.1 让渡清单的准确范围 | device-checker `proc.vendor_so` |
| T6 | Mesa freedreno 对本核的 XML 覆盖度（CP 包 / state6 枚举） | W1 生死判据 | 读上游 `src/freedreno/`（需联网/取源码树） |

---

## 9. 未决问题

- **Q1 显示栈边界**：composer/allocator 是否也算"图形栈 so"必须换成厂商件？本文当前
  立场是**不换**（§3.6），因为它与 virtio display 冲突且不产生 App 可见的 GL/VK 身份。
  若要求换，需再造一层假 DRM/msm_drm 主设备，工作量另计。
- **Q2 OpenCL / EVA / NPU**：`libgsl.so` 里含 `NpuCmdProc`/`NpuContext` 符号，gold 另有
  `libOpenCL_adreno.so`、`msm-eva.ko`。是否纳入同一桥接层，或明确排除。
- **Q3 maps 多余条目**：让渡后系统进程（uid<10000）仍走 lineage mesa，会出现
  "系统侧 mesa、App 侧 adreno"的双栈并存；是否要求集合完全相等（note13 §10 曾定"完全相等"档）。
- **Q4 32 位面**：本盘纯 64 位口径（`abilist32` 空、`zygote64`），厂商 `lib/egl/*` 不参与。
- **Q5 与临时抢先三键的冲突**：`build/baselines/*.json` 里为修 zygote EGL 竞态加入的
  `ro.hardware.egl=mesa`/`vulkan=virtio`/`gralloc=minigbm` 与本方案互斥（§D4），启动本方案时须回退。

---

## 10. 本方案明确不做

- 不产出任何图形 `.so`（无 interposer、无蹦床转发、无壳）。
- 不改写 `glGetString` / `vkGetPhysicalDeviceProperties` 之类的返回值——身份由厂商驱动自报。
- 不把整机 vendor 基座换成 gold（note13 路线 C 的整机版，已回退）。
- 不在 QEMU 里做 Adreno 设备模型（MMIO/寄存器空间/GMU 时序）——UMDK 形态下不需要（§1.5）。

---

## 11. 参考（相对路径）

- 取证件：`../roms/popsicle_images_OS3.0.319.0.WPBCNXM_16.0/super/{vendor_a.img,vendor_dlkm_a.img,odm_a.img}`
- 重定向与让渡实现：`../kernel/source/fs/namei.c`（CDH 白名单）
- 现有图形处理（对照，非本方案手段）：`../note13-5G-15/build/vendor/source/android.hardware.gpu/`
- 身份面三路线与严格验收档：`../note13-5G-15/build/doc/vendor-so-identity.md`
- 合成 vendor 架构与屏蔽落点断言：`scripts/build-patch.py`（`MUST_ABSENT`）、`vendor/build/sepolicy.py`
- 属性落地机制：`vendor/build/props.py` + `baselines/popsicle_images_OS3.0.319.0.WPBCNXM_20260824.0000.00_16.0_cn_9d8daa8b5b.json`
