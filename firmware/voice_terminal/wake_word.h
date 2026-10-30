#pragma once
// =============================================================
// wake_word.h - 离线语音唤醒“小鱼”（ESP-SR AFE + MultiNet mn7_cn）
//
// 实现方式：不用 WakeNet（原 “Hi ESP” 专用的 wakenet 模型已从模型分区
// 整体移除，旧唤醒词不可能再触发）；改用 MultiNet 中文命令识别，
// 把“小鱼”（拼音 xiao yu）注册为唯一命令词，命中即视为唤醒。
//
// 模型来源：flash “model” 分区（0xBF0000，分区方案 custom，见草图
// partitions.csv），内容 = firmware/tools/srmodels.bin：
//   mn7_cn（中文命令识别）+ vadnet1_medium（VAD）+ fst（拼音->音素表）
//
// 运行模型：主循环 IDLE 态由 wakeWordPoll() 驱动（读麦 -> AFE -> MultiNet），
// 与 recordClip 共用同一个任务，因此不会争抢 I2S RX。
// 录音 / 上传 / 播放期间 wakeWordSuspend() 挂起，结束后 Resume 恢复。
// =============================================================
#include <Arduino.h>

// 初始化：加载模型分区并创建 AFE / MultiNet 实例。setup() 调用一次。
// 返回 false = 模型分区缺失或内存不足；此时按键录音流程照常，仅无语音唤醒。
bool wakeWordInit();

// 是否初始化成功（可用于决定是否轮询）。
bool wakeWordReady();

// IDLE 态每圈调用：读一帧麦克风 -> AFE 增强 -> MultiNet 检测。
// 返回 true = 命中“小鱼”（本次调用内已复位检测器，一次性事件）。
bool wakeWordPoll();

// 挂起：进入录音/上传/播放前调用，停止读麦克风。
void wakeWordSuspend();

// 恢复：清 AFE 缓冲、复位 MultiNet、丢弃功放余音后重新开始监听。
void wakeWordResume();

// 当前是否处于监听状态（挂起后为 false）。
bool wakeWordEnabled();
