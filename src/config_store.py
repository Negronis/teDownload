from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .app_credentials import DEFAULT_API_HASH, DEFAULT_API_ID


def app_root() -> Path:
    """Project root in dev; directory of the .exe when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = app_root()
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"
DEFAULTS: dict[str, Any] = {
    "api_id": DEFAULT_API_ID,
    "api_hash": DEFAULT_API_HASH,
    "download_dir": "downloads",
    "naming_template": "{index}_{title}_{tags}",
    "naming_mode": "auto",  # auto | manual
    "session_name": "tg_downloader",
    "max_retries": 3,
    "concurrency": 3,  # 同时下载媒体数，范围 1–5
    "prefer_qr_login": True,
    "proxy": "",
    "clipboard_auto_fill": True,
    "folder_by_caption": True,
    "media_types": "both",  # both | video | image
    "history_log_enabled": True,
    "history_log_dir": "",  # empty = software root
}

# 早期默认模板不含 title/tags，启动时自动迁到新默认
_LEGACY_NAMING_TEMPLATES = {
    "{index}_{date}_{chat}_{msg_id}",
    "{index}_{date}_{chat}_{msg_id}.mp4",
}


def _normalize_api(data: dict[str, Any]) -> dict[str, Any]:
    """Fill missing/placeholder API credentials with built-in defaults."""
    api_id = data.get("api_id") or 0
    api_hash = str(data.get("api_hash") or "").strip()
    try:
        api_id = int(api_id)
    except (TypeError, ValueError):
        api_id = 0
    if not api_id or not api_hash or api_hash == "your_api_hash_here":
        data["api_id"] = DEFAULT_API_ID
        data["api_hash"] = DEFAULT_API_HASH
    else:
        data["api_id"] = api_id
        data["api_hash"] = api_hash
    return data


def _migrate_naming_template(data: dict[str, Any]) -> dict[str, Any]:
    raw = str(data.get("naming_template") or "").strip()
    if not raw or raw in _LEGACY_NAMING_TEMPLATES:
        data["naming_template"] = DEFAULTS["naming_template"]
        return data
    # 旧模板完全没引用正文/标签时，也迁到默认（保留用户故意写的自定义）
    if (
        "{title}" not in raw
        and "{tags}" not in raw
        and "{caption}" not in raw
        and raw.replace(".mp4", "") in {t.replace(".mp4", "") for t in _LEGACY_NAMING_TEMPLATES}
    ):
        data["naming_template"] = DEFAULTS["naming_template"]
    return data


def load_config() -> dict[str, Any]:
    data = dict(DEFAULTS)
    # bundled example may live beside exe or inside _MEIPASS
    candidates = [CONFIG_PATH, EXAMPLE_PATH]
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "config.example.json"
        candidates.append(bundled)
    loaded_user = False
    for path in candidates:
        if path.exists():
            try:
                data.update(json.loads(path.read_text(encoding="utf-8")))
                if path == CONFIG_PATH:
                    loaded_user = True
                    break
            except Exception:  # noqa: BLE001
                continue
    data = _normalize_api(data)
    before = str(data.get("naming_template") or "")
    data = _migrate_naming_template(data)
    # 若从旧模板迁过来，写回用户 config，避免下次又被旧值覆盖
    if loaded_user and before != data.get("naming_template"):
        try:
            save_config(data)
        except Exception:  # noqa: BLE001
            pass
    return data


def save_config(data: dict[str, Any]) -> None:
    merged = dict(DEFAULTS)
    merged.update(data)
    CONFIG_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def resolve_download_dir(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def clamp_concurrency(value: Any, default: int = 3) -> int:
    """同时下载媒体数：最少 1，最多 5。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(5, n))


CREDENTIALS_TYPE = "tg_downloader_credentials"


def export_credentials(
    path: Path,
    *,
    api_id: int | str,
    api_hash: str,
    proxy: str = "",
    include_proxy: bool = True,
) -> None:
    payload: dict[str, Any] = {
        "type": CREDENTIALS_TYPE,
        "version": 1,
        "api_id": int(api_id),
        "api_hash": str(api_hash).strip(),
    }
    if include_proxy:
        payload["proxy"] = (proxy or "").strip()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def import_credentials(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("凭证文件格式无效")

    # accept our format, or a plain {api_id, api_hash} file
    api_id = data.get("api_id")
    api_hash = str(data.get("api_hash") or "").strip()
    if api_id is None or not api_hash:
        raise ValueError("文件中缺少 api_id / api_hash")
    try:
        api_id = int(api_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("api_id 必须是数字") from exc
    if api_id <= 0:
        raise ValueError("api_id 无效")

    result: dict[str, Any] = {"api_id": api_id, "api_hash": api_hash}
    if "proxy" in data and data.get("proxy") is not None:
        result["proxy"] = str(data.get("proxy") or "").strip()
    return result
