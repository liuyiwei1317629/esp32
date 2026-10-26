// =============================================================
// audio_io.cpp - I2S（driver/i2s_std.h, core 3.x / IDF 5.x）实现
// RX：ICS43434，32-bit slot，软件转 s16（方案 B，假设见 config.h）
// TX：MAX98357A，16-bit mono Philips；FreeRTOS 任务 + 流缓冲
// =============================================================
#include "audio_io.h"
#include "config.h"
#include "driver/i2s_std.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/stream_buffer.h"

static i2s_chan_handle_t s_rx = nullptr;
static i2s_chan_handle_t s_tx = nullptr;
static StreamBufferHandle_t s_sb = nullptr;

// TX 时钟配置备份：诊断时用来在运行时改采样率（见 playerSetRate）
static i2s_std_clk_config_t s_tx_clk = {};
static bool s_tx_clk_ok = false;

// ---------------- 播放器状态 ----------------
enum PlayerState : uint8_t { P_IDLE, P_FILLING, P_PLAYING };
static volatile PlayerState s_state = P_IDLE;
static volatile bool     s_end = false;    // 网络侧已喂完（流结束）
static volatile bool     s_abort = false;  // /stop 打断
static volatile uint32_t s_underruns = 0;
static volatile uint32_t s_played = 0;

// TX DMA 深度（仅用于会话结束时一次性估算 DMA 排空等待，不用来做节拍）
static constexpr uint32_t TX_DMA_DESC  = 6;
static constexpr uint32_t TX_DMA_FRAME = 240;  // 6 描述符 × 240 帧 = 1440 帧 ≈ 90 ms

static void txEnable()  { if (s_tx) i2s_channel_enable(s_tx); }
static void txDisable() { if (s_tx) i2s_channel_disable(s_tx); }

// 话筒那一路（I2S0）与功放那一路（I2S1）同时使能时，功放会彻底静音。
// 实测结论（同一块板、同一接线，只改这一项）：
//   只开 TX                 -> 有声
//   RX + TX 同时开           -> 无声（数据写入量、underruns 全部正常）
//   播放窗口内临时关掉 RX     -> 又有声
// 两路独立 I2S 各自产生同频不同相的 BCLK/WS，模块的时钟网络被互相拉扯，
// 功放解析不出帧边界。本项目是「录完再放」，两者从不同时使用，
// 因此只需在播放窗口内关掉 RX，播放结束立刻恢复。
// 单总线方案（MIC_AMP_SHARED_CLOCKS=1）下 RX/TX 是同一个通道，不能单独关。
static void rxEnable()  { if (s_rx && !MIC_AMP_SHARED_CLOCKS) i2s_channel_enable(s_rx); }
static void rxDisable() { if (s_rx && !MIC_AMP_SHARED_CLOCKS) i2s_channel_disable(s_rx); }

// ---------------- 24-bit -> s16（方案 B） ----------------
int16_t mic24_to_s16(int32_t raw) {
  // 假设 24-bit 数据在 32-bit slot 中左对齐（MSB 对齐），右移 MIC_SHIFT 取高 16 位。
  // 未实测：若串口显示直流偏置/削顶/全零，只改 config.h 的常量或本函数。
  int32_t v = raw >> MIC_SHIFT;
  if (v > 32767) v = 32767;
  else if (v < -32768) v = -32768;
  return (int16_t)v;
}

size_t micReadRaw(int32_t* dst, size_t max_samples, uint32_t timeout_ms) {
  if (!s_rx) return 0;
  size_t bytes_read = 0;
  esp_err_t err = i2s_channel_read(s_rx, dst, max_samples * sizeof(int32_t),
                                   &bytes_read, pdMS_TO_TICKS(timeout_ms));
  if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
    Serial.printf("mic_read_err=%d\n", (int)err);
  }
  return bytes_read / sizeof(int32_t);
}

// ---------------- 整段录音后处理 ----------------
// 二阶级联（RBJ cookbook），transposed direct form II
struct Biquad { float b0, b1, b2, a1, a2, z1, z2; };

static void bqReset(Biquad& q) { q.z1 = 0.0f; q.z2 = 0.0f; }

