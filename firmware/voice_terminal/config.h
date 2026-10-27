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
// 开口宽限：按下按键后先给这么久等开口，期间不做静音判断。
// 原来 last_voice 初始化为 t0，等于"一按下就开始倒计时 1.2 s"，
// 实测导致 record_ms=1213 就自动结束、整段只有直流漂移（用户还没开口）。
static constexpr uint32_t REC_LEAD_MS        = 4000;
static constexpr uint32_t REC_SILENCE_END_MS = 1200;         // 检测到人声后，静音 1.2 s 自动结束
// 自适应噪声门：阈值 = max(此下限, 环境底噪 × 2.0)。
// 实测底噪块 RMS 约 100~120、语音块 600+，固定阈值 500 在这个房间里会误判。
static constexpr int      REC_VAD_MIN_RMS    = 280;          // 阈值下限（s16 RMS）
static constexpr float    REC_VAD_FACTOR     = 2.0f;         // 相对底噪的倍数
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
// 缓冲深度决定能吸收多长的服务端停顿。实测（2026-10-27）：bridge 产出快于实时，
// 缓冲长期满；断续来自 1.1~5.6 s 的句间合成停顿，1 s 缓冲会被抽干。
// 首选 3 s，按 2 s、1 s 逐级回退（内部 RAM 不够时自动降级）。
static constexpr size_t   PLAY_STREAM_BYTES_MAX = 98304; // 3 s
static constexpr size_t   PLAY_STREAM_BYTES_MID = 65536; // 2 s
static constexpr size_t   PLAY_STREAM_BYTES    = 32768;  // 1 s 兜底
// 起始水位只决定"多晚开播"，不决定"开局有多少存款"：bridge 首句是整段突发到达，
// 播放一开手里就握着整句音频（实测 4~5 s），水位设 160 ms 还是 1.8 s 都一样。
// 实测（2026-10-27）：1.8 s 水位对开头缺口无效，已回退。
static constexpr size_t   PLAY_PREBUFFER_BYTES = 5120;   // 160 ms 起始水位，达到才开 I2S TX
static constexpr size_t   PLAY_FRAME_BYTES     = 1024;   // 音频任务每次写 I2S 的粒度（32 ms）
static constexpr uint32_t PLAY_READ_TIMEOUT_MS = 50;     // 流缓冲读超时；超时即 underrun 写静音
// ⚠️ 15 ms 太短：音频任务读一块要 32 ms 的数据，主任务只要被抢占十几毫秒就会
// 触发 underrun，写进 32 ms 静音 —— 听感就是随机的"咔/毛刺"。
// 但注意：这只是降低误判概率，真正的修法是 playTone 预生成整段再一次性入队。

