// =============================================================
// voice_terminal.ino - ESP32-S3 语音终端固件骨架（F 包）
// 链路：ICS43434 录音 -> POST /turn -> bridge -> chunked PCM -> MAX98357A
// 状态机：IDLE -> RECORDING -> UPLOADING/PLAYING -> IDLE
// 任何错误回 IDLE 并打印原因；不死循环、不重启。
// =============================================================
#include <Arduino.h>
#include <WiFi.h>
#include "esp_heap_caps.h"
#include "config.h"
#include "audio_io.h"
#include "http_turn.h"

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
  Serial.printf("state=%s heap=%u psram=%u\n", s,
                (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getFreePsram());
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
#if APP_MODE != APP_MODE_RECORD_ONLY
static bool wifiEnsure() {
  if (WiFi.status() == WL_CONNECTED) return true;
  uint32_t t0 = millis();
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);   // 降低延迟
  WiFi.begin(WIFI_SSID, WIFI_PASS);   // 不打印密码
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - t0 > WIFI_CONNECT_TIMEOUT_MS) {
      Serial.println("wifi_ms=timeout ip=none");
      return false;
    }
    delay(200);
  }
  Serial.printf("wifi_ms=%lu ip=%s\n", (unsigned long)(millis() - t0),
                WiFi.localIP().toString().c_str());
  return true;
}
#endif

// ---------------- 录音：松手 / 静音 1.2 s / 最长 15 s ----------------
// 返回 PCM 字节数；0 表示丢弃（太短或错误）。PCM 写入 s_rec。
static size_t recordClip(uint32_t* out_ms) {
  constexpr size_t FRAME_SAMPLES = 320;      // 20 ms @ 16 kHz
  static int32_t raw[FRAME_SAMPLES];
  uint32_t t0 = millis();
  uint32_t last_voice = t0;
  size_t total_bytes = 0;
  double sum_sq = 0; uint32_t n_s16 = 0;
  int16_t peak = 0;
  int32_t raw_min = INT32_MAX, raw_max = INT32_MIN; double raw_sq = 0; uint32_t n_raw = 0;
  int16_t s16_min = INT16_MAX, s16_max = INT16_MIN;

  for (;;) {
    // 结束条件：松开按键 / 静音 1.2 s / 最长 15 s
    if (!debouncedDown()) { s_waitRelease = false; break; }
    uint32_t now = millis();
    if (now - t0 >= REC_MAX_MS) break;
    if (now - last_voice >= REC_SILENCE_END_MS) break;
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
    if (frame_rms >= REC_SILENCE_RMS) last_voice = millis();
  }

  uint32_t ms = millis() - t0;
  *out_ms = ms;
  float rms = n_s16 ? sqrtf((float)(sum_sq / n_s16)) : 0.0f;
  float raw_rms = n_raw ? sqrtf((float)(raw_sq / n_raw)) : 0.0f;
  Serial.printf("mic_raw min=%ld max=%ld rms=%.0f | s16 min=%d max=%d rms=%.0f\n",
                (long)(n_raw ? raw_min : 0), (long)(n_raw ? raw_max : 0), raw_rms,
                (int)(n_s16 ? s16_min : 0), (int)(n_s16 ? s16_max : 0), rms);
  Serial.printf("record_ms=%lu bytes=%u rms=%.0f peak=%d\n",
                (unsigned long)ms, (unsigned)total_bytes, rms, (int)peak);

  if (ms < REC_MIN_MS) {   // 短于 0.3 s 丢弃
    Serial.println("record_discarded=too_short");
    return 0;
  }
  return total_bytes;
}

// ---------------- 本地回放（RECORD_ONLY 验证话筒/功放/喇叭/电源/引脚） ----------------
#if APP_MODE == APP_MODE_RECORD_ONLY
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

  if (!psramFound()) {   // 没有 PSRAM 就停住，不静默用内部 RAM
    Serial.println("FATAL: PSRAM not found. Check board settings: ESP32S3 Dev Module / OPI PSRAM.");
    for (;;) delay(1000);
  }
  s_rec = (uint8_t*)heap_caps_malloc(REC_BUF_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!s_rec) {
    Serial.println("FATAL: PSRAM alloc failed for record buffer");
    for (;;) delay(1000);
  }
  Serial.printf("rec_buf=%uB psram_free=%u\n",
                (unsigned)REC_BUF_BYTES, (unsigned)ESP.getFreePsram());

  pinMode(PIN_BUTTON, INPUT_PULLUP);   // BOOT 低有效；复位时不要按住
  audioInit();

#if APP_MODE == APP_MODE_TURN_STOP
  httpSetAbortPoll(abortPoll);
#endif
#if APP_MODE != APP_MODE_RECORD_ONLY
  wifiEnsure();
#endif
  logState("IDLE");
}

// ---------------- loop：状态机 ----------------
void loop() {
  switch (s_state) {
    case ST_IDLE: {
      bool down = debouncedDown();
      if (!down) s_waitRelease = false;
      if (down && !s_waitRelease) {   // 按住 BOOT 开始录音
        s_waitRelease = true;         // 防止 RECORD_ONLY/错误早退后按住键重复触发
        s_state = ST_RECORDING;
        logState("RECORDING");
      }
      delay(5);
      break;
    }

    case ST_RECORDING: {
      uint32_t rec_ms = 0;
      size_t pcm_bytes = recordClip(&rec_ms);
      if (pcm_bytes == 0) {
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
