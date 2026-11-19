from __future__ import annotations

import winreg
from typing import Optional


def detect_windows_proxy() -> Optional[str]:
    """
    Read Windows user Internet Settings proxy.
    Clash Verge / many clients enable system proxy here.
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enable:
                return None
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None

    server = (server or "").strip()
    if not server:
        return None

    # formats: 127.0.0.1:7897  or  http=127.0.0.1:7897;https=...;socks=...
    if "=" in server or ";" in server:
        parts = {}
        for chunk in server.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                parts[k.strip().lower()] = v.strip()
            else:
                parts["all"] = chunk
        for key in ("socks", "socks5", "http", "https", "all"):
            if key in parts and parts[key]:
                hostport = parts[key]
                if key.startswith("socks"):
                    return f"socks5://{hostport}"
                return f"http://{hostport}"
        return None

    # plain host:port → Clash HTTP mixed port is most common
    return f"http://{server}"


def detect_proxy_or_empty() -> str:
    return detect_windows_proxy() or ""
