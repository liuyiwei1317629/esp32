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

// ---------------- 播放器状态 ----------------
enum PlayerState : uint8_t { P_IDLE, P_FILLING, P_PLAYING };
static volatile PlayerState s_state = P_IDLE;
static volatile bool     s_end = false;    // 网络侧已喂完（流结束）
static volatile bool     s_abort = false;  // /stop 打断
static volatile uint32_t s_underruns = 0;
static volatile uint32_t s_played = 0;

// TX DMA 深度（仅用于会话结束时一次性估算 DMA 排空等待，不用来做节拍）
static constexpr uint32_t TX_DMA_DESC  = 6;
static constexpr uint32_t TX_DMA_FRAME = 240;  // 6*240*2B = 2880B ~= 90 ms

static void txEnable()  { if (s_tx) i2s_channel_enable(s_tx); }
static void txDisable() { if (s_tx) i2s_channel_disable(s_tx); }

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

// ---------------- 音频任务（消费流缓冲 -> I2S TX） ----------------
static void playerTask(void*) {
  static uint8_t frame[PLAY_FRAME_BYTES];
  for (;;) {
    // abort 必须先于 P_IDLE 处理：主任务可能在播放刚排空时置位，若先看 P_IDLE
    // 会永远跳过这里，playerBusy() 随后卡死。
    if (s_abort) {  // /stop：flush + disable/enable 清 DMA + 回空闲
      xStreamBufferReset(s_sb);
      txDisable();
      txEnable();
      txDisable();
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
        vTaskDelay(pdMS_TO_TICKS(TX_DMA_DESC * TX_DMA_FRAME * 2 / BYTES_PER_MS + 20));
        txDisable();
        s_end = false;
        s_state = P_IDLE;
        continue;
      }
      memset(frame, 0, PLAY_FRAME_BYTES);  // underrun：写静音，不忙等
      got = PLAY_FRAME_BYTES;
      s_underruns = s_underruns + 1;
    }
    size_t written = 0;
    i2s_channel_write(s_tx, frame, got, &written, pdMS_TO_TICKS(100));
    s_played += written;
  }
}

// ---------------- 播放器对外接口 ----------------
void playerBegin() {
  xStreamBufferReset(s_sb);
  s_underruns = 0;
  s_played = 0;
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
    .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
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
  // TX 不 enable，播放时再 enable
#endif

  // 音频任务优先级高于网络（loopTask 为 1）
  xTaskCreatePinnedToCore(playerTask, "audio_tx", 4096, nullptr, 4, nullptr, 1);
  Serial.printf("audio_init ok shared_clocks=%d\n", (int)MIC_AMP_SHARED_CLOCKS);
}
