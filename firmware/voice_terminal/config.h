#pragma once
// =============================================================
// config.h - ESP32-S3 语音终端固件骨架：全部可调常量集中在此
// 目标：Arduino-ESP32 core 3.x / ESP-IDF 5.x（driver/i2s_std.h）
// 引脚与契约来自 handoff F 包，不要随意改；改动前对照第 3.3 节。
// =============================================================
#include <Arduino.h>

// ===== 应用模式（编译期常量）=====
#define APP_MODE_RECORD_ONLY 0   // 录音 + 串口统计 + 本地回放，不联网
#define APP_MODE_TURN        1   // 录音 + POST /turn + 播放回复
#define APP_MODE_TURN_STOP   2   // 再加 /stop 打断

#ifndef APP_MODE
#define APP_MODE APP_MODE_RECORD_ONLY   // 默认：第一次上电测试用
#endif

// ===== 引脚（与 handoff F 包 3.3 一致）=====
static constexpr int PIN_MIC_SD    = 6;   // ICS43434 SD（话筒数据）
static constexpr int PIN_MIC_SCK   = 4;   // ICS43434 SCK（话筒位时钟）
static constexpr int PIN_MIC_WS    = 5;   // ICS43434 WS（话筒字选择）
static constexpr int PIN_AMP_DIN   = 7;   // MAX98357A DIN（功放数据）
static constexpr int PIN_AMP_BCLK  = 15;  // MAX98357A BCLK（功放位时钟）
static constexpr int PIN_AMP_LRCLK = 16;  // MAX98357A LRCLK（功放字选择）
static constexpr int PIN_BUTTON    = 0;   // BOOT 按键，低有效，内部上拉；复位时不要按住

// 到货后 G 实测 SCK<->BCLK、WS<->LRCLK 是否导通。
// 若导通 = 模块内部共时钟，不能把两组时钟分别接两个 GPIO 输出，
// 必须先报告 bb96；单总线路径需要 TX/RX 都用 32-bit slot 并做样本扩展，
// 当前实现不完整，打开会直接编译失败。
#define MIC_AMP_SHARED_CLOCKS 0   // 默认 0 = 两路独立 I2S

#if MIC_AMP_SHARED_CLOCKS
#error "MIC_AMP_SHARED_CLOCKS=1 is not implemented: RX was never initialized and TX/RX slot widths conflict. Keep 0 until G verifies shared clocks and the single-bus path is rewritten."
#endif

// ===== 音频参数（接口契约固定：16000 Hz / 16 bit / mono）=====
static constexpr uint32_t AUDIO_SAMPLE_RATE = 16000;
static constexpr int      AUDIO_BITS        = 16;
static constexpr int      AUDIO_CHANNELS    = 1;
static constexpr uint32_t BYTES_PER_MS      = AUDIO_SAMPLE_RATE * 2 / 1000;  // 32 B/ms

// ===== 录音 =====
static constexpr uint32_t REC_MAX_MS         = 15000;        // 最长录音 15 s
static constexpr uint32_t REC_MIN_MS         = 300;          // 短于此丢弃
static constexpr uint32_t REC_SILENCE_END_MS = 1200;         // 静音 1.2 s 自动结束
static constexpr int      REC_SILENCE_RMS    = 500;          // s16 RMS 阈值（未验证，G 按串口数据调）
static constexpr size_t   REC_BUF_BYTES      = 512 * 1024;   // PSRAM；15 s 需 480000 B

// ===== 话筒 24-bit -> s16 转换 =====
// 采用方案 B：RX 以 32-bit slot 读取 int32_t，再用 mic24_to_s16() 转换。
// 假设：ICS43434 在 32-bit slot 中输出 24-bit 数据、左对齐（MSB 对齐），
// 因此右移 16 位取高 16 位。MIC_SHIFT=16 只是基于该假设的初始值，未经实测！
// G 会用串口打印的原始 min/max/RMS 判断直流偏置/削顶/全零，再只改这里的常量。
static constexpr int  MIC_DATA_BITS      = 24;    // 话筒名义有效位宽
static constexpr int  MIC_SHIFT          = 16;    // 右移位数（假设左对齐；未验证）
static constexpr bool MIC_LEFT_JUSTIFIED = true;  // 假设数据 MSB 对齐；若为右对齐需重新评估 MIC_SHIFT
static constexpr bool MIC_I2S_SLOT_LEFT  = true;  // 假设话筒在左声道（L/R 接低）；实测相反改 false

// ===== 播放 =====
static constexpr size_t   PLAY_STREAM_BYTES    = 32768;  // xStreamBufferCreate(32768, 1)
static constexpr size_t   PLAY_PREBUFFER_BYTES = 5120;   // 160 ms 起始水位，达到才开 I2S TX
static constexpr size_t   PLAY_FRAME_BYTES     = 1024;   // 音频任务每次写 I2S 的粒度（32 ms）
static constexpr uint32_t PLAY_READ_TIMEOUT_MS = 15;     // 流缓冲读超时；超时即 underrun 写静音

// ===== 网络 =====
static constexpr uint32_t WIFI_CONNECT_TIMEOUT_MS = 15000;  // Wi-Fi 连接超时
static constexpr uint32_t HTTP_CONNECT_TIMEOUT_MS = 5000;   // TCP 连接超时
static constexpr uint32_t HTTP_UPLOAD_TIMEOUT_MS  = 20000;  // 整个 WAV 请求体的发送上限
static constexpr uint32_t HTTP_READ_TIMEOUT_MS    = 20000;  // 读超时（首字节可能 5-9 s）

// ===== 按键 =====
static constexpr uint32_t BUTTON_DEBOUNCE_MS = 30;  // 去抖；不用长按/双击等复杂手势
