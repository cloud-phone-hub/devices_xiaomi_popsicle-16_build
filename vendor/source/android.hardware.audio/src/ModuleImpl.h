/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * IModule 实现：CDH 虚拟音频模块（"primary"）。
 *
 * 语义参照 lineage-22 hardware/interfaces/audio/aidl/default（Module/ModuleStub/
 * StreamStub），但做两处针对性裁剪，使得闭包完全不依赖 libxml2：
 *  1. 不解析 audio_policy_configuration.xml —— 端口/路由均硬编码在 buildConfig()；
 *  2. 不引入 tinyalsa/alsa/usb/bluetooth —— 音频一律进 virtual DAC
 *     （VirtualDac/VdacStream：输出流落私有 ring buffer、输入流读取回声，
 *     满足 REMOTE_SUBMIX 真实音频通路。FMQ 协议自足实现于 FmqSupport.cpp，
 *     不依赖 libfmq.so/libcutils.so）。
 *
 * 端口拓扑（module = primary）：
 *  mix out(1) --route(mix->dev)--> dev speaker(2) / out_submix(6)
 *  mix in (3) <--route(dev->mix)-- dev mic(4) / in_submix(5)
 */

#pragma once

#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include <aidl/android/hardware/audio/core/BnModule.h>

#include "VdacCommon.h"
#include "VdacStream.h"

namespace aidl::android::hardware::audio::core {

class ModuleImpl : public BnModule {
  public:
    // 模块实例角色：primary（默认，speaker/mic）或 r_submix（REMOTE_SUBMIX 捕获）。
    enum class Role { Primary, RemoteSubmix };

    explicit ModuleImpl(Role role = Role::Primary);

    // ---- 配置/元数据 ----
    ndk::ScopedAStatus setModuleDebug(const ModuleDebug& in_debug) override;
    ndk::ScopedAStatus getAudioPatches(std::vector<AudioPatch>* _aidl_return) override;
    ndk::ScopedAStatus getAudioPort(int32_t in_portId,
                                    ::aidl::android::media::audio::common::AudioPort*
                                            _aidl_return) override;
    ndk::ScopedAStatus getAudioPortConfigs(
            std::vector<::aidl::android::media::audio::common::AudioPortConfig>* _aidl_return)
            override;
    ndk::ScopedAStatus getAudioPorts(
            std::vector<::aidl::android::media::audio::common::AudioPort>* _aidl_return) override;
    ndk::ScopedAStatus getAudioRoutes(std::vector<AudioRoute>* _aidl_return) override;
    ndk::ScopedAStatus getAudioRoutesForAudioPort(
            int32_t in_portId, std::vector<AudioRoute>* _aidl_return) override;
    ndk::ScopedAStatus getSupportedPlaybackRateFactors(
            SupportedPlaybackRateFactors* _aidl_return) override;
    ndk::ScopedAStatus getMicrophones(
            std::vector<::aidl::android::media::audio::common::MicrophoneInfo>* _aidl_return)
            override;
    ndk::ScopedAStatus getMmapPolicyInfos(
            ::aidl::android::media::audio::common::AudioMMapPolicyType mmapPolicyType,
            std::vector<::aidl::android::media::audio::common::AudioMMapPolicyInfo>* _aidl_return)
            override;
    ndk::ScopedAStatus supportsVariableLatency(bool* _aidl_return) override;
    ndk::ScopedAStatus getAAudioMixerBurstCount(int32_t* _aidl_return) override;
    ndk::ScopedAStatus getAAudioHardwareBurstMinUsec(int32_t* _aidl_return) override;

    // ---- 端口/补丁控制 ----
    ndk::ScopedAStatus connectExternalDevice(const ::aidl::android::media::audio::common::AudioPort&
                                                     in_templateIdAndAdditionalData,
                                             ::aidl::android::media::audio::common::AudioPort*
                                                     _aidl_return) override;
    ndk::ScopedAStatus disconnectExternalDevice(int32_t in_portId) override;
    ndk::ScopedAStatus prepareToDisconnectExternalDevice(int32_t in_portId) override;
    ndk::ScopedAStatus setAudioPortConfig(
            const ::aidl::android::media::audio::common::AudioPortConfig& in_requested,
            ::aidl::android::media::audio::common::AudioPortConfig* out_suggested,
            bool* _aidl_return) override;
    ndk::ScopedAStatus resetAudioPortConfig(int32_t in_portConfigId) override;
    ndk::ScopedAStatus setAudioPatch(const AudioPatch& in_requested,
                                     AudioPatch* _aidl_return) override;
    ndk::ScopedAStatus resetAudioPatch(int32_t in_patchId) override;