static void bqLowpass(Biquad& q, float fs, float f0, float Q) {
  float w0 = 2.0f * (float)PI * f0 / fs, c = cosf(w0), s = sinf(w0), al = s / (2.0f * Q);
  float a0 = 1.0f + al;
  q.b0 = (1.0f - c) * 0.5f / a0; q.b1 = (1.0f - c) / a0; q.b2 = q.b0;
  q.a1 = -2.0f * c / a0;         q.a2 = (1.0f - al) / a0;
  bqReset(q);
}

static void bqHighpass(Biquad& q, float fs, float f0, float Q) {
  float w0 = 2.0f * (float)PI * f0 / fs, c = cosf(w0), s = sinf(w0), al = s / (2.0f * Q);
  float a0 = 1.0f + al;
  q.b0 = (1.0f + c) * 0.5f / a0; q.b1 = -(1.0f + c) / a0; q.b2 = q.b0;
  q.a1 = -2.0f * c / a0;         q.a2 = (1.0f - al) / a0;
  bqReset(q);
}

// 峰值型 EQ（RBJ）：用于给 2~3 kHz 做存在感提升，改善小喇叭的清晰度
static void bqPeaking(Biquad& q, float fs, float f0, float Q, float dB) {
  float A = powf(10.0f, dB / 40.0f);
  float w0 = 2.0f * (float)PI * f0 / fs, c = cosf(w0), s = sinf(w0), al = s / (2.0f * Q);
  float a0 = 1.0f + al / A;
  q.b0 = (1.0f + al * A) / a0;
  q.b1 = -2.0f * c / a0;
  q.b2 = (1.0f - al * A) / a0;
  q.a1 = -2.0f * c / a0;
  q.a2 = (1.0f - al / A) / a0;
  bqReset(q);
}

static inline float bqRun(Biquad& q, float x) {
  float y = q.b0 * x + q.z1;
  q.z1 = q.b1 * x - q.a1 * y + q.z2;
  q.z2 = q.b2 * x - q.a2 * y;
  return y;
}

// 软限幅：KNEE 以上曲线压缩，最多到 KNEE+ROOM，绝不硬削顶
static inline int32_t softLimit(int32_t v) {
  int32_t sign = (v < 0) ? -1 : 1;
  int32_t a = (v < 0) ? -v : v;
  if (a > LIMIT_KNEE) {
    int32_t over = a - LIMIT_KNEE;
    a = LIMIT_KNEE + (int32_t)((int64_t)LIMIT_ROOM * over / (over + LIMIT_ROOM));
  }
  if (a > 32767) a = 32767;
  return sign * a;
}

static constexpr size_t CLIP_BLK = 512;      // 32 ms @16k
static constexpr size_t CLIP_MAX_BLK = 512;  // 15 s / 32 ms = 469

// 块级增益插值：第 k 块内从 g[k-1] 线性过渡到 g[k]
static inline float gateAt(const float* g, size_t k, size_t p) {
  float s = (k == 0) ? g[0] : g[k - 1];
  float e = g[k];
  return s + (e - s) * ((float)p / (float)CLIP_BLK);
}

