/*
 * Copyright (C) 2008-2015 The Android Open Source Project
 * Copyright (c) 2026 fei_cong(https://github.com/feicong/feicong-course)
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 *  * Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 *  * Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 * "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 * LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 * A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 * OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 * SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 * LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 * DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 * THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

// Dynamic process startup ABI derived from AOSP android-15.0.0_r36 Bionic.

#include <stddef.h>
#include <stdint.h>

typedef void init_func_t(int, char*[], char*[]);
typedef void fini_func_t(void);

typedef struct {
  init_func_t** preinit_array;
  init_func_t** init_array;
  fini_func_t** fini_array;
  size_t preinit_array_count;
  size_t init_array_count;
  size_t fini_array_count;
} structors_array_t;

extern int main(int argc, char** argv, char** env);

extern "C" {
extern void __libc_init(void* raw_args,
                        void (*onexit)(void),
                        int (*slingshot)(int, char**, char**),
                        const structors_array_t* structors) __attribute__((noreturn));
extern fini_func_t* __fini_array_start[];
extern fini_func_t* __fini_array_end[];

static void call_fini_array(void) {
  fini_func_t** array = __fini_array_start;
  size_t count = (size_t)(__fini_array_end - __fini_array_start);
  while (count-- > 0) {
    (*array[count])();
  }
}

static fini_func_t* fini_array_with_sentinels[] = {
    (fini_func_t*)(uintptr_t)-1,
    &call_fini_array,
    (fini_func_t*)0,
};

__attribute__((used)) static void cdh_start_main(void* raw_args) {
  structors_array_t array = {};
  if (__fini_array_end - __fini_array_start > 0) {
    array.fini_array = fini_array_with_sentinels;
  }
  __libc_init(raw_args, NULL, &main, &array);
}

__asm__(".text; .global _start; .type _start,%function; _start:"
        "bti j; mov x29,#0; mov x30,#0; mov x0,sp; b cdh_start_main;"
        ".size _start, .-_start");

#if __ANDROID_API__ >= 29
__asm__(".section .tdata,\"awT\",@progbits\n"
        ".p2align 6\n"
        ".text\n"
        ".reloc 0, R_AARCH64_NONE, .tdata\n");
#endif

__attribute__((visibility("hidden"))) void* __dso_handle = (void*)0;

extern int __cxa_atexit(void (*handler)(void*), void* argument, void* dso);

__attribute__((visibility("hidden"))) void __atexit_handler_wrapper(void* function) {
  if (function != NULL) {
    (*reinterpret_cast<void (*)(void)>(function))();
  }
}

__attribute__((visibility("hidden"))) int atexit(void (*function)(void)) {
  return __cxa_atexit(&__atexit_handler_wrapper, reinterpret_cast<void*>(function), &__dso_handle);
}

extern int __register_atfork(void (*prepare)(void),
                             void (*parent)(void),
                             void (*child)(void),
                             void* dso);

__attribute__((visibility("hidden"))) int pthread_atfork(void (*prepare)(void),
                                                          void (*parent)(void),
                                                          void (*child)(void)) {
  return __register_atfork(prepare, parent, child, &__dso_handle);
}

}  // extern "C"
