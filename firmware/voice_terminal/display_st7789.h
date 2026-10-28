#pragma once
#include <Arduino.h>

// ST7789 最小驱动：只做整屏刷新（320x240 横屏），不建字库、不做局部刷新。
void lcdInit();
void lcdFill(uint16_t color);
void lcdBlitFrame(const uint16_t* fb);   // 320*240 个像素，已按高字节在前排好