bool clipEnhance(int16_t* x, size_t n) {
  if (!x || n < CLIP_BLK * 2) return false;
  size_t nb = n / CLIP_BLK;
  if (nb > CLIP_MAX_BLK) nb = CLIP_MAX_BLK;

  static float rmsv[CLIP_MAX_BLK];
  static float sorted[CLIP_MAX_BLK];
  static float gblk[CLIP_MAX_BLK];

  // 0) 先去整段直流。实测话筒输出有几十个 LSB 的缓慢漂移（dc=171/185），
  //    不先减掉，它会在高通滤波器的起始/收敛过程里制造一个大瞬态，
  //    把峰值测量顶高、进而把归一化倍数压低。
  int64_t sum = 0;
  size_t ns = nb * CLIP_BLK;
  for (size_t i = 0; i < ns; i++) sum += x[i];
  int32_t dc = (int32_t)(sum / (int64_t)ns);
  for (size_t i = 0; i < ns; i++) {
    int32_t v = (int32_t)x[i] - dc;
    if (v > 32767) v = 32767; else if (v < -32768) v = -32768;
    x[i] = (int16_t)v;
  }

  // 1) 块 RMS
  for (size_t k = 0; k < nb; k++) {
    const int16_t* p = x + k * CLIP_BLK;
    double acc = 0;
    for (size_t i = 0; i < CLIP_BLK; i++) { double v = p[i]; acc += v * v; }
    rmsv[k] = sqrtf((float)(acc / CLIP_BLK));
  }

  // 2) 底噪 = 块 RMS 的 20 分位（插入排序，最多 469 个元素）
  for (size_t k = 0; k < nb; k++) sorted[k] = rmsv[k];
  for (size_t i = 1; i < nb; i++) {
    float v = sorted[i];
    size_t j = i;
    while (j > 0 && sorted[j - 1] > v) { sorted[j] = sorted[j - 1]; j--; }
    sorted[j] = v;
  }
  float floor_rms = sorted[nb / 5];
  if (floor_rms < 8.0f) floor_rms = 8.0f;   // 数字静音时不把底噪估成 0

  // 3) 块级降噪门增益 + 滑动平均（防止块间跳变产生音节级抖动）
  for (size_t k = 0; k < nb; k++) {
    float r = rmsv[k] / floor_rms;
    float t = (r - CLIP_GATE_OFF) / (CLIP_GATE_ON - CLIP_GATE_OFF);
    if (t < 0.0f) t = 0.0f; else if (t > 1.0f) t = 1.0f;
    gblk[k] = CLIP_GATE_FLOOR + (1.0f - CLIP_GATE_FLOOR) * t;
  }
  {
    static float sm[CLIP_MAX_BLK];
    for (size_t k = 0; k < nb; k++) {
      float acc = 0.0f; size_t cnt = 0;
      int lo = (int)k - CLIP_GATE_SMOOTH, hi = (int)k + CLIP_GATE_SMOOTH;
      for (int j = lo; j <= hi; j++) {
        if (j < 0 || j >= (int)nb) continue;
        acc += gblk[j]; cnt++;
      }
      sm[k] = acc / (float)cnt;
    }
    for (size_t k = 0; k < nb; k++) gblk[k] = sm[k];
  }

  // 4) 高通 ×2（4 阶，砍 200~300 Hz 低频）+ 存在感提升 + 低通 + 门控
  Biquad hp, hp2, pk, lp;
  bqHighpass(hp,  (float)AUDIO_SAMPLE_RATE, CLIP_HP_HZ,  0.707f);
  bqHighpass(hp2, (float)AUDIO_SAMPLE_RATE, CLIP_HP2_HZ, CLIP_HP2_Q);
  bqPeaking (pk,  (float)AUDIO_SAMPLE_RATE, CLIP_PRESENCE_HZ, CLIP_PRESENCE_Q, CLIP_PRESENCE_DB);
  bqLowpass (lp,  (float)AUDIO_SAMPLE_RATE, CLIP_LP_HZ,  0.707f);
  float peak = 0.0f;
  static float postRms[CLIP_MAX_BLK];   // 滤波+门控后的块 RMS，用于 RMS 基准归一化
  // 统计峰值时跳过开头 3 块（96 ms）：那里是提示音余音，不是人声。
  const size_t kPeakStart = (nb > 12) ? 3 : 0;
  for (size_t k = 0; k < nb; k++) {
    int16_t* p = x + k * CLIP_BLK;
    double acc = 0;
    for (size_t i = 0; i < CLIP_BLK; i++) {
      float v = bqRun(lp, bqRun(pk, bqRun(hp2, bqRun(hp, (float)p[i])))) * gateAt(gblk, k, i);
      p[i] = (int16_t)softLimit((int32_t)v);   // 先落回 int16 保证两遍一致
      acc += (double)v * v;
    }
    postRms[k] = sqrtf((float)(acc / CLIP_BLK));
    if (k < kPeakStart) continue;
    int16_t* q = p;
    for (size_t i = 0; i < CLIP_BLK; i++) {
      float a = q[i] < 0 ? -(float)q[i] : (float)q[i];
      if (a > peak) peak = a;
    }
  }
  if (peak < (float)CLIP_MIN_PEAK) {
    // 带通后几乎没有能量：话筒没录到人声（多半是开口前就超时了）。
    // 这种情况绝不拿去播放，否则用户听到的就是一段被放大 8 倍的底噪。
    Serial.printf("clip nb=%u dc=%ld floor=%.0f peak=%.0f -> 无人声，丢弃\n",
                  (unsigned)nb, (long)dc, (double)floor_rms, (double)peak);
    return false;
  }

  // 5) 压缩：把每个块的 RMS 推向目标值，而不是整段乘一个系数。
  //    这版硬件的峰值上限只有 8000（功放 12 dB 增益，超过就内部削顶），
  //    所以不能再靠抬峰值提音量，唯一办法是压缩动态范围：
  //    语音弱音节比强音节低 20~30 dB，统一增益只能照顾一头，
  //    块级压缩把弱音节抬起来，整体能量上去、峰值仍受控。
  static float sp[CLIP_MAX_BLK];
  size_t nsp = 0;
  for (size_t k = kPeakStart; k < nb; k++) {
    if (postRms[k] > floor_rms * 3.0f && postRms[k] > 20.0f) sp[nsp++] = postRms[k];
  }
  float base;
  if (nsp >= 4) {
    for (size_t i = 1; i < nsp; i++) {   // 插入排序（最多 469 个元素）
      float v = sp[i]; size_t j = i;
      while (j > 0 && sp[j - 1] > v) { sp[j] = sp[j - 1]; j--; }
      sp[j] = v;
    }
    size_t bi = (size_t)((float)nsp * CLIP_SPEECH_MED);
    if (bi >= nsp) bi = nsp - 1;
    base = sp[bi];
  } else {
    base = peak / 1.6f;   // 语音块太少（极短录音）：退回峰值估算
  }
  if (base < 20.0f) base = 20.0f;

  // 5a) 每块期望增益
  //     阈值用 floor×2.5：静音块必须排除在压缩之外，否则底噪会被放大 10 倍，
  //     听起来就是持续的"嘶——"。压缩只对真实的语音块生效。
  const float compThr = floor_rms * CLIP_COMP_THR;
  static float cg[CLIP_MAX_BLK];
  for (size_t k = 0; k < nb; k++) {
    float r = postRms[k];
    float want = (r > compThr && r > 30.0f) ? ((float)CLIP_TARGET_RMS / r) : 1.0f;
    if (want > CLIP_GAIN_MAX) want = CLIP_GAIN_MAX;
    if (want < CLIP_GAIN_MIN) want = CLIP_GAIN_MIN;
    cg[k] = want;
  }

  // 5b) 快攻慢放 + 双向平滑：避免块间跳变造成"抽吸感"（那也是一种毛刺）
  static float tmpg[CLIP_MAX_BLK];
  float g = cg[0];
  for (size_t k = 0; k < nb; k++) {
    float t = cg[k];
    if (t < g) g = t;                      // 快攻：遇到大声立刻收
    else       g += (t - g) * 0.2f;        // 慢放：约 5 块（160 ms）跟到新值
    tmpg[k] = g;
  }
  for (size_t k = 0; k < nb; k++) {
    float acc = 0.0f; int cnt = 0;
    for (int j = (int)k - 2; j <= (int)k + 2; j++) {
      if (j < 0 || j >= (int)nb) continue;
      acc += tmpg[j]; cnt++;
    }
    cg[k] = acc / (float)cnt;
  }

  // 5c) 应用：块内线性插值消除块边界台阶 + 开头 64 ms 淡入
  const size_t FADE = 2 * CLIP_BLK;
  size_t off = 0;
  for (size_t k = 0; k < nb; k++) {
    float g0 = cg[k];
    float g1 = (k + 1 < nb) ? cg[k + 1] : g0;
    for (size_t i = 0; i < CLIP_BLK; i++, off++) {
      float gg = g0 + (g1 - g0) * ((float)i / (float)CLIP_BLK);
      if (off < FADE) gg *= (float)off / (float)FADE;
      x[off] = (int16_t)softLimit((int32_t)((float)x[off] * gg));
    }
  }

  Serial.printf("clip nb=%u dc=%ld floor=%.0f peak=%.0f base=%.0f g0=%.2fx gN=%.2fx cap=%d\n",
                (unsigned)nb, (long)dc, (double)floor_rms, (double)peak, (double)base,
                (double)cg[0], (double)cg[nb - 1], (int)CLIP_PEAK_CAP);

  // 诊断：块 RMS 与块增益（×100）逐块打印，用于判断"毛刺"是否来自门控抖动
  Serial.print("brms=");
  for (size_t k = 0; k < nb; k++) Serial.printf("%d,", (int)rmsv[k]);
  Serial.println();
  Serial.print("gate=");
  for (size_t k = 0; k < nb; k++) Serial.printf("%d,", (int)(gblk[k] * 100.0f));
  Serial.println();
  return true;
}

