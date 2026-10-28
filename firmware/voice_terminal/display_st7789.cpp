// =============================================================
// display_st7789.cpp - ST7789 240x320 屏的最小驱动（横屏 320x240）
// 只用 Arduino SPI（FSPI）+ writePixels 整帧发；不做局部刷新、不引第三方库。
// =============================================================
#include "display_st7789.h"
#include "config.h"
#include <SPI.h>

static SPIClass s_spi(FSPI);

static void cmd(uint8_t c) {
  digitalWrite(PIN_LCD_DC, LOW);
  digitalWrite(PIN_LCD_CS, LOW);
  s_spi.write(c);
  digitalWrite(PIN_LCD_CS, HIGH);
}

static void cmdData(uint8_t c, const uint8_t* d, size_t n) {
  digitalWrite(PIN_LCD_DC, LOW);
  digitalWrite(PIN_LCD_CS, LOW);
  s_spi.write(c);
  digitalWrite(PIN_LCD_DC, HIGH);
  s_spi.writeBytes(d, n);
  digitalWrite(PIN_LCD_CS, HIGH);
}

static void setWindow(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1) {
  uint8_t buf[4];
  buf[0] = x0 >> 8; buf[1] = x0 & 0xFF; buf[2] = x1 >> 8; buf[3] = x1 & 0xFF;
  cmdData(0x2A, buf, 4);
  buf[0] = y0 >> 8; buf[1] = y0 & 0xFF; buf[2] = y1 >> 8; buf[3] = y1 & 0xFF;
  cmdData(0x2B, buf, 4);
  cmd(0x2C);                       // RAMWR：后面跟的就是像素
}

void lcdInit() {
  pinMode(PIN_LCD_CS, OUTPUT);
  pinMode(PIN_LCD_DC, OUTPUT);
  pinMode(PIN_LCD_RST, OUTPUT);
  digitalWrite(PIN_LCD_CS, HIGH);
  digitalWrite(PIN_LCD_DC, HIGH);

  digitalWrite(PIN_LCD_RST, HIGH); delay(10);
  digitalWrite(PIN_LCD_RST, LOW);  delay(20);
  digitalWrite(PIN_LCD_RST, HIGH); delay(120);

  s_spi.begin(PIN_LCD_SCLK, -1, PIN_LCD_MOSI, -1);
  s_spi.beginTransaction(SPISettings(LCD_SPI_HZ, MSBFIRST, SPI_MODE0));

  cmd(0x01); delay(120);                                  // SWRESET
  cmd(0x11); delay(120);                                  // SLPOUT
  uint8_t colmod = 0x55; cmdData(0x3A, &colmod, 1);       // RGB565
  uint8_t madctl = 0x60; cmdData(0x36, &madctl, 1);       // MX|MV：横屏
  uint8_t porctrl[5] = {0x0C, 0x0C, 0x00, 0x33, 0x33}; cmdData(0xB2, porctrl, 5);
  uint8_t gate[1] = {0x35}; cmdData(0xB7, gate, 1);
  uint8_t vcom[1] = {0x19}; cmdData(0xBB, vcom, 1);
  uint8_t lcm[1] = {0x2C}; cmdData(0xC0, lcm, 1);
  uint8_t vdvv[1] = {0x01}; cmdData(0xC2, vdvv, 1);
  uint8_t vrh[1] = {0x12}; cmdData(0xC3, vrh, 1);
  uint8_t vdvv2[1] = {0x20}; cmdData(0xC4, vdvv2, 1);
  uint8_t fr[2] = {0x0F, 0x01}; cmdData(0xC6, fr, 2);
  uint8_t pw[1] = {0xA4}; cmdData(0xD0, pw, 1);
  uint8_t pv[2] = {0xA1, 0x00}; cmdData(0xE0, pv, 2);
  if (LCD_INVERT) cmd(0x21);                              // INVON
  cmd(0x13); delay(10);                                   // NORON
  cmd(0x29); delay(20);                                   // DISPON
  lcdFill(0x0000);
}

void lcdFill(uint16_t color) {
  static uint8_t line[LCD_W * 2];
  uint8_t hi = color >> 8, lo = color & 0xFF;
  for (int i = 0; i < LCD_W; i++) { line[i * 2] = hi; line[i * 2 + 1] = lo; }
  setWindow(0, 0, LCD_W - 1, LCD_H - 1);
  digitalWrite(PIN_LCD_DC, HIGH);
  digitalWrite(PIN_LCD_CS, LOW);
  for (int y = 0; y < LCD_H; y++) s_spi.writeBytes(line, sizeof(line));
  digitalWrite(PIN_LCD_CS, HIGH);
}

void lcdBlitFrame(const uint16_t* fb) {
  setWindow(0, 0, LCD_W - 1, LCD_H - 1);
  digitalWrite(PIN_LCD_DC, HIGH);
  digitalWrite(PIN_LCD_CS, LOW);
  s_spi.writePixels(fb, (uint32_t)LCD_W * LCD_H);
  digitalWrite(PIN_LCD_CS, HIGH);
}
