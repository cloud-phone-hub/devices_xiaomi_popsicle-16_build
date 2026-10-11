// Copyright (c) 2025-2026 fei_cong(https://github.com/feicong/feicong-course)
#pragma once

#include "common/libs/fs/shared_fd.h"
#include "common/libs/security/keymaster_channel.h"

namespace cuttlefish {

class SharedFdKeymasterChannel : public KeymasterChannel {
 public:
    SharedFdKeymasterChannel(SharedFD input, SharedFD output);
    bool SendRequest(AndroidKeymasterCommand command, const keymaster::Serializable& message) override;
    bool SendResponse(AndroidKeymasterCommand command, const keymaster::Serializable& message) override;
    ManagedKeymasterMessage ReceiveMessage() override;

 private:
    bool SendMessage(AndroidKeymasterCommand command, bool response, const keymaster::Serializable& message);
    SharedFD input_;
    SharedFD output_;
};

}  // namespace cuttlefish
