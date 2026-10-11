/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * FMQ 支撑实现（自足，避免链接 libfmq.so / libcutils.so）：
 *  1. ashmem_create_region / ashmem_set_prot_region —— MessageQueueBase 分配共享内存用；
 *  2. native_handle_create/close/delete/clone —— MQDescriptor 的 fd 容器；
 *  3. android::hardware::details::{logError,check,errorWriteLog}；
 *  4. android::hardware::EventFlag —— 真实 futex 实现（对齐 libfmq EventFlag.cpp）。
 *
 * 背景（issue-video-fast-playback 复发修复，2026-09-16）：
 * AIDL audio HAL 的流协议靠 FMQ 的 eventflag 字做跨进程阻塞同步
 * （command/reply 队列的 readBlocking/writeBlocking）。此前本文件是***纯桩***
 * （wait 恒 TIMED_OUT、wake 空操作），加上 VdacStreamChannel 构造 FMQ 时
 * configureEventFlagWord=false，导致：
 *   * 工作线程 cmdMQ()->readBlocking() 立即失败 -> 线程退出，START/BURST 命令无人消费
 *   * 流描述符不带 efword -> framework 侧 sendCommand 的 writeBlocking/readBlocking 同样失败
 * -> AudioFlinger mixer 拿不到流推进 -> Frames written 恒 0 -> 播放器无音频时钟
 * -> 在线播放核心失同步 runaway（视频倍速 + 花屏）。
 * 本实现与 libfmq 行为一致：wake 置位 + FUTEX_WAKE_BITSET，wait 用
 * FUTEX_WAIT_BITSET + CLOCK_MONOTONIC 绝对超时；跨进程共享同一 efword。
 */

#include <android/log.h>
#include <atomic>
#include <cerrno>
#include <climits>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <linux/ashmem.h>
#include <linux/futex.h>
#include <new>
#include <string>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#include <fmq/EventFlag.h>

// native_handle_t：ABI 与 libcutils/bionic 一致（NDK 无该头，自研定义）。
#include <sys/types.h>
#define NATIVE_HANDLE_VERSION 1
typedef struct native_handle {
    int version;  /* sizeof(native_handle_t) */
    int numFds;   /* number of file-descriptors at &data[0] */
    int numInts;  /* number of ints at &data[numFds] */
    int data[0];  /* numFds + numInts ints */
} native_handle_t;

// VdacStream.cpp 提供的单调时钟（libfmq 模板 + EventFlag 依赖），前置声明。
namespace android {
int64_t elapsedRealtimeNano();
}  // namespace android

// ---------------- ashmem（libcutils 等价物） ----------------

