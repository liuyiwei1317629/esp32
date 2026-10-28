// =============================================================
// screen_client.cpp - 屏幕推流客户端
// 一条长连接 GET /screen，收 multipart/x-mixed-replace 的 JPEG 帧，
// 用 TJpg_Decoder 解到 PSRAM 帧缓冲，再整帧刷屏（40 MHz ≈ 31 ms）。
// 按键只置标志：真正发 POST /screen/key 是在屏幕任务里，录音不等它。
// =============================================================
#include "screen_client.h"
#include "config.h"

#if APP_MODE != APP_MODE_RECORD_ONLY

#include "display_st7789.h"
#include "offline_jpg.h"
#include "wifi_setup.h"
#include <WiFi.h>
#include <TJpg_Decoder.h>

#if __has_include("secrets.h")
  #include "secrets.h"
#else
  #error "secrets.h not found: copy secrets.example.h to secrets.h and fill real values."
#endif

static uint16_t* s_fb  = nullptr;     // PSRAM 帧缓冲：320*240*2 = 153,600 B
static uint8_t*  s_jpg = nullptr;     // PSRAM 收帧缓冲
static volatile bool s_keyPending = false;
static bool s_offlineShown = false;

static bool blitCb(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t* bmp) {
  for (uint16_t j = 0; j < h; j++) {
    uint16_t row = (uint16_t)(y + j);
    if (row >= LCD_H) break;
    uint16_t* dst = s_fb + (size_t)row * LCD_W;
    const uint16_t* src = bmp + (size_t)j * w;
    for (uint16_t i = 0; i < w; i++) {
      uint16_t col = (uint16_t)(x + i);
      if (col >= LCD_W) break;
      dst[col] = src[i];
    }
  }
  return true;
}

// 解一张 JPEG 并整帧刷屏；失败返回 false（下一帧还会再试）
static bool drawJpeg(const uint8_t* buf, size_t n) {
  memset(s_fb, 0, (size_t)LCD_W * LCD_H * 2);
  JRESULT r = TJpgDec.drawJpg(0, 0, buf, (uint32_t)n);
  if (r != JDR_OK) {
    Serial.printf("screen_jpeg_err=%d\n", (int)r);
    return false;
  }
  lcdBlitFrame(s_fb);
  return true;
}

static void showOffline() {
  if (s_offlineShown) return;
  s_offlineShown = drawJpeg(kOfflineJpg, kOfflineJpgLen);
  if (s_offlineShown) Serial.println("screen=offline_image");
}

// 读一行（去 CRLF），带绝对超时；连接断开/超时返回 false
static bool readLine(WiFiClient& c, char* out, size_t cap, uint32_t ms) {
  size_t n = 0;
  uint32_t deadline = millis() + ms;
  while ((int32_t)(millis() - deadline) < 0) {
    while (c.available()) {
      char ch = (char)c.read();
      if (ch == '\r') continue;
      if (ch == '\n') { out[n] = '\0'; return true; }
      if (n + 1 < cap) out[n++] = ch;
    }
    if (!c.connected() && !c.available()) return false;
    delay(1);
  }
  return false;
}

static bool readExact(WiFiClient& c, uint8_t* dst, size_t n, uint32_t ms) {
  size_t got = 0;
  uint32_t deadline = millis() + ms;
  while (got < n && (int32_t)(millis() - deadline) < 0) {
    int avail = c.available();
    if (avail > 0) {
      size_t want = n - got;
      if ((size_t)avail < want) want = (size_t)avail;
      int r = c.read(dst + got, want);
      if (r <= 0) break;
      got += (size_t)r;
    } else {
      if (!c.connected()) break;
      delay(1);
    }
  }
  return got == n;
}

