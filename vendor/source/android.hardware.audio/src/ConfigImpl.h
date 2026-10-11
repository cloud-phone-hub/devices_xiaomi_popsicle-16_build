/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * IConfig 实现：返回 CDH 虚机默认的环绕声配置与音频策略引擎配置。
 * 语义参照 lineage-22 hardware/interfaces/audio/aidl/default/Config.cpp，
 * 但不解析任何 XML（不引 libxml2），全部用硬编码默认值。
 */

#pragma once

#include <aidl/android/hardware/audio/core/BnConfig.h>

namespace aidl::android::hardware::audio::core {

class ConfigImpl : public BnConfig {
  public:
    ndk::ScopedAStatus getSurroundSoundConfig(
            SurroundSoundConfig* _aidl_return) override;
    ndk::ScopedAStatus getEngineConfig(
            ::aidl::android::media::audio::common::AudioHalEngineConfig* _aidl_return) override;
};

}  // namespace aidl::android::hardware::audio::core