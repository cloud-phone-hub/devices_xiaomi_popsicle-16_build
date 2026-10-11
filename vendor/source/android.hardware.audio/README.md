# android.hardware.audio —— note13-5G-15 自研 audio HAL（AIDL v2）

> **自研服务 + 官方 AIDL 展开源**，依赖自包含：ABI 运行库从 note13 自己 lineage-22.0
> super 研磨，AOSP platform 头构建期一次性真实复制（非软链、不借用其他模块产物），
> 官方 `aidl_api` 快照固化在 `aidl/aosp/`。结构对齐 `android.hardware.media.v412`。

## 0. 模块定位

de-apex 提取的 lineage audio 服务闭包依赖 `libxml2 -> MIUI /system 版 libxml2 ->
libandroidicu`（vendor 命名空间不可达）导致 `CANNOT LINK`。本方案改为**自研服务**：
实现只依赖 `libbinder_ndk / libc++ / liblog`，闭包内根本没有 `libxml2`。

提供 `android.hardware.audio.core` v2 与 `android.hardware.audio.effect` v2 两套 AIDL 服务，
承载 guest 全部播放/录音/音量/音效，并把 PCM 同时喂给 VirtualDac（捕获回显）与
QEMU intel-hda 声卡（host 出声 + 时钟锚定）。

## 1. 服务与 VINTF 实例

两个 PIE 服务二进制（`vendor/etc/init/android.hardware.audio.service-aidl.example.rc`）：

| init 服务 | 二进制 | 用户 |
|---|---|---|
| `vendor.audio-hal-aidl` | `android.hardware.audio.service-aidl.example` | audioserver |
| `vendor.audio-effect-hal-aidl` | `android.hardware.audio.effect.service-aidl.example` | audioserver |

VINTF 声明（`vendor/etc/vintf/manifest/android.hardware.audio.service-aidl.xml`）：

| HAL | 版本 | 实例 |
|---|---|---|
| `android.hardware.audio.core` | 2 | `IModule/default`、`IModule/primary`、`IModule/r_submix`、`IConfig/default` |
| `android.hardware.audio.effect` | 2 | `IFactory/default` |

`r_submix` 实例供 REMOTE_SUBMIX（scrcpy/推流）捕获；`primary` 承载扬声器主输出。

## 2. 音量组配置（ConfigImpl，关键）

`IConfig.getEngineConfig()` 必须返回**非空 volumeGroups**，否则 framework 音量组
Min/Max=0、音量键无效（加上去即被夹回静音）。两个硬约束：

1. **卷组名 = 引擎默认策略引用的流类型字符串**（`AUDIO_STREAM_MUSIC` / `AUDIO_STREAM_VOICE_CALL`
   …）。引擎默认策略（`EngineDefaultConfig.h` 的 `gOrderedStrategies`）用
   `attributesGroup.volumeGroupName` 按名引用卷组；名字对不上时 `processParsingResult`
   会建空组（Min/Max=0）并打 `No configuration of %s found`。
2. **`productStrategies` 必须为空**。小米闭源扩展 `libaudiopolicycustomextensions.so` 的
   `AudioPolicyManagerCustomImpl::gainTable_getVolumeDbFromComputeVolume` 在 HAL 提供
   productStrategies 时会 `map::at key not found` 崩溃 audioserver；留空则引擎用内置全集
   （`gDefaultEngineConfig.productStrategies`）。

当前返回 13 个卷组（名字精确匹配流类型），曲线取自
`/system_ext/etc/audio_policy_engine_default_stream_volumes.xml`：

| 卷组 | Min..Max |
|---|---|
| `AUDIO_STREAM_MUSIC` | 0..25 |
| `AUDIO_STREAM_VOICE_CALL` / `ALARM` / `ACCESSIBILITY` | 1..7 / 1..7 / 1..15 |
| `AUDIO_STREAM_SYSTEM` / `RING` / `NOTIFICATION` / `ENFORCED_AUDIBLE` | 0..7 |
| `AUDIO_STREAM_BLUETOOTH_SCO` / `DTMF` / `TTS` / `ASSISTANT` / `CALL_ASSISTANT` | 0..15 |

