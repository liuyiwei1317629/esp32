// =============================================================
// voice_terminal.ino - ESP32-S3 语音终端固件骨架（F 包）
// 链路：ICS43434 录音 -> POST /turn -> bridge -> chunked PCM -> MAX98357A
// 状态机：IDLE -> RECORDING -> UPLOADING/PLAYING -> IDLE
// 任何错误回 IDLE 并打印原因；不死循环、不重启。
// =============================================================
#include <Arduino.h>
#include "esp_heap_caps.h"
#include "esp_psram.h"
#include "config.h"
#include "audio_io.h"
#include "http_turn.h"
#include "screen_client.h"
#include "wifi_setup.h"
#include "wake_word.h"

#if APP_MODE != APP_MODE_RECORD_ONLY
  #if __has_include("secrets.h")
    #include "secrets.h"
  #else
    #error "secrets.h not found: copy secrets.example.h to secrets.h and fill real values. Do NOT commit it."
  #endif
#endif

enum AppState : uint8_t { ST_IDLE, ST_RECORDING, ST_TURN };
static AppState s_state = ST_IDLE;

static uint8_t* s_rec = nullptr;   // PSRAM 录音缓冲（纯 PCM，不含 WAV 头）
static bool s_waitRelease = false; // 打断后须先松开按键，避免同一次按键又触发录音
static bool s_recFromWake = false; // 本次录音由“小鱼”语音唤醒触发（无按键，靠静音收尾）

// ---------------- 按键（GPIO0，低有效，30 ms 去抖） ----------------
static bool rawDown() { return digitalRead(PIN_BUTTON) == LOW; }

static bool debouncedDown() {
  static bool stable = false;
  static bool last_raw = false;
  static uint32_t changed_at = 0;
  bool r = rawDown();
  if (r != last_raw) { last_raw = r; changed_at = millis(); }
  if (millis() - changed_at >= BUTTON_DEBOUNCE_MS) stable = r;
  return stable;
}

// 播放期间的打断轮询（仅 TURN_STOP 注册）。
// 录音那一次按键必须已经松开：否则"按住录音 → 静音/超时自动结束"会在
// 播放刚一开始时被当成新的打断按键。
#if APP_MODE == APP_MODE_TURN_STOP
static bool abortPoll() {
  bool down = debouncedDown();
  if (!down) s_waitRelease = false;
  return down && !s_waitRelease;
}
#endif

