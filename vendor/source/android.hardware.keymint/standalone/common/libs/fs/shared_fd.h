// Copyright (c) 2025-2026 fei_cong(https://github.com/feicong/feicong-course)
#pragma once

#include <fcntl.h>
#include <memory>
#include <string>

namespace cuttlefish {

class SharedFD {
 public:
    class State {
     public:
        explicit State(int fd);
        ~State();
        bool IsOpen() const;
        int SetTerminalRaw();
        std::string StrError() const;
        int Fd() const;

     private:
        int fd_;
        int error_;
    };

    SharedFD() = default;
    explicit SharedFD(std::shared_ptr<State> state);
    static SharedFD Open(const std::string& path, int flags);
    State* operator->() const;

 private:
    std::shared_ptr<State> state_;
};

}  // namespace cuttlefish