配套把小米引擎音量配置链镜像到 `/vendor/etc`（MTK 默认引擎硬编码读取）：
`audio_policy_engine_configuration.xml`（含 `xi:include`）+ `product_strategies` /
`stream_volumes` / `default_stream_volumes` 三件，以及 `primary_audio_policy_configuration.xml`。

## 3. 音频数据通路（VdacStream 双路）

`VdacStreamOut::transfer()` 把每帧 PCM 同时落两条路径：

1. **VirtualDac**（进程内环形缓冲）—— REMOTE_SUBMIX 捕获端（scrcpy/推流）读回声，不阻塞；
2. **QEMU intel-hda 声卡**（ALSA `/dev/snd/pcmC0D0p`，`AlsaPcm`）—— `write` 按声卡消费节奏
   阻塞，把 AudioFlinger 的 burst 背压锚定到真实声卡时钟，修复音频时钟偏快（1.25x）导致的
   在线播放 2 倍速快进（详见 `build/doc/evidence/issue-video-fast-playback/solution.md`）。

ALSA 打开失败时回退纯 VirtualDac，不阻断播放。硬件音量（`setHwVolume` -> `mHwVolumes`，
`mVolumeMutex` 保护）逐声道线性增益；实际音量由 AudioFlinger 数字域处理（卷组见 §2），
本通路默认 unity 直通。

## 4. host 出声链路（QEMU 侧，非本模块）

guest PCM -> ALSA `/dev/snd/pcmC0D0p` -> QEMU intel-hda -> mixeng -> coreaudio -> host 扬声器。
两处 QEMU 侧关键配置（在 `core/host/daemon/qemu/qemu.go` 与 `qemu/source/audio/coreaudio.m`）：

- **`hda-output,...,mixer=off`**：禁用 HDA codec 功放仿真。guest 内核 HDA 驱动会下发
  AC_AMP 静音动词（jack 自动静音/驱动默认），把 mixeng SW voice 音量置 0（`mute=1`）导致
  host 静音；本项目音量已在 Android 数字域处理，codec 功放层多余且有害，故关闭，PCM 以
  unity 直通。
- **coreaudio 用 `DefaultSystemOutputDevice`**（系统扬声器）而非 `DefaultOutputDevice`
  （可能是无声的 AirPlay/蓝牙），避免 guest 音频被路由到无消费设备、ALSA 写阻塞卡死
  AudioFlinger。

## 5. 依赖（deps.py）

**链接期 ABI 运行库**沿用仓库共享的 `build/vendor/build/deps.py` 研磨逻辑，来源全部为
note13 自己的 lineage-22.0 super（vendor_a 解包 `/system_a.img`），组装到
`out/dist/vendor-compile-dep/`。本模块只链接 `libc++.so / libbinder_ndk.so`（运行期由
note13 guest 自身的 `/vendor/lib64 + /system/lib64` 提供）。

**编译期 platform-source 自包含**：构建期只需要 AOSP 三方源码的头（libfmq 模板 / cutils /
utils / libbase / libsystem）。本机有现成 `android-15.0.0_r36` 快照
（`out/tmp/media-v412-work/platform-source`）时**一次性真实复制**（`cp -RP`，保留源内软链
本体避免悬空链报错）所需子目录到本模块自有 `out/tmp/audio-ndk-work/platform-source`；
复制完成后与源无关、自包含，**不产生任何软链，也不在构建期引用其他模块**。快照缺失时才
独立 git 拉取（partial clone 弱网下不可靠，故优先复用快照）。

## 6. 构建期依赖（deps.py，带缓存离线复用）

| 依赖 | 来源 | 缓存标记 |
|---|---|---|
| `aidl/aosp` 展开源 | 官方 `aidl_api` 快照（6 硬件包 + frameworks 类型，各含版本目录 + `.hash`）；`.gitignore` 排除，随构建机固化 | `.hash` 非空 |
| AOSP platform 头 | 本机 `media-v412` 快照 `cp -RP` 复制（缺失才 git 拉取，tag `android-15.0.0_r36`） | `.platform-complete` |
| ABI 运行库 | note13 lineage-22.0 super（共享 deps 研磨） | 共享 deps 标记 |
| AIDL NDK 绑定 | 构建期用 `aidl/aosp` + SDK `aidl` 生成 | — |

