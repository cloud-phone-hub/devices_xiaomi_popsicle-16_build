/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * virtual DAC 流实现（AIDL audio HAL 协议面）：
 *  - VdacStreamChannel：每个流一对 command/reply FMQ + 一个数据 FMQ
 *  - VdacWorker：高优先级命令循环线程（读 command -> 驱动 transfer -> 写 reply），
 *    状态机精简为 STANDBY/IDLE/ACTIVE/PAUSED，兼容 audioflinger 常规调用序列
 *    （start -> burst* -> pause/flush/standby -> close）。
 *  - VdacStreamOut：primary 播放流，burst 的 PCM 落入 VirtualDac（虚拟扬声器）。
 *  - VdacStreamIn：输入流，从 VirtualDac 读取回声数据（REMOTE_SUBMIX/MIC 通路）。
 */

#pragma once

#include <atomic>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include <pthread.h>

#include <fmq/AidlMessageQueue.h>
#include <fmq/EventFlag.h>
#include <utils/Errors.h>

#include <aidl/android/hardware/audio/core/BnStreamCommon.h>
#include <aidl/android/hardware/audio/core/BnStreamIn.h>
#include <aidl/android/hardware/audio/core/BnStreamOut.h>
#include <aidl/android/hardware/audio/core/StreamDescriptor.h>
#include <aidl/android/hardware/common/fmq/SynchronizedReadWrite.h>

#include "AlsaPcm.h"
#include "VdacCommon.h"

namespace aidl::android::hardware::audio::core {

// 单个打开流的数据通道（FMQ 生成方）。
class VdacStreamChannel {
  public:
    using CommandMQ = ::android::AidlMessageQueue<
            StreamDescriptor::Command,
            ::aidl::android::hardware::common::fmq::SynchronizedReadWrite>;
    using ReplyMQ = ::android::AidlMessageQueue<
            StreamDescriptor::Reply,
            ::aidl::android::hardware::common::fmq::SynchronizedReadWrite>;
    using DataMQ = ::android::AidlMessageQueue<
            int8_t, ::aidl::android::hardware::common::fmq::SynchronizedReadWrite>;

    VdacStreamChannel(size_t bufferBytes, int32_t sampleRate, int32_t frameSizeBytes,
                      int32_t nominalLatencyMs);
    ~VdacStreamChannel();

    bool valid() const { return mCommandMQ != nullptr && mReplyMQ != nullptr && mDataMQ != nullptr; }
    void fillDescriptor(StreamDescriptor* desc) const;
    void reset();

    CommandMQ* cmdMQ() const { return mCommandMQ.get(); }
    ReplyMQ* replyMQ() const { return mReplyMQ.get(); }
    DataMQ* dataMQ() const { return mDataMQ.get(); }
    size_t frameSize() const { return mFrameSizeBytes; }
    size_t bufferBytes() const { return mBufferBytes; }
    int32_t sampleRate() const { return mSampleRate; }
    int32_t nominalLatencyMs() const { return mNominalLatencyMs; }

    // 帧计数：worker 线程推进，binder 线程读取。
    std::atomic<int64_t> frames{0};

  private:
    std::unique_ptr<CommandMQ> mCommandMQ;
    std::unique_ptr<ReplyMQ> mReplyMQ;
    std::unique_ptr<DataMQ> mDataMQ;
    size_t mBufferBytes;
    int32_t mSampleRate;
    int32_t mFrameSizeBytes;
    int32_t mNominalLatencyMs;
};

// 驱动接口：由具体流实现，只在 worker 线程被调用。
class VdacDriver {
  public:
    virtual ~VdacDriver() = default;
    virtual ::android::status_t start() = 0;
    // 输出：消费 buffer 中的 frames 帧；输入：向 buffer 填充 frames 帧（不足补零）。
    virtual ::android::status_t transfer(void* buffer, size_t frames, size_t* actualFrames,
                                         int32_t* latencyMs) = 0;
    virtual ::android::status_t pause() = 0;
    virtual ::android::status_t flush() = 0;
    virtual ::android::status_t standby() = 0;
    virtual ::android::status_t drain(StreamDescriptor::DrainMode mode) = 0;
    virtual void shutdown() = 0;
};

// 命令循环线程。
class VdacWorker {
  public:
    VdacWorker(VdacStreamChannel* ch, VdacDriver* driver, bool isInput);
    ~VdacWorker();
    bool start();
    // 唤醒 worker（写退出命令）并等待结束。可重复调用。
    void requestExit();

  private:
    static void* threadMain(void* self);
    void run();
    void handleCommand(const StreamDescriptor::Command& cmd, StreamDescriptor::Reply* reply);

    VdacStreamChannel* mCh;
    VdacDriver* mDriver;
    bool mIsInput;
    pthread_t mThread = 0;
    std::vector<int8_t> mScratch;
    StreamDescriptor::State mState = StreamDescriptor::State::STANDBY;
    volatile bool mExitRequested = false;
};

// ---- IStreamCommon 委托 ----
class VdacStreamOut;
class VdacStreamIn;

class VdacStreamCommon : public BnStreamCommon {
  public:
    explicit VdacStreamCommon(VdacStreamOut* out);
    explicit VdacStreamCommon(VdacStreamIn* in);

    // IStreamCommon
    ndk::ScopedAStatus close() override;
    ndk::ScopedAStatus prepareToClose() override;
    ndk::ScopedAStatus updateHwAvSyncId(int32_t) override;
    ndk::ScopedAStatus getVendorParameters(const std::vector<std::string>&,
                                           std::vector<VendorParameter>* _aidl_return) override;
    ndk::ScopedAStatus setVendorParameters(const std::vector<VendorParameter>&, bool) override;
    ndk::ScopedAStatus addEffect(
            const std::shared_ptr<::aidl::android::hardware::audio::effect::IEffect>&) override;
    ndk::ScopedAStatus removeEffect(
            const std::shared_ptr<::aidl::android::hardware::audio::effect::IEffect>&) override;

