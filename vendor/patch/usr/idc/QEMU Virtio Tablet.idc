# CDH: QEMU Virtio Tablet 强制识别为触屏（finger）。
# 背景：无 idc 时 InputReader 按能力把该设备分类为 STYLUS（toolType=STYLUS），
# 一旦有悬停指针（isHovering=true）占住 channel 的 motion state，
# InputDispatcher::InputState::trackMotion 会拒绝后续非 stylus 事件
# （scrcpy/adb 注入的触摸）→ "dropping inconsistent event" → 触摸/滑动全部无效。
touch.deviceType = touchScreen
touch.toolType = finger
device.internal = 1