extern "C" int ashmem_create_region(const char* name, size_t size) {
    int fd = open("/dev/ashmem", O_RDWR | O_CLOEXEC);
    if (fd < 0) return -1;
    if (name != nullptr) {
        (void)ioctl(fd, ASHMEM_SET_NAME, name);
    }
    if (ioctl(fd, ASHMEM_SET_SIZE, size) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

extern "C" int ashmem_set_prot_region(int fd, int prot) {
    return ioctl(fd, ASHMEM_SET_PROT_MASK, static_cast<unsigned long>(prot)) < 0 ? -1 : 0;
}

// ---------------- native_handle（libcutils 等价物） ----------------

extern "C" native_handle_t* native_handle_create(int numFds, int numInts) {
    if (numFds < 0 || numInts < 0) return nullptr;
    const size_t handleSize = sizeof(native_handle_t) + (numFds + numInts) * sizeof(int);
    native_handle_t* handle =
            static_cast<native_handle_t*>(malloc(handleSize));  // NOLINT
    if (handle == nullptr) return nullptr;
    handle->version = NATIVE_HANDLE_VERSION;
    handle->numFds = numFds;
    handle->numInts = numInts;
    return handle;
}

extern "C" int native_handle_close(const native_handle_t* handle) {
    if (handle == nullptr) return 0;
    for (int i = 0; i < handle->numFds; ++i) {
        close(handle->data[i]);
    }
    return 0;
}

extern "C" void native_handle_delete(const native_handle_t* handle) {
    if (handle == nullptr) return;
    (void)native_handle_close(handle);
    free(const_cast<native_handle_t*>(handle));  // NOLINT
}

extern "C" native_handle_t* native_handle_clone(const native_handle_t* handle) {
    if (handle == nullptr) return nullptr;
    native_handle_t* clone = native_handle_create(handle->numFds, handle->numInts);
    if (clone == nullptr) return nullptr;
    for (int i = 0; i < handle->numFds; ++i) {
        clone->data[i] = fcntl(handle->data[i], F_DUPFD_CLOEXEC, 0);
    }
    for (int i = 0; i < handle->numInts; ++i) {
        clone->data[handle->numFds + i] = handle->data[handle->numFds + i];
    }
    return clone;
}

// ---------------- hardware::details 支持函数（libfmq 等价物） ----------------

namespace android {
namespace hardware {
namespace details {

void logError(const std::string& message) {
    __android_log_print(ANDROID_LOG_ERROR, "cdh_fmq", "%s", message.c_str());
}

void errorWriteLog(int tag, const char* message) {
    __android_log_print(ANDROID_LOG_WARN, "cdh_fmq", "errorWriteLog(%d): %s", tag, message);
}

void check(bool exp, const char* message) {
    if (!exp) logError(message);
}

}  // namespace details

// ---------------- EventFlag（futex 实现，对齐 libfmq EventFlag.cpp） ----------------

status_t EventFlag::createEventFlag(std::atomic<uint32_t>* efWordPtr, EventFlag** ef) {
    if (ef == nullptr) return BAD_VALUE;
    status_t status = NO_MEMORY;
    *ef = nullptr;
    auto* evFlag = new (std::nothrow) EventFlag(efWordPtr, &status);
    if (evFlag != nullptr) {
        if (status == NO_ERROR) {
            *ef = evFlag;
        } else {
            delete evFlag;
        }
    }
    return status;
}

status_t EventFlag::deleteEventFlag(EventFlag** ef) {
    if (ef == nullptr || *ef == nullptr) return BAD_VALUE;
    status_t status = unmapEventFlagWord((*ef)->mEfWordPtr, &(*ef)->mEfWordNeedsUnmapping);
    delete *ef;
    *ef = nullptr;
    return status;
}

EventFlag::EventFlag(std::atomic<uint32_t>* efWordPtr, status_t* status)
    : mEfWordNeedsUnmapping(false) {
    if (efWordPtr == nullptr) {
        *status = BAD_VALUE;
    } else {
        *status = NO_ERROR;
        mEfWordPtr = efWordPtr;
    }
}

EventFlag::~EventFlag() {
    (void)unmapEventFlagWord(mEfWordPtr, &mEfWordNeedsUnmapping);
}

status_t EventFlag::wake(uint32_t bitmask) {
    if (bitmask == 0 || mEfWordPtr == nullptr) return NO_ERROR;
    const uint32_t old = std::atomic_fetch_or(mEfWordPtr, bitmask);
    // 仅当有新增置位才 FUTEX_WAKE，避免空唤醒。
    if ((~old & bitmask) != 0) {
        constexpr int kIntMax = INT_MAX;
        int ret = syscall(__NR_futex, mEfWordPtr, FUTEX_WAKE_BITSET, kIntMax, nullptr, nullptr,
                          static_cast<uint32_t>(bitmask));
        if (ret == -1) {
            __android_log_print(ANDROID_LOG_WARN, "cdh_fmq", "EventFlag wake futex failed: %s",
                                strerror(errno));
        }
    }
    return NO_ERROR;
}

status_t EventFlag::waitHelper(uint32_t bitmask, uint32_t* efState, int64_t timeoutNanoSeconds) {
    if (bitmask == 0 || efState == nullptr || mEfWordPtr == nullptr) return BAD_VALUE;
    const uint32_t old = std::atomic_fetch_and(mEfWordPtr, ~bitmask);
    const uint32_t setBits = old & bitmask;
    if (setBits != 0) {
        *efState = setBits;
        return NO_ERROR;
    }
    const uint32_t efWord = old & ~bitmask;
    int ret = 0;
    if (timeoutNanoSeconds > 0) {
        struct timespec waitTimeAbsolute;
        addNanosecondsToCurrentTime(timeoutNanoSeconds, &waitTimeAbsolute);
        ret = syscall(__NR_futex, mEfWordPtr, FUTEX_WAIT_BITSET, efWord, &waitTimeAbsolute, nullptr,
                      static_cast<uint32_t>(bitmask));
    } else {
        ret = syscall(__NR_futex, mEfWordPtr, FUTEX_WAIT_BITSET, efWord, nullptr, nullptr,
                      static_cast<uint32_t>(bitmask));
    }
    if (ret == -1) {
        *efState = 0;
        return -errno;  // -EAGAIN / -ETIMEDOUT / -EINTR
    }
    const uint32_t after = std::atomic_fetch_and(mEfWordPtr, ~bitmask);
    *efState = after & bitmask;
    if (*efState == 0) return -EINTR;  // 虚假唤醒
    return NO_ERROR;
}

status_t EventFlag::wait(uint32_t bitmask, uint32_t* efState, int64_t timeoutNanoSeconds,
                         bool retry) {
    if (!retry) {
        return waitHelper(bitmask, efState, timeoutNanoSeconds);
    }
    bool shouldTimeOut = timeoutNanoSeconds != 0;
    int64_t prevTimeNs = shouldTimeOut ? android::elapsedRealtimeNano() : 0;
    status_t status;
    while (true) {
        status = waitHelper(bitmask, efState, timeoutNanoSeconds);
        if (status != -EAGAIN && status != -EINTR) break;
        if (shouldTimeOut) {
            const int64_t currentTimeNs = android::elapsedRealtimeNano();
            timeoutNanoSeconds -= currentTimeNs - prevTimeNs;
            prevTimeNs = currentTimeNs;
            if (timeoutNanoSeconds <= 0) {
                status = -ETIMEDOUT;
                *efState = 0;
                break;
            }
        }
    }
    return status;
}

status_t EventFlag::unmapEventFlagWord(std::atomic<uint32_t>* efWordPtr,
                                       bool* efWordNeedsUnmapping) {
    // FMQ 的 efword 由 MessageQueueBase 通过 mapGrantorDescr 管理生命周期
    // （mEfWordNeedsUnmapping 恒 false），这里保持与 libfmq 相同的空操作语义。
    (void)efWordPtr;
    (void)efWordNeedsUnmapping;
    return NO_ERROR;
}

void EventFlag::addNanosecondsToCurrentTime(int64_t nanoSeconds, struct timespec* timeAbs) {
    constexpr int64_t kNanosPerSecond = 1000000000LL;
    clock_gettime(CLOCK_MONOTONIC, timeAbs);
    timeAbs->tv_sec += nanoSeconds / kNanosPerSecond;
    timeAbs->tv_nsec += nanoSeconds % kNanosPerSecond;
    if (timeAbs->tv_nsec >= kNanosPerSecond) {
        timeAbs->tv_sec++;
        timeAbs->tv_nsec -= kNanosPerSecond;
    }
}

}  // namespace hardware
}  // namespace android