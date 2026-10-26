#pragma once
// =============================================================
// audio_io.h - I2S 话筒采集（24-bit -> s16）与异步播放
// 网络/主任务喂数据，独立音频任务消费；音频任务优先级更高。
// =============================================================
#include <Arduino.h>

// 初始化 I2S RX/TX 通道、播放流缓冲与音频任务。setup() 调用一次。
void audioInit();

// 运行时改 TX 采样率（只动播放，不动话筒那一路）。仅用于诊断：
// 采样率减半 -> BCLK 减半，可判断杂音是否与时钟频率相关（信号完整性）。
// 返回 true = 已生效。必须在没有播放进行时调用。
bool playerSetRate(uint32_t hz);

// 方案 B：32-bit slot 读出的原始值 -> s16。
// 转换假设见 config.h（MIC_SHIFT / MIC_LEFT_JUSTIFIED），未经实测。
int16_t mic24_to_s16(int32_t raw);

// 从话筒读一帧原始 32-bit 样本。返回样本数；超时返回 0。
size_t micReadRaw(int32_t* dst, size_t max_samples, uint32_t timeout_ms);

// 整段录音后处理（原地改 samples 个 s16）：去直流 + 250~3800Hz 带通 +
// 块级降噪门 + 整体归一化到 CLIP_TARGET_PEAK。
// 返回 false = 带通后峰值过低（没录到人声），调用方应丢弃该段、不要播放。
bool clipEnhance(int16_t* samples, size_t count);

// ---------------- 播放器（流式 PCM -> I2S TX） ----------------
// 会话流程：playerBegin() -> playerFeed() 多次 -> playerEnd()
// 打断：playerAbort()（/stop：flush、清 DMA、回空闲）
void  playerBegin();                    // 开始一次播放会话：清缓冲、清计数
void  playerFeed(const uint8_t* data, size_t n);  // 喂 PCM，可多次、可小段
void  playerEnd();                      // 流结束；播完剩余数据后自动停
void  playerAbort();                    // 立即打断：清空缓冲、清 DMA、回空闲
bool  playerBusy();                     // 播放会话是否仍在进行
uint32_t playerUnderruns();             // 本次会话 underrun 次数
uint32_t playerPlayedBytes();           // 本次会话已写 I2S 的字节数
