# Telegram 批量下载器

桌面端图形界面：粘贴消息链接批量下载视频/图片，单条进度、断点续传、失败重试、自定义保存目录。

## 功能

- 批量粘贴 `t.me/...` / `t.me/c/...` 消息链接（支持逗号分隔）
- 支持 **视频** 与 **图片**（含相册）；可选「仅视频 / 仅图片 / 全部」
- 图形化任务管理：进度、筛选、单条重试/暂停、打开文件、清除已完成
- **断点续传**：暂停/失败后保留 `.part`，再次下载从断点继续
- **失败自动重试**（可配置次数）+ 手动「重试失败项」
- **按文案建文件夹**：每条链接按标题/标签建目录；目录内再分「图片」「视频」；重名则追加消息 ID
- **下载记录日志**：按时间命名 `.log`，记录链接、文案、数量/类型、时间与保存路径（默认软件根目录）
- **自定义下载文件夹** / 命名模板 / 剪贴板自动填入链接
- **扫码登录** / 手机号登录；本地会话自动恢复

## 关于「串联 Telegram Desktop 登录态」

**不能直接共用。** Desktop 的 `tdata` 与本工具（Telethon）会话格式不同，官方也不支持互相导入。

更省事的做法：

1. 本工具点 **扫码登录**
2. 用已经登录的手机 Telegram（或 Desktop「设置 → 设备」）扫码授权
3. 授权一次后，本地会生成 `tg_downloader.session`，下次自动登录

仍需在 [my.telegram.org](https://my.telegram.org) 申请自己的 **API ID / API Hash**（只是开发者凭证，不是二次注册账号）。

## 安装

```bash
cd Telegram批量下载器
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## 运行（开发）

```bash
python main.py
```

1. 填写 API ID / Hash  
2. 选择下载目录  
3. 扫码登录  
4. 粘贴链接 → 开始下载  

## 打包成 exe

双击 `build.bat`，或：

```bash
.\.venv\Scripts\activate
pip install pyinstaller
pyinstaller --noconfirm tg_downloader.spec
```

完成后得到：

```text
dist\Telegram批量下载器.exe
```

- 单文件、无黑框窗口  
- `config.json`、`.session`、`downloads` 都会生成在 **exe 同目录**  
- 可把 `config.example.json` 复制为同目录 `config.json` 再改  

> Windows 可能提示未知发布者，选「仍要运行」即可（自打包未签名）。

## 断点续传说明

- 下载中会生成隐藏临时文件：`.{频道}_{消息id}.part`
- 点「暂停/取消」会停在当前进度并保留 `.part`
- 再次对同一消息下载/重试时，从已下载字节继续
- 完成后自动改名为最终文件并删除临时文件

## 配置项

界面可改，也会写入 `config.json`：

| 项 | 含义 |
|----|------|
| download_dir | 下载目录（可绝对路径） |
| naming_template | 默认 `{index}_{title}_{tags}`；还可用 `{caption}` `{date}` `{chat}` `{msg_id}` |
| naming_mode | `auto` 自动按模板命名；`manual` 下载前批量重命名 |
| clipboard_auto_fill | 是否自动识别剪贴板中的 Telegram 链接并填入 |
| folder_by_caption | 是否按消息文案为每条链接自动创建子文件夹 |
| media_types | `both` / `video` / `image`：下载图片+视频、仅视频或仅图片 |
| history_log_enabled | 是否写入下载记录日志 |
| history_log_dir | 日志目录；留空为软件根目录；文件名 `download_YYYYMMDD_HHMMSS.log` |
| max_retries | 单条失败自动重试次数 |
| concurrency | 同时下载媒体数（图片/视频并行），范围 **1–5**，默认 3 |

## 合规

仅用于下载你有权访问的内容，请遵守 Telegram 服务条款与当地法律。
