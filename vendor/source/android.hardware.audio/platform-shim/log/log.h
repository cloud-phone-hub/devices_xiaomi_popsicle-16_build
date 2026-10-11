/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * 编译期 shim：为 AOSP platform 头（fmq 模板 / utils/Log.h）提供 <log/log.h>。
 * 使用 NDK 真头 <android/log.h>（有 __android_log_print 声明），避免
 * platform-source 的 liblog_stub 顶掉 android/log.h。
 */

#pragma once

#include <cstdio>
#include <cstdlib>

#include <android/log.h>

#ifndef LOG_TAG
#define LOG_TAG ""
#endif

#define IF_ALOG(priority, tag) \
    if (__android_log_print)  /* runtime-dead branch, keeps fmt args alive */
#define ALOGV(...) __android_log_print(ANDROID_LOG_VERBOSE, LOG_TAG, __VA_ARGS__)
#define ALOGD(...) __android_log_print(ANDROID_LOG_DEBUG, LOG_TAG, __VA_ARGS__)
#define ALOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define ALOGW(...) __android_log_print(ANDROID_LOG_WARN, LOG_TAG, __VA_ARGS__)
#define ALOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)
#define LOG_FATAL(...) __android_log_print(ANDROID_LOG_FATAL, LOG_TAG, __VA_ARGS__)
#define LOG_ALWAYS_FATAL LOG_FATAL

#define ALOGD_IF(cond, ...)                 \
    do {                                    \
        if (cond) ALOGD(__VA_ARGS__);       \
    } while (0)
#define ALOGE_IF(cond, ...)                 \
    do {                                    \
        if (cond) ALOGE(__VA_ARGS__);       \
    } while (0)
#define LOG_FATAL_IF(cond, ...)             \
    do {                                    \
        if (cond) LOG_FATAL(__VA_ARGS__);   \
    } while (0)
#define LOG_ALWAYS_FATAL_IF LOG_FATAL_IF
#define ALOG_ASSERT(cond, ...) LOG_FATAL_IF(!(cond), __VA_ARGS__)

inline int android_errorWriteLog(int tag, const char* subTag) {
    (void)tag;
    ALOGE("android_errorWriteLog: %s", subTag);
    return 0;
}