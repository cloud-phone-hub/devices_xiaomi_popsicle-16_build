/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "ModuleImpl.h"

#include <android/log.h>

#include <cinttypes>

#include "AlsaPcm.h"
#include <aidl/android/hardware/audio/common/SinkMetadata.h>
#include <aidl/android/hardware/audio/common/SourceMetadata.h>
#include <aidl/android/media/audio/common/AudioOutputFlags.h>

// 诊断日志（CDH issue-scrcpy-audio-vdac）：policy 驱动层触达确认。
#define CDH_TAG "cdh_audio"
#define CDH_LOG(...) __android_log_print(ANDROID_LOG_INFO, CDH_TAG, __VA_ARGS__)

using ::aidl::android::hardware::audio::common::SinkMetadata;
using ::aidl::android::hardware::audio::common::SourceMetadata;
using ::aidl::android::hardware::audio::core::AudioPatch;
using ::aidl::android::hardware::audio::core::AudioRoute;
using ::aidl::android::hardware::audio::core::ModuleDebug;
using ::aidl::android::hardware::audio::core::VendorParameter;
using ::aidl::android::media::audio::common::AudioChannelLayout;
using ::aidl::android::media::audio::common::AudioDevice;
using ::aidl::android::media::audio::common::AudioDeviceAddress;
using ::aidl::android::media::audio::common::AudioDeviceDescription;
using ::aidl::android::media::audio::common::AudioDeviceType;
using ::aidl::android::media::audio::common::AudioFormatDescription;
using ::aidl::android::media::audio::common::AudioFormatType;
using ::aidl::android::media::audio::common::AudioGain;
using ::aidl::android::media::audio::common::AudioIoFlags;
using ::aidl::android::media::audio::common::AudioMMapPolicyType;
using ::aidl::android::media::audio::common::AudioMode;
using ::aidl::android::media::audio::common::AudioOutputFlags;
using ::aidl::android::media::audio::common::AudioPort;
using ::aidl::android::media::audio::common::AudioPortConfig;
using ::aidl::android::media::audio::common::AudioPortDeviceExt;
using ::aidl::android::media::audio::common::AudioPortExt;
using ::aidl::android::media::audio::common::AudioPortMixExt;
using ::aidl::android::media::audio::common::AudioProfile;
using ::aidl::android::media::audio::common::AudioEncapsulationType;
using ::aidl::android::media::audio::common::PcmType;

