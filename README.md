# 小鱼语音链路

ESP32-S3 语音终端 + PC 端的本地语音陪伴链路：设备只负责录和放，ASR / 大脑 / TTS 都在 PC 上跑。

**全部文档已合并成一份：[`项目总结.md`](项目总结.md)** —— 现状、声线、设备、记忆、运行环境、排障、待办、硬约束都在里面。

| 想做什么 | 看哪 |
|---|---|
| 把项目跑起来 | `项目总结.md` §6 |
| 改代码 / 看接口契约 | `bridge/README.md` |
| 烧固件 / 接线 | `firmware/README.md` |
| 硬件上机调试 | `handoff/G-hardware-bringup.md` |
| 屏幕设计 | `design/2026-10-28-screen-stream-plan.md` |

日常启动：双击桌面「小鱼调试台」（等价 `start-console.cmd`），浏览器开 `http://127.0.0.1:8080/console`。

注意：密钥只在 `%USERPROFILE%\.deepseek\`，不进本仓库；不要读 `secrets.h`、不要打印 token。
