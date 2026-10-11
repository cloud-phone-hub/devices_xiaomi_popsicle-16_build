// IKeyMintDevice v4 垫片：v4（A16 基线）新增 setAdditionalAttestationInfo(KeyParameter[])，
// 上游 device-cuttlefish pinned commit 的 remote HAL 仍是 v3 期代码，未实现该纯虚方法。
// CDH 云机不做 module hash 下发（attestation 走 host secure_env 工厂密钥），此处空实现。
#include <guest/hals/keymint/remote/remote_keymint_device.h>

namespace aidl::android::hardware::security::keymint {

ScopedAStatus RemoteKeyMintDevice::setAdditionalAttestationInfo(
    const std::vector<KeyParameter>&) {
  return ScopedAStatus::ok();
}

}  // namespace aidl::android::hardware::security::keymint
