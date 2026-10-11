#include <android/binder_ibinder.h>
#include <android/binder_status.h>

extern "C" {
void ABinderProcess_setThreadPoolMaxThreadCount(uint32_t numThreads);
void ABinderProcess_joinThreadPool(void);
binder_status_t AServiceManager_addService(AIBinder *binder,
                                           const char *instance);
}

#include <aidl/android/hardware/vibrator/BnVibrator.h>
#include <aidl/android/hardware/vibrator/BnVibratorManager.h>
#include <aidl/android/hardware/vibrator/Braking.h>
#include <aidl/android/hardware/vibrator/CompositeEffect.h>
#include <aidl/android/hardware/vibrator/CompositePrimitive.h>
#include <aidl/android/hardware/vibrator/Effect.h>
#include <aidl/android/hardware/vibrator/EffectStrength.h>
#include <aidl/android/hardware/vibrator/IVibrator.h>
#include <aidl/android/hardware/vibrator/IVibratorCallback.h>
#include <aidl/android/hardware/vibrator/IVibratorManager.h>
#include <aidl/android/hardware/vibrator/PrimitivePwle.h>

#include <chrono>
#include <memory>
#include <string>
#include <thread>
#include <vector>

using aidl::android::hardware::vibrator::Braking;
using aidl::android::hardware::vibrator::BnVibrator;
using aidl::android::hardware::vibrator::BnVibratorManager;
using aidl::android::hardware::vibrator::CompositeEffect;
using aidl::android::hardware::vibrator::CompositePrimitive;
using aidl::android::hardware::vibrator::Effect;
using aidl::android::hardware::vibrator::EffectStrength;
using aidl::android::hardware::vibrator::IVibrator;
using aidl::android::hardware::vibrator::IVibratorCallback;
using aidl::android::hardware::vibrator::IVibratorManager;
using aidl::android::hardware::vibrator::PrimitivePwle;
using ndk::ScopedAStatus;

static constexpr const char *kVibratorServiceName =
    "android.hardware.vibrator.IVibrator/default";
static constexpr const char *kVibratorManagerServiceName =
    "android.hardware.vibrator.IVibratorManager/default";

#include "vibrator_target.inc"

static ScopedAStatus unsupported() {
  return ScopedAStatus::fromExceptionCode(EX_UNSUPPORTED_OPERATION);
}

static ScopedAStatus badValue() {
  return ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
}

static void
completeCallback(const std::shared_ptr<IVibratorCallback> &callback) {
  if (callback != nullptr) {
    callback->onComplete();
  }
}

static void completeCallbackAfter(
    const std::shared_ptr<IVibratorCallback> &callback, int32_t timeoutMs) {
  if (callback == nullptr) {
    return;
  }
  std::thread([callback, timeoutMs]() {
    std::this_thread::sleep_for(std::chrono::milliseconds(timeoutMs));
    callback->onComplete();
  }).detach();
}

class TargetVibrator : public BnVibrator {
public:
  ScopedAStatus getCapabilities(int32_t *out) override {
    *out = kTargetVibratorCapabilities;
    return ScopedAStatus::ok();
  }

  ScopedAStatus off() override { return ScopedAStatus::ok(); }

  ScopedAStatus on(int32_t timeoutMs,
                   const std::shared_ptr<IVibratorCallback> &callback) override {
    if (timeoutMs <= 0) {
      return badValue();
    }
    if ((kTargetVibratorCapabilities & IVibrator::CAP_ON_CALLBACK) != 0) {
      completeCallbackAfter(callback, timeoutMs);
    }
    return ScopedAStatus::ok();
  }

  ScopedAStatus perform(Effect, EffectStrength,
                        const std::shared_ptr<IVibratorCallback> &callback,
                        int32_t *out) override {
    *out = 0;
    if ((kTargetVibratorCapabilities & IVibrator::CAP_PERFORM_CALLBACK) != 0) {
      completeCallback(callback);
    }
    return ScopedAStatus::ok();
  }

  ScopedAStatus getSupportedEffects(std::vector<Effect> *out) override {
    // 真机（Redmi Note 13 5G）hal VibratorInfo.supportedEffects = CLICK / DOUBLE_CLICK /
    // TICK / HEAVY_CLICK / TEXTURE_TICK，由 config/vibrator.json 生成。
    *out = targetSupportedEffects();
    return ScopedAStatus::ok();
  }

  ScopedAStatus setAmplitude(float amplitude) override {
    if ((kTargetVibratorCapabilities & IVibrator::CAP_AMPLITUDE_CONTROL) == 0) {
      return unsupported();
    }
    if (amplitude <= 0.0f || amplitude > 1.0f) {
      return badValue();
    }
    return ScopedAStatus::ok();
  }

  ScopedAStatus setExternalControl(bool enabled) override {
    return enabled ? unsupported() : ScopedAStatus::ok();
  }

  ScopedAStatus getCompositionDelayMax(int32_t *out) override {
    *out = kTargetCompositionDelayMax;
    return ScopedAStatus::ok();
  }