namespace aidl::android::hardware::audio::core {

static AudioFormatDescription makePcm16Format() {
    AudioFormatDescription fmt;
    fmt.type = AudioFormatType::PCM;
    fmt.pcm = PcmType::INT_16_BIT;
    return fmt;
}

// PCM16 / 48kHz / stereo 输出 profile（与 primary policy mixPort 声明全量对齐）
static AudioProfile makeOutProfile() {
    AudioProfile profile;
    profile.name = "pcm16-48k";
    profile.format = makePcm16Format();
    profile.channelMasks = {
            AudioChannelLayout::make<AudioChannelLayout::layoutMask>(
                    AudioChannelLayout::LAYOUT_STEREO),
            AudioChannelLayout::make<AudioChannelLayout::layoutMask>(
                    AudioChannelLayout::LAYOUT_MONO)};
    profile.sampleRates = {8000, 11025, 16000, 32000, 44100, 48000};
    profile.encapsulationType = AudioEncapsulationType::NONE;
    return profile;
}

// PCM16 / 48kHz 输入 profile（mono + stereo，与 primary policy 声明对齐）
static AudioProfile makeInProfile() {
    AudioProfile profile;
    profile.name = "pcm16-48k-in";
    profile.format = makePcm16Format();
    profile.channelMasks = {
            AudioChannelLayout::make<AudioChannelLayout::layoutMask>(
                    AudioChannelLayout::LAYOUT_STEREO),
            AudioChannelLayout::make<AudioChannelLayout::layoutMask>(
                    AudioChannelLayout::LAYOUT_MONO)};
    profile.sampleRates = {8000, 11025, 16000, 32000, 44100, 48000};
    profile.encapsulationType = AudioEncapsulationType::NONE;
    return profile;
}

// PCM16 / 48kHz / stereo 输入 profile（r_submix 模块要求 IN_STEREO）
static AudioProfile makeStereoInProfile() {
    AudioProfile profile = makeInProfile();
    profile.name = "pcm16-48k-stereo-in";
    profile.channelMasks = {
            AudioChannelLayout::make<AudioChannelLayout::layoutMask>(
                    AudioChannelLayout::LAYOUT_STEREO)};
    return profile;
}

// 端口 flags 与 MIUI policy 请求精确对齐（Hal2AidlMapper 按**精确 flags**匹配 mix port）：
//  - 输出 mix 口：output 标签 + PRIMARY（policy 开默认输出请求 output:2 == 1<<PRIMARY）
//  - 输出设备口：output 标签（默认端口判定 port.flags.getTag()==output 才计入）
//  - 输入口：input 标签（默认 AudioIoFlags{} 即 input tag，这里显式化防歧义）
static AudioIoFlags makeOutMixFlags() {
    AudioIoFlags flags;
    flags.set<AudioIoFlags::output>(1 << static_cast<int32_t>(AudioOutputFlags::PRIMARY));
    return flags;
}

static AudioIoFlags makeOutputFlags() {
    AudioIoFlags flags;
    flags.set<AudioIoFlags::output>(0);
    return flags;
}

static AudioIoFlags makeInputFlags() {
    AudioIoFlags flags;
    flags.set<AudioIoFlags::input>(0);
    return flags;
}

static AudioPortMixExt makeMixExt(int32_t handle) {
    AudioPortMixExt ext;
    ext.handle = handle;
    ext.maxOpenStreamCount = 0;  // unlimited
    ext.maxActiveStreamCount = 0;
    ext.recommendedMuteDurationMs = 0;
    return ext;
}

static AudioPortDeviceExt makeDeviceExt(AudioDeviceType type, bool isDefault,
                                        const std::string& address = "") {
    AudioPortDeviceExt ext;
    // AIDL v2 attached 判定：AudioPolicyConfig.cpp 要求设备端口 connection 为空
    // （或 IN_SUBMIX 且 address 非空）才作为 attached 设备置为 available。
    // 此前设 CONNECTION_ANALOG 导致 IN_SUBMIX/IN_DEFAULT 等设备永远不 available，
    // engine 找不到输入设备 -> getInputForAttr -19（-ENODEV）。
    ext.device.type.connection = "";  // 无连接类型 = 内置 attached 设备
    ext.device.type.type = type;
    // Hal2AidlMapper 默认端口判定：port.flags tag==input/output 且
    // deviceExt.flags 带 FLAG_INDEX_DEFAULT_DEVICE。只有 speaker（出）与
    // IN_DEFAULT 口（入）打默认位；其余设备口 flags=0，避免覆盖默认端口 id。
    ext.flags = isDefault ? (1 << AudioPortDeviceExt::FLAG_INDEX_DEFAULT_DEVICE) : 0;
    if (!address.empty()) {
        ext.device.address.set<AudioDeviceAddress::id>(address);
    }
    return ext;
}

ModuleImpl::ModuleImpl(Role role) : mRole(role) {
    // 进程内单例：primary 写入、r_submix 读取同一 VirtualDac。
    static std::shared_ptr<cdh::audio::VirtualDac> sDac =
            std::make_shared<cdh::audio::VirtualDac>();
    mDac = sDac;
    buildConfig();
}

void ModuleImpl::buildConfig() {
    if (mRole == Role::Primary) {
        buildPrimaryConfig();
    } else {
        buildSubmixConfig();
    }
}

void ModuleImpl::buildPrimaryConfig() {
    // 1. out mix 端口（flags= PRIMARY，与 policy 默认输出请求精确匹配）
    AudioPort outMix;
    outMix.id = 1;
    outMix.name = "out";
    outMix.profiles = {makeOutProfile()};
    outMix.flags = makeOutMixFlags();
    outMix.extraAudioDescriptors = {};
    outMix.gains = {};
    outMix.ext.set<AudioPortExt::mix>(makeMixExt(1));

    // 2. speaker 设备端口（默认输出：output 标签 + 默认位）
    AudioPort outSpeaker;
    outSpeaker.id = 2;
    outSpeaker.name = "speaker";
    outSpeaker.profiles = {makeOutProfile()};
    outSpeaker.flags = makeOutputFlags();
    outSpeaker.extraAudioDescriptors = {};
    outSpeaker.gains = {};
    outSpeaker.ext.set<AudioPortExt::device>(makeDeviceExt(AudioDeviceType::OUT_SPEAKER, true));

    // 3. in mix 端口（录音）
    AudioPort inMix;
    inMix.id = 3;
    inMix.name = "in";
    inMix.profiles = {makeInProfile()};
    inMix.flags = makeInputFlags();
    inMix.extraAudioDescriptors = {};
    inMix.gains = {};
    inMix.ext.set<AudioPortExt::mix>(makeMixExt(2));

    // 4. 麦克风设备端口（IN_BUILTIN_MIC 默认地址 bottom，policy augmentData 后按 @:bottom 匹配）
    AudioPort inMic;
    inMic.id = 4;
    inMic.name = "mic";
    inMic.profiles = {makeInProfile()};
    inMic.flags = makeInputFlags();
    inMic.extraAudioDescriptors = {};
    inMic.gains = {};
    inMic.ext.set<AudioPortExt::device>(makeDeviceExt(AudioDeviceType::IN_MICROPHONE, false, "bottom"));

    // 5. in-submix 设备端口（REMOTE_SUBMIX 捕获路由目标；AOSP 默认 address "0"）
    AudioPort inSubmix;
    inSubmix.id = 5;
    inSubmix.name = "in_submix";
    inSubmix.profiles = {makeInProfile()};
    inSubmix.flags = makeInputFlags();
    inSubmix.extraAudioDescriptors = {};
    inSubmix.gains = {};
    inSubmix.ext.set<AudioPortExt::device>(makeDeviceExt(AudioDeviceType::IN_SUBMIX, false, "0"));

    // 6. out-submix 设备端口（virtual DAC 回声对应的播放入口；默认 address "0"）
    AudioPort outSubmix;
    outSubmix.id = 6;
    outSubmix.name = "out_submix";
    outSubmix.profiles = {makeOutProfile()};
    outSubmix.flags = makeOutputFlags();
    outSubmix.extraAudioDescriptors = {};
    outSubmix.gains = {};
    outSubmix.ext.set<AudioPortExt::device>(makeDeviceExt(AudioDeviceType::OUT_SUBMIX, false, "0"));

    // 7. in-default 设备端口（MIUI policy 对 REMOTE_SUBMIX 内部回退的默认输入端；默认输入位）
    AudioPort inDefault;
    inDefault.id = 8;
    inDefault.name = "microphone_1";
    inDefault.profiles = {makeInProfile()};
    inDefault.flags = makeInputFlags();
    inDefault.extraAudioDescriptors = {};
    inDefault.gains = {};
    inDefault.ext.set<AudioPortExt::device>(makeDeviceExt(AudioDeviceType::IN_DEFAULT, true));

    mPorts = {outMix, outSpeaker, inMix, inMic, inSubmix, outSubmix, inDefault};

    // 路由：播放 out->speaker；录音 mic->in；捕获 in-submix->in；播放镜像 out->out-submix
    AudioRoute playRoute;
    playRoute.sourcePortIds = {1};
    playRoute.sinkPortId = 2;
    playRoute.isExclusive = false;
    AudioRoute recRoute;
    recRoute.sourcePortIds = {4};
    recRoute.sinkPortId = 3;
    recRoute.isExclusive = false;
    AudioRoute inSubmixRoute;
    inSubmixRoute.sourcePortIds = {5};
    inSubmixRoute.sinkPortId = 3;
    inSubmixRoute.isExclusive = false;
    AudioRoute outSubmixRoute;
    outSubmixRoute.sourcePortIds = {1};
    outSubmixRoute.sinkPortId = 6;
    outSubmixRoute.isExclusive = false;
    AudioRoute inDefaultRoute;
    inDefaultRoute.sourcePortIds = {8};
    inDefaultRoute.sinkPortId = 3;
    inDefaultRoute.isExclusive = false;
    mRoutes = {playRoute, recRoute, inSubmixRoute, outSubmixRoute, inDefaultRoute};

    mOutMixPortId = 1;
    mInMixPortId = 3;
}

// r_submix 模块拓扑（对齐 MIUI /vendor/etc/r_submix_audio_policy_configuration.xml：
// 48k stereo 16bit，Remote Submix In/Out 设备；输入流读 VirtualDac 回声）。
// 注意：端口 flags 与 policy 一致保持 0（Hal2AidlMapper 按精确 flags 匹配 mix port）。
void ModuleImpl::buildSubmixConfig() {
    AudioPort outMix;  // 10
    outMix.id = 10;
    outMix.name = "r_submix output";
    outMix.profiles = {makeOutProfile()};
    outMix.flags = makeOutputFlags();
    outMix.extraAudioDescriptors = {};
    outMix.gains = {};
    outMix.ext.set<AudioPortExt::mix>(makeMixExt(10));

    AudioPort outDev;  // 11 Remote Submix Out
    outDev.id = 11;
    outDev.name = "Remote Submix Out";
    outDev.profiles = {makeOutProfile()};
    outDev.flags = makeOutputFlags();
    outDev.extraAudioDescriptors = {};
    outDev.gains = {};
    outDev.ext.set<AudioPortExt::device>(makeDeviceExt(AudioDeviceType::OUT_SUBMIX, false, "0"));

    AudioPort inMix;  // 12 r_submix input（policy 声明 IN_STEREO，用立体声 profile）
    inMix.id = 12;
    inMix.name = "r_submix input";
    inMix.profiles = {makeStereoInProfile()};
    inMix.flags = makeInputFlags();
    inMix.extraAudioDescriptors = {};
    inMix.gains = {};
    inMix.ext.set<AudioPortExt::mix>(makeMixExt(12));

    AudioPort inDev;  // 13 Remote Submix In
    inDev.id = 13;
    inDev.name = "Remote Submix In";
    inDev.profiles = {makeStereoInProfile()};
    inDev.flags = makeInputFlags();
    inDev.extraAudioDescriptors = {};
    inDev.gains = {};
    inDev.ext.set<AudioPortExt::device>(makeDeviceExt(AudioDeviceType::IN_SUBMIX, false, "0"));

    mPorts = {outMix, outDev, inMix, inDev};

    AudioRoute outRoute;
    outRoute.sourcePortIds = {10};
    outRoute.sinkPortId = 11;
    outRoute.isExclusive = false;
    AudioRoute inRoute;
    inRoute.sourcePortIds = {13};
    inRoute.sinkPortId = 12;
    inRoute.isExclusive = false;
    mRoutes = {outRoute, inRoute};

    mOutMixPortId = 10;
    mInMixPortId = 12;
}

// ---------------- 配置/元数据 ----------------

ndk::ScopedAStatus ModuleImpl::setModuleDebug(const ModuleDebug&) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getAudioPatches(std::vector<AudioPatch>* out) {
    *out = mPatches;
    return ndk::ScopedAStatus::ok();
}

const AudioPort* ModuleImpl::findPort(int32_t portId) const {
    for (const auto& p : mPorts) {
        if (p.id == portId) return &p;
    }
    return nullptr;
}

ndk::ScopedAStatus ModuleImpl::getAudioPort(int32_t portId, AudioPort* out) {
    std::lock_guard<std::mutex> lock(mMutex);
    const AudioPort* port = findPort(portId);
    if (port == nullptr) {
        return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
    }
    *out = *port;
    CDH_LOG("getAudioPort: %s port=%" PRId32 "(%s)", mRole == Role::Primary ? "primary" : "r_submix",
            portId, out->name.c_str());
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getAudioPortConfigs(std::vector<AudioPortConfig>* out) {
    std::lock_guard<std::mutex> lock(mMutex);
    *out = mPortConfigs;
    CDH_LOG("getAudioPortConfigs: %s config_count=%zu", mRole == Role::Primary ? "primary" : "r_submix",
            mPortConfigs.size());
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getAudioPorts(std::vector<AudioPort>* out) {
    std::lock_guard<std::mutex> lock(mMutex);
    *out = mPorts;
    CDH_LOG("getAudioPorts: role=%s port_count=%zu", mRole == Role::Primary ? "primary" : "r_submix",
            mPorts.size());
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getAudioRoutes(std::vector<AudioRoute>* out) {
    std::lock_guard<std::mutex> lock(mMutex);
    *out = mRoutes;
    CDH_LOG("getAudioRoutes: role=%s route_count=%zu", mRole == Role::Primary ? "primary" : "r_submix",
            mRoutes.size());
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getAudioRoutesForAudioPort(int32_t portId,
                                                         std::vector<AudioRoute>* out) {
    std::lock_guard<std::mutex> lock(mMutex);
    out->clear();
    for (const auto& route : mRoutes) {
        if (route.sinkPortId == portId) {
            out->push_back(route);
            continue;
        }
        for (int32_t src : route.sourcePortIds) {
            if (src == portId) {
                out->push_back(route);
                break;
            }
        }
    }
    CDH_LOG("getAudioRoutesForAudioPort: %s port=%" PRId32 " routes=%zu",
            mRole == Role::Primary ? "primary" : "r_submix", portId, out->size());
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getSupportedPlaybackRateFactors(
        SupportedPlaybackRateFactors* out) {
    out->minSpeed = 0.5f;
    out->maxSpeed = 2.0f;
    out->minPitch = 0.5f;
    out->maxPitch = 2.0f;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getMicrophones(
        std::vector<::aidl::android::media::audio::common::MicrophoneInfo>* out) {
    out->clear();  // 无内置麦克风
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getMmapPolicyInfos(
        ::aidl::android::media::audio::common::AudioMMapPolicyType,
        std::vector<::aidl::android::media::audio::common::AudioMMapPolicyInfo>* out) {
    out->clear();  // 不支持 MMAP
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::supportsVariableLatency(bool* out) {
    *out = false;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getAAudioMixerBurstCount(int32_t*) {
    return ndk::ScopedAStatus::fromExceptionCode(EX_UNSUPPORTED_OPERATION);
}

ndk::ScopedAStatus ModuleImpl::getAAudioHardwareBurstMinUsec(int32_t*) {
    return ndk::ScopedAStatus::fromExceptionCode(EX_UNSUPPORTED_OPERATION);
}

// ---------------- 端口/补丁控制 ----------------

ndk::ScopedAStatus ModuleImpl::connectExternalDevice(const AudioPort&, AudioPort*) {
    // 无动态端口（设备均为静态 profile），不支持外部设备连接。
    return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
}

ndk::ScopedAStatus ModuleImpl::disconnectExternalDevice(int32_t) {
    return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
}

ndk::ScopedAStatus ModuleImpl::prepareToDisconnectExternalDevice(int32_t) {
    return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
}

AudioPortConfig* ModuleImpl::findPortConfig(int32_t id) {
    for (auto& c : mPortConfigs) {
        if (c.id == id) return &c;
    }
    return nullptr;
}

const AudioPortConfig* ModuleImpl::findPortConfig(int32_t id) const {
    for (const auto& c : mPortConfigs) {
        if (c.id == id) return &c;
    }
    return nullptr;
}

ndk::ScopedAStatus ModuleImpl::setAudioPortConfig(const AudioPortConfig& requested,
                                                 AudioPortConfig* suggested, bool* applied) {
    std::lock_guard<std::mutex> lock(mMutex);
    const AudioPort* port = findPort(requested.portId);
    if (port == nullptr) {
        CDH_LOG("setAudioPortConfig: %s REJECTED unknown portId=%" PRId32 " (cfgId=%" PRId32 ")",
                mRole == Role::Primary ? "primary" : "r_submix", requested.portId, requested.id);
        return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
    }
    CDH_LOG("setAudioPortConfig: %s port=%" PRId32 "(%s) cfgId=%" PRId32
            " maxActiveStreamCount=%" PRId32 " sampleRate=%" PRId32,
            mRole == Role::Primary ? "primary" : "r_submix", requested.portId, port->name.c_str(),
            requested.id, requested.ext.getTag() == AudioPortExt::mix
                                 ? requested.ext.get<AudioPortExt::mix>().maxActiveStreamCount
                                 : -1,
            requested.sampleRate.has_value() ? requested.sampleRate.value().value : 0);
    if (requested.id != 0) {
        AudioPortConfig* existing = findPortConfig(requested.id);
        if (existing == nullptr) {
            return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
        }
        *existing = requested;
        *suggested = requested;
        *applied = true;
        return ndk::ScopedAStatus::ok();
    }
    // 新建配置：取端口第一个 profile 兜底填 sampleRate/format/channelMask
    AudioPortConfig cfg = requested;
    cfg.id = mNextPortConfigId++;
    if (!cfg.sampleRate.has_value() && !port->profiles.empty()) {
        cfg.sampleRate = ::aidl::android::media::audio::common::Int{
                port->profiles[0].sampleRates.empty() ? 0
                                                      : port->profiles[0].sampleRates[0]};
    }
    if (!cfg.format.has_value() && !port->profiles.empty()) {
        cfg.format = port->profiles[0].format;
    }
    if (!cfg.channelMask.has_value() && !port->profiles.empty() &&
        !port->profiles[0].channelMasks.empty()) {
        cfg.channelMask = port->profiles[0].channelMasks[0];
    }
    if (!cfg.flags.has_value()) {
        cfg.flags = port->flags;
    }
    if (cfg.ext.getTag() == AudioPortExt::Tag::unspecified) {
        cfg.ext = port->ext;
    }
    mPortConfigs.push_back(cfg);
    *suggested = cfg;
    *applied = true;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::resetAudioPortConfig(int32_t portConfigId) {
    std::lock_guard<std::mutex> lock(mMutex);
    for (auto it = mPortConfigs.begin(); it != mPortConfigs.end(); ++it) {
        if (it->id == portConfigId) {
            mPortConfigs.erase(it);
            return ndk::ScopedAStatus::ok();
        }
    }
    return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
}

ndk::ScopedAStatus ModuleImpl::setAudioPatch(const AudioPatch& requested, AudioPatch* out) {
    std::lock_guard<std::mutex> lock(mMutex);
    // 校验两端 port config 存在
    for (int32_t id : requested.sourcePortConfigIds) {
        if (findPortConfig(id) == nullptr) {
            return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
        }
    }
    for (int32_t id : requested.sinkPortConfigIds) {
        if (findPortConfig(id) == nullptr) {
            return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
        }
    }
    AudioPatch patch = requested;
    if (patch.id != 0) {
        // 更新已存在 patch
        for (auto& p : mPatches) {
            if (p.id == patch.id) {
                p = patch;
                *out = p;
                return ndk::ScopedAStatus::ok();
            }
        }
        return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
    }
    patch.id = mNextPatchId++;
    patch.minimumStreamBufferSizeFrames = 1;
    patch.latenciesMs = std::vector<int32_t>(patch.sinkPortConfigIds.size(), 1);
    mPatches.push_back(patch);
    *out = patch;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::resetAudioPatch(int32_t patchId) {
    std::lock_guard<std::mutex> lock(mMutex);
    for (auto it = mPatches.begin(); it != mPatches.end(); ++it) {
        if (it->id == patchId) {
            mPatches.erase(it);
            return ndk::ScopedAStatus::ok();
        }
    }
    return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
}

// ---------------- 主音量/静音 ----------------

ndk::ScopedAStatus ModuleImpl::getMasterMute(bool* out) {
    *out = mMasterMute;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::setMasterMute(bool mute) {
    mMasterMute = mute;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getMasterVolume(float* out) {
    *out = mMasterVolume;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::setMasterVolume(float volume) {
    if (volume < 0.0f || volume > 1.0f) {
        return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
    }
    mMasterVolume = volume;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getMicMute(bool* out) {
    *out = mMicMute;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::setMicMute(bool mute) {
    mMicMute = mute;
    return ndk::ScopedAStatus::ok();
}

// ---------------- 通知 ----------------

ndk::ScopedAStatus ModuleImpl::updateAudioMode(AudioMode) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::updateScreenRotation(IModule::ScreenRotation) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::updateScreenState(bool) {
    return ndk::ScopedAStatus::ok();
}

// ---------------- 可选子接口 ----------------

ndk::ScopedAStatus ModuleImpl::getTelephony(std::shared_ptr<ITelephony>* _aidl_return) {
    *_aidl_return = nullptr;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getBluetooth(std::shared_ptr<IBluetooth>*) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getBluetoothA2dp(std::shared_ptr<IBluetoothA2dp>*) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getBluetoothLe(std::shared_ptr<IBluetoothLe>*) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getSoundDose(
        std::shared_ptr<sounddose::ISoundDose>*) {
    return ndk::ScopedAStatus::ok();
}

// ---------------- 流（virtual DAC：输出落盘回环、输入回声） ----------------

namespace {

// 输入流请求缓冲最小值（帧），保证 data FMQ 容量不小于一次 burst。
constexpr int32_t kMinInputBufferFrames = 2048;
constexpr int32_t kMaxStreamBufferBytes = 1 << 18;  // 256 KiB
constexpr int32_t kInputFrameSizeBytes = 2;         // PCM16 mono（in profile）
constexpr int32_t kOutputFrameSizeBytes = 4;        // PCM16 stereo（out profile）
constexpr int32_t kNominalLatencyMs = 20;

size_t clampBufferBytes(uint64_t requestedBytes, size_t minBytes) {
    size_t bytes = static_cast<size_t>(requestedBytes);
    if (bytes < minBytes) bytes = minBytes;
    if (bytes > kMaxStreamBufferBytes) bytes = kMaxStreamBufferBytes;
    return bytes;
}

}  // namespace

ndk::ScopedAStatus ModuleImpl::openInputStream(const IModule::OpenInputStreamArguments& args,
                                               IModule::OpenInputStreamReturn* ret) {
    std::lock_guard<std::mutex> lock(mMutex);
    const AudioPortConfig* cfg = findPortConfig(args.portConfigId);
    if (cfg == nullptr) {
        CDH_LOG("openInputStream: %s config NOT FOUND cfgId=%" PRId32,
                mRole == Role::Primary ? "primary" : "r_submix", args.portConfigId);
        return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
    }
    const AudioPort* port = findPort(cfg->portId);
    if (port == nullptr || port->ext.getTag() != AudioPortExt::mix || port->id != mInMixPortId) {
        // 只允许在本模块的 in mix 端口上开输入流。
        CDH_LOG("openInputStream: %s REJECTED portId=%" PRId32 " mixId=%" PRId32
                " cfg=%" PRId32 " wantMix=%" PRId32,
                mRole == Role::Primary ? "primary" : "r_submix",
                (port ? port->id : -1), port ? (port->ext.getTag() == AudioPortExt::mix
                                                ? port->ext.get<AudioPortExt::mix>().handle : -1) : -1,
                args.portConfigId, mInMixPortId);
        return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
    }
    // 帧大小由角色决定：primary mono(2B)、r_submix stereo(4B)。
    const int32_t frameSize = (mRole == Role::Primary) ? kInputFrameSizeBytes
                                                       : kOutputFrameSizeBytes;
    if (mInStream) {
        if (mInStream->channel() != nullptr) {
            CDH_LOG("openInputStream: %s REJECTED stream busy", mRole == Role::Primary ? "primary" : "r_submix");
            return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_STATE);
        }
        mInStream.reset();
    }
    const size_t bufferBytes = clampBufferBytes(
            static_cast<uint64_t>(args.bufferSizeFrames) * frameSize,
            kMinInputBufferFrames * frameSize);
    auto channel = std::make_unique<VdacStreamChannel>(
            bufferBytes, cdh::audio::kSampleRateHz, frameSize, kNominalLatencyMs);
    auto stream = ndk::SharedRefBase::make<VdacStreamIn>(std::move(channel), mDac);
    if (!stream->init()) {
        return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_STATE);
    }
    mInStream = stream;
    CDH_LOG("openInputStream: %s OK cfgId=%" PRId32 " frames=%" PRId64 " buffer=%zu",
            mRole == Role::Primary ? "primary" : "r_submix", args.portConfigId,
            args.bufferSizeFrames, bufferBytes);
    ret->stream = stream;
    ret->desc = StreamDescriptor{};
    stream->channel()->fillDescriptor(&ret->desc);
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::openOutputStream(const IModule::OpenOutputStreamArguments& args,
                                                IModule::OpenOutputStreamReturn* ret) {
    std::lock_guard<std::mutex> lock(mMutex);
    const AudioPortConfig* cfg = findPortConfig(args.portConfigId);
    if (cfg == nullptr) {
        CDH_LOG("openOutputStream: %s config NOT FOUND cfgId=%" PRId32,
                mRole == Role::Primary ? "primary" : "r_submix", args.portConfigId);
        return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
    }
    const AudioPort* port = findPort(cfg->portId);
    if (port == nullptr || port->ext.getTag() != AudioPortExt::mix || port->id != mOutMixPortId) {
        // 只允许在本模块的 out mix 端口上开输出流。
        CDH_LOG("openOutputStream: %s REJECTED portId=%" PRId32 " cfg=%" PRId32 " wantMix=%" PRId32,
                mRole == Role::Primary ? "primary" : "r_submix",
                (port ? port->id : -1), args.portConfigId, mOutMixPortId);
        return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
    }
    if (mOutStream) {
        if (mOutStream->channel() != nullptr) {
            return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_STATE);
        }
        mOutStream.reset();
    }
    const size_t bufferBytes = clampBufferBytes(
            static_cast<uint64_t>(args.bufferSizeFrames) * kOutputFrameSizeBytes,
            kMinInputBufferFrames * kOutputFrameSizeBytes);
    auto channel = std::make_unique<VdacStreamChannel>(
            bufferBytes, cdh::audio::kSampleRateHz, kOutputFrameSizeBytes, kNominalLatencyMs);
    // 对接 QEMU intel-hda 声卡：ALSA write 按声卡消费节奏阻塞，为 AudioFlinger
    // 提供真实时钟背压（修复音频时钟偏快 1.25x -> 在线播放 2 倍速快进）。
    // 设备固定 PCM16 / 48kHz / stereo（VirtualDac 同参）；打开失败回退纯内存
    // VirtualDac（REMOTE_SUBMIX 捕获不受影响），不阻断播放。
    auto alsa = std::make_unique<cdh::audio::AlsaPcm>();
    if (!alsa->open("/dev/snd/pcmC0D0p", cdh::audio::kSampleRateHz,
                    cdh::audio::kChannelCount)) {
        alsa.reset();
    }
    auto stream = ndk::SharedRefBase::make<VdacStreamOut>(std::move(channel), mDac,
                                                          std::move(alsa));
    if (!stream->init()) {
        return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_STATE);
    }
    mOutStream = stream;
    CDH_LOG("openOutputStream: %s OK cfgId=%" PRId32 " frames=%" PRId64 " buffer=%zu",
            mRole == Role::Primary ? "primary" : "r_submix", args.portConfigId,
            args.bufferSizeFrames, bufferBytes);
    ret->stream = stream;
    ret->desc = StreamDescriptor{};
    stream->channel()->fillDescriptor(&ret->desc);
    return ndk::ScopedAStatus::ok();
}

// ---------------- 其它 ----------------

ndk::ScopedAStatus ModuleImpl::generateHwAvSyncId(int32_t* out) {
    *out = mNextHwAvSyncId++;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ModuleImpl::getVendorParameters(const std::vector<std::string>&,
                                                  std::vector<VendorParameter>*) {
    return ndk::ScopedAStatus::fromExceptionCode(EX_UNSUPPORTED_OPERATION);
}

ndk::ScopedAStatus ModuleImpl::setVendorParameters(const std::vector<VendorParameter>&, bool) {
    return ndk::ScopedAStatus::fromExceptionCode(EX_UNSUPPORTED_OPERATION);
}

ndk::ScopedAStatus ModuleImpl::addDeviceEffect(
        int32_t, const std::shared_ptr<::aidl::android::hardware::audio::effect::IEffect>&) {
    return ndk::ScopedAStatus::fromExceptionCode(EX_UNSUPPORTED_OPERATION);
}

ndk::ScopedAStatus ModuleImpl::removeDeviceEffect(
        int32_t, const std::shared_ptr<::aidl::android::hardware::audio::effect::IEffect>&) {
    return ndk::ScopedAStatus::fromExceptionCode(EX_UNSUPPORTED_OPERATION);
}

}  // namespace aidl::android::hardware::audio::core