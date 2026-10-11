/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "VdacCommon.h"

#include <algorithm>
#include <cstring>

namespace cdh::audio {

VirtualDac::VirtualDac() : mBuf(kDacBufferBytes) {}

void VirtualDac::writeLocked(const uint8_t* src, size_t bytes) {
    if (bytes == 0) return;
    // 空间不足时丢弃最旧数据，保证当前写入不阻塞（等价于扬声器消费）。
    // 因调用方已保证 bytes <= 容量，drop 量必然 < mCount。
    while (mCount + bytes > mBuf.size()) {
        const size_t toDrop = mCount + bytes - mBuf.size();
        mHead = (mHead + toDrop) % mBuf.size();
        mCount -= toDrop;
    }

    const size_t first = std::min(bytes, mBuf.size() - mTail);
    std::memcpy(mBuf.data() + mTail, src, first);
    if (first < bytes) {
        std::memcpy(mBuf.data(), src + first, bytes - first);
    }
    mTail = (mTail + bytes) % mBuf.size();
    mCount += bytes;
    mFramesWritten += bytes / kFrameSizeBytes;
}

size_t VirtualDac::write(const void* data, size_t bytes) {
    // 按帧对齐裁剪（HAL 侧总是写整帧）。
    bytes -= bytes % kFrameSizeBytes;
    if (bytes == 0) return 0;
    std::lock_guard<std::mutex> lock(mMutex);
    const size_t capacity = mBuf.size();
    // 若单次写入超过容量，只保留最新 capacity 大小的尾部。
    if (bytes > capacity) {
        data = static_cast<const uint8_t*>(data) + (bytes - capacity);
        bytes = capacity;
    }
    writeLocked(static_cast<const uint8_t*>(data), bytes);
    return bytes;
}

size_t VirtualDac::read(void* out, size_t bytes) {
    bytes -= bytes % kFrameSizeBytes;
    if (bytes == 0) return 0;
    std::lock_guard<std::mutex> lock(mMutex);
    const size_t n = std::min(bytes, mCount);
    if (n == 0) return 0;
    auto* dst = static_cast<uint8_t*>(out);
    const size_t first = std::min(n, mBuf.size() - mHead);
    std::memcpy(dst, mBuf.data() + mHead, first);
    if (first < n) {
        std::memcpy(dst + first, mBuf.data(), n - first);
    }
    mHead = (mHead + n) % mBuf.size();
    mCount -= n;
    return n;
}

size_t VirtualDac::available() const {
    std::lock_guard<std::mutex> lock(mMutex);
    return mCount;
}

void VirtualDac::flush() {
    std::lock_guard<std::mutex> lock(mMutex);
    mHead = mTail = mCount = 0;
}

uint64_t VirtualDac::framesWritten() const {
    std::lock_guard<std::mutex> lock(mMutex);
    return mFramesWritten;
}

}  // namespace cdh::audio