#pragma once
// =============================================================
// secrets.example.h - 占位符模板
// 使用方法：复制本文件为同目录下的 secrets.h 并填入真实值。
// secrets.h 已被 .gitignore 忽略，绝不要提交、打印或上传真实值。
// 真实 token 只存在于设备本地；固件里没有 DeepSeek API key，
// 设备只持有 bridge_token。
// =============================================================

#define WIFI_SSID "YOUR_2G4_SSID"       // 仅 2.4 GHz；ESP32-S3 不支持 5 GHz
#define WIFI_PASS "YOUR_PASSWORD"
#define BRIDGE_HOST "192.168.0.100"     // PC bridge 局域网地址，无 TLS
#define BRIDGE_PORT 8080
#define BRIDGE_TOKEN "YOUR_BRIDGE_TOKEN"
#define SESSION_ID "dev1"