    // ---- 主音量/静音 ----
    ndk::ScopedAStatus getMasterMute(bool* _aidl_return) override;
    ndk::ScopedAStatus setMasterMute(bool in_mute) override;
    ndk::ScopedAStatus getMasterVolume(float* _aidl_return) override;
    ndk::ScopedAStatus setMasterVolume(float in_volume) override;
    ndk::ScopedAStatus getMicMute(bool* _aidl_return) override;
    ndk::ScopedAStatus setMicMute(bool in_mute) override;

    // ---- 通知（信息型，no-op） ----
    ndk::ScopedAStatus updateAudioMode(::aidl::android::media::audio::common::AudioMode in_mode)
            override;
    ndk::ScopedAStatus updateScreenRotation(IModule::ScreenRotation in_rotation) override;
    ndk::ScopedAStatus updateScreenState(bool in_isTurnedOn) override;

    // ---- 可选子接口 ----
    ndk::ScopedAStatus getTelephony(std::shared_ptr<ITelephony>* _aidl_return) override;
    ndk::ScopedAStatus getBluetooth(std::shared_ptr<IBluetooth>* _aidl_return) override;
    ndk::ScopedAStatus getBluetoothA2dp(std::shared_ptr<IBluetoothA2dp>* _aidl_return) override;
    ndk::ScopedAStatus getBluetoothLe(std::shared_ptr<IBluetoothLe>* _aidl_return) override;
    ndk::ScopedAStatus getSoundDose(
            std::shared_ptr<sounddose::ISoundDose>* _aidl_return) override;

    // ---- 流（MVP：暂不支持） ----
    ndk::ScopedAStatus openInputStream(const IModule::OpenInputStreamArguments& in_args,
                                       IModule::OpenInputStreamReturn* _aidl_return) override;
    ndk::ScopedAStatus openOutputStream(const IModule::OpenOutputStreamArguments& in_args,
                                        IModule::OpenOutputStreamReturn* _aidl_return) override;

    // ---- 其它 ----
    ndk::ScopedAStatus generateHwAvSyncId(int32_t* _aidl_return) override;
    ndk::ScopedAStatus getVendorParameters(const std::vector<std::string>& in_ids,
                                           std::vector<VendorParameter>* _aidl_return) override;
    ndk::ScopedAStatus setVendorParameters(const std::vector<VendorParameter>& in_parameters,
                                           bool in_async) override;
    ndk::ScopedAStatus addDeviceEffect(
            int32_t in_portConfigId,
            const std::shared_ptr<::aidl::android::hardware::audio::effect::IEffect>& in_effect)
            override;
    ndk::ScopedAStatus removeDeviceEffect(
            int32_t in_portConfigId,
            const std::shared_ptr<::aidl::android::hardware::audio::effect::IEffect>& in_effect)
            override;

  private:
    void buildConfig();
    void buildPrimaryConfig();
    void buildSubmixConfig();

    const ::aidl::android::media::audio::common::AudioPort* findPort(
            int32_t portId) const;
    ::aidl::android::media::audio::common::AudioPortConfig* findPortConfig(
            int32_t portConfigId);
    const ::aidl::android::media::audio::common::AudioPortConfig* findPortConfig(
            int32_t portConfigId) const;

    std::mutex mMutex;
    bool mMasterMute = false;
    bool mMicMute = false;
    float mMasterVolume = 1.0f;
    int32_t mNextHwAvSyncId = 1;
    int32_t mNextPortConfigId = 1;
    int32_t mNextPatchId = 1;
    std::vector<::aidl::android::media::audio::common::AudioPort> mPorts;
    std::vector<::aidl::android::media::audio::common::AudioPortConfig> mPortConfigs;
    std::vector<AudioRoute> mRoutes;
    std::vector<AudioPatch> mPatches;

    // ---- virtual DAC（虚拟扬声器/麦克风回声） ----
    // MQModuleImpl 实例间共享（primary 写入、r_submix 读取），进程内单例。
    std::shared_ptr<cdh::audio::VirtualDac> mDac;
    std::shared_ptr<VdacStreamOut> mOutStream;
    std::shared_ptr<VdacStreamIn> mInStream;

    Role mRole = Role::Primary;
    int32_t mOutMixPortId = 1;
    int32_t mInMixPortId = 3;
};

}  // namespace aidl::android::hardware::audio::core