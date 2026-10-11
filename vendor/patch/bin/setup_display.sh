#!/system/bin/sh
# headless 云机显示/电源防呆设置：全程不注入按键（避免误触用户操作）。
# - SettingsProvider 可能晚于 sys.boot_completed 置位就绪，重试直到能写入；
# - 屏永不自动关（screen_off_timeout 超长）+ 插电常亮 + 禁 Doze + 禁锁屏，
#   launcher 不会被 60s 入睡/Keyguard 盖掉；无按键副作用。
i=0
while [ "$i" -lt 20 ]; do
    if settings put system screen_off_timeout 2147483647 2>/dev/null; then
        break
    fi
    i=$((i + 1))
    sleep 1
done
settings put global stay_on_while_plugged_in 7 2>/dev/null
settings put secure lockscreen.disabled 1 2>/dev/null
settings put secure user_setup_complete 1 2>/dev/null
settings put global device_provisioned 1 2>/dev/null

# ---- 蜂窝数据首启补齐（对齐 gold 真机 /data 的两个键）----
# AOSP 双卡机型的"用户移动数据开关"按 subId 存 Global.MOBILE_DATA<subId>，缺省值取
# ro.com.android.mobiledata（gold/本机均为 false）；"默认数据卡"存
# Global.multi_sim_data_call。gold /data 镜像里这两个键缺失时：电话进程
# DataSettingsManager 判定"用户关闭移动数据"（reason=USER,enabled=false），且
# SubscriptionManagerService 的默认数据卡为 INVALID → PhoneSwitcher 对无 specifier
# 的网络请求 shouldApply=false → DNC 收不到任何请求 → 框架永不发起 SetupDataCall
# （移动数据开关、PS 域注册都正常也照样无数据）。二者只在电话进程初始化时读取，
# 故补齐后需重启电话进程让框架重读（SIGKILL 由 AMS 自动拉起持久进程）。
seeded=0
if [ "$(settings get global multi_sim_data_call)" = "null" ]; then
    settings put global multi_sim_data_call 1 2>/dev/null && seeded=1
fi
for k in mobile_data0 mobile_data1; do
    if [ "$(settings get global $k)" = "null" ]; then
        settings put global $k 1 2>/dev/null && seeded=1
    fi
done
if [ "$seeded" = "1" ]; then
    log -t cdh_provision "seed cellular data settings (mobile_data<slot>/multi_sim_data_call)"
    kill -9 "$(pidof com.android.phone)" 2>/dev/null
    i=0
    while [ "$i" -lt 30 ]; do
        sleep 2
        i=$((i + 1))
        [ -n "$(pidof com.android.phone)" ] && break
    done
    log -t cdh_provision "phone restarted pid=$(pidof com.android.phone)"
fi
svc power stayon true 2>/dev/null
cmd deviceidle disable 2>/dev/null
pm disable com.android.provision 2>/dev/null || true
pm disable com.miui.bugreport 2>/dev/null || true
am force-stop com.android.provision 2>/dev/null || true
# 解锁兜底（非按键；已解锁时为无副作用 no-op）
cmd window dismiss-keyguard 2>/dev/null
exit 0
