/*
 * Copyright (C) 2026 cloud-phone-hub
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * audio HAL 服务入口：注册
 *   android.hardware.audio.core.IConfig/default
 *   android.hardware.audio.core.IModule/primary
 * 与 lineage audio/aidl/default/main.cpp 对齐（不注册 MST/MST 无关项），
 * 但模块配置硬编码、不解析任何 XML。
 */

#include <android/binder_manager.h>
#include <android/binder_process.h>
#include <android/log.h>

#include <cstdio>

#include "ConfigImpl.h"
#include "ModuleImpl.h"

using ::aidl::android::hardware::audio::core::ConfigImpl;
using ::aidl::android::hardware::audio::core::IConfig;
using ::aidl::android::hardware::audio::core::IModule;
using ::aidl::android::hardware::audio::core::ModuleImpl;

namespace {
void logRegister(const char* what, binder_status_t status) {
    if (status == STATUS_OK) {
        __android_log_print(ANDROID_LOG_INFO, "cdh_audio", "registered %s", what);
    } else {
        __android_log_print(ANDROID_LOG_ERROR, "cdh_audio", "FAILED to register %s (status=%d)",
                            what, status);
    }
}
}  // namespace

int main() {
    ABinderProcess_setThreadPoolMaxThreadCount(16);

    auto config = ndk::SharedRefBase::make<ConfigImpl>();
    const std::string configFqn = std::string(IConfig::descriptor) + "/default";
    binder_status_t status = AServiceManager_addService(config->asBinder().get(),
                                                        configFqn.c_str());
    logRegister(configFqn.c_str(), status);

    // 主模块：注册 IModule/default + IModule/primary（MIUI AudioPolicyManager
    // 查询 IModule/default；实例共享同一批端口/状态）。
    auto module = ndk::SharedRefBase::make<ModuleImpl>();
    for (const char* instance : {"default", "primary"}) {
        const std::string moduleFqn = std::string(IModule::descriptor) + "/" + instance;
        status = AServiceManager_addService(module->asBinder().get(), moduleFqn.c_str());
        logRegister(moduleFqn.c_str(), status);
    }

    // REMOTE_SUBMIX 捕获实例：MIUI policy 的 r_submix 模块（
    // /vendor/etc/r_submix_audio_policy_configuration.xml）。共享 VirtualDac 回声。
    auto remote = ndk::SharedRefBase::make<ModuleImpl>(ModuleImpl::Role::RemoteSubmix);
    const std::string remoteFqn = std::string(IModule::descriptor) + "/r_submix";
    status = AServiceManager_addService(remote->asBinder().get(), remoteFqn.c_str());
    logRegister(remoteFqn.c_str(), status);

    ABinderProcess_joinThreadPool();
    return 1;  // 不应到达
}