// ---------------- 音频任务（消费流缓冲 -> I2S TX） ----------------
static uint32_t s_playPeak = 0;

// 单声道 s16 -> 双声道 s16（同一数据写左、右两个槽）。
//   1) 双槽展开：MAX98357A 只认 32/48/64 BCLK-per-frame，单槽(16 bit/帧)
//      会让它解析帧结构失败，输出的是噪音。
//   2) 固定 1.0x：音量已由 clipEnhance 在录音侧定好，这里再放大就是二次增益，
//      会把底噪一起抬起来（实测踩过这个坑）。
//   3) 软限幅：绝不硬削顶（硬削顶的方波谐波就是"刺耳噪音"的来源）。
// 返回写入目标缓冲的字节数（= 样本数 × 4）。
static inline size_t monoToStereoGain(const uint8_t* src, size_t srcBytes, uint8_t* dst) {
  const int16_t* in  = reinterpret_cast<const int16_t*>(src);
  int16_t*       out = reinterpret_cast<int16_t*>(dst);
  size_t n = srcBytes >> 1;
  if (n == 0) return 0;

  for (size_t i = 0; i < n; i++) {
    int32_t v = ((int32_t)in[i] * PLAY_FIXED_GAIN_Q8) >> 8;
    int16_t s = (int16_t)softLimit(v);
    out[i * 2]     = s;   // 左槽
    out[i * 2 + 1] = s;   // 右槽
    uint32_t absv = (uint32_t)(s < 0 ? -(int32_t)s : s);
    if (absv > s_playPeak) s_playPeak = absv;
  }
  return n * 4;
}

