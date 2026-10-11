/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * virtual DAC 流实现：FMQ 通道 / 命令循环 / common / in / out。
 * 数据流：输出流 write -> VirtualDac；输入流 read <- VirtualDac。
 */

#include "VdacStream.h"

#include <algorithm>
#include <cstring>
#include <ctime>

#include <android/log.h>

using ::aidl::android::hardware::audio::common::AudioOffloadMetadata;
using ::aidl::android::hardware::audio::common::SinkMetadata;
using ::aidl::android::hardware::audio::common::SourceMetadata;
using ::aidl::android::media::audio::common::AudioDualMonoMode;
using ::aidl::android::media::audio::common::AudioLatencyMode;
using ::aidl::android::media::audio::common::AudioPlaybackRate;
using ::aidl::android::media::audio::common::MicrophoneDynamicInfo;

// libfmq 模板 readBlocking/writeBlocking 依赖 libutils 的时钟符号。
// 本服务未链接 libutils.so，这里提供等价的单调时钟实现（全局 ::android 命名空间）。
namespace android {
int64_t elapsedRealtimeNano() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<int64_t>(ts.tv_sec) * 1000000000LL + ts.tv_nsec;
}
}  // namespace android

namespace aidl::android::hardware::audio::core {

namespace {

constexpr char kLogTag[] = "cdh_vdac_stream";

int64_t monoNowNs() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<int64_t>(ts.tv_sec) * 1000000000LL + ts.tv_nsec;
}

StreamDescriptor::Position makePosition(const VdacStreamChannel* ch) {
    StreamDescriptor::Position p;
    p.frames = ch->frames.load();
    p.timeNs = monoNowNs();
    return p;
}

// 填 OK 面板（status/observable/latency/xrunFrames 默认就位）
void populateOkReply(const VdacStreamChannel* ch, StreamDescriptor::Reply* reply) {
    reply->status = STATUS_OK;
    reply->observable = makePosition(ch);
    reply->latencyMs = ch->nominalLatencyMs();
    reply->xrunFrames = 0;
}

// 数据搬运后唤醒对端阻塞的读写（framework 侧 writeBlocking 等 FMQ_NOT_FULL /
// readBlocking 等 FMQ_NOT_EMPTY，靠共享 efword 跨进程唤醒）。
void wakeEfWord(std::atomic<uint32_t>* word, uint32_t bits) {
    if (word == nullptr) return;
    ::android::hardware::EventFlag* ef = nullptr;
    if (::android::hardware::EventFlag::createEventFlag(word, &ef) == ::android::NO_ERROR && ef) {
        ef->wake(bits);
        ::android::hardware::EventFlag::deleteEventFlag(&ef);
    }
}

}  // namespace

// ==================== VdacStreamChannel ====================

VdacStreamChannel::VdacStreamChannel(size_t bufferBytes, int32_t sampleRate,
                                     int32_t frameSizeBytes, int32_t nominalLatencyMs)
    : mBufferBytes(bufferBytes),
      mSampleRate(sampleRate),
      mFrameSizeBytes(frameSizeBytes),
      mNominalLatencyMs(nominalLatencyMs) {
    // configureEventFlagWord=true：描述符携带共享 efword，命令/应答/数据的
    // readBlocking/writeBlocking 才能在 HAL 与 audioserver 之间跨进程阻塞协调。
    // （此前 false + EventFlag 纯桩 -> 工作线程 readBlocking 立即失败退出、
    //   framework 侧 sendCommand 同样失败 -> 音频时钟死掉。）
    mCommandMQ = std::make_unique<CommandMQ>(1, true);
    mReplyMQ = std::make_unique<ReplyMQ>(1, true);
    mDataMQ = std::make_unique<DataMQ>(bufferBytes, true);
}

VdacStreamChannel::~VdacStreamChannel() { reset(); }

