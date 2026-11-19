from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from .config_store import ROOT


def resolve_history_log_dir(raw: str | None = None) -> Path:
    text = (raw or "").strip()
    if not text:
        return ROOT
    path = Path(text)
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


class DownloadHistoryLogger:
    """Append human-readable download records to a time-named log file."""

    def __init__(self, log_dir: Path | None = None, *, enabled: bool = True) -> None:
        self.log_dir = Path(log_dir) if log_dir else ROOT
        self.enabled = enabled
        self._lock = threading.Lock()
        self._session_path: Path | None = None

    def _session_file(self) -> Path:
        if self._session_path is None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._session_path = self.log_dir / f"download_{stamp}.log"
        return self._session_path

    @property
    def current_path(self) -> Path | None:
        return self._session_path

    def record(
        self,
        *,
        link: str,
        caption: str,
        paths: Sequence[Path],
        kinds: Sequence[str],
        finished_at: datetime | None = None,
    ) -> Path | None:
        if not self.enabled:
            return None
        when = finished_at or datetime.now()
        video_n = sum(1 for k in kinds if k == "视频")
        image_n = sum(1 for k in kinds if k == "图片")
        other_n = len(kinds) - video_n - image_n
        type_parts: list[str] = []
        if video_n:
            type_parts.append(f"视频 {video_n}")
        if image_n:
            type_parts.append(f"图片 {image_n}")
        if other_n:
            type_parts.append(f"其他 {other_n}")
        type_text = "、".join(type_parts) if type_parts else "无"
        body = (caption or "").strip() or "（无文案）"
        if len(body) > 2000:
            body = body[:2000] + "…（已截断）"
        path_lines = "\n".join(f"  {p}" for p in paths) if paths else "  （无）"
        block = (
            f"{'=' * 60}\n"
            f"下载时间: {when.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"链接: {link}\n"
            f"消息内容:\n{body}\n"
            f"下载数量: {len(paths)}（{type_text}）\n"
            f"保存路径:\n{path_lines}\n"
            f"{'=' * 60}\n\n"
        )
        target = self._session_file()
        with self._lock:
            with target.open("a", encoding="utf-8") as f:
                f.write(block)
        return target


def summarize_kinds(kinds: Iterable[str]) -> str:
    kinds = list(kinds)
    video_n = sum(1 for k in kinds if k == "视频")
    image_n = sum(1 for k in kinds if k == "图片")
    parts = []
    if video_n:
        parts.append(f"视频{video_n}")
    if image_n:
        parts.append(f"图片{image_n}")
    return " ".join(parts) if parts else f"共{len(kinds)}"
