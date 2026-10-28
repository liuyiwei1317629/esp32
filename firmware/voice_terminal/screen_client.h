#pragma once
#include <Arduino.h>

// 屏幕推流客户端：独立任务收 MJPEG、解码、整帧刷屏。
void screenStart();
void screenNotifyKey();   // 非阻塞：只置标志，录音路径里调用安全
