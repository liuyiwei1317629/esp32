// =============================================================
// wake_word.cpp - 离线语音唤醒“小鱼”实现
// 头文件注释里有整体设计；这里只写实现要点。
//
// 1) esp_srmodel_init("model") 扫描模型分区；我们打包时只放了
//    mn7_cn + vadnet1_medium + fst，没有 wakenet 模型 —— 这是刻意的：
//    WakeNet 通道整体没有数据可用，“Hi ESP” 从机制上不可能再触发。
// 2) AFE 用 afe_config_init("M", ...) 的默认链（NS=WEBRTC、VAD=vadnet），
//    显式关掉 wakenet_init。
// 3) MultiNet 命令词是全局单例（esp_mn_commands_*），只注册一条：
//    “xiao yu”。检测状态机 DETECTED/TIMEOUT 都要 clean() 复位，
//    否则 CTC 解码器会一直消化旧音频。
// 4) feed / detect 帧长不同：AFE fetch 出来的帧先积累进 FIFO，
//    攒够一个 MultiNet 帧再喂 detect()。
// =============================================================
#include "ESP_SR.h"  // 不用它的类；包含它才会让构建系统编译 ESP_SR 库并触发
                     // platform.txt 的 postobjcopy 钩子，把 srmodels.bin 拷进
                     // 构建目录（烧录时烧到 model 分区，见 HANDOFF.md）。
                     // 包装库拖入的 flite/wakenet 会被链接器 --gc-sections 回收。
#include "config.h"
#include "audio_io.h"
#include "wake_word.h"

#include <string.h>
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_mn_iface.h"
#include "esp_mn_models.h"
#include "esp_mn_speech_commands.h"
#include "model_path.h"

// “小鱼”在 MultiNet 命令表里的 command_id（只有一个命令，取 1）
#define WAKE_CMD_ID 1

static srmodel_list_t*      s_models   = nullptr;
static afe_config_t*        s_afe_cfg  = nullptr;
static esp_afe_sr_iface_t*  s_afe      = nullptr;
static esp_afe_sr_data_t*   s_afe_data = nullptr;
static esp_mn_iface_t*      s_mn       = nullptr;
static model_iface_data_t*  s_mn_data  = nullptr;

static int      s_feedChunk = 0;    // AFE 一帧的样本数（16-bit）
static int      s_mnChunk   = 0;    // MultiNet 一帧的样本数
static int32_t* s_raw       = nullptr;  // micReadRaw 原始 32-bit slot
static int16_t* s_in        = nullptr;  // 转换后的 s16 送 AFE
static int16_t* s_fifo      = nullptr;  // fetch -> detect 的积累缓冲
static int      s_fifoLen   = 0;        // FIFO 内现有样本数

static volatile bool s_ready   = false;
static volatile bool s_enabled = false;

bool wakeWordReady()   { return s_ready; }
bool wakeWordEnabled() { return s_ready && s_enabled; }
void wakeWordSuspend() { s_enabled = false; }

