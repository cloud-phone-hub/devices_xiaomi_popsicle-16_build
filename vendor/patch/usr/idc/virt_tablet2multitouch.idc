# CDH: 覆盖 lineage 自带版本，强制 toolType=finger。
# 背景：无 toolType 时 InputReader 保留内核上报的 STYLUS 能力（Sources 含 STYLUS），
# 一旦出现悬停指针（isHovering=true）就占住 channel 的 motion state，
# InputDispatcher::InputState::trackMotion 拒绝后续非 stylus 注入
# → "dropping inconsistent event" → scrcpy/adb 触摸全失效。
device.internal = 1

# The device should behave as a touch screen, which uses the same orientation
# as the built-in display.
touch.deviceType = touchScreen
touch.toolType = finger
touch.orientationAware = 1