void VdacStreamChannel::fillDescriptor(StreamDescriptor* desc) const {
    if (mCommandMQ) desc->command = mCommandMQ->dupeDesc();
    if (mReplyMQ) desc->reply = mReplyMQ->dupeDesc();
    desc->frameSizeBytes = mFrameSizeBytes;
    desc->bufferSizeFrames = mBufferBytes / mFrameSizeBytes;
    if (mDataMQ) {
        desc->audio.set<StreamDescriptor::AudioBuffer::fmq>(mDataMQ->dupeDesc());
    }
}

void VdacStreamChannel::reset() {
    mCommandMQ.reset();
    mReplyMQ.reset();
    mDataMQ.reset();
}

// ==================== VdacWorker ====================

VdacWorker::VdacWorker(VdacStreamChannel* ch, VdacDriver* driver, bool isInput)
    : mCh(ch), mDriver(driver), mIsInput(isInput) {
    mScratch.resize(ch->bufferBytes());
}

VdacWorker::~VdacWorker() { requestExit(); }

void* VdacWorker::threadMain(void* self) {
    static_cast<VdacWorker*>(self)->run();
    return nullptr;
}

bool VdacWorker::start() {
    if (mThread != 0) return false;
    return pthread_create(&mThread, nullptr, VdacWorker::threadMain, this) == 0;
}

void VdacWorker::requestExit() {
    if (mThread == 0) return;
    if (mExitRequested) return;
    mExitRequested = true;
    // 写入退出命令并 wake 命令队列 efword：worker 阻塞在 readBlocking 上，
    // 靠 FUTEX_WAKE 即时解开（真实 EventFlag，见 FmqSupport.cpp）。
    const StreamDescriptor::Command exit =
            StreamDescriptor::Command::make<StreamDescriptor::Command::halReservedExit>(1);
    pthread_t thread = mThread;
    for (int i = 0; i < 400; ++i) {
        if (mCh->cmdMQ() && mCh->cmdMQ()->write(&exit, 1)) {
            wakeEfWord(mCh->cmdMQ()->getEventFlagWord(), 0x2 /* FMQ_NOT_EMPTY */);
            break;
        }
        usleep(5000);
    }
    pthread_join(thread, nullptr);
    mThread = 0;
}

void VdacWorker::run() {
    for (;;) {
        if (mExitRequested) break;
        StreamDescriptor::Command cmd{};
        if (!mCh->cmdMQ()->readBlocking(&cmd, 1)) {
            // 队列损坏/客户端已断开：退出。
            break;
        }
        if (mExitRequested) break;
        const auto tag = cmd.getTag();
        if (tag == StreamDescriptor::Command::halReservedExit) break;

        StreamDescriptor::Reply reply{};
        reply.status = STATUS_BAD_VALUE;
        handleCommand(cmd, &reply);
        reply.state = mState;
        if (!mCh->replyMQ()->writeBlocking(&reply, 1)) {
            break;
        }
    }
    mDriver->shutdown();
}