  ScopedAStatus getCompositionSizeMax(int32_t *out) override {
    *out = kTargetCompositionSizeMax;
    return ScopedAStatus::ok();
  }

  ScopedAStatus
  getSupportedPrimitives(std::vector<CompositePrimitive> *out) override {
    out->clear();
    return ScopedAStatus::ok();
  }

  ScopedAStatus getPrimitiveDuration(CompositePrimitive, int32_t *out) override {
    *out = 0;
    return unsupported();
  }

  ScopedAStatus
  compose(const std::vector<CompositeEffect> &composite,
          const std::shared_ptr<IVibratorCallback> &callback) override {
    if (composite.empty()) {
      return badValue();
    }
    (void)callback;
    return ScopedAStatus::ok();
  }

  ScopedAStatus getSupportedAlwaysOnEffects(std::vector<Effect> *out) override {
    out->clear();
    return ScopedAStatus::ok();
  }

  ScopedAStatus alwaysOnEnable(int32_t, Effect, EffectStrength) override {
    return unsupported();
  }

  ScopedAStatus alwaysOnDisable(int32_t) override { return unsupported(); }

  ScopedAStatus getResonantFrequency(float *out) override {
    *out = 0.0f;
    return unsupported();
  }

  ScopedAStatus getQFactor(float *out) override {
    *out = 0.0f;
    return unsupported();
  }

  ScopedAStatus getFrequencyResolution(float *out) override {
    *out = 0.0f;
    return unsupported();
  }

  ScopedAStatus getFrequencyMinimum(float *out) override {
    *out = 0.0f;
    return unsupported();
  }

  ScopedAStatus getBandwidthAmplitudeMap(std::vector<float> *out) override {
    out->clear();
    return unsupported();
  }

  ScopedAStatus getPwlePrimitiveDurationMax(int32_t *out) override {
    *out = 0;
    return unsupported();
  }

  ScopedAStatus getPwleCompositionSizeMax(int32_t *out) override {
    *out = 0;
    return unsupported();
  }

  ScopedAStatus getSupportedBraking(std::vector<Braking> *out) override {
    out->clear();
    return ScopedAStatus::ok();
  }

  ScopedAStatus
  composePwle(const std::vector<PrimitivePwle> &,
              const std::shared_ptr<IVibratorCallback> &) override {
    return unsupported();
  }
};

class TargetVibratorManager : public BnVibratorManager {
public:
  explicit TargetVibratorManager(std::shared_ptr<IVibrator> vibrator)
      : vibrator_(std::move(vibrator)) {}

  ScopedAStatus getCapabilities(int32_t *out) override {
    *out = kTargetVibratorManagerCapabilities;
    return ScopedAStatus::ok();
  }

  ScopedAStatus getVibratorIds(std::vector<int32_t> *out) override {
    // 真机（Redmi Note 13 5G）VibratorInfo.id = 1 -> 由 config/vibrator.json 生成。
    *out = targetVibratorIds();
    return ScopedAStatus::ok();
  }

  ScopedAStatus getVibrator(int32_t vibratorId,
                            std::shared_ptr<IVibrator> *out) override {
    bool known = false;
    for (const int32_t id : targetVibratorIds()) {
      if (id == vibratorId) {
        known = true;
        break;
      }
    }
    if (!known) {
      return badValue();
    }
    *out = vibrator_;
    return ScopedAStatus::ok();
  }

  ScopedAStatus prepareSynced(const std::vector<int32_t> &vibratorIds) override {
    const std::vector<int32_t> known = targetVibratorIds();
    if (vibratorIds.size() != known.size()) {
      return badValue();
    }
    for (size_t index = 0; index < known.size(); ++index) {
      if (vibratorIds[index] != known[index]) {
        return badValue();
      }
    }
    return ScopedAStatus::ok();
  }

  ScopedAStatus
  triggerSynced(const std::shared_ptr<IVibratorCallback> &callback) override {
    completeCallback(callback);
    return ScopedAStatus::ok();
  }

  ScopedAStatus cancelSynced() override { return ScopedAStatus::ok(); }

private:
  std::shared_ptr<IVibrator> vibrator_;
};

static binder_status_t registerAsService(const std::shared_ptr<ndk::ICInterface> &service,
                                         const char *name) {
  return AServiceManager_addService(service->asBinder().get(), name);
}

int main() {
  ABinderProcess_setThreadPoolMaxThreadCount(2);
  auto vibrator = ndk::SharedRefBase::make<TargetVibrator>();

  if (registerAsService(vibrator, kVibratorServiceName) != STATUS_OK) {
    return 2;
  }
  if (kTargetRegisterVibratorManager) {
    auto manager = ndk::SharedRefBase::make<TargetVibratorManager>(vibrator);
    if (registerAsService(manager, kVibratorManagerServiceName) != STATUS_OK) {
      return 3;
    }
  }

  ABinderProcess_joinThreadPool();
  return 0;
}