// ===== 录音后处理（整段，不是逐块）=====
// 实测（2026-10-26）：话筒链路本身正常，裸录音 s16 peak=3806（12% 满幅）、rms=467，
// 属于"录得偏轻但波形结构正常"。之前播放侧做逐块 AGC，静音块把增益压回 1.0x、
// 说话块每块只爬 25%，结果整句都在低增益下播，只有结尾爬到高位
// —— 这正是"只听清最后一个字 + 整体偏小 + 底噪突出"的原因。
// 改为：录完一次性做 去直流/带通/降噪/归一化，播放侧只做安全限幅 + 快升慢降。
// 归一日标：把真实峰值归一到这里，之后波形不再触碰限幅器（破音的根因就是
// 之前用 95 分位做基准、峰值被推到 24000 撞上 20000 的拐点，顶部被压缩成谐波）。
// 实测对照：6555 太小 / 21334 破音，故取中间值。
static constexpr int32_t CLIP_TARGET_PEAK = 15000;   // 已废弃（保留仅为引用不报错），改看下面的 RMS 目标
// 归一化基准改成"语音块 RMS"，并用块级压缩补回感知响度。
// 原因（2026-10-26 实测）：5 次同类说话，输出峰值都精确等于 15000，
// 但增益在 3.73x~5.98x 之间跳 —— 因为"峰值"往往由某个瞬间尖峰（口水音、
// 桌面碰撞、爆破音）决定，尖峰一高，整句主体就被压低。人耳感知的是 RMS。
// 更关键：这版硬件的峰值上限只有 8000，所以不能再靠抬峰值提音量，
// 只能压缩动态范围（把弱音节抬起来），这是唯一不产生削顶失真的办法。
// 目标 RMS：这是**唯一真正控制音量的参数**（原因见下方 CLIP_PEAK_CAP 的说明）。
// 2026-10-26 20:53：地回路修好（模块与板子同坞 + 板子换 25 cm 短线）后，
// 阶梯 4000~20000 全部干净，峰值天花板解除，故 1400 -> 3000（+6.6 dB）。
// 峰均比 3.3 时峰值约 10000，仍远低于实测干净上限 20000。
// 2026-10-26 21:42：3000 → 2700（-0.9 dB）。
//   实测证据（3000 版连录 4 次）：play_peak = 8618 / 6485 / 7853 / 7221，
//   其中 8618 那次用户明确听到破音，其余三次正常 —— 功放削顶上限约 8000 得到坐实。
//   8618 的来源是那一次输入峰值偏高（2356，其余 1920~2193），不是算法波动（gmax 仅 6.77×）。
//   2700 后同条件预期峰值约 7756，全部落进安全区，而音量只降 0.9 dB（听不出）。
//   ⚠️ 未采用「调低 LIMIT_KNEE」的方案：软限幅是逐样本非线性压缩，会引入谐波（听感"糊"），
//   为偶发峰值牺牲整体音质不划算。LIMIT_KNEE 保持 32000 不动。
static constexpr int32_t CLIP_TARGET_RMS  = 2700;
static constexpr float   CLIP_SPEECH_MED  = 0.60f;   // 取语音块 RMS 的 60 分位作为代表值
// ⚠️ 2026-10-26 20:53 更正：CLIP_PEAK_CAP **目前是死参数，不参与任何信号处理**。
// 全仓库只有 audio_io.cpp:300 那一行 Serial.printf 读它、打印成 "cap=..."，
// 没有任何一处用它限制峰值。真正的峰值上限来自 softLimit()（LIMIT_KNEE/LIMIT_ROOM
// = 32000/1，等于不限制）+ 32767 硬钳位。
// 历史上多轮"调峰值上限"改的都是这个死值，所以"改了没变化"是正常的，不是耳朵判断错。
// 保留该常量只为串口日志格式不变；要真正限制峰值，必须另加一步整段归一化。
static constexpr int32_t CLIP_PEAK_CAP    = 4600;
static constexpr float   CLIP_COMP_THR    = 2.5f;    // 压缩阈值 = 2.5×底噪：静音块不参与压缩（否则底噪被抬 10 倍）
static constexpr int32_t CLIP_MIN_PEAK    = 500;     // 带通后峰值低于此值视为没录到人声，直接丢弃不放
// 2026-10-26 21:34：12.0 → 8.0 回退，与 TARGET_RMS 回退配套（见上方说明）。
static constexpr float   CLIP_GAIN_MAX    = 8.0f;    // 最大补偿倍数
static constexpr float   CLIP_GAIN_MIN    = 0.25f;   // 最大衰减倍数（别把大声的录音压太狠）
// 存在感提升：小喇叭最容易丢的是 2~3 kHz，这一段决定"清不清楚"。
// +5 dB 偏冲，改 +3 dB。
static constexpr float   CLIP_PRESENCE_HZ = 2200.0f;
static constexpr float   CLIP_PRESENCE_DB = 2.0f;
static constexpr float   CLIP_PRESENCE_Q  = 1.0f;
// 降噪门：只压"确定的静音"，压深了会产生音节级的音量抖动（听感就是"毛刺/发毛"）。
// 实测：floor=0.22（-13 dB）+ ON=4x 时，处在阈值附近的弱音节块来回跳，
// 一块 32 ms 内从 0.22 爬到 1.0，形成 ~5 Hz 的振幅调制。
// 现在只保留极浅的静音抑制：最深 -3 dB，且只有真正静音（<3x 底噪）才动。
static constexpr float   CLIP_GATE_FLOOR  = 0.70f;
static constexpr float   CLIP_GATE_ON     = 10.0f;
static constexpr float   CLIP_GATE_OFF    = 3.0f;
static constexpr int     CLIP_GATE_SMOOTH = 2;      // 块增益滑动平均半径（±2 块 = ±64 ms）
static constexpr float   CLIP_HP_HZ       = 250.0f;  // 第一级高通：去直流、去电源嗡声
static constexpr float   CLIP_LP_HZ       = 3800.0f; // 电话带上限：去高频嘶声
// 第二级高通（与第一级级联成 4 阶）：专门砍掉 200~300 Hz 的低频能量。
// 实测本机用户语音 92% 的能量在 500 Hz 以下、最强峰 208~232 Hz，
// 这部分小喇叭既发不出来（振膜行程不够）又要吃掉功放几乎全部余量，
// 先在这里削掉，中频才能在不失真的前提下推上去。
static constexpr float   CLIP_HP2_HZ      = 300.0f;
static constexpr float   CLIP_HP2_Q       = 0.707f;