void VdacWorker::handleCommand(const StreamDescriptor::Command& cmd,
                               StreamDescriptor::Reply* reply) {
    switch (cmd.getTag()) {
        case StreamDescriptor::Command::getStatus:
            populateOkReply(mCh, reply);
            break;
        case StreamDescriptor::Command::start:
            if (mState == StreamDescriptor::State::STANDBY) {
                if (mDriver->start() == ::android::OK) {
                    mState = StreamDescriptor::State::IDLE;
                    populateOkReply(mCh, reply);
                } else {
                    mState = StreamDescriptor::State::ERROR;
                }
            } else {
                reply->status = STATUS_INVALID_OPERATION;
            }
            break;
        case StreamDescriptor::Command::burst: {
            const int32_t clientBytes = cmd.get<StreamDescriptor::Command::burst>();
            if (mState == StreamDescriptor::State::STANDBY ||
                mState == StreamDescriptor::State::ERROR) {
                reply->status = STATUS_INVALID_OPERATION;
                break;
            }
            if (mState == StreamDescriptor::State::IDLE ||
                mState == StreamDescriptor::State::PAUSED) {
                mState = StreamDescriptor::State::ACTIVE;
            }
            if (clientBytes < 0) {
                reply->status = STATUS_BAD_VALUE;
                break;
            }
            // 数据搬运（帧对齐）。
            const size_t frameSize = mCh->frameSize();
            size_t want = static_cast<size_t>(clientBytes);
            want = std::min(want, mScratch.size());
            want -= want % frameSize;
            const size_t frames = want / frameSize;
            size_t actual = 0;
            int32_t latency = mCh->nominalLatencyMs();
            if (mIsInput) {
                // 驱动填数据（不足补零由驱动完成），再写入 data FMQ。
                mDriver->transfer(mScratch.data(), frames, &actual, &latency);
                size_t bytes = actual * frameSize;
                if (bytes > 0) {
                    if (!mCh->dataMQ()->write(mScratch.data(), bytes)) {
                        reply->status = STATUS_NOT_ENOUGH_DATA;
                        reply->latencyMs = latency;
                        break;
                    }
                    wakeEfWord(mCh->dataMQ()->getEventFlagWord(), 0x2 /* FMQ_NOT_EMPTY */);
                    reply->fmqByteCount = static_cast<int32_t>(bytes);
                    mCh->frames += static_cast<int64_t>(actual);
                } else {
                    reply->fmqByteCount = 0;
                }
            } else {
                // 先从 data FMQ 读，再交给驱动（写进 VirtualDac）。
                const size_t avail = mCh->dataMQ()->availableToRead();
                const size_t n = std::min(want, avail);
                if (n > 0) {
                    if (!mCh->dataMQ()->read(mScratch.data(), n)) {
                        reply->status = STATUS_NOT_ENOUGH_DATA;
                        break;
                    }
                    wakeEfWord(mCh->dataMQ()->getEventFlagWord(), 0x1 /* FMQ_NOT_FULL */);
                    const size_t nf = n / frameSize;
                    size_t act = 0;
                    mDriver->transfer(mScratch.data(), nf, &act, &latency);
                    reply->fmqByteCount = static_cast<int32_t>(act * frameSize);
                    mCh->frames += static_cast<int64_t>(act);
                } else {
                    reply->fmqByteCount = 0;
                }
            }
            reply->latencyMs = latency;
            reply->observable = makePosition(mCh);
            reply->status = STATUS_OK;
            break;
        }
        case StreamDescriptor::Command::standby:
            if (mState == StreamDescriptor::State::STANDBY) {
                populateOkReply(mCh, reply);
            } else if (mDriver->standby() == ::android::OK) {
                mState = StreamDescriptor::State::STANDBY;
                populateOkReply(mCh, reply);
            } else {
                mState = StreamDescriptor::State::ERROR;
            }
            break;
        case StreamDescriptor::Command::pause:
            if (mState == StreamDescriptor::State::ACTIVE) {
                if (mDriver->pause() == ::android::OK) {
                    mState = StreamDescriptor::State::PAUSED;
                    populateOkReply(mCh, reply);
                } else {
                    mState = StreamDescriptor::State::ERROR;
                }
            } else {
                reply->status = STATUS_INVALID_OPERATION;
            }
            break;
        case StreamDescriptor::Command::flush:
            if (mState == StreamDescriptor::State::PAUSED) {
                if (mDriver->flush() == ::android::OK) {
                    mState = StreamDescriptor::State::STANDBY;
                    populateOkReply(mCh, reply);
                } else {
                    mState = StreamDescriptor::State::ERROR;
                }
            } else {
                reply->status = STATUS_INVALID_OPERATION;
            }
            break;
        case StreamDescriptor::Command::drain: {
            const auto mode = cmd.get<StreamDescriptor::Command::drain>();
            if (!mIsInput && mState == StreamDescriptor::State::ACTIVE) {
                // 无硬件缓存，drain 立即完成。
                mDriver->drain(mode);
                mState = StreamDescriptor::State::STANDBY;
                populateOkReply(mCh, reply);
            } else {
                reply->status = STATUS_INVALID_OPERATION;
            }
            break;
        }
        case StreamDescriptor::Command::halReservedExit:
            // 已在 run() 处理。
            break;
    }
}