static void playerTask(void*) {
  alignas(4) static uint8_t frame[PLAY_FRAME_BYTES];
  alignas(4) static uint8_t stereo[PLAY_FRAME_BYTES * 2];
  for (;;) {
    // abort 必须先于 P_IDLE 处理：主任务可能在播放刚排空时置位，若先看 P_IDLE
    // 会永远跳过这里，playerBusy() 随后卡死。
    if (s_abort) {  // /stop：flush + disable/enable 清 DMA + 回空闲
      xStreamBufferReset(s_sb);
      txDisable();
      txEnable();
      txDisable();
      rxEnable();    // 同样恢复话筒，否则 /stop 之后就再也录不到音
      s_end = false;
      s_abort = false;
      s_state = P_IDLE;
      Serial.println("player=aborted");
      continue;
    }

    if (s_state == P_IDLE) { vTaskDelay(pdMS_TO_TICKS(5)); continue; }

    if (s_state == P_FILLING) {
      // 预缓冲：达到 160 ms 水位才开 TX；整个回复短于水位时在流结束时开播
      size_t avail = xStreamBufferBytesAvailable(s_sb);
      if (avail >= PLAY_PREBUFFER_BYTES || (s_end && avail > 0)) {
        rxDisable();   // 必须先关话筒那一路，否则功放收不到有效帧（见 rxDisable 注释）
        txEnable();
        s_state = P_PLAYING;
      } else if (s_end && avail == 0) {
        s_end = false;
        s_state = P_IDLE;   // 空回复
      } else {
        vTaskDelay(pdMS_TO_TICKS(2));
      }
      continue;
    }

    // P_PLAYING
    size_t got = xStreamBufferReceive(s_sb, frame, PLAY_FRAME_BYTES,
                                      pdMS_TO_TICKS(PLAY_READ_TIMEOUT_MS));
    if (got == 0) {
      if (s_end && xStreamBufferBytesAvailable(s_sb) == 0) {
        // 播完：等 DMA 排空再停 TX（一次性等待，非节拍），下一轮重新 enable
        // 排空时长 = DMA 总帧数 / 采样率，用帧数算，与单/双声道字节数无关
        vTaskDelay(pdMS_TO_TICKS(TX_DMA_DESC * TX_DMA_FRAME * 1000u / AUDIO_SAMPLE_RATE + 20));
        txDisable();
        rxEnable();    // 播放结束，恢复话筒那一路，下一轮才能录音
        Serial.printf("play_peak=%u gain_x=%.2f\n",
                      (unsigned)s_playPeak, (double)PLAY_FIXED_GAIN_Q8 / 256.0);
        s_end = false;
        s_state = P_IDLE;
        continue;
      }
      memset(frame, 0, PLAY_FRAME_BYTES);  // underrun：写静音，不忙等
      got = PLAY_FRAME_BYTES;
      s_underruns = s_underruns + 1;
    }
    size_t outBytes = monoToStereoGain(frame, got, stereo);
    size_t written = 0;
    i2s_channel_write(s_tx, stereo, outBytes, &written, pdMS_TO_TICKS(100));
    s_played += written;
  }
}

