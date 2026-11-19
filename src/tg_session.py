from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import auth

from .config_store import ROOT


AskStr = Callable[[], Awaitable[str]]
AskCode = Callable[[str], Awaitable[str]]
ShowQr = Callable[[str], Awaitable[None]]
ClearQr = Callable[[], Awaitable[None]]
StatusCb = Callable[[str], None]


def session_path(session_name: str) -> Path:
    return ROOT / session_name


def parse_proxy(proxy_url: str) -> Any:
    """
    Accept:
      socks5://127.0.0.1:1080
      socks5://user:pass@127.0.0.1:1080
      http://127.0.0.1:7890
      127.0.0.1:1080   (treated as socks5)
    """
    raw = (proxy_url or "").strip()
    if not raw:
        return None

    try:
        import socks  # PySocks
    except ImportError as exc:
        raise RuntimeError("缺少 PySocks，无法使用代理。请重新安装/打包依赖。") from exc

    kind = "socks5"
    rest = raw
    if "://" in raw:
        kind, rest = raw.split("://", 1)
        kind = kind.lower().strip()

    username = None
    password = None
    if "@" in rest:
        cred, rest = rest.rsplit("@", 1)
        if ":" in cred:
            username, password = cred.split(":", 1)
        else:
            username = cred

    if ":" not in rest:
        raise ValueError("代理格式错误，示例：socks5://127.0.0.1:1080")
    host, port_s = rest.rsplit(":", 1)
    host = host.strip().strip("[]")
    port = int(port_s)

    type_map = {
        "socks5": socks.SOCKS5,
        "socks5h": socks.SOCKS5,
        "socks4": socks.SOCKS4,
        "http": socks.HTTP,
        "https": socks.HTTP,
    }
    if kind not in type_map:
        raise ValueError("代理类型仅支持 socks5 / socks4 / http")

    return (type_map[kind], host, port, True, username, password)


def build_client(
    api_id: int,
    api_hash: str,
    session_name: str,
    proxy_url: str = "",
) -> TelegramClient:
    proxy = parse_proxy(proxy_url) if proxy_url else None
    return TelegramClient(
        str(session_path(session_name)),
        api_id,
        api_hash,
        proxy=proxy,
        connection_retries=5,
        retry_delay=1,
        timeout=30,
        # 下载大文件时减少无谓重连抖动
        auto_reconnect=True,
        request_retries=3,
    )


async def ensure_login(
    client: TelegramClient,
    *,
    prefer_qr: bool = True,
    ask_phone: AskStr,
    ask_code: AskCode,
    ask_password: AskStr,
    show_qr: Optional[ShowQr] = None,
    clear_qr: Optional[ClearQr] = None,
    on_status: Optional[StatusCb] = None,
) -> str:
    def status(msg: str) -> None:
        if on_status:
            on_status(msg)

    status("正在连接 Telegram…")
    try:
        await asyncio.wait_for(client.connect(), timeout=25)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            "连接 Telegram 超时（25秒）。\n"
            "若在国内网络，请在软件里填写本地代理，例如：\n"
            "socks5://127.0.0.1:1080\n"
            "或 http://127.0.0.1:7890\n"
            "（端口改成你自己的翻墙软件端口）"
        ) from exc
    except Exception as exc:
        raise ConnectionError(
            f"连接 Telegram 失败：{exc}\n"
            "常见原因：网络无法直连。请开启代理并在软件中填写代理地址。"
        ) from exc

    if await client.is_user_authorized():
        status("已恢复本地登录会话")
        return "session"

    if prefer_qr:
        if show_qr is None:
            raise RuntimeError("扫码登录不可用：界面未提供二维码显示")
        status("正在生成二维码…")
        await _qr_login(
            client,
            show_qr=show_qr,
            clear_qr=clear_qr,
            ask_password=ask_password,
            on_status=status,
        )
        return "qr"

    await _phone_login(
        client,
        ask_phone=ask_phone,
        ask_code=ask_code,
        ask_password=ask_password,
    )
    return "phone"


async def _qr_login(
    client: TelegramClient,
    *,
    show_qr: ShowQr,
    clear_qr: Optional[ClearQr],
    ask_password: AskStr,
    on_status: Optional[StatusCb] = None,
) -> None:
    def status(msg: str) -> None:
        if on_status:
            on_status(msg)

    try:
        qr = await asyncio.wait_for(client.qr_login(), timeout=25)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            "生成二维码超时。请检查网络/代理后重试。"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"无法生成二维码：{exc}") from exc

    status("请用手机 Telegram 扫码…")
    await show_qr(qr.url)

    try:
        for i in range(6):
            try:
                await qr.wait(timeout=30)
                break
            except asyncio.TimeoutError:
                status(f"二维码刷新中（{i + 1}/6）…")
                await qr.recreate()
                await show_qr(qr.url)
        else:
            raise TimeoutError(
                "扫码超时。请用手机 Telegram：设置 → 设备 → 扫描二维码，扫电脑上的码。"
            )
    except SessionPasswordNeededError:
        password = await ask_password()
        if not password:
            raise RuntimeError("未输入两步验证密码，已取消登录")
        await client.sign_in(password=password)
    finally:
        if clear_qr:
            await clear_qr()

    if not await client.is_user_authorized():
        raise RuntimeError("扫码登录未完成，请重试「扫码登录」")


def _describe_code_delivery(sent) -> str:
    t = getattr(sent, "type", None)
    if isinstance(t, auth.SentCodeTypeApp):
        return (
            "验证码已发到【Telegram App 内】，不是短信。\n"
            "请打开手机或电脑版 Telegram，找名为「Telegram」的官方对话查看登录码。"
        )
    if isinstance(t, auth.SentCodeTypeSms):
        return "验证码已以短信发送（部分地区常收不到，建议改用扫码登录）。"
    if isinstance(t, auth.SentCodeTypeCall):
        return "Telegram 将通过电话语音播报验证码。"
    if isinstance(t, auth.SentCodeTypeFlashCall):
        return "Telegram 将通过来电号码尾号提供验证码。"
    return "请到已登录的 Telegram 里查看验证码（优先看「Telegram」官方对话）。"


async def _phone_login(
    client: TelegramClient,
    *,
    ask_phone: AskStr,
    ask_code: AskCode,
    ask_password: AskStr,
) -> None:
    phone = await ask_phone()
    if not phone:
        raise RuntimeError("未输入手机号，已取消登录")
    if not phone.startswith("+"):
        raise RuntimeError("手机号请带国际区号，例如中国大陆：+8613xxxxxxxxx")

    sent = await client.send_code_request(phone)
    hint = _describe_code_delivery(sent)
    code = await ask_code(hint)
    if not code:
        raise RuntimeError("未输入验证码，已取消登录")

    code = code.replace(" ", "").replace("-", "")

    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        password = await ask_password()
        if not password:
            raise RuntimeError("未输入两步验证密码，已取消登录")
        await client.sign_in(password=password)
