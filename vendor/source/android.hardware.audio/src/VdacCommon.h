/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * virtual DAC：进程内 PCM 环形缓冲（"虚拟扬声器"）。
 *
 * 语义对应真实器件中的 DAC+扬声器：
 *  - 输出流（AudioFlinger 混音结果）通过 write() 把 PCM 灌进来；
 *  - 无人消费时丢弃最旧帧（等价于扬声器把声音"播掉"，避免阻塞写端）；
 *  - 输入流（REMOTE_SUBMIX / MIC 回声）通过 read() 读到这些 PCM。
 *
 * 固定参数：PCM16 / 48kHz / 立体声（与端口 profile 一致）。
 */

#pragma once

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <vector>

namespace cdh::audio {

// 帧参数（与 ModuleImpl.cpp 端口 profile 一致）
constexpr int kSampleRateHz = 48000;
constexpr int kChannelCount = 2;
constexpr int kFrameSizeBytes = kChannelCount * sizeof(int16_t);  // 4

// 环形缓冲容量：1 MiB ≈ 250ms @48k stereo，足够消费侧调度抖动。
constexpr size_t kDacBufferBytes = 1u << 20;

class VirtualDac {
  public:
    VirtualDac();

    // 输出端写入（阻塞写端：空间不足时丢弃最旧数据腾出空间）。
    // 返回实际写入字节数（正常情况下等于 bytes）。
    size_t write(const void* data, size_t bytes);

    // 消费端读取：至多读取 bytes；不足时返回实际读到的字节数（不补零，
    // 由调用方决定补零策略）。缓冲为空返回 0。
    size_t read(void* out, size_t bytes);

    // 当前可读字节数。
    size_t available() const;

    // 丢弃全部缓冲内容。
    void flush();

    // 累计写入帧数（输出流的 presentation position 用）。
    uint64_t framesWritten() const;

  private:
    void writeLocked(const uint8_t* src, size_t bytes);

    mutable std::mutex mMutex;
    std::vector<uint8_t> mBuf;
    size_t mHead = 0;  // 读位置
    size_t mTail = 0;  // 写位置
    size_t mCount = 0;
    uint64_t mFramesWritten = 0;
};

}  // namespace cdh::audio