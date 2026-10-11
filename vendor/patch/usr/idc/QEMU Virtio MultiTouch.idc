# CDH: QEMU Virtio MultiTouch 强制识别为触屏（finger）。
# 与 "QEMU Virtio Tablet.idc" 同理：避免被 InputReader 分类为 STYLUS，
# 防止悬停指针占住 InputDispatcher motion state 导致触摸注入被丢弃。
touch.deviceType = touchScreen
touch.toolType = finger
device.internal = 1
