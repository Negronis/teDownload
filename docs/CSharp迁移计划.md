# C# 迁移计划（已确认）

用 **C# WPF UI + WTelegramClient（或 TDLib）** 在**新建文件夹**重做本工具，解决 CustomTkinter 高并发界面卡顿。不直接覆盖本 Python 目录。

## 仓库

- GitHub：`telegramDownload`
- Python 版目录：`Telegram批量下载器/`（过渡维护）
- C# 版：同级新建文件夹（如 `TelegramDownloader.CSharp/`）

## 步骤

1. 安装 .NET SDK，脚手架 WPF + WTelegramClient  
2. 登录 + 单链接下载打通  
3. 批量任务、按文案文件夹、`图片/`/`视频/`、媒体类型筛选  
4. 设置页、并发 1–5、下载记录日志、UI 进度节流  
5. 发布 exe，功能对齐后考虑停更 Python 版  

## 需对齐的现有能力

链接批量解析、扫码/手机号登录、代理、断点续传、失败重试、命名模板、剪贴板填入、历史日志。