  private:
    VdacStreamOut* mOut = nullptr;
    VdacStreamIn* mIn = nullptr;
};

// ---- 输出流（播放 -> VirtualDac + QEMU intel-hda 声卡） ----
class VdacStreamOut : public BnStreamOut, public VdacDriver {
  public:
    VdacStreamOut(std::unique_ptr<VdacStreamChannel> channel,
                  std::shared_ptr<cdh::audio::VirtualDac> dac,
                  std::unique_ptr<cdh::audio::AlsaPcm> alsa = nullptr);
    ~VdacStreamOut() override;

    bool init();
    void closeStream();

    // IStreamOut
    ndk::ScopedAStatus getStreamCommon(std::shared_ptr<IStreamCommon>* _aidl_return) override;
    ndk::ScopedAStatus updateMetadata(
            const ::aidl::android::hardware::audio::common::SourceMetadata&) override;
    ndk::ScopedAStatus updateOffloadMetadata(
            const ::aidl::android::hardware::audio::common::AudioOffloadMetadata&) override;
    ndk::ScopedAStatus getHwVolume(std::vector<float>* _aidl_return) override;
    ndk::ScopedAStatus setHwVolume(const std::vector<float>& in_channelVolumes) override;
    ndk::ScopedAStatus getAudioDescriptionMixLevel(float* _aidl_return) override;
    ndk::ScopedAStatus setAudioDescriptionMixLevel(float) override;
    ndk::ScopedAStatus getDualMonoMode(
            ::aidl::android::media::audio::common::AudioDualMonoMode* _aidl_return) override;
    ndk::ScopedAStatus setDualMonoMode(
            ::aidl::android::media::audio::common::AudioDualMonoMode) override;
    ndk::ScopedAStatus getRecommendedLatencyModes(
            std::vector<::aidl::android::media::audio::common::AudioLatencyMode>*) override;
    ndk::ScopedAStatus setLatencyMode(
            ::aidl::android::media::audio::common::AudioLatencyMode) override;
    ndk::ScopedAStatus getPlaybackRateParameters(
            ::aidl::android::media::audio::common::AudioPlaybackRate*) override;
    ndk::ScopedAStatus setPlaybackRateParameters(
            const ::aidl::android::media::audio::common::AudioPlaybackRate&) override;
    ndk::ScopedAStatus selectPresentation(int32_t, int32_t) override;

    // VdacDriver
    ::android::status_t start() override;
    ::android::status_t transfer(void* buffer, size_t frames, size_t* actualFrames,
                                 int32_t* latencyMs) override;
    ::android::status_t pause() override;
    ::android::status_t flush() override;
    ::android::status_t standby() override;
    ::android::status_t drain(StreamDescriptor::DrainMode) override;
    void shutdown() override;

    VdacStreamChannel* channel() { return mChannel.get(); }

  private:
    std::unique_ptr<VdacStreamChannel> mChannel;
    std::shared_ptr<cdh::audio::VirtualDac> mDac;
    std::unique_ptr<cdh::audio::AlsaPcm> mAlsa;
    std::unique_ptr<VdacWorker> mWorker;
    std::mutex mVolumeMutex;  // 保护 mHwVolumes（binder 写 / worker 读）
    std::vector<float> mHwVolumes;
    std::shared_ptr<VdacStreamCommon> mCommon;
};

// ---- 输入流（读取 VirtualDac 回声） ----
class VdacStreamIn : public BnStreamIn, public VdacDriver {
  public:
    VdacStreamIn(std::unique_ptr<VdacStreamChannel> channel,
                 std::shared_ptr<cdh::audio::VirtualDac> dac);
    ~VdacStreamIn() override;

    bool init();
    void closeStream();

    // IStreamIn
    ndk::ScopedAStatus getStreamCommon(std::shared_ptr<IStreamCommon>* _aidl_return) override;
    ndk::ScopedAStatus getActiveMicrophones(
            std::vector<::aidl::android::media::audio::common::MicrophoneDynamicInfo>*)
            override;
    ndk::ScopedAStatus getMicrophoneDirection(
            IStreamIn::MicrophoneDirection* _aidl_return) override;
    ndk::ScopedAStatus setMicrophoneDirection(IStreamIn::MicrophoneDirection) override;
    ndk::ScopedAStatus getMicrophoneFieldDimension(float* _aidl_return) override;
    ndk::ScopedAStatus setMicrophoneFieldDimension(float) override;
    ndk::ScopedAStatus updateMetadata(
            const ::aidl::android::hardware::audio::common::SinkMetadata&) override;
    ndk::ScopedAStatus getHwGain(std::vector<float>* _aidl_return) override;
    ndk::ScopedAStatus setHwGain(const std::vector<float>& in_channelGains) override;

    // VdacDriver
    ::android::status_t start() override;
    ::android::status_t transfer(void* buffer, size_t frames, size_t* actualFrames,
                                 int32_t* latencyMs) override;
    ::android::status_t pause() override;
    ::android::status_t flush() override;
    ::android::status_t standby() override;
    ::android::status_t drain(StreamDescriptor::DrainMode) override;
    void shutdown() override;

    VdacStreamChannel* channel() { return mChannel.get(); }

  private:
    std::unique_ptr<VdacStreamChannel> mChannel;
    std::shared_ptr<cdh::audio::VirtualDac> mDac;
    std::unique_ptr<VdacWorker> mWorker;
    std::vector<float> mHwGains;
    std::shared_ptr<VdacStreamCommon> mCommon;
};

}  // namespace aidl::android::hardware::audio::core