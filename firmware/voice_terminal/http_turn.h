#pragma once
// =============================================================
// http_turn.h - 与 PC bridge 的最小 HTTP/1.1 客户端
// 契约（handoff F 包第 4 节，不要改）：
//   POST /turn?session_id=dev1，body 为完整 WAV（16k/16bit/mono）
//   头：X-Auth: <bridge_token>、Content-Type: audio/wav、Content-Length
//   响应：chunked PCM（s16le/16000/mono，X-Voice 头）或 204 / 401
//   POST /stop：全局打断，带 X-Auth，body 可为空
// 自己解析状态行/头/chunked，不依赖 HTTPClient 的隐式行为。
// =============================================================
#include <Arduino.h>

struct TurnResult {
  bool ok = false;          // 200 且 PCM 全部喂给播放器
  bool no_voice = false;    // 204：空识别
  bool aborted = false;     // 本地触发 /stop 打断
  int status = 0;           // HTTP 状态码
  uint32_t first_pcm_ms = 0;  // 从请求发出到首字节 PCM
  uint32_t pcm_bytes = 0;     // 收到的 PCM 总字节
  char err[96] = {0};         // 错误原因（不含 token/密码）
};

// 播放期间的打断轮询钩子：返回 true 表示用户按下 BOOT 要求打断。
// 由 .ino 注册；仅 APP_MODE_TURN_STOP 会注册非空钩子。
typedef bool (*AbortPollFn)();
void httpSetAbortPoll(AbortPollFn fn);

// 上传 WAV 并把响应 PCM 实时喂给播放器（playerBegin 需已由调用方完成）。
// 内部处理：chunked / Content-Length 两种响应、X-Voice 校验、204/401、短读与超时。
bool httpPostTurn(const uint8_t* wav, size_t wav_len, TurnResult* res);

// 全局打断。返回是否成功发出并收到响应。
bool httpPostStop(uint32_t* out_ms, int* out_status);
