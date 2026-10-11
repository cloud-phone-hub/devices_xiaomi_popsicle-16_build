/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "EffectFactoryImpl.h"

using ::aidl::android::media::audio::common::AudioUuid;

namespace aidl::android::hardware::audio::effect {

ndk::ScopedAStatus EffectFactoryImpl::queryEffects(
        const std::optional<AudioUuid>&, const std::optional<AudioUuid>&,
        const std::optional<AudioUuid>&, std::vector<Descriptor>* out) {
    out->clear();  // 无内置音效
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus EffectFactoryImpl::queryProcessing(
        const std::optional<Processing::Type>&, std::vector<Processing>* out) {
    out->clear();
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus EffectFactoryImpl::createEffect(const AudioUuid&, std::shared_ptr<IEffect>*) {
    // 无支持的效果实现，任何 UUID 都非法。
    return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
}

ndk::ScopedAStatus EffectFactoryImpl::destroyEffect(const std::shared_ptr<IEffect>&) {
    return ndk::ScopedAStatus::ok();
}

}  // namespace aidl::android::hardware::audio::effect