// ==================== VdacStreamCommon ====================

VdacStreamCommon::VdacStreamCommon(VdacStreamOut* out) : mOut(out) {}

VdacStreamCommon::VdacStreamCommon(VdacStreamIn* in) : mIn(in) {}

ndk::ScopedAStatus VdacStreamCommon::close() {
    if (mOut) {
        mOut->closeStream();
    } else if (mIn) {
        mIn->closeStream();
    } else {
        return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_STATE);
    }
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamCommon::prepareToClose() {
    // 无阻塞的 HAL 侧 I/O 线程，无需提前解锁。
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamCommon::updateHwAvSyncId(int32_t) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamCommon::getVendorParameters(
        const std::vector<std::string>&, std::vector<VendorParameter>* _aidl_return) {
    _aidl_return->clear();
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamCommon::setVendorParameters(const std::vector<VendorParameter>&,
                                                         bool) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamCommon::addEffect(
        const std::shared_ptr<::aidl::android::hardware::audio::effect::IEffect>&) {
    return ndk::ScopedAStatus::fromExceptionCode(EX_UNSUPPORTED_OPERATION);
}

ndk::ScopedAStatus VdacStreamCommon::removeEffect(
        const std::shared_ptr<::aidl::android::hardware::audio::effect::IEffect>&) {
    return ndk::ScopedAStatus::fromExceptionCode(EX_UNSUPPORTED_OPERATION);
}

// ==================== VdacStreamOut ====================

namespace {
// helper：请求声道数 = 0 时返回 true（未指定 -> 接受默认）
bool gainLengthOk(size_t requested, size_t channels) {
    return requested == 0 || requested == channels;
}
}  // namespace

VdacStreamOut::VdacStreamOut(std::unique_ptr<VdacStreamChannel> channel,
                             std::shared_ptr<cdh::audio::VirtualDac> dac,
                             std::unique_ptr<cdh::audio::AlsaPcm> alsa)
    : mChannel(std::move(channel)),
      mDac(std::move(dac)),
      mAlsa(std::move(alsa)) {
    mHwVolumes.resize(cdh::audio::kChannelCount, 1.0f);
    mCommon = ndk::SharedRefBase::make<VdacStreamCommon>(this);
}

VdacStreamOut::~VdacStreamOut() { closeStream(); }

bool VdacStreamOut::init() {
    if (mChannel == nullptr || !mChannel->valid()) {
        __android_log_print(ANDROID_LOG_ERROR, kLogTag, "output channel FMQ invalid");
        return false;
    }
    mWorker = std::make_unique<VdacWorker>(mChannel.get(), this, false /*isInput*/);
    if (!mWorker->start()) {
        __android_log_print(ANDROID_LOG_ERROR, kLogTag, "failed to start output worker");
        return false;
    }
    return true;
}

void VdacStreamOut::closeStream() {
    if (mWorker) {
        mWorker->requestExit();
        mWorker.reset();
    }
    if (mAlsa) {
        mAlsa->close();
        mAlsa.reset();
    }
    mChannel.reset();
}

ndk::ScopedAStatus VdacStreamOut::getStreamCommon(std::shared_ptr<IStreamCommon>* _aidl_return) {
    *_aidl_return = mCommon;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamOut::updateMetadata(const SourceMetadata&) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamOut::updateOffloadMetadata(const AudioOffloadMetadata&) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamOut::getHwVolume(std::vector<float>* _aidl_return) {
    *_aidl_return = mHwVolumes;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamOut::setHwVolume(const std::vector<float>& in_channelVolumes) {
    if (!gainLengthOk(in_channelVolumes.size(), mHwVolumes.size())) {
        return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
    }
    if (!in_channelVolumes.empty()) {
        std::lock_guard<std::mutex> lock(mVolumeMutex);
        mHwVolumes = in_channelVolumes;
    }
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamOut::getAudioDescriptionMixLevel(float* _aidl_return) {
    *_aidl_return = 0.0f;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamOut::setAudioDescriptionMixLevel(float) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamOut::getDualMonoMode(AudioDualMonoMode* _aidl_return) {
    *_aidl_return = AudioDualMonoMode::OFF;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamOut::setDualMonoMode(AudioDualMonoMode) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamOut::getRecommendedLatencyModes(
        std::vector<AudioLatencyMode>* _aidl_return) {
    _aidl_return->clear();
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamOut::setLatencyMode(AudioLatencyMode) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamOut::getPlaybackRateParameters(AudioPlaybackRate* _aidl_return) {
    _aidl_return->speed = 1.0f;
    _aidl_return->pitch = 1.0f;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamOut::setPlaybackRateParameters(const AudioPlaybackRate&) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamOut::selectPresentation(int32_t, int32_t) {
    return ndk::ScopedAStatus::ok();
}

::android::status_t VdacStreamOut::start() { return ::android::OK; }

::android::status_t VdacStreamOut::transfer(void* buffer, size_t frames, size_t* actualFrames,
                                            int32_t* latencyMs) {
    if (frames == 0) {
        *actualFrames = 0;
        return ::android::OK;
    }
    const size_t bytes = frames * mChannel->frameSize();
    // 应用硬件音量（线性增益 0..1 逐声道），否则音量键只改 index、响度不变。
    {
        std::vector<float> vols;
        {
            std::lock_guard<std::mutex> lock(mVolumeMutex);
            vols = mHwVolumes;
        }
        const size_t channels = mChannel->frameSize() / 2;  // PCM16 交错
        if (vols.size() == channels) {
            const bool needsScale = std::any_of(vols.begin(), vols.end(),
                                                [](float g) { return g < 1.0f; });
            if (needsScale) {
                auto* pcm = static_cast<int16_t*>(buffer);
                for (size_t f = 0; f < frames; ++f) {
                    for (size_t c = 0; c < channels; ++c) {
                        float v = pcm[f * channels + c] * vols[c];
                        if (v > 32767.0f) v = 32767.0f;
                        if (v < -32768.0f) v = -32768.0f;
                        pcm[f * channels + c] = static_cast<int16_t>(v);
                    }
                }
            }
        }
    }
    // 音频数据同时落两条路径：
    //  1) VirtualDac（进程内环形缓冲）——REMOTE_SUBMIX 捕获端（scrcpy/推流）读回声；
    //  2) QEMU intel-hda 声卡（/dev/snd/pcmC0D0p）——ALSA write 按声卡消费节奏阻塞，
    //     把 AudioFlinger 的 burst 背压锚定到真实声卡时钟，修复音频时钟偏快（1.25x）
    //     导致的在线播放 2 倍速快进。ALSA 打开失败时回退纯 VirtualDac，不阻断播放。
    if (mAlsa && mAlsa->isOpen()) {
        // 先喂 VirtualDac（不阻塞、捕获优先），再阻塞写 ALSA（时钟锚点）。
        mDac->write(buffer, bytes);
        const size_t written = mAlsa->write(buffer, bytes);
        // 声卡实际接受的帧数；若 ALSA 失败（流坏）则按请求帧数回填，避免 AudioFlinger
        // 误判（位置时钟退化为系统时钟，等价回退路径）。
        *actualFrames = (written > 0) ? written / mChannel->frameSize() : frames;
    } else {
        mDac->write(buffer, bytes);
        *actualFrames = frames;
    }
    *latencyMs = mChannel->nominalLatencyMs();
    return ::android::OK;
}

::android::status_t VdacStreamOut::pause() { return ::android::OK; }

::android::status_t VdacStreamOut::flush() {
    if (mAlsa && mAlsa->isOpen()) mAlsa->drop();
    mDac->flush();
    return ::android::OK;
}

::android::status_t VdacStreamOut::standby() {
    if (mAlsa && mAlsa->isOpen()) mAlsa->drop();
    return ::android::OK;
}

::android::status_t VdacStreamOut::drain(StreamDescriptor::DrainMode) {
    if (mAlsa && mAlsa->isOpen()) mAlsa->drain();
    return ::android::OK;
}

void VdacStreamOut::shutdown() {}

// ==================== VdacStreamIn ====================

VdacStreamIn::VdacStreamIn(std::unique_ptr<VdacStreamChannel> channel,
                           std::shared_ptr<cdh::audio::VirtualDac> dac)
    : mChannel(std::move(channel)), mDac(std::move(dac)) {
    mHwGains.resize(1, 1.0f);  // mono 输入
    mCommon = ndk::SharedRefBase::make<VdacStreamCommon>(this);
}

VdacStreamIn::~VdacStreamIn() { closeStream(); }

bool VdacStreamIn::init() {
    if (mChannel == nullptr || !mChannel->valid()) {
        __android_log_print(ANDROID_LOG_ERROR, kLogTag, "input channel FMQ invalid");
        return false;
    }
    mWorker = std::make_unique<VdacWorker>(mChannel.get(), this, true /*isInput*/);
    if (!mWorker->start()) {
        __android_log_print(ANDROID_LOG_ERROR, kLogTag, "failed to start input worker");
        return false;
    }
    return true;
}

void VdacStreamIn::closeStream() {
    if (mWorker) {
        mWorker->requestExit();
        mWorker.reset();
    }
    mChannel.reset();
}

ndk::ScopedAStatus VdacStreamIn::getStreamCommon(std::shared_ptr<IStreamCommon>* _aidl_return) {
    *_aidl_return = mCommon;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamIn::getActiveMicrophones(
        std::vector<MicrophoneDynamicInfo>* _aidl_return) {
    _aidl_return->clear();
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamIn::getMicrophoneDirection(
        IStreamIn::MicrophoneDirection* _aidl_return) {
    *_aidl_return = IStreamIn::MicrophoneDirection::UNSPECIFIED;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamIn::setMicrophoneDirection(IStreamIn::MicrophoneDirection) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamIn::getMicrophoneFieldDimension(float* _aidl_return) {
    *_aidl_return = 0.0f;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamIn::setMicrophoneFieldDimension(float) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamIn::updateMetadata(const SinkMetadata&) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamIn::getHwGain(std::vector<float>* _aidl_return) {
    *_aidl_return = mHwGains;
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus VdacStreamIn::setHwGain(const std::vector<float>& in_channelGains) {
    if (!gainLengthOk(in_channelGains.size(), mHwGains.size())) {
        return ndk::ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
    }
    if (!in_channelGains.empty()) mHwGains = in_channelGains;
    return ndk::ScopedAStatus::ok();
}

::android::status_t VdacStreamIn::start() { return ::android::OK; }

::android::status_t VdacStreamIn::transfer(void* buffer, size_t frames, size_t* actualFrames,
                                           int32_t* latencyMs) {
    const size_t frameSize = mChannel->frameSize();
    const size_t bytes = frames * frameSize;
    if (bytes == 0) {
        *actualFrames = 0;
        return ::android::OK;
    }
    // 只返回实际读到的帧数，缓冲不足不补零：AudioFlinger 对 0 帧自行补静音。
    // 此前补零返回满帧会让录音 resampler 输入持续溢出（Overrun 刷屏）。
    const size_t n = mDac->read(buffer, bytes);
    *actualFrames = n / frameSize;
    *latencyMs = mChannel->nominalLatencyMs();
    return ::android::OK;
}

::android::status_t VdacStreamIn::pause() { return ::android::OK; }

::android::status_t VdacStreamIn::flush() {
    mDac->flush();
    return ::android::OK;
}

::android::status_t VdacStreamIn::standby() { return ::android::OK; }

::android::status_t VdacStreamIn::drain(StreamDescriptor::DrainMode) {
    return STATUS_INVALID_OPERATION;
}

void VdacStreamIn::shutdown() {}

}  // namespace aidl::android::hardware::audio::core