// ---------------- 播放器对外接口 ----------------
void playerBegin() {
  xStreamBufferReset(s_sb);
  s_underruns = 0;
  s_played = 0;
  s_playPeak = 0;
  s_end = false;
  s_abort = false;
  s_state = P_FILLING;
}

void playerFeed(const uint8_t* data, size_t n) {
  // 由网络/主任务调用；缓冲满时短等重试（网络侧节奏，不影响音频任务）
  size_t off = 0;
  while (off < n && !s_abort) {
    size_t sent = xStreamBufferSend(s_sb, data + off, n - off, pdMS_TO_TICKS(50));
    if (sent == 0) {
      if (s_state == P_IDLE) break;
      continue;
    }
    off += sent;
  }
}

void playerEnd()   { s_end = true; }
void playerAbort() { s_abort = true; }
bool playerBusy()  { return s_state != P_IDLE || s_abort; }
uint32_t playerUnderruns()   { return s_underruns; }
uint32_t playerPlayedBytes() { return s_played; }

// 运行时改 TX 采样率。采样率减半 => BCLK 减半，用来判断杂音是否与时钟频率相关：
//   降速后明显变干净 -> 信号完整性（杜邦线过长/串扰/驱动不足）
//   降速后一样脏     -> 与时钟无关，是功放或数据格式本身
// TX 平时是 disabled 状态，所以这里直接 reconfig，不需要 enable/disable 包一层。
bool playerSetRate(uint32_t hz) {
  if (!s_tx || !s_tx_clk_ok) return false;
  if (s_state != P_IDLE) return false;
  i2s_std_clk_config_t clk = s_tx_clk;
  clk.sample_rate_hz = hz;
  esp_err_t e = i2s_channel_reconfig_std_clock(s_tx, &clk);
  if (e == ESP_OK) {
    s_tx_clk = clk;
    Serial.printf("tx_rate -> %lu Hz  BCLK=%lu Hz\n",
                  (unsigned long)hz, (unsigned long)(hz * 16 * 2));
  } else {
    Serial.printf("tx_rate %lu FAILED: %s\n", (unsigned long)hz, esp_err_to_name(e));
  }
  return e == ESP_OK;
}

