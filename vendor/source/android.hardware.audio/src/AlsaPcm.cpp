/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * 原生 ALSA PCM 输出实现：open / hw_params / sw_params / write。
 *
 * 使用内核 ALSA uapi（<sound/asound.h>）而非 libtinyalsa：
 *  - vendor 二进制不能链接 /system 分区 libtinyalsa.so（ld 命名空间隔离）；
 *  - NDK sysroot 自带 asound.h，ioctl 序列与 tinyalsa pcm_open 等价。
 *
 * QEMU intel-hda 播放节点固定 PCM16 / 支持 16000-96000 采样率 / 双声道
 * （guest /proc/asound/card0/codec#0 实测 rates[0x1fc]）。
 */

#include "AlsaPcm.h"

#include <android/log.h>

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <sound/asound.h>
#include <sys/ioctl.h>
#include <unistd.h>

namespace cdh::audio {

namespace {

constexpr char kLogTag[] = "cdh_vdac_alsa";

// snd_mask / snd_interval 辅助（tinyalsa 内联函数等价实现）。
void maskSet(struct snd_mask* m, unsigned int bit) {
    m->bits[bit / 32] |= (1u << (bit % 32));
}

void maskAny(struct snd_mask* m) {
    for (size_t i = 0; i < sizeof(m->bits) / sizeof(m->bits[0]); i++) {
        m->bits[i] = ~0u;
    }
}

void intervalAny(struct snd_interval* i) {
    i->min = 0;
    i->max = 0x7fffffff;
    i->openmin = 0;
    i->openmax = 0;
    i->integer = 0;
    i->empty = 0;
}

void intervalSetExact(struct snd_interval* i, unsigned int value) {
    i->min = value;
    i->max = value;
    i->openmin = 0;
    i->openmax = 0;
    i->integer = 1;
    i->empty = 0;
}

int logIoctlErr(const char* what, int err) {
    __android_log_print(ANDROID_LOG_ERROR, kLogTag, "%s failed: %s (%d)",
                        what, strerror(err), err);
    return err;
}

}  // namespace

AlsaPcm::~AlsaPcm() { close(); }

bool AlsaPcm::open(const char* device, uint32_t sampleRate, uint32_t channels) {
    if (mFd >= 0) close();

    mFd = ::open(device, O_WRONLY | O_CLOEXEC);
    if (mFd < 0) {
        logIoctlErr("open", errno);
        return false;
    }

    // ---- HW_PARAMS：先全量置 any，再覆盖需要的参数 ----
    struct snd_pcm_hw_params params;
    std::memset(&params, 0, sizeof(params));
    // 内核 snd_pcm_hw_params() 强制 rmask=~0U（请求全部参数）后逐一 refine：
    // 未显式设置的 mask 若为全 0（bits 空）会被判为非法约束、未设置的
    // interval 若为 [0,0] 也会 refine 失败 -> ioctl 返回 EINVAL（实测
    // SNDRV_PCM_IOCTL_HW_PARAMS failed: Invalid argument）。正确做法同
    // ALSA lib snd_pcm_hw_params_any()：先对所有 mask/interval 置 any
    // （mask 全 1、interval [0, UINT_MAX]），再覆盖要设置的参数。
    // 注意：mask 不能用"全 1"作为最终值——内核 params_access()/params_format()
    // 取 mask 最低置位位，全 1 会让 ACCESS 选中 0（MMAP_INTERLEAVED）而非
    // RW_INTERLEAVED(3)，后续 write() 因 access 不匹配返回 EINVAL（实测
    // SNDRV_PCM_IOCTL_HW_PARAMS 成功后 write failed: Invalid argument）。
    for (size_t k = 0; k < SNDRV_PCM_HW_PARAM_LAST_MASK -
                             SNDRV_PCM_HW_PARAM_FIRST_MASK + 1; k++) {
        maskAny(&params.masks[k]);
    }
    for (size_t k = 0; k < SNDRV_PCM_HW_PARAM_LAST_INTERVAL -
                             SNDRV_PCM_HW_PARAM_FIRST_INTERVAL + 1; k++) {
        intervalAny(&params.intervals[k]);
    }
    // 关键：access/format/subformat 必须清空 any 后只留目标值，
    // 否则 params_access() 选到 MMAP access 导致 write EINVAL。
    std::memset(&params.masks[SNDRV_PCM_HW_PARAM_ACCESS], 0,
                sizeof(struct snd_mask));
    std::memset(&params.masks[SNDRV_PCM_HW_PARAM_FORMAT], 0,
                sizeof(struct snd_mask));
    std::memset(&params.masks[SNDRV_PCM_HW_PARAM_SUBFORMAT], 0,
                sizeof(struct snd_mask));
    params.rmask = (1u << SNDRV_PCM_HW_PARAM_ACCESS) |
                   (1u << SNDRV_PCM_HW_PARAM_FORMAT) |
                   (1u << SNDRV_PCM_HW_PARAM_SUBFORMAT) |
                   (1u << SNDRV_PCM_HW_PARAM_CHANNELS) |
                   (1u << SNDRV_PCM_HW_PARAM_RATE) |
                   (1u << SNDRV_PCM_HW_PARAM_PERIOD_SIZE) |
                   (1u << SNDRV_PCM_HW_PARAM_BUFFER_SIZE) |
                   (1u << SNDRV_PCM_HW_PARAM_PERIODS);
    params.cmask = 0;
    params.info = 0;
    params.msbits = 0;

    maskSet(&params.masks[SNDRV_PCM_HW_PARAM_ACCESS],
            SNDRV_PCM_ACCESS_RW_INTERLEAVED);
    maskSet(&params.masks[SNDRV_PCM_HW_PARAM_FORMAT], SNDRV_PCM_FORMAT_S16_LE);
    maskSet(&params.masks[SNDRV_PCM_HW_PARAM_SUBFORMAT],
            SNDRV_PCM_SUBFORMAT_STD);
    // intervals 数组按参数偏移（param - FIRST_INTERVAL）索引，同内核 ABI。
    intervalSetExact(&params.intervals[SNDRV_PCM_HW_PARAM_CHANNELS -
                                       SNDRV_PCM_HW_PARAM_FIRST_INTERVAL],
                     channels);
    intervalSetExact(&params.intervals[SNDRV_PCM_HW_PARAM_RATE -
                                       SNDRV_PCM_HW_PARAM_FIRST_INTERVAL],
                     sampleRate);
    // 周期/缓冲/periods：显式给合理值（QEMU hda 约束 period_bytes>=128、
    // periods>=2）。放任内核选值会拿到极端 period=32 帧（0.67ms）导致
    // write 频繁触发且可能 EINVAL；固定 40ms 周期 × 4 = 160ms 缓冲。
    intervalSetExact(&params.intervals[SNDRV_PCM_HW_PARAM_PERIOD_SIZE -
                                       SNDRV_PCM_HW_PARAM_FIRST_INTERVAL],
                     sampleRate / 25);          // 40ms 周期（帧）
    intervalSetExact(&params.intervals[SNDRV_PCM_HW_PARAM_BUFFER_SIZE -
                                       SNDRV_PCM_HW_PARAM_FIRST_INTERVAL],
                     sampleRate / 25 * 4);      // 4 周期 = 160ms 缓冲（帧）
    params.intervals[SNDRV_PCM_HW_PARAM_PERIODS -
                     SNDRV_PCM_HW_PARAM_FIRST_INTERVAL].integer = 1;

    int err = ::ioctl(mFd, SNDRV_PCM_IOCTL_HW_PARAMS, &params);
    if (err < 0) {
        logIoctlErr("SNDRV_PCM_IOCTL_HW_PARAMS", errno);
        close();
        return false;
    }

    mSampleRate = params.intervals[SNDRV_PCM_HW_PARAM_RATE -
                                   SNDRV_PCM_HW_PARAM_FIRST_INTERVAL].max;
    mChannels = params.intervals[SNDRV_PCM_HW_PARAM_CHANNELS -
                                 SNDRV_PCM_HW_PARAM_FIRST_INTERVAL].max;
    mBufferSizeFrames = params.intervals[SNDRV_PCM_HW_PARAM_BUFFER_SIZE -
                                         SNDRV_PCM_HW_PARAM_FIRST_INTERVAL].max;
    mPeriodSizeFrames = params.intervals[SNDRV_PCM_HW_PARAM_PERIOD_SIZE -
                                         SNDRV_PCM_HW_PARAM_FIRST_INTERVAL].max;

    // ---- SW_PARAMS：start 阈值 = 1 帧即开始，avail_min = 周期 ----
    struct snd_pcm_sw_params sw;
    std::memset(&sw, 0, sizeof(sw));
    sw.tstamp_mode = SNDRV_PCM_TSTAMP_NONE;
    sw.period_step = 1;
    sw.avail_min = mPeriodSizeFrames > 0 ? mPeriodSizeFrames : 1;
    // 等 buffer 填充到一半再启动，避免 write 一帧就开播导致
    // AudioFlinger 供给与声卡消费节奏错位（start_threshold=1 实测
    // 频繁 XRUN：声卡 48k 实时消费，burst 供给跟不上 -> EPIPE）。
    sw.start_threshold = (mBufferSizeFrames > 0) ?
                                 mBufferSizeFrames / 2 : 1;
    sw.stop_threshold = mBufferSizeFrames > 0 ? mBufferSizeFrames : 0x7fffffff;
    sw.silence_threshold = 0;
    sw.silence_size = 0;
    sw.boundary = 0x7fffffff;
    sw.proto = SNDRV_PCM_VERSION;

    err = ::ioctl(mFd, SNDRV_PCM_IOCTL_SW_PARAMS, &sw);
    if (err < 0) {
        logIoctlErr("SNDRV_PCM_IOCTL_SW_PARAMS", errno);
        close();
        return false;
    }

    err = ::ioctl(mFd, SNDRV_PCM_IOCTL_PREPARE);
    if (err < 0) {
        logIoctlErr("SNDRV_PCM_IOCTL_PREPARE", errno);
        close();
        return false;
    }

    __android_log_print(ANDROID_LOG_INFO, kLogTag,
                        "opened %s: %u Hz %u ch buffer=%u period=%u frames",
                        device, mSampleRate, mChannels, mBufferSizeFrames,
                        mPeriodSizeFrames);
    return true;
}

size_t AlsaPcm::write(const void* data, size_t bytes) {
    if (mFd < 0) return 0;
    const size_t frameSize = mChannels * 2;  // S16_LE
    if (frameSize == 0) return 0;
    bytes -= bytes % frameSize;
    if (bytes == 0) return 0;

    // 阻塞写：QEMU 声卡按实时速率消费，write 在缓冲满时等待，
    // 从而把 AudioFlinger 的 burst 节奏锚定到真实声卡时钟。
    // ALSA 单次 write 帧数不得超过内核 buffer_size（超限返回 EINVAL），
    // 故按 buffer_size 拆块写。
    size_t total = 0;
    const auto* p = static_cast<const uint8_t*>(data);
    const size_t maxChunk = (mBufferSizeFrames > 0 ? mBufferSizeFrames : 1) *
                            frameSize;
    while (total < bytes) {
        const size_t want = std::min(bytes - total, maxChunk);
        ssize_t n = ::write(mFd, p + total, want);
        if (n > 0) {
            total += static_cast<size_t>(n);
            continue;
        }
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) {
            // XRUN/欠载恢复：QEMU 声卡按实时速率消费，AudioFlinger 供给
            // 偶尔跟不上（buffer 空）时 write 返回 EPIPE(broken pipe)。
            // 恢复序列 DROP + PREPARE（掉进 SETUP 再由下次 write 重启）。
            if (errno == EPIPE) {
                // 只恢复一次，避免死循环刷日志。
                if (!mRecovered) {
                    mRecovered = true;
                    ::ioctl(mFd, SNDRV_PCM_IOCTL_DROP);
                    ::ioctl(mFd, SNDRV_PCM_IOCTL_PREPARE);
                    __android_log_print(ANDROID_LOG_WARN, kLogTag,
                                        "XRUN 恢复（DROP+PREPARE）");
                    continue;
                }
                mRecovered = false;
                return total;
            }
            // 诊断：write 失败时打印流状态与可用帧（帮助定位 EINVAL 根因——
            // 若流处于 SETUP/OPEN 态（未 prepare）或 buffer/period 约束
            // 不满足时 write 返回 EINVAL；XRUN 态返回 EPIPE）。
            mRecovered = false;
            struct snd_pcm_status st;
            std::memset(&st, 0, sizeof(st));
            int sr = ::ioctl(mFd, SNDRV_PCM_IOCTL_STATUS, &st);
            int state = (sr == 0) ? st.state : -1;
            __android_log_print(ANDROID_LOG_ERROR, kLogTag,
                                "write failed: %s (%d) want=%zu state=%d "
                                "appl=%lu hw=%lu",
                                strerror(errno), errno, want, state,
                                (unsigned long)st.appl_ptr,
                                (unsigned long)st.hw_ptr);
            return total;
        }
        break;
    }
    return total;
}

void AlsaPcm::drop() {
    if (mFd < 0) return;
    ::ioctl(mFd, SNDRV_PCM_IOCTL_DROP);
    ::ioctl(mFd, SNDRV_PCM_IOCTL_PREPARE);
}

void AlsaPcm::drain() {
    if (mFd < 0) return;
    ::ioctl(mFd, SNDRV_PCM_IOCTL_DRAIN);
    ::ioctl(mFd, SNDRV_PCM_IOCTL_PREPARE);
}

void AlsaPcm::close() {
    if (mFd < 0) return;
    ::close(mFd);
    mFd = -1;
    mSampleRate = 0;
    mChannels = 0;
    mBufferSizeFrames = 0;
    mPeriodSizeFrames = 0;
    mRecovered = false;
}

}  // namespace cdh::audio