bool wakeWordInit() {
  s_models = esp_srmodel_init("model");
  if (!s_models || s_models->num == 0) {
    Serial.println("wake: FATAL no models in 'model' partition (flash firmware/tools/srmodels.bin)");
    return false;
  }
  char* mn_name = esp_srmodel_filter(s_models, ESP_MN_PREFIX, NULL);
  if (!mn_name) {
    Serial.println("wake: FATAL no multinet model in partition");
    return false;
  }
  Serial.printf("wake: models=%d multinet=%s\n", s_models->num, mn_name);

  // AFE：单麦克风 “M”，SR 场景，高性能模式；默认带 WEBRTC NS + vadnet VAD。
  s_afe_cfg = afe_config_init("M", s_models, AFE_TYPE_SR, AFE_MODE_HIGH_PERF);
  if (!s_afe_cfg) {
    Serial.println("wake: FATAL afe_config_init failed");
    return false;
  }
  s_afe_cfg->wakenet_init = false;                  // 不用 WakeNet：旧词通道整体关闭
  s_afe_cfg->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;
  s_afe_cfg->afe_perferred_core = 0;                // AFE 内部任务放 core0（core1 有播放/主循环）

  s_afe = (esp_afe_sr_iface_t*)esp_afe_handle_from_config(s_afe_cfg);
  if (!s_afe) {
    Serial.println("wake: FATAL afe handle failed");
    return false;
  }
  s_afe_data = s_afe->create_from_config(s_afe_cfg);
  if (!s_afe_data) {
    Serial.println("wake: FATAL afe create failed");
    return false;
  }
  s_feedChunk = s_afe->get_feed_chunksize(s_afe_data);

  // MultiNet：mn7_cn（中文，命令词运行时可配）
  s_mn = esp_mn_handle_from_name(mn_name);
  if (!s_mn) {
    Serial.println("wake: FATAL multinet handle failed");
    return false;
  }
  s_mn_data = s_mn->create(mn_name, WAKE_DET_TIMEOUT_MS);
  if (!s_mn_data) {
    Serial.println("wake: FATAL multinet create failed");
    return false;
  }
  char* lang = s_mn->get_language(s_mn_data);
  if (!lang || strcmp(lang, ESP_MN_CHINESE) != 0) {
    Serial.printf("wake: FATAL multinet lang=%s (expect cn)\n", lang ? lang : "null");
    return false;
  }
  s_mnChunk = s_mn->get_samp_chunksize(s_mn_data);

  // 唯一命令词：“小鱼”。拼音不带声调（mn7_cn 中文命令格式）。
  esp_mn_commands_clear();
  esp_err_t e = esp_mn_commands_add(WAKE_CMD_ID, WAKE_WORD_PINYIN);
  esp_mn_error_t* err = (e == ESP_OK) ? esp_mn_commands_update() : nullptr;
  if (e != ESP_OK || err != nullptr) {
    Serial.printf("wake: FATAL command '%s' rejected (err=%d phrases=%d)\n",
                  WAKE_WORD_PINYIN, (int)e, err ? err->num : -1);
    return false;
  }
  s_mn->set_det_threshold(s_mn_data, WAKE_DET_THRESHOLD);
  s_mn->print_active_speech_commands(s_mn_data);
  Serial.printf("wake: ok word='%s' feed=%d mn=%d samples/frame\n",
                WAKE_WORD_PINYIN, s_feedChunk, s_mnChunk);

  // 帧缓冲（内部 RAM，几 KB，热路径要快）
  s_raw  = (int32_t*)malloc(sizeof(int32_t) * s_feedChunk);
  s_in   = (int16_t*)malloc(sizeof(int16_t) * s_feedChunk);
  s_fifo = (int16_t*)malloc(sizeof(int16_t) * (2 * s_mnChunk + s_feedChunk));
  if (!s_raw || !s_in || !s_fifo) {
    Serial.println("wake: FATAL buffer alloc failed");
    return false;
  }

  s_ready = true;
  s_enabled = true;
  return true;
}

void wakeWordResume() {
  if (!s_ready) return;
  // 清掉提示音/播放串进来的旧音频：AFE 缓冲复位 + 丢弃一段实时输入
  s_afe->reset_buffer(s_afe_data);
  s_mn->clean(s_mn_data);
  s_fifoLen = 0;
  const uint32_t t0 = millis();
  while (millis() - t0 < WAKE_RESUME_DISCARD_MS) {
    micReadRaw(s_raw, s_feedChunk, 50);   // 读掉就扔
  }
  s_enabled = true;
}

bool wakeWordPoll() {
  if (!s_ready || !s_enabled) return false;

  size_t got = micReadRaw(s_raw, s_feedChunk, 100);
  if (got == 0) return false;
  if (got < (size_t)s_feedChunk) {          // 超时部分帧：补读凑满整帧再喂
    got += micReadRaw(s_raw + got, s_feedChunk - got, 100);
    if (got < (size_t)s_feedChunk) return false;
  }
  for (int i = 0; i < s_feedChunk; i++) s_in[i] = mic24_to_s16(s_raw[i]);
  s_afe->feed(s_afe_data, s_in);

  // 每次 feed 只取一帧：AFE 输出队列不会空，drain 式循环会永远退不出来。
  bool hit = false;
  afe_fetch_result_t* res = s_afe->fetch_with_delay(s_afe_data, 0);
  if (res == nullptr || res->data == nullptr || res->data_size <= 0) return false;
  const int n = res->data_size / (int)sizeof(int16_t);
  if (s_fifoLen + n > 2 * s_mnChunk + s_feedChunk) s_fifoLen = 0;  // 溢出保护
  memcpy(s_fifo + s_fifoLen, res->data, (size_t)n * sizeof(int16_t));
  s_fifoLen += n;
  while (s_fifoLen >= s_mnChunk) {
    esp_mn_state_t st = s_mn->detect(s_mn_data, s_fifo);
    memmove(s_fifo, s_fifo + s_mnChunk,
            (size_t)(s_fifoLen - s_mnChunk) * sizeof(int16_t));
    s_fifoLen -= s_mnChunk;

    if (st == ESP_MN_STATE_DETECTED) {
      esp_mn_results_t* r = s_mn->get_results(s_mn_data);
      const bool thisHit = (r->num > 0) && (r->command_id[0] == WAKE_CMD_ID);
      Serial.printf("wake: detected '%s' id=%d prob=%.2f\n",
                    r->string, r->command_id[0], (double)r->prob[0]);
      s_mn->clean(s_mn_data);
      s_fifoLen = 0;
      if (thisHit) { hit = true; break; }
    } else if (st == ESP_MN_STATE_TIMEOUT) {
      s_mn->clean(s_mn_data);          // 没听全命令词：复位解码器
      s_fifoLen = 0;
    }
  }

  return hit;
}
