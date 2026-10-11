/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "ConfigImpl.h"

#include <android/log.h>

#include <aidl/android/hardware/audio/core/SurroundSoundConfig.h>
#include <aidl/android/media/audio/common/AudioHalAttributesGroup.h>
#include <aidl/android/media/audio/common/AudioHalProductStrategy.h>
#include <aidl/android/media/audio/common/AudioHalVolumeCurve.h>
#include <aidl/android/media/audio/common/AudioHalVolumeGroup.h>
#include <aidl/android/media/audio/common/AudioProductStrategyType.h>
#include <aidl/android/media/audio/common/AudioStreamType.h>

#include <iterator>
#include <utility>
#include <vector>

using ::aidl::android::hardware::audio::core::SurroundSoundConfig;
using ::aidl::android::media::audio::common::AudioHalAttributesGroup;
using ::aidl::android::media::audio::common::AudioHalEngineConfig;
using ::aidl::android::media::audio::common::AudioHalProductStrategy;
using ::aidl::android::media::audio::common::AudioHalVolumeCurve;
using ::aidl::android::media::audio::common::AudioHalVolumeGroup;
using ::aidl::android::media::audio::common::AudioProductStrategyType;
using ::aidl::android::media::audio::common::AudioStreamType;

namespace aidl::android::hardware::audio::core {

ndk::ScopedAStatus ConfigImpl::getSurroundSoundConfig(SurroundSoundConfig* out) {
    // 无环绕声硬件：返回默认（全空）配置。AudioPolicyManager 初始化期调用一次。
    *out = SurroundSoundConfig{};
    __android_log_print(ANDROID_LOG_INFO, "cdh_audio",
                        "IConfig.getSurroundSoundConfig -> default(empty)");
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus ConfigImpl::getEngineConfig(AudioHalEngineConfig* out) {
    using CurvePoint = AudioHalVolumeCurve::CurvePoint;
    using Cat = AudioHalVolumeCurve::DeviceCategory;
    using Pts = std::vector<std::pair<int8_t, int32_t>>;  // (index 0..100, attenuationMb)

    // 引擎默认策略(gOrderedStrategies)通过 attributesGroup.volumeGroupName 引用卷组，
    // 名字是"AUDIO_STREAM_<X>"（流类型字符串）。若找不到同名卷组，引擎会建一个
    // 空默认卷组(Min/Max=0,无曲线)并打 "No configuration ... using default volume
    // configuration" —— 这就是音量键加不上去(Max=0)的根因。因此卷组名必须精确
    // 匹配流类型字符串，曲线值取自机型 /system_ext 引擎音量配置。

    // 参考曲线（与 audio_policy_engine_default_stream_volumes.xml 一致）
    const Pts kFullScale = {{0, 0}, {100, 0}};
    const Pts kSilent = {{0, -9600}, {100, -9600}};
    const Pts kSystem = {{1, -2400}, {33, -1800}, {66, -1200}, {100, -600}};
    const Pts kMedia = {{1, -5800}, {20, -4000}, {60, -1700}, {100, 0}};
    const Pts kHeadset = {{1, -4950}, {33, -3350}, {66, -1700}, {100, 0}};
    const Pts kSpeaker = {{1, -5800}, {20, -4000}, {60, -1700}, {100, 0}};
    const Pts kSpeakerSystem = {{1, -4680}, {42, -2070}, {85, -540}, {100, 0}};
    const Pts kExtMedia = {{1, -5800}, {20, -4000}, {60, -2100}, {100, -1000}};
    const Pts kHearingAid = {{1, -12700}, {20, -8000}, {60, -4000}, {100, 0}};
    const Pts kNonMutable = {{0, -5800}, {20, -4000}, {60, -1700}, {100, 0}};
    const Pts kNonMutableHeadset = {{0, -4950}, {33, -3350}, {66, -1700}, {100, 0}};
    const Pts kNonMutableSpeaker = {{0, -5800}, {20, -4000}, {60, -1700}, {100, 0}};
    const Pts kNonMutableEarpiece = {{0, -4950}, {33, -3350}, {66, -1700}, {100, 0}};
    const Pts kNonMutableExt = {{0, -5800}, {20, -4000}, {60, -2100}, {100, -1000}};
    const Pts kNonMutableHearingAid = {{0, -12700}, {20, -8000}, {60, -4000}, {100, 0}};
    // 组内内联曲线（stream_volumes.xml 写死而非引用）
    const Pts kCallHeadset = {{0, -4200}, {33, -2800}, {66, -1400}, {100, 0}};
    const Pts kCallSpeaker = {{0, -2400}, {33, -1600}, {66, -800}, {100, 0}};
    const Pts kCallEarpiece = {{0, -2700}, {33, -1800}, {66, -900}, {100, 0}};
    const Pts kScoEarpiece = {{0, -4200}, {33, -2800}, {66, -1400}, {100, 0}};
    const Pts kSystemHeadset = {{1, -3000}, {33, -2600}, {66, -2200}, {100, -1800}};
    const Pts kSystemSpeaker = {{1, -5100}, {57, -2800}, {71, -2500}, {85, -2300}, {100, -2100}};
    const Pts kEnforcedSpeaker = {{1, -3400}, {71, -2400}, {100, -2000}};
    const Pts kDtmfSpeaker = {{1, -4000}, {71, -2400}, {100, -1400}};

    auto makeCurve = [](Cat cat, const Pts& points) {
        AudioHalVolumeCurve curve;
        curve.deviceCategory = cat;
        for (const auto& [idx, att] : points) {
            curve.curvePoints.push_back(CurvePoint{idx, att});
        }
        return curve;
    };
    auto makeGroup = [&makeCurve](const char* name, int min, int max,
                                  const std::vector<std::pair<Cat, const Pts*>>& curves) {
        AudioHalVolumeGroup group;
        group.name = name;
        group.minIndex = min;
        group.maxIndex = max;
        for (const auto& [cat, pts] : curves) {
            group.volumeCurves.push_back(makeCurve(cat, *pts));
        }
        return group;
    };
    using PairCatPts = std::vector<std::pair<Cat, const Pts*>>;
    const auto C5 = [](const Pts* hs, const Pts* spk, const Pts* ep, const Pts* ext, const Pts* ha) {
        return PairCatPts{{Cat::HEADSET, hs}, {Cat::SPEAKER, spk}, {Cat::EARPIECE, ep},
                          {Cat::EXT_MEDIA, ext}, {Cat::HEARING_AID, ha}};
    };

    // 卷组名 = 引擎默认策略引用的流类型字符串（必须精确匹配）
    const AudioHalVolumeGroup voiceCallGroup =
            makeGroup("AUDIO_STREAM_VOICE_CALL", 1, 7,
                      C5(&kCallHeadset, &kCallSpeaker, &kCallEarpiece, &kMedia, &kHearingAid));
    const AudioHalVolumeGroup systemGroup =
            makeGroup("AUDIO_STREAM_SYSTEM", 0, 7,
                      C5(&kSystemHeadset, &kSystemSpeaker, &kSystem, &kExtMedia, &kHearingAid));
    const AudioHalVolumeGroup ringGroup =
            makeGroup("AUDIO_STREAM_RING", 0, 7,
                      C5(&kHeadset, &kSpeaker, &kHeadset, &kExtMedia, &kHearingAid));
    const AudioHalVolumeGroup musicGroup =
            makeGroup("AUDIO_STREAM_MUSIC", 0, 25,
                      C5(&kMedia, &kSpeaker, &kMedia, &kMedia, &kHearingAid));
    const AudioHalVolumeGroup alarmGroup =
            makeGroup("AUDIO_STREAM_ALARM", 1, 7,
                      C5(&kNonMutableHeadset, &kNonMutableSpeaker, &kNonMutableEarpiece,
                         &kNonMutableExt, &kNonMutableHearingAid));
    const AudioHalVolumeGroup notificationGroup =
            makeGroup("AUDIO_STREAM_NOTIFICATION", 0, 7,
                      C5(&kHeadset, &kSpeakerSystem, &kHeadset, &kExtMedia, &kHeadset));
    const AudioHalVolumeGroup bluetoothScoGroup =
            makeGroup("AUDIO_STREAM_BLUETOOTH_SCO", 0, 15,
                      C5(&kCallHeadset, &kCallSpeaker, &kScoEarpiece, &kMedia, &kHearingAid));
    const AudioHalVolumeGroup enforcedAudibleGroup =
            makeGroup("AUDIO_STREAM_ENFORCED_AUDIBLE", 0, 7,
                      C5(&kSystemHeadset, &kEnforcedSpeaker, &kSystem, &kExtMedia, &kHearingAid));
    const AudioHalVolumeGroup dtmfGroup =
            makeGroup("AUDIO_STREAM_DTMF", 0, 15,
                      C5(&kSystemHeadset, &kDtmfSpeaker, &kSystem, &kExtMedia, &kHearingAid));
    const AudioHalVolumeGroup ttsGroup =
            makeGroup("AUDIO_STREAM_TTS", 0, 15,
                      C5(&kSilent, &kFullScale, &kSilent, &kSilent, &kSilent));
    const AudioHalVolumeGroup accessibilityGroup =
            makeGroup("AUDIO_STREAM_ACCESSIBILITY", 1, 15,
                      C5(&kNonMutable, &kNonMutableSpeaker, &kNonMutable,
                         &kNonMutable, &kNonMutableHearingAid));
    const AudioHalVolumeGroup assistantGroup =
            makeGroup("AUDIO_STREAM_ASSISTANT", 0, 15,
                      C5(&kMedia, &kSpeaker, &kMedia, &kMedia, &kHearingAid));
    const AudioHalVolumeGroup callAssistantGroup =
            makeGroup("AUDIO_STREAM_CALL_ASSISTANT", 0, 15,
                      C5(&kMedia, &kSpeaker, &kMedia, &kMedia, &kHearingAid));

    // 默认音频策略引擎：productStrategies 保持空 -> 使用引擎内置默认策略集
    // (gOrderedStrategies，覆盖全部流类型，不会 map::at 崩溃)。
    *out = AudioHalEngineConfig{};
    out->volumeGroups = {voiceCallGroup, systemGroup, ringGroup, musicGroup, alarmGroup,
                         notificationGroup, bluetoothScoGroup, enforcedAudibleGroup,
                         dtmfGroup, ttsGroup, accessibilityGroup, assistantGroup,
                         callAssistantGroup};

    __android_log_print(ANDROID_LOG_INFO, "cdh_audio",
                        "IConfig.getEngineConfig -> strategies=%zu volumeGroups=%zu music[min=%d,max=%d]",
                        out->productStrategies.size(), out->volumeGroups.size(),
                        musicGroup.minIndex, musicGroup.maxIndex);
    return ndk::ScopedAStatus::ok();
}

}  // namespace aidl::android::hardware::audio::core