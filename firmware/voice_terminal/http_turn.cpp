// =============================================================
// http_turn.cpp - 手写 HTTP/1.1 客户端（WiFiClient 直连，无 TLS，局域网）
// 明确自己解析 chunked：0 chunk 结束、忽略 ;ext 扩展、逐 chunk 末尾 CRLF、
// 短读/超时保护。不假设每次 read 返回完整 chunk。
// =============================================================
#include "http_turn.h"
#include "config.h"
#include "audio_io.h"
#include "wifi_setup.h"
#include <WiFi.h>

#if APP_MODE != APP_MODE_RECORD_ONLY
  #if __has_include("secrets.h")
    #include "secrets.h"
  #else
    #error "secrets.h not found: copy secrets.example.h to secrets.h and fill real values."
  #endif
#else
  // RECORD_ONLY 不联网；以下占位符仅让本文件可编译，不会真正被调用。
  #define BRIDGE_HOST "0.0.0.0"
  #define BRIDGE_PORT 0
  #define BRIDGE_TOKEN ""
  #define SESSION_ID "dev1"
#endif

static AbortPollFn s_abortPoll = nullptr;
void httpSetAbortPoll(AbortPollFn fn) { s_abortPoll = fn; }

static void setErr(TurnResult* res, const char* msg) {
  if (res) snprintf(res->err, sizeof(res->err), "%s", msg);
}

// 读一行（去 CRLF），带绝对超时；连接断开/超时返回 false
static bool readLine(WiFiClient& c, char* buf, size_t cap, uint32_t timeout_ms) {
  size_t n = 0;
  uint32_t deadline = millis() + timeout_ms;
  while ((int32_t)(millis() - deadline) < 0) {
    while (c.available()) {
      char ch = (char)c.read();
      if (ch == '\r') continue;
      if (ch == '\n') { buf[n] = '\0'; return true; }
      if (n < cap - 1) buf[n++] = ch;
    }
    if (!c.connected() && !c.available()) return false;
    delay(1);
  }
  return false;
}

// 读满 n 字节，带绝对超时；返回实际读到的字节数
static size_t readFully(WiFiClient& c, uint8_t* buf, size_t n, uint32_t timeout_ms) {
  size_t got = 0;
  uint32_t deadline = millis() + timeout_ms;
  while (got < n && (int32_t)(millis() - deadline) < 0) {
    int avail = c.available();
    if (avail > 0) {
      size_t want = n - got;
      if ((size_t)avail < want) want = (size_t)avail;
      int r = c.read(buf + got, want);
      if (r <= 0) break;
      got += (size_t)r;
    } else {
      if (!c.connected()) break;
      delay(1);
    }
  }
  return got;
}

