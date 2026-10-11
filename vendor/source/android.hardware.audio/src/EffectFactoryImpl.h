/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * IEffect Factory 空实现：不提供任何音效，但接口可查询、可注册，
 * 避免 audioserver 在 effect 服务缺失时走错误路径。
 * 语义参照 lineage audio/aidl/default/EffectMain.cpp。
 */

#pragma once

#include <aidl/android/hardware/audio/effect/BnFactory.h>
#include <aidl/android/media/audio/common/AudioUuid.h>

namespace aidl::android::hardware::audio::effect {

class EffectFactoryImpl : public BnFactory {
  public:
    ndk::ScopedAStatus queryEffects(
            const std::optional<::aidl::android::media::audio::common::AudioUuid>& in_type,
            const std::optional<::aidl::android::media::audio::common::AudioUuid>&
                    in_implementation,
            const std::optional<::aidl::android::media::audio::common::AudioUuid>& in_proxy,
            std::vector<Descriptor>* _aidl_return) override;
    ndk::ScopedAStatus queryProcessing(const std::optional<Processing::Type>& in_type,
                                       std::vector<Processing>* _aidl_return) override;
    ndk::ScopedAStatus createEffect(
            const ::aidl::android::media::audio::common::AudioUuid& in_implUuid,
            std::shared_ptr<IEffect>* _aidl_return) override;
    ndk::ScopedAStatus destroyEffect(const std::shared_ptr<IEffect>& in_handle) override;
};

}  // namespace aidl::android::hardware::audio::effect