// ===== 回放（固定增益 + 软限幅）=====
// 教训（2026-10-26 实测）：录音侧 clipEnhance 已把整段归一化，若播放侧再做逐块 AGC，
// 增益会叠加两次（实测 clip 3.33x + play 8.00x），连停顿处的底噪都被抬到接近满幅。
// 因此播放侧只做 1:1 直通 + 软限幅，音量由录音侧一次性决定。
// 限幅拐点必须高于归一日标，否则归一化白做（曾经的 10800 把输出硬顶在
// 11600 = 35% 满幅，这就是"声音小"的硬上限）。
static constexpr int32_t PLAY_FIXED_GAIN_Q8 = 256;   // 1.0x
// ⚠️ 2026-10-26 重大更正：限幅器本身曾经是毛刺的制造者。
// 上一版把 KNEE 压到 4200 追求"安全"，导致任何超过 4200 的样本都被曲线压缩。
// 实测播放阶梯：level=5500 进、play_peak=4505 出 —— 证明限幅器在正常工作音量下
// 就持续压缩波形，产生的谐波就是用户听到的"毛刺"。
// 更严重的是：之前"功放上限 8000"的结论是在 KNEE=7000 时测的，8000 刚好触发
// 限幅器，所以那个结论不可信，必须旁路限幅器重测。
//
// 现在：KNEE 设到 32000（等于关闭限幅），只保留 32767 的硬钳位防溢出。
// 待测出功放真实上限后再按实测值设定。
static constexpr int32_t LIMIT_KNEE = 32000;
static constexpr int32_t LIMIT_ROOM = 1;     // 极小，避免在 32000 以下产生任何压缩

// ===== 网络 =====
static constexpr uint32_t WIFI_CONNECT_TIMEOUT_MS = 15000;  // Wi-Fi 连接超时
static constexpr uint32_t HTTP_CONNECT_TIMEOUT_MS = 5000;   // TCP 连接超时
static constexpr uint32_t HTTP_UPLOAD_TIMEOUT_MS  = 20000;  // 整个 WAV 请求体的发送上限
static constexpr uint32_t HTTP_READ_TIMEOUT_MS    = 20000;  // 读超时（首字节可能 5-9 s）

// ===== 按键 =====
static constexpr uint32_t BUTTON_DEBOUNCE_MS = 30;  // 去抖；不用长按/双击等复杂手势