bool httpPostTurn(const uint8_t* wav, size_t wav_len, TurnResult* res) {
  res->ok = false; res->no_voice = false; res->aborted = false;
  res->status = 0; res->first_pcm_ms = 0; res->pcm_bytes = 0; res->err[0] = '\0';

  WiFiClient c;
  c.setNoDelay(true);
  uint32_t t0 = millis();
  const char* host = wifiBridgeHost();   // NVS 里改过就用新的，否则 secrets.h
  if (!c.connect(host, BRIDGE_PORT, HTTP_CONNECT_TIMEOUT_MS)) {
    setErr(res, "tcp connect timeout/fail");
    return false;
  }

  // ---- 请求行 + 头（Content-Length = 44 + pcm_bytes，由调用方传入的 WAV 总长）----
  c.printf("POST /turn?session_id=%s HTTP/1.1\r\n"
           "Host: %s:%d\r\n"
           "X-Auth: %s\r\n"
           "Content-Type: audio/wav\r\n"
           "Content-Length: %u\r\n"
           "Connection: close\r\n\r\n",
           SESSION_ID, host, (int)BRIDGE_PORT, BRIDGE_TOKEN, (unsigned)wav_len);
  // ---- 请求体：分块写，不假设一次写完 ----
  // 鉴权失败时服务器可能不读 body 就直接回 401。若在上传途中检测到响应
  // 或发送超时，就停止上传、转去解析状态行，避免把 401 误报成上传失败。
  size_t off = 0;
  uint32_t upload_deadline = millis() + HTTP_UPLOAD_TIMEOUT_MS;
  while (off < wav_len) {
    if (c.available()) break;   // 提前响应（401/204 等）
    if ((int32_t)(millis() - upload_deadline) >= 0) {
      setErr(res, "upload timeout");
      c.stop(); return false;
    }
    size_t want = wav_len - off;
    if (want > 2048) want = 2048;
    size_t w = c.write(wav + off, want);
    if (w == 0) {
      if (c.available()) break;
      if (!c.connected()) { setErr(res, "upload broken"); c.stop(); return false; }
      delay(1);
      continue;
    }
    off += w;
  }

  // ---- 状态行 ----
  char line[256];
  if (!readLine(c, line, sizeof(line), HTTP_READ_TIMEOUT_MS)) {
    setErr(res, "status line timeout");
    c.stop(); return false;
  }
  // "HTTP/1.1 200 OK"
  int status = 0;
  sscanf(line, "HTTP/%*s %d", &status);
  res->status = status;

  // ---- 响应头 ----
  bool chunked = false;
  long content_length = -1;
  bool voice_ok = false;
  for (;;) {
    if (!readLine(c, line, sizeof(line), HTTP_READ_TIMEOUT_MS)) {
      setErr(res, "headers timeout");
      c.stop(); return false;
    }
    if (line[0] == '\0') break;  // 空行 = 头结束
    char* colon = strchr(line, ':');
    if (!colon) continue;
    *colon = '\0';
    const char* val = colon + 1;
    while (*val == ' ') val++;
    if (strcasecmp(line, "Transfer-Encoding") == 0 && strcasestr(val, "chunked")) chunked = true;
    else if (strcasecmp(line, "Content-Length") == 0) content_length = atol(val);
    else if (strcasecmp(line, "X-Voice") == 0 && strstr(val, "s16le/16000/mono")) voice_ok = true;
  }

  // ---- 状态码处理 ----
  if (status == 204) {          // 空识别：无音频
    res->no_voice = true;
    c.stop(); return true;
  }
  if (status == 401) {
    setErr(res, "401 auth failed: check bridge_token in secrets.h");
    c.stop(); return false;
  }
  if (status != 200) {          // 读掉/丢弃 body，打印前 200 字符
    char body[201];
    size_t bn = readFully(c, (uint8_t*)body, 200, 2000);
    body[bn] = '\0';
    Serial.printf("http_err status=%d body200=%s\n", status, body);
    setErr(res, "unexpected status");
    c.stop(); return false;
  }
  if (!voice_ok) {              // X-Voice 必须是 s16le/16000/mono
    setErr(res, "X-Voice mismatch, stop playback");
    c.stop(); return false;
  }

  // ---- 响应体 -> 播放器 ----
  bool body_err = false;
  if (chunked) {
    for (;;) {
      if (!readLine(c, line, sizeof(line), HTTP_READ_TIMEOUT_MS)) {
        setErr(res, "chunk size timeout"); body_err = true; break;
      }
      // 十六进制 chunk size，可带 ";ext"，忽略扩展
      char* end = nullptr;
      unsigned long csz = strtoul(line, &end, 16);
      if (end == line) { setErr(res, "bad chunk size"); body_err = true; break; }
      if (csz == 0) {           // 结束：读 trailers 到空行
        while (readLine(c, line, sizeof(line), 2000)) {
          if (line[0] == '\0') break;
        }
        break;
      }
      unsigned long remain = csz;
      uint8_t buf[512];
      while (remain > 0) {
        if (s_abortPoll && s_abortPoll()) {   // 播放中按 BOOT：打断
          playerAbort();                      // 本地先静音，不等 /stop 往返
          uint32_t ms = 0; int st = 0;
          httpPostStop(&ms, &st);
          Serial.printf("stop_ms=%lu result=%d\n", (unsigned long)ms, st);
          res->aborted = true;
          c.stop(); return false;
        }
        size_t want = remain < sizeof(buf) ? (size_t)remain : sizeof(buf);
        size_t r = readFully(c, buf, want, HTTP_READ_TIMEOUT_MS);
        if (r == 0) { setErr(res, "chunk short read/timeout"); body_err = true; break; }
        // 按小段喂入：起始水位 5120B 一到就开播，目标 150-200 ms
        playerFeed(buf, r);
        if (res->first_pcm_ms == 0) res->first_pcm_ms = millis() - t0;
        res->pcm_bytes += r;
        remain -= r;
      }
      if (body_err) break;
      // chunk 末尾 CRLF
      uint8_t crlf[2];
      if (readFully(c, crlf, 2, HTTP_READ_TIMEOUT_MS) != 2) {
        setErr(res, "chunk CRLF missing"); body_err = true; break;
      }
    }
  } else if (content_length > 0) {   // 非 chunked：按长度读
    long remain = content_length;
    uint8_t buf[512];
    while (remain > 0) {
      if (s_abortPoll && s_abortPoll()) {
        playerAbort();                    // 本地先静音，不等 /stop 往返
        uint32_t ms = 0; int st = 0;
        httpPostStop(&ms, &st);
        Serial.printf("stop_ms=%lu result=%d\n", (unsigned long)ms, st);
        res->aborted = true;
        c.stop(); return false;
      }
      size_t want = (long)sizeof(buf) < remain ? sizeof(buf) : (size_t)remain;
      size_t r = readFully(c, buf, want, HTTP_READ_TIMEOUT_MS);
      if (r == 0) { setErr(res, "body short read/timeout"); body_err = true; break; }
      playerFeed(buf, r);
      if (res->first_pcm_ms == 0) res->first_pcm_ms = millis() - t0;
      res->pcm_bytes += r;
      remain -= (long)r;
    }
  } else {
    setErr(res, "no chunk/length info");
    body_err = true;
  }

  c.stop();
  res->ok = !body_err;
  return res->ok;
}

bool httpPostStop(uint32_t* out_ms, int* out_status) {
  WiFiClient c;
  uint32_t t0 = millis();
  if (out_status) *out_status = 0;
  if (!c.connect(wifiBridgeHost(), BRIDGE_PORT, HTTP_CONNECT_TIMEOUT_MS)) {
    if (out_ms) *out_ms = millis() - t0;
    return false;
  }
  c.printf("POST /stop HTTP/1.1\r\n"
           "Host: %s:%d\r\n"
           "X-Auth: %s\r\n"
           "Content-Length: 0\r\n"
           "Connection: close\r\n\r\n",
           wifiBridgeHost(), (int)BRIDGE_PORT, BRIDGE_TOKEN);
  char line[128];
  bool ok = readLine(c, line, sizeof(line), HTTP_READ_TIMEOUT_MS);
  if (ok && out_status) sscanf(line, "HTTP/%*s %d", out_status);
  // 读掉剩余 body（若有），然后关连接
  while (readLine(c, line, sizeof(line), 500)) {
    if (line[0] == '\0') break;
  }
  c.stop();
  if (out_ms) *out_ms = millis() - t0;
  return ok;
}