static void postKey() {
  WiFiClient c;
  if (!c.connect(wifiBridgeHost(), BRIDGE_PORT, 800)) return;
  c.printf("POST /screen/key HTTP/1.1\r\nHost: %s:%d\r\nX-Auth: %s\r\n"
           "Content-Length: 0\r\nConnection: close\r\n\r\n",
           wifiBridgeHost(), (int)BRIDGE_PORT, BRIDGE_TOKEN);
  char line[96];
  readLine(c, line, sizeof(line), 800);
  c.stop();
}

static void screenTask(void*) {
  char line[256];
  for (;;) {
    if (s_keyPending) { s_keyPending = false; postKey(); }

    WiFiClient c;
    c.setNoDelay(true);
    if (!c.connect(wifiBridgeHost(), BRIDGE_PORT, 3000)) {
      showOffline();
      vTaskDelay(pdMS_TO_TICKS(SCREEN_RECONNECT_MS));
      continue;
    }
    c.printf("GET /screen?session_id=%s HTTP/1.1\r\nHost: %s:%d\r\nX-Auth: %s\r\n"
             "Connection: close\r\n\r\n",
             SESSION_ID, wifiBridgeHost(), (int)BRIDGE_PORT, BRIDGE_TOKEN);

    if (!readLine(c, line, sizeof(line), SCREEN_IDLE_TIMEOUT_MS) ||
        strncmp(line, "HTTP/1.1 200", 12) != 0) {
      Serial.printf("screen_http=%s\n", line);
      c.stop();
      showOffline();
      vTaskDelay(pdMS_TO_TICKS(SCREEN_RECONNECT_MS));
      continue;
    }
    while (readLine(c, line, sizeof(line), 2000)) {   // 跳过响应头
      if (!line[0]) break;
    }

    for (;;) {                                        // 一条连接里读很多帧
      if (s_keyPending) { s_keyPending = false; postKey(); }
      if (!readLine(c, line, sizeof(line), SCREEN_IDLE_TIMEOUT_MS)) break;
      if (strncmp(line, "--frame", 7) != 0) continue;
      size_t len = 0;
      while (readLine(c, line, sizeof(line), 2000)) {
        if (!line[0]) break;
        if (strncasecmp(line, "Content-Length:", 15) == 0) len = (size_t)atol(line + 15);
      }
      if (len == 0 || len > SCREEN_JPEG_MAX) {
        Serial.printf("screen_bad_len=%u\n", (unsigned)len);
        break;
      }
      if (!readExact(c, s_jpg, len, 3000)) {
        Serial.println("screen_short_frame");
        break;
      }
      uint8_t crlf[2];
      readExact(c, crlf, 2, 500);
      if (drawJpeg(s_jpg, len)) s_offlineShown = false;
    }
    c.stop();
    showOffline();                                    // 流断了 = 电脑不在了
    vTaskDelay(pdMS_TO_TICKS(500));
  }
}

void screenStart() {
  s_fb  = (uint16_t*)heap_caps_malloc((size_t)LCD_W * LCD_H * 2,
                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  s_jpg = (uint8_t*)heap_caps_malloc(SCREEN_JPEG_MAX,
                                     MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!s_fb || !s_jpg) {
    Serial.println("screen=alloc_failed (screen off, voice unaffected)");
    return;
  }
  lcdInit();
  TJpgDec.setSwapBytes(true);       // ST7789 吃大端 RGB565
  TJpgDec.setCallback(blitCb);
  xTaskCreatePinnedToCore(screenTask, "screen", 8192, nullptr, 2, nullptr, 0);
  Serial.printf("screen=started fb=%uB jpg=%uB psram_free=%u\n",
                (unsigned)(LCD_W * LCD_H * 2), (unsigned)SCREEN_JPEG_MAX,
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
}

void screenNotifyKey() { s_keyPending = true; }

#else   // RECORD_ONLY：不联网，只把屏刷黑，省得上电满屏雪花

#include "display_st7789.h"

void screenStart() { lcdInit(); }
void screenNotifyKey() {}

#endif
