/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * 原生 ALSA PCM 输出（QEMU intel-hda 声卡对接）。
 *
 * 背景：guest 主输出音频此前只写进程内 VirtualDac 环形缓冲（write 永不阻塞），
 * AudioFlinger 混音器得不到真实硬件背压，音频时钟（AudioTrack 位置 vs 墙钟）
 * 偏快 1.25x，在线播放核心以错误时钟驱动 A/V 同步 -> 剧情 2 倍速快进。
 * 本类用内核 ALSA uapi（ioctl + write）直接打开 QEMU intel-hda 的播放节点
 * （/dev/snd/pcmC0D0p），PCM 写入会按 QEMU `-audiodev none` 后端的实时消费
 * 节奏阻塞，从而把 AudioFlinger 的 burst 背压锚定到真实声卡时钟。
 *
 * 不依赖 libtinyalsa：Android vendor 二进制不能链接 /system 的 libtinyalsa.so，
 * 直接用 <sound/asound.h>（NDK sysroot 自带完整 uapi）走 ioctl。
 */

#pragma once

#include <cstddef>
#include <cstdint>

namespace cdh::audio {

class AlsaPcm {
  public:
    AlsaPcm() = default;
    ~AlsaPcm();

    // 打开并配置设备（PCM16 / 指定采样率 / 指定声道）。失败返回 false，
    // 调用方应回退到纯内存 VirtualDac 路径，不阻断音频输出。
    bool open(const char* device, uint32_t sampleRate, uint32_t channels);

    bool isOpen() const { return mFd >= 0; }
    uint32_t sampleRate() const { return mSampleRate; }
    uint32_t channels() const { return mChannels; }
    uint32_t bufferSizeFrames() const { return mBufferSizeFrames; }

    // 阻塞写入 PCM 数据，返回实际写入字节数；0 表示失败（流已坏）。
    // 阻塞节奏由 QEMU 声卡消费速率决定，提供真实时钟背压。
    size_t write(const void* data, size_t bytes);

    // 丢弃未播放数据（standby/flush 用）。
    void drop();
    // 排空缓冲（drain 用）。
    void drain();
    // 关闭设备。
    void close();

  private:
    int mFd = -1;
    uint32_t mSampleRate = 0;
    uint32_t mChannels = 0;
    uint32_t mBufferSizeFrames = 0;
    uint32_t mPeriodSizeFrames = 0;
    bool mRecovered = false;
};

}  // namespace cdh::audio