`aidl/aosp` 6 个版本化硬件包（`AIDL_PACKAGES`，codegen 用其版本号 + `.hash`）：
`android.hardware.common`(v2)、`android.hardware.common.fmq`(v1)、
`android.hardware.audio.common`(v2)、`android.hardware.audio.core`(v2)、
`android.hardware.audio.core.sounddose`(v1)、`android.hardware.audio.effect`(v2)；
另含 framework 稳定类型 `android.media.audio.common`（无版本，`frameworks/av/media/audio/aidl`）。

## 7. 构建编排（build.py）

工具链：NDK r29 自带 clang/lld/sysroot/builtins（不依赖宿主 brew llvm），SDK `aidl` 36.1.0，
target `aarch64-linux-android35`（env `CDH_*` 可覆盖）。流程：

1. **AIDL codegen**：逐版本化包读 `.hash`，用 `--version/--hash --stability=vintf
   --min_sdk_version=35` 生成 NDK 绑定（对齐 v412）；framework 稳定类型无版本单独生成。
2. **编译**：生成的绑定 `.cpp` + `src/*.cpp` 全部 `-std=c++17 -fno-exceptions -fno-rtti
   -Wall -Wextra -Werror`；AOSP platform 头走 `-isystem`（避免其自带警告触发 `-Werror`），
   `log/log.h` 用本模块 `platform-shim`（NDK 真 `android/log.h`）。
3. **链接两个 PIE 服务**：共享业务对象（ConfigImpl / ModuleImpl / EffectFactoryImpl /
   VdacStream / VdacCommon / FmqSupport / AlsaPcm）+ 各自 main；`-pie -nostdlib
   -Wl,-e,_start -Wl,--dynamic-linker,/system/bin/linker64`，aarch64 LSE/atomic builtins
   来自 NDK compiler-rt。

`FmqSupport.cpp` 自实现 ashmem / native_handle / EventFlag，替代 `libfmq.so / libcutils.so`，
保持闭包最小。

## 8. 产物

落 `VENDOR_STAGE_DIR`（相对路径即镜像内 `/vendor` 路径）：

```
bin/hw/android.hardware.audio.service-aidl.example          (0755, hal_audio_default_exec)
bin/hw/android.hardware.audio.effect.service-aidl.example   (0755, hal_audio_default_exec)
etc/init/android.hardware.audio.service-aidl.example.rc     (0644, vendor_configs_file)
etc/vintf/manifest/android.hardware.audio.service-aidl.xml  (0644, vendor_configs_file)
etc/primary_audio_policy_configuration.xml                  (0644, vendor_configs_file)
etc/audio_policy_engine_configuration.xml                   (0644, vendor_configs_file)
etc/audio_policy_engine_product_strategies.xml              (同上)
etc/audio_policy_engine_stream_volumes.xml                  (同上)
etc/audio_policy_engine_default_stream_volumes.xml          (同上)
```

## 9. SELinux / baselines

`register_labels()` 把标签登记进 baselines 清单（`vendor.selinux-labels`）：

- 两个 bin -> `u:object_r:hal_audio_default_exec:s0`
- rc / vintf / policy / 引擎配置 xml -> `u:object_r:vendor_configs_file:s0`

## 10. 已知限制

- **`aidl/aosp` 被 `.gitignore` 排除**（与 v412 一致）：随构建机固化，新克隆需先放置该
  AIDL 快照（含各包 `.hash`）才能通过 `deps.py --check`，否则 codegen 缺源。
- **`getEngineConfig` 卷组名/`productStrategies` 强约束**（见 §2）：改名或填 productStrategies
  会分别导致音量组塌缩为 Min/Max=0、或小米闭源扩展崩溃 audioserver。
- **host 出声依赖 QEMU 侧 `mixer=off` + `DefaultSystemOutputDevice`**（见 §4）：二者在
  daemon/QEMU，不在本模块；若 host 无声先核对这两处与 host 系统输出设备选择。
- **`bluetooth.audio` VINTF 未声明**：build.py 头注释曾提及，实际 `VINTF_NAMES` 只写
  `android.hardware.audio.service-aidl.xml`（蓝牙音频走平台既有路径）。