// ---------------- I2S 初始化 ----------------
void audioInit() {
  s_sb = xStreamBufferCreate(PLAY_STREAM_BYTES, 1);
  configASSERT(s_sb);

#if MIC_AMP_SHARED_CLOCKS
  // 单总线方案（未实测！仅当 G 确认模块内部共时钟并报告 bb96 后才允许打开）：
  // 同一 I2S 端口同时承载 RX/TX，时钟只用 BCLK/LRCLK 一组，
  // 话筒 SD 与功放 DIN 共用该时钟，不再使用 GPIO4/5 输出第二组时钟。
  i2s_chan_config_t both_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  both_cfg.dma_desc_num = TX_DMA_DESC;
  both_cfg.dma_frame_num = TX_DMA_FRAME;
  ESP_ERROR_CHECK(i2s_new_channel(&both_cfg, &s_tx, &s_rx));
  i2s_std_config_t std_cfg = {
    .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(AUDIO_SAMPLE_RATE),
    .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
    .gpio_cfg = {
      .mclk = I2S_GPIO_UNUSED,
      .bclk = (gpio_num_t)PIN_AMP_BCLK,
      .ws   = (gpio_num_t)PIN_AMP_LRCLK,
      .dout = (gpio_num_t)PIN_AMP_DIN,
      .din  = (gpio_num_t)PIN_MIC_SD,
      .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
    },
  };
  std_cfg.slot_cfg.slot_mask = MIC_I2S_SLOT_LEFT ? I2S_STD_SLOT_LEFT : I2S_STD_SLOT_RIGHT;
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &std_cfg));  // 同端口 rx+tx 一起初始化
  // TX 方向改为 16-bit slot
  i2s_std_slot_config_t tx_slot =
      I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO);
  ESP_ERROR_CHECK(i2s_channel_reconfig_std_slot(s_tx, &tx_slot));
  ESP_ERROR_CHECK(i2s_channel_enable(s_rx));
  // TX 不 enable，播放时再 enable
#else
  // 默认：两路独立 I2S。RX=I2S_NUM_0（SD/SCK/WS），TX=I2S_NUM_1（DIN/BCLK/LRCLK）
  i2s_chan_config_t rx_chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  ESP_ERROR_CHECK(i2s_new_channel(&rx_chan_cfg, nullptr, &s_rx));
  i2s_std_config_t rx_std = {
    .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(AUDIO_SAMPLE_RATE),
    .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
    .gpio_cfg = {
      .mclk = I2S_GPIO_UNUSED,
      .bclk = (gpio_num_t)PIN_MIC_SCK,
      .ws   = (gpio_num_t)PIN_MIC_WS,
      .dout = I2S_GPIO_UNUSED,
      .din  = (gpio_num_t)PIN_MIC_SD,
      .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
    },
  };
  rx_std.slot_cfg.slot_mask = MIC_I2S_SLOT_LEFT ? I2S_STD_SLOT_LEFT : I2S_STD_SLOT_RIGHT;
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx, &rx_std));
  ESP_ERROR_CHECK(i2s_channel_enable(s_rx));  // RX 常开；录音时读取

  i2s_chan_config_t tx_chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
  tx_chan_cfg.dma_desc_num = TX_DMA_DESC;
  tx_chan_cfg.dma_frame_num = TX_DMA_FRAME;
  ESP_ERROR_CHECK(i2s_new_channel(&tx_chan_cfg, &s_tx, nullptr));
  i2s_std_config_t tx_std = {
    .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(AUDIO_SAMPLE_RATE),
    // 必须 STEREO：单槽会让 BCLK = 16 x LRCLK，MAX98357A 只认 32/48/64，
    // 帧结构不匹配时它输出的是噪音。写入时把单声道样本复制到左右两槽。
    .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
    .gpio_cfg = {
      .mclk = I2S_GPIO_UNUSED,
      .bclk = (gpio_num_t)PIN_AMP_BCLK,
      .ws   = (gpio_num_t)PIN_AMP_LRCLK,
      .dout = (gpio_num_t)PIN_AMP_DIN,
      .din  = I2S_GPIO_UNUSED,
      .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
    },
  };
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &tx_std));
  s_tx_clk = tx_std.clk_cfg;      // 记住时钟配置，playerSetRate 用
  s_tx_clk_ok = true;
  Serial.printf("tx_clk src=%d rate=%lu bclk=%lu\n",
                (int)tx_std.clk_cfg.clk_src,
                (unsigned long)tx_std.clk_cfg.sample_rate_hz,
                (unsigned long)(tx_std.clk_cfg.sample_rate_hz * 16 * 2));
  // TX 不 enable，播放时再 enable
#endif

  // 音频任务优先级高于网络（loopTask 为 1）
  xTaskCreatePinnedToCore(playerTask, "audio_tx", 4096, nullptr, 4, nullptr, 1);
  Serial.printf("audio_init ok shared_clocks=%d\n", (int)MIC_AMP_SHARED_CLOCKS);
}
