// Copyright (c) 2025-2026 fei_cong(https://github.com/feicong/feicong-course)

#include <android/api-level.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/system_properties.h>

namespace {

int sdk_version = 0;
int is_pre_release = 0;
pthread_once_t availability_once = PTHREAD_ONCE_INIT;

void readAndroidVersion() {
  char value[PROP_VALUE_MAX];
  sdk_version = __system_property_get("ro.build.version.sdk", value) == 0
                    ? __ANDROID_API_FUTURE__
                    : atoi(value);
  is_pre_release =
      __system_property_get("ro.build.version.codename", value) == 0 ||
      strcmp(value, "REL") != 0;
}

}  // namespace

extern "C" int32_t __isOSVersionAtLeast(int32_t major, int32_t minor,
                                         int32_t subminor) {
  (void)minor;
  (void)subminor;
  pthread_once(&availability_once, readAndroidVersion);
  return sdk_version >= major || is_pre_release;
}
