#pragma once
// =============================================================
// wifi_setup.h - Wi-Fi 配网（NVS 账号 + 热点配网页面）与 bridge 地址
// 只有 TURN / TURN_STOP 用；RECORD_ONLY 不联网，不调用。
//
// 连网顺序：NVS 里保存的账号 -> secrets.h 里的编译期账号 -> 开热点配网页。
// 配网成功后账号写进 NVS 并软重启，之后换路由器不用重新烧录。
// 页面里还能改 bridge 地址（PC 端 IP 变了也不用重新烧录）。
// =============================================================
#include <Arduino.h>

// true = 已连上 Wi-Fi；false = 连不上（可能刚开过配网热点）
bool wifiSetupEnsure();

// 手动开一次配网热点（串口敲 portal 触发）。不动按键语义：
// BOOT 键始终是"按住说话"，长按不做别的事。
void wifiSetupForcePortal();

// 清掉 NVS 里保存的 Wi-Fi 账号和电脑地址（串口敲 forget），
// 下一次连网回到 secrets.h 的编译期账号。
void wifiSetupForget();

// 处理串口命令（portal / forget）。在 IDLE 和配网热点循环里都要调用。
void wifiSetupPollSerial();

// 当前生效的 bridge 地址：NVS 优先，其次 secrets.h 的 BRIDGE_HOST。
// 地址可以写成 IP（192.168.0.101）或主机名（mypc.local）；后者靠 mDNS 解析。
const char* wifiBridgeHost();