// ---------------- 串口日志 ----------------
static void logState(const char* s) {
  // 不用 ESP.getFreePsram()：它在本板会随 psramFound() 误报而恒为 0
  Serial.printf("state=%s heap=%u psram=%u\n", s,
                (unsigned)ESP.getFreeHeap(),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
}

// ---------------- 44 字节 WAV 头（PCM=1，mono，16000 Hz，16 bit） ----------------
#if APP_MODE != APP_MODE_RECORD_ONLY
static void buildWavHeader(uint8_t h[44], uint32_t pcm_bytes) {
  uint32_t byte_rate = AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * (AUDIO_BITS / 8);
  uint16_t block_align = AUDIO_CHANNELS * (AUDIO_BITS / 8);
  uint32_t riff_size = 36 + pcm_bytes;
  memcpy(h + 0,  "RIFF", 4);
  memcpy(h + 4,  &riff_size, 4);
  memcpy(h + 8,  "WAVE", 4);
  memcpy(h + 12, "fmt ", 4);
  uint32_t fmt_size = 16;            memcpy(h + 16, &fmt_size, 4);
  uint16_t audio_fmt = 1;            memcpy(h + 20, &audio_fmt, 2);
  uint16_t channels = AUDIO_CHANNELS;memcpy(h + 22, &channels, 2);
  uint32_t rate = AUDIO_SAMPLE_RATE; memcpy(h + 24, &rate, 4);
  memcpy(h + 28, &byte_rate, 4);
  memcpy(h + 32, &block_align, 2);
  uint16_t bits = AUDIO_BITS;        memcpy(h + 34, &bits, 2);
  memcpy(h + 36, "data", 4);
  memcpy(h + 40, &pcm_bytes, 4);
}
#endif

// ---------------- Wi-Fi（仅 TURN / TURN_STOP 使用；2.4 GHz） ----------------
// 细节都在 wifi_setup.cpp：NVS 账号 -> secrets.h 账号 -> 热点配网。
// 这里每次都重试，因为路由器重启后要能自己连回去。
#if APP_MODE != APP_MODE_RECORD_ONLY
static bool wifiEnsure() { return wifiSetupEnsure(); }
#endif

// ---------------- 录音：松手 / 静音 1.2 s / 最长 15 s ----------------
// 返回 PCM 字节数；0 表示丢弃（太短或错误）。PCM 写入 s_rec。
// fromWake=true（“小鱼”唤醒）：没有按键可松，结束只靠 静音 1.2s / 超时 / 缓冲满。
static size_t recordClip(uint32_t* out_ms, bool fromWake) {
  constexpr size_t FRAME_SAMPLES = 320;      // 20 ms @ 16 kHz
  static int32_t raw[FRAME_SAMPLES];
  uint32_t t0 = millis();
  uint32_t last_voice = t0;
  size_t total_bytes = 0;
  double sum_sq = 0; uint32_t n_s16 = 0;
  int16_t peak = 0;
  int32_t raw_min = INT32_MAX, raw_max = INT32_MIN; double raw_sq = 0; uint32_t n_raw = 0;
  int16_t s16_min = INT16_MAX, s16_max = INT16_MIN;
  bool voiceYet = false;
  int voiceRun = 0;       // 连续超阈值的帧数（防提示音余音被当成人声）
  float noiseFloor = 200.0f;   // 环境底噪估计（s16 RMS），随静音帧缓慢下探

  // 丢掉开头 5 帧（100 ms）。提示音播完到 I2S RX 重新使能之间会吃到一次
  // 切换尖峰 + 功放余音（MAX98357A 本身有 ~20 ms 延迟），实测尾音持续约
  // 60~100 ms。留在录音里会污染峰值统计：5 次实测的归一化倍数在 3.72x 与
  // 7.48x 之间跳，表现为"同样距离说话，有时响有时轻"。
  for (int i = 0; i < 5; i++) micReadRaw(raw, FRAME_SAMPLES, 50);

  for (;;) {
    // 结束条件：松开按键（仅按键录音）/ 静音 1.2 s（仅在人声出现之后）/ 最长 15 s
    if (!fromWake && !debouncedDown()) { s_waitRelease = false; break; }
    uint32_t now = millis();
    if (now - t0 >= REC_MAX_MS) break;
    if (total_bytes + FRAME_SAMPLES * 2 > REC_BUF_BYTES) break;

    size_t got = micReadRaw(raw, FRAME_SAMPLES, 100);
    if (got == 0) continue;

    double frame_sq = 0;
    int16_t* dst = (int16_t*)(s_rec + total_bytes);
    for (size_t i = 0; i < got; i++) {
      int32_t r = raw[i];
      if (r < raw_min) raw_min = r;
      if (r > raw_max) raw_max = r;
      raw_sq += (double)r * r;
      int16_t v = mic24_to_s16(r);
      dst[i] = v;
      if (v < s16_min) s16_min = v;
      if (v > s16_max) s16_max = v;
      int16_t a = v < 0 ? (v == INT16_MIN ? INT16_MAX : -v) : v;
      if (a > peak) peak = a;
      sum_sq += (double)v * v;
      frame_sq += (double)v * v;
    }
    n_raw += got; n_s16 += got;
    total_bytes += got * 2;

    float frame_rms = sqrtf((float)(frame_sq / got));
    float thr = noiseFloor * REC_VAD_FACTOR;
    if (thr < (float)REC_VAD_MIN_RMS) thr = (float)REC_VAD_MIN_RMS;

    // 唤醒触发时，「嘀」声和功放余音会顶过 VAD 阈值；若把它当人声，
    // 用户还没开口就按 1.2 s 静音收尾，录到空片段。前 0.8 s 不判开口，
    // 且要连续 3 帧（60 ms）超过阈值才算真的开口。
    const bool loud = (frame_rms > thr);
    const bool onsetOk = (millis() - t0 >= (fromWake ? 800u : 0u));
    if (loud && onsetOk) {
      if (++voiceRun >= 3) voiceYet = true;
      last_voice = millis();
    } else if (!loud) {
      voiceRun = 0;
      if (!voiceYet && frame_rms < noiseFloor) {
        // 只在人声出现前跟踪底噪（慢速下探），开口后不再改阈值
        noiseFloor = noiseFloor * 0.9f + frame_rms * 0.1f;
      }
    }

    if (voiceYet && (millis() - last_voice >= REC_SILENCE_END_MS)) break;
    if (!voiceYet && (millis() - t0 >= REC_LEAD_MS)) break;   // 一直没开口：别空录
  }

  uint32_t ms = millis() - t0;
  *out_ms = ms;
  float rms = n_s16 ? sqrtf((float)(sum_sq / n_s16)) : 0.0f;
  float raw_rms = n_raw ? sqrtf((float)(raw_sq / n_raw)) : 0.0f;
  Serial.printf("mic_raw min=%ld max=%ld rms=%.0f | s16 min=%d max=%d rms=%.0f\n",
                (long)(n_raw ? raw_min : 0), (long)(n_raw ? raw_max : 0), raw_rms,
                (int)(n_s16 ? s16_min : 0), (int)(n_s16 ? s16_max : 0), rms);
  Serial.printf("record_ms=%lu bytes=%u rms=%.0f peak=%d floor=%.0f voice=%d\n",
                (unsigned long)ms, (unsigned)total_bytes, rms, (int)peak,
                (double)noiseFloor, (int)voiceYet);

  if (ms < REC_MIN_MS) {   // 短于 0.3 s 丢弃
    Serial.println("record_discarded=too_short");
    return 0;
  }
  if (!voiceYet) {
    Serial.println("record_discarded=no_voice(lead_timeout)");
    return 0;
  }
  return total_bytes;
}

// ---------------- 提示音（喇叭通路自检） ----------------
// 发一声短"嘀"：开机一次、每次按下录音键一次。
// 目的只有一个：把"软件没送数据"和"功放/喇叭不出声"当场分开。
// 听到嘀 = 功放/喇叭/接线全好，问题只可能在话筒或数据处理。
// 1024 点正弦表（Q15）。只在 setup 里算一次。
// 为什么不用 sinf(2π·hz·k/fs) 直接算：k 累积到几万时相位参数会到 1e5 弧度量级，
// float 只有 24 位尾数，在那个量级上的角度精度只有千分之几弧度，而且误差随 k
// 线性增长 —— 越往后波形越抖，听感就是"一直发毛"。前面几轮基于测试音的判断
// 可能都被这个误差污染过。
static int16_t g_sine[1024];

static void sineInit() {
  for (int i = 0; i < 1024; i++) {
    g_sine[i] = (int16_t)lrintf(sinf(2.0f * (float)PI * (float)i / 1024.0f) * 32767.0f);
  }
}

// 播放用的常驻缓冲（PSRAM，只分配一次）
static int16_t* toneBufGet(size_t samples) {
  static int16_t* buf = nullptr;
  static size_t   cap = 0;
  if (samples > cap) {
    if (buf) heap_caps_free(buf);
    cap = samples + 1024;
    buf = (int16_t*)heap_caps_malloc(cap * 2, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buf) { cap = 0; Serial.println("tone_buf=alloc_failed"); return nullptr; }
  }
  return buf;
}

// 纯音：整段波形先算完放进 PSRAM，再一次性交给播放任务。
//
// 相位用 32 位定点累加器（Q32）+ 1024 点查表，全程无浮点累积误差：
//   step  = hz / fs × 2^32，每样本整数相加，溢出自然回绕，正好一个周期
//   index = phase >> 22   （取高 10 位 → 0..1023）
// 这样无论播 3 秒还是 30 秒，波形精度完全一致。
//
// 另外：旧版是"边算 sinf 边 playerFeed"，音频任务 15 ms 内取不到数据就会
// underrun 写静音。现在播放期间主任务完全不参与，DMA 由缓冲直接供给。
// 波形生成用的采样率。诊断时会临时改（playerSetRate），
// 所以生成侧必须跟着变，否则同一 hz 参数听起来音高会变，对照就不成立。
static uint32_t g_gen_rate = AUDIO_SAMPLE_RATE;

static void playTone(uint16_t hz, uint16_t ms, int16_t amp) {
  const uint32_t fs    = g_gen_rate;
  const uint32_t total = fs * ms / 1000;
  const uint32_t fade  = fs * 5 / 1000;    // 5 ms 淡入淡出
  const uint32_t lead  = fs * 20 / 1000;   // 20 ms 前导静音

  const size_t need = (size_t)lead + total + lead;
  int16_t* b = toneBufGet(need);
  if (!b) return;

  memset(b, 0, (size_t)lead * 2);                       // 前导静音（同时避开功放上电咔声）
  memset(b + lead + total, 0, (size_t)lead * 2);        // 尾部静音
  int16_t* p = b + lead;

  const uint32_t step  = (uint32_t)(((uint64_t)hz << 32) / fs);
  const float    scale = (float)amp / 32767.0f;
  uint32_t phase = 0;
  for (uint32_t k = 0; k < total; k++) {
    float env = 1.0f;
    if (k < fade)               env = (float)k / (float)fade;
    else if (k + fade >= total) env = (float)(total - k) / (float)fade;
    int32_t s = (int32_t)g_sine[phase >> 22];
    p[k] = (int16_t)((float)s * scale * env);
    phase += step;
  }

  playerBegin();
  playerFeed((const uint8_t*)b, need * 2);   // 一次性入队（远小于 32 KB 流缓冲）
  playerEnd();
  while (playerBusy()) delay(1);
  Serial.printf("  -> underruns=%lu played=%lu/need=%u\n",
                (unsigned long)playerUnderruns(),
                (unsigned long)playerPlayedBytes(), (unsigned)(need * 2));
}

// ---------------- 本地回放（RECORD_ONLY 验证话筒/功放/喇叭/电源/引脚） ----------------
#if APP_MODE == APP_MODE_RECORD_ONLY
// 把录音原始 PCM 以 base64 从串口导出，供 PC 端做频谱分析（定位噪音来源）。
// 只在 RECORD_ONLY 且 MIC_DUMP=1 时编译进去；正式版关掉。
// 注意：一段 4.8 s 录音要传 205 KB base64，115200 波特率要 ~18 s，
// 期间喇叭一声不出，很容易被误判成"卡死/没声音"。诊断完必须关掉。
#define MIC_DUMP 0

#if MIC_DUMP
static const char B64T[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static void dumpB64(const uint8_t* p, size_t n) {
  Serial.printf("DUMP %u\n", (unsigned)n);
  char line[516];
  size_t li = 0, i = 0;
  while (i + 2 < n) {
    uint32_t v = ((uint32_t)p[i] << 16) | ((uint32_t)p[i + 1] << 8) | p[i + 2];
    line[li++] = B64T[(v >> 18) & 63];
    line[li++] = B64T[(v >> 12) & 63];
    line[li++] = B64T[(v >> 6) & 63];
    line[li++] = B64T[v & 63];
    i += 3;
    if (li >= 512) { line[li] = 0; Serial.println(line); li = 0; }
  }
  size_t rem = n - i;
  if (rem) {
    uint32_t v = (uint32_t)p[i] << 16;
    if (rem == 2) v |= (uint32_t)p[i + 1] << 8;
    line[li++] = B64T[(v >> 18) & 63];
    line[li++] = B64T[(v >> 12) & 63];
    line[li++] = (rem == 2) ? B64T[(v >> 6) & 63] : '=';
    line[li++] = '=';
  }
  if (li) { line[li] = 0; Serial.println(line); }
  Serial.flush();
  Serial.println("DUMPEND");
}
#endif

static void localPlayback(const uint8_t* pcm, size_t n) {
  uint32_t t0 = millis();
  playerBegin();
  size_t off = 0;
  while (off < n) {           // 小段喂入；流缓冲满时 playerFeed 自带等待
    size_t want = n - off;
    if (want > PLAY_FRAME_BYTES) want = PLAY_FRAME_BYTES;
    playerFeed(pcm + off, want);
    off += want;
  }
  playerEnd();
  while (playerBusy()) delay(5);
  Serial.printf("play_ms=%lu underruns=%lu\n",
                (unsigned long)(millis() - t0), (unsigned long)playerUnderruns());
}
#endif

// ---------------- setup ----------------
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\nvoice_terminal skeleton mode=%d (0=RECORD_ONLY 1=TURN 2=TURN_STOP)\n",
                (int)APP_MODE);

  // 不用 psramFound()：本板（core 3.3.12 + OPI PSRAM）该标志恒为 false，
  // 但 PSRAM 实际可用（esp_psram_is_initialized()=1、容量 8MB），故直接读 IDF 层真实容量。
  if (esp_psram_get_size() == 0) {   // 没有 PSRAM 就停住，不静默用内部 RAM
    Serial.println("FATAL: PSRAM not found. Check board settings: ESP32S3 Dev Module / OPI PSRAM.");
    for (;;) delay(1000);
  }
  s_rec = (uint8_t*)heap_caps_malloc(REC_BUF_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!s_rec) {
    Serial.println("FATAL: PSRAM alloc failed for record buffer");
    for (;;) delay(1000);
  }
  Serial.printf("rec_buf=%uB psram_free=%u\n",
                (unsigned)REC_BUF_BYTES,
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

  pinMode(PIN_BUTTON, INPUT_PULLUP);   // BOOT 低有效；复位时不要按住
  audioInit();
  sineInit();          // 1024 点正弦表，playTone 查表用（一次算好，之后零浮点累积）
  delay(200);
  if (WAKE_WORD_ENABLE) {
    // “小鱼”离线唤醒。失败（模型分区缺失/内存不足）只关掉语音唤醒，
    // BOOT 键录音流程完全不受影响 —— 卡在这里只会让整机变砖，不值得。
    wakeWordInit();
  }
#if APP_MODE == APP_MODE_TURN_STOP
  httpSetAbortPoll(abortPoll);
#endif
#if APP_MODE != APP_MODE_RECORD_ONLY
  wifiEnsure();
#endif
  screenStart();       // 屏任务独立跑在 core 0 / prio 2，不碰音频任务
  logState("IDLE");
}

// ---------------- loop：状态机 ----------------
void loop() {
  switch (s_state) {
    case ST_IDLE: {
#if APP_MODE != APP_MODE_RECORD_ONLY
      wifiSetupPollSerial();   // 串口 portal / forget
#endif
      // ---- 语音唤醒“小鱼”：IDLE 态常开监听 ----
      // 录音/上传/播放结束后回到 IDLE 的第一圈先恢复监听（内部会丢弃
      // 功放余音再开始检测），之后每圈喂一帧给 AFE+MultiNet。
      if (WAKE_WORD_ENABLE && wakeWordReady() && !wakeWordEnabled()) wakeWordResume();
      if (WAKE_WORD_ENABLE && wakeWordPoll()) {     // 命中“小鱼”
        wakeWordSuspend();                          // 录音期间停监听，避免争抢麦克风
        s_recFromWake = true;
        screenNotifyKey();                          // 联网提示与按键路径一致
        playTone(1000, 60, 4000);                   // “嘀” = 开始录音
        s_state = ST_RECORDING;
        logState("RECORDING(wake)");
        break;
      }
      bool down = debouncedDown();
      if (!down) s_waitRelease = false;
      if (down && !s_waitRelease) {   // 按住 BOOT 开始录音
        s_waitRelease = true;         // 防止 RECORD_ONLY/错误早退后按住键重复触发
        wakeWordSuspend();            // 按键录音同样要停唤醒监听
        s_recFromWake = false;
        screenNotifyKey();            // 只置标志：联网在屏幕任务里做，录音不等它
        playTone(1000, 60, 4000);     // "嘀" = 开始录音（电平压低，减少被话筒拾取）
        s_state = ST_RECORDING;
        logState("RECORDING");
      }
      delay(5);
      break;
    }

    case ST_RECORDING: {
      uint32_t rec_ms = 0;
      size_t pcm_bytes = recordClip(&rec_ms, s_recFromWake);
      if (pcm_bytes == 0) {
        s_state = ST_IDLE;
        logState("IDLE");
        break;
      }
      // 整段后处理：去直流 + 250~3800Hz 带通 + 降噪门 + 归一化。
      // 必须在播放/上传之前，播放侧只做安全限幅。
#if APP_MODE == APP_MODE_RECORD_ONLY && MIC_DUMP
      dumpB64(s_rec, pcm_bytes);   // 先把未处理的原始 PCM 导出去做频谱分析
#endif
      if (!clipEnhance((int16_t*)s_rec, pcm_bytes / 2)) {
        // 没录到人声：不放噪音、不上传
        s_state = ST_IDLE;
        logState("IDLE");
        break;
      }

#if APP_MODE == APP_MODE_RECORD_ONLY
      localPlayback(s_rec, pcm_bytes);
      s_state = ST_IDLE;
      logState("IDLE");
#else
      s_state = ST_TURN;
      logState("UPLOADING/PLAYING");
      if (!wifiEnsure()) {            // 断线重连失败也回 IDLE
        Serial.println("err=wifi_unavailable");
        s_state = ST_IDLE;
        logState("IDLE");
        break;
      }

      // WAV = 44 字节头 + PCM；Content-Length = 44 + pcm_bytes
      static uint8_t wav_hdr[44];
      buildWavHeader(wav_hdr, pcm_bytes);
      const size_t wav_len = 44 + pcm_bytes;
      uint8_t* wav = (uint8_t*)heap_caps_malloc(wav_len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
      if (!wav) {
        Serial.println("err=wav_alloc_failed");
        s_state = ST_IDLE;
        logState("IDLE");
        break;
      }
      memcpy(wav, wav_hdr, 44);
      memcpy(wav + 44, s_rec, pcm_bytes);

      uint32_t t0 = millis();
      playerBegin();
      TurnResult res;
      httpPostTurn(wav, wav_len, &res);
      playerEnd();
      while (playerBusy()) {
#if APP_MODE == APP_MODE_TURN_STOP
        // 流已经结束但本地还在排空时，也必须能打断。先 playerAbort()
        // 保证本地立刻静音，再发 /stop（网络往返不能拖住 flush）。
        if (!res.aborted && abortPoll()) {
          playerAbort();
          uint32_t stop_ms = 0;
          int stop_status = 0;
          httpPostStop(&stop_ms, &stop_status);
          res.aborted = true;
          s_waitRelease = true;
          Serial.printf("stop_ms=%lu result=%d\n",
                        (unsigned long)stop_ms, stop_status);
        }
#endif
        delay(5);
      }
      heap_caps_free(wav);

      uint32_t total_ms = millis() - t0;
      Serial.printf("upload_ms=%lu status=%d first_pcm_ms=%lu pcm_bytes=%lu\n",
                    (unsigned long)total_ms, res.status,
                    (unsigned long)res.first_pcm_ms, (unsigned long)res.pcm_bytes);
      Serial.printf("play_ms=%lu underruns=%lu\n",
                    (unsigned long)(res.pcm_bytes / BYTES_PER_MS),
                    (unsigned long)playerUnderruns());
      if (res.no_voice) Serial.println("result=no_voice(204)");
      else if (res.aborted) Serial.println("result=aborted");
      else if (!res.ok) Serial.printf("err=%s\n", res.err);
      else Serial.println("result=ok");

      s_waitRelease = true;   // 若刚才是打断退出，先等松手
      s_state = ST_IDLE;
      logState("IDLE");
#endif
      break;
    }

    case ST_TURN:   // 实际处理在 ST_RECORDING 分支内顺序完成
    default:
      s_state = ST_IDLE;
      break;
  }
}
