from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from telethon import TelegramClient
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageEntityHashtag,
    MessageMediaDocument,
)

from .download_history import DownloadHistoryLogger
from .link_parser import MessageRef
from .naming import (
    extract_caption_parts,
    folder_name_from_caption,
    render_filename,
    resolve_unique_folder,
    sanitize,
    write_link_marker,
)

# both | video | image
MEDIA_TYPES = frozenset({"both", "video", "image"})


ProgressCb = Callable[[str, str, float, str], None]
LogCb = Callable[[str], None]

# Telegram getFile 单次上限约 1MB；用满可减少往返
_DOWNLOAD_REQUEST_SIZE = 1024 * 1024
_FILE_BUFFER = 8 * 1024 * 1024
_PROGRESS_INTERVAL = 0.8
# 同一任务进度回调最小间隔，避免并发时刷爆 UI
_TASK_PROGRESS_MIN_INTERVAL = 0.35


class TaskStatus(str, Enum):
    PENDING = "等待中"
    RUNNING = "下载中"
    DONE = "完成"
    SKIPPED = "跳过"
    ERROR = "失败"
    CANCELLED = "已取消"


@dataclass
class DownloadTask:
    index: int
    ref: MessageRef
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0
    message: str = ""
    saved_path: Optional[Path] = None
    attempts: int = 0
    part_path: Optional[Path] = None
    stable_id: str = ""
    custom_name: Optional[str] = None
    filename: str = ""


@dataclass
class Downloader:
    client: TelegramClient
    download_dir: Path
    naming_template: str
    max_retries: int = 3
    retry_delay: float = 2.0
    concurrency: int = 3  # 同时下载媒体流，1–5
    folder_by_caption: bool = True
    media_types: str = "both"  # both | video | image
    history: Optional[DownloadHistoryLogger] = None
    on_progress: Optional[ProgressCb] = None
    on_log: Optional[LogCb] = None
    _cancel: bool = field(default=False, init=False)
    _active_cancel: set[str] = field(default_factory=set, init=False)
    _entity_cache: dict = field(default_factory=dict, init=False)
    _media_sem: asyncio.Semaphore = field(init=False, repr=False)
    _last_progress_at: dict[str, float] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.concurrency = max(1, min(5, int(self.concurrency or 3)))
        self._media_sem = asyncio.Semaphore(self.concurrency)

    def _want_video(self) -> bool:
        return self.media_types in ("both", "video")

    def _want_image(self) -> bool:
        return self.media_types in ("both", "image")

    def cancel(self) -> None:
        self._cancel = True

    def cancel_task(self, task_id: str) -> None:
        self._active_cancel.add(task_id)

    def _emit_progress(
        self,
        task_id: str,
        status: str,
        ratio: float,
        text: str,
        *,
        force: bool = False,
    ) -> None:
        if not self.on_progress:
            return
        # 终态 / 强制 / 间隔足够才推送，防止并发文件进度把界面打卡死
        terminal = status in {
            TaskStatus.DONE.value,
            TaskStatus.ERROR.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.SKIPPED.value,
        }
        now = time.monotonic()
        last = self._last_progress_at.get(task_id, 0.0)
        if not force and not terminal and ratio < 1.0 and (now - last) < _TASK_PROGRESS_MIN_INTERVAL:
            return
        self._last_progress_at[task_id] = now
        self.on_progress(task_id, status, ratio, text)

    def _log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)

    def _should_stop(self, task_id: str) -> bool:
        return self._cancel or task_id in self._active_cancel

    async def resolve_entity(self, ref: MessageRef):
        key = ref.username or ref.chat_id
        cached = self._entity_cache.get(key)
        if cached is not None:
            return cached
        if ref.username:
            entity = await self.client.get_entity(ref.username)
        else:
            assert ref.chat_id is not None
            entity = await self.client.get_entity(ref.chat_id)
        self._entity_cache[key] = entity
        return entity

    async def fetch_message(self, ref: MessageRef) -> Message:
        entity = await self.resolve_entity(ref)
        msg = await self.client.get_messages(entity, ids=ref.message_id)
        if msg is None:
            raise ValueError("消息不存在或无权访问")
        return msg

    def stable_key(self, ref: MessageRef) -> str:
        if ref.username:
            return sanitize(f"{ref.username}_{ref.message_id}")
        return sanitize(f"c{abs(ref.chat_id or 0)}_{ref.message_id}")

    def part_paths(self, save_dir: Path, stable_id: str) -> tuple[Path, Path]:
        part = save_dir / f".{stable_id}.part"
        meta = save_dir / f".{stable_id}.meta.json"
        return part, meta

    def _write_meta(self, meta: Path, data: dict) -> None:
        meta.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def download_one(self, task: DownloadTask) -> DownloadTask:
        task_id = str(task.index)
        self._active_cancel.discard(task_id)
        task.stable_id = self.stable_key(task.ref)

        if self._should_stop(task_id):
            task.status = TaskStatus.CANCELLED
            task.message = "已取消"
            self._emit_progress(task_id, task.status.value, task.progress, task.message)
            return task

        max_attempts = max(1, self.max_retries + 1)
        last_error = ""

        for attempt in range(1, max_attempts + 1):
            if self._should_stop(task_id):
                task.status = TaskStatus.CANCELLED
                task.message = "已取消（可断点续传）"
                self._emit_progress(task_id, task.status.value, task.progress, task.message)
                return task

            task.attempts = attempt
            task.status = TaskStatus.RUNNING
            suffix = f"（第 {attempt}/{max_attempts} 次）" if attempt > 1 else ""
            task.message = f"解析消息…{suffix}"
            self._emit_progress(task_id, task.status.value, task.progress, task.message)

            try:
                await self._download_once(task)
                return task
            except _CancelledDownload:
                task.status = TaskStatus.CANCELLED
                task.message = "已暂停，支持断点续传"
                self._emit_progress(task_id, task.status.value, task.progress, task.message)
                self._log(f"[{task.index}] 已取消，保留临时文件以便续传")
                return task
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                task.status = TaskStatus.ERROR
                task.message = last_error
                self._emit_progress(task_id, task.status.value, task.progress, task.message)
                self._log(f"[{task.index}] 失败（尝试 {attempt}/{max_attempts}）：{exc}")
                if attempt < max_attempts and not self._should_stop(task_id):
                    delay = self.retry_delay * attempt
                    task.message = f"重试等待 {delay:.0f}s… {last_error}"
                    self._emit_progress(task_id, TaskStatus.PENDING.value, task.progress, task.message)
                    await asyncio.sleep(delay)

        task.status = TaskStatus.ERROR
        task.message = last_error or "下载失败"
        self._emit_progress(task_id, task.status.value, task.progress, task.message)
        return task

    async def suggest_filename(self, task: DownloadTask) -> str:
        msg = await self.fetch_message(task.ref)
        album = await self._album_messages(msg) if msg.grouped_id else [msg]
        items = self._collect_media_items(msg, album)
        media_msg = items[0]
        caption = self._caption_from(msg, media_msg, album)
        parts = extract_caption_parts(caption)
        chat_name = await self._chat_name(task.ref, media_msg)
        folder = self._resolve_folder(parts, task, msg.id)
        kind = _media_kind(media_msg)
        name = self._filename_for(
            task, media_msg, parts=parts, chat_name=chat_name, sub_index=1
        )
        if folder:
            display = f"{folder}/{kind}/{name}"
            if len(items) > 1:
                display = f"{folder}/（共 {len(items)} 个文件）"
        else:
            display = name
            if len(items) > 1:
                display = f"（共 {len(items)} 个文件）"
        task.filename = display
        return display

    def _resolve_folder(
        self, parts: dict[str, str], task: DownloadTask, msg_id: int
    ) -> str:
        if not self.folder_by_caption:
            return ""
        if task.custom_name and not Path(task.custom_name).suffix:
            base = sanitize(task.custom_name, 80)
        else:
            base = folder_name_from_caption(parts, fallback=f"msg_{msg_id}")
        return resolve_unique_folder(self.download_dir, base, msg_id)

    def _filename_for(
        self,
        task: DownloadTask,
        media_msg: Message,
        *,
        parts: dict[str, str],
        chat_name: str,
        sub_index: int,
    ) -> str:
        ext = _guess_ext(media_msg)
        # 单文件手动名（带扩展名）仍作为文件名
        if task.custom_name and Path(task.custom_name).suffix and sub_index == 1:
            return sanitize(Path(task.custom_name).name, 180)
        date = media_msg.date or datetime.now()
        filename = render_filename(
            self.naming_template,
            index=task.index,
            chat=chat_name,
            msg_id=media_msg.id,
            date=date,
            title=parts["title"],
            tags=parts["tags"],
            caption=parts["caption"],
            ext=ext,
        )
        if sub_index > 1:
            stem, suffix = Path(filename).stem, Path(filename).suffix
            filename = sanitize(f"{stem}_{sub_index:02d}", 180) + suffix
        return filename

    def _collect_media_items(self, origin: Message, album: list[Message]) -> list[Message]:
        items: list[Message] = []
        seen: set[int] = set()
        want_v, want_i = self._want_video(), self._want_image()
        # 链接指向的那条优先
        ordered = [origin] + [m for m in album if m.id != origin.id]
        for m in ordered:
            if m.id in seen:
                continue
            ok = (want_v and _has_video(m)) or (want_i and _has_image(m) and not _has_video(m))
            if ok:
                seen.add(m.id)
                items.append(m)
        if not items:
            if want_v and want_i:
                raise ValueError("该消息中未找到视频或图片")
            if want_v:
                raise ValueError("该消息中未找到视频（当前仅下载视频）")
            raise ValueError("该消息中未找到图片（当前仅下载图片）")
        return items

    async def _download_once(self, task: DownloadTask) -> None:
        task_id = str(task.index)
        msg = await self.fetch_message(task.ref)
        album = await self._album_messages(msg) if msg.grouped_id else [msg]
        items = self._collect_media_items(msg, album)
        caption = self._caption_from(msg, items[0], album)
        parts = extract_caption_parts(caption)
        if parts["tags"] or parts["title"]:
            self._log(
                f"[{task.index}] 命名：title={parts['title']!r} tags={parts['tags']!r}"
            )
        chat_name = await self._chat_name(task.ref, items[0])
        folder = self._resolve_folder(parts, task, msg.id)
        root_dir = self.download_dir / folder if folder else self.download_dir
        root_dir.mkdir(parents=True, exist_ok=True)
        if folder:
            write_link_marker(root_dir, msg.id)
            self._log(f"[{task.index}] 文案文件夹：{folder}（{len(items)} 个文件）")

        total_files = len(items)
        ratios = [0.0] * total_files
        # 单文件无需占满并发；多文件才并行
        parallel = total_files > 1 and self.concurrency > 1
        if parallel:
            self._log(
                f"[{task.index}] 并行下载 {total_files} 个文件（并发 ≤{self.concurrency}）"
            )
        self._emit_progress(
            task_id,
            TaskStatus.RUNNING.value,
            0.0,
            f"准备下载 {total_files} 个文件…",
            force=True,
        )

        async def one(i: int, media_msg: Message) -> tuple[Path, str]:
            if self._should_stop(task_id):
                raise _CancelledDownload()
            filename = self._filename_for(
                task, media_msg, parts=parts, chat_name=chat_name, sub_index=i
            )
            kind = _media_kind(media_msg)
            if folder:
                type_dir = root_dir / kind  # 图片 / 视频
                type_dir.mkdir(parents=True, exist_ok=True)
                save_dir = type_dir
                rel = f"{folder}/{kind}/{filename}"
            else:
                save_dir = root_dir
                rel = filename

            def on_file_progress(file_ratio: float, text: str) -> None:
                ratios[i - 1] = max(0.0, min(1.0, file_ratio))
                overall = sum(ratios) / total_files
                task.progress = overall
                task.message = text
                self._emit_progress(
                    task_id, TaskStatus.RUNNING.value, overall, text
                )

            async with self._media_sem:
                if self._should_stop(task_id):
                    raise _CancelledDownload()
                path = await self._download_media_item(
                    task,
                    media_msg,
                    save_dir=save_dir,
                    filename=filename,
                    file_i=i,
                    file_n=total_files,
                    folder=folder,
                    chat_name=chat_name,
                    kind=kind,
                    on_file_progress=on_file_progress,
                )
            ratios[i - 1] = 1.0
            return path, kind

        if parallel:
            results = await asyncio.gather(
                *(one(i, m) for i, m in enumerate(items, start=1)),
                return_exceptions=True,
            )
        else:
            results = []
            for i, m in enumerate(items, start=1):
                try:
                    results.append(await one(i, m))
                except BaseException as exc:  # noqa: BLE001
                    results.append(exc)
        saved: list[Path] = []
        kinds: list[str] = []
        first_err: BaseException | None = None
        for r in results:
            if isinstance(r, _CancelledDownload):
                raise r
            if isinstance(r, BaseException):
                if first_err is None:
                    first_err = r
                continue
            path, kind = r
            saved.append(path)
            kinds.append(kind)
        if first_err is not None and not saved:
            raise first_err
        if first_err is not None:
            raise first_err

        task.progress = 1.0
        task.status = TaskStatus.DONE
        if folder:
            task.saved_path = root_dir
            task.filename = f"{folder}/（{len(saved)} 个文件）"
            task.message = task.filename
        else:
            task.saved_path = saved[-1]
            task.filename = saved[-1].name
            task.message = saved[-1].name
        self._emit_progress(
            task_id, task.status.value, 1.0, task.message, force=True
        )
        self._log(f"[{task.index}] 完成 {len(saved)}/{total_files} → {task.message}")
        self._write_history(
            task,
            caption=caption,
            paths=saved,
            kinds=kinds,
        )

    def _write_history(
        self,
        task: DownloadTask,
        *,
        caption: str,
        paths: list[Path],
        kinds: list[str],
    ) -> None:
        if not self.history:
            return
        try:
            log_path = self.history.record(
                link=task.ref.canonical_url(),
                caption=caption,
                paths=paths,
                kinds=kinds,
            )
            if log_path:
                self._log(f"[{task.index}] 已写入下载记录：{log_path.name}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[{task.index}] 写入下载记录失败：{exc}")

    async def _download_media_item(
        self,
        task: DownloadTask,
        media_msg: Message,
        *,
        save_dir: Path,
        filename: str,
        file_i: int,
        file_n: int,
        folder: str,
        chat_name: str,
        kind: str,
        on_file_progress: Optional[Callable[[float, str], None]] = None,
    ) -> Path:
        task_id = str(task.index)
        stable = f"{task.stable_id}_{media_msg.id}"
        final_path = save_dir / filename
        part_path, meta_path = self.part_paths(save_dir, stable)
        task.part_path = part_path
        prefix = f"({file_i}/{file_n}) "

        def emit(file_ratio: float, text: str) -> None:
            if on_file_progress:
                on_file_progress(file_ratio, text)
            else:
                overall = ((file_i - 1) + max(0.0, min(1.0, file_ratio))) / max(1, file_n)
                task.progress = overall
                task.message = text
                self._emit_progress(
                    task_id, TaskStatus.RUNNING.value, overall, text
                )

        media = media_msg.media
        total = _media_size(media_msg)
        self._write_meta(
            meta_path,
            {
                "stable_id": stable,
                "final_name": filename,
                "folder": folder,
                "message_id": media_msg.id,
                "chat": chat_name,
                "kind": kind,
                "total": total,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )

        if final_path.exists() and (total <= 0 or final_path.stat().st_size >= total):
            self._cleanup_temp(part_path, meta_path)
            emit(1.0, f"{prefix}已存在：{final_path.name}")
            return final_path

        if final_path.exists() and total > 0 and final_path.stat().st_size < total:
            if not part_path.exists():
                final_path.replace(part_path)

        offset = part_path.stat().st_size if part_path.exists() else 0
        if total > 0 and offset > total:
            part_path.unlink(missing_ok=True)
            offset = 0

        download_target = (
            media_msg.document or media_msg.video or media_msg.photo or media
        )
        if download_target is None:
            raise ValueError("无法定位可下载媒体")

        mode = "ab" if offset else "wb"
        received = offset
        last_emit = 0.0
        speed_t0 = time.monotonic()
        speed_b0 = received
        speed_bps = 0.0
        chunks_since_check = 0

        if offset > 0:
            self._log(f"[{task.index}] {prefix}断点续传：已有 {_fmt_bytes(offset)}")

        with part_path.open(mode, buffering=_FILE_BUFFER) as f:
            async for chunk in self.client.iter_download(
                download_target,
                offset=offset,
                request_size=_DOWNLOAD_REQUEST_SIZE,
            ):
                chunks_since_check += 1
                if chunks_since_check >= 2:
                    chunks_since_check = 0
                    if self._should_stop(task_id):
                        raise _CancelledDownload()
                f.write(chunk)
                received += len(chunk)

                now = time.monotonic()
                dt = now - speed_t0
                if dt >= 1.0:
                    speed_bps = (received - speed_b0) / dt
                    speed_t0 = now
                    speed_b0 = received

                if now - last_emit < _PROGRESS_INTERVAL and (
                    not total or received < total
                ):
                    continue
                last_emit = now
                file_ratio = (received / total) if total else 0.0
                if total:
                    text = (
                        f"{prefix}{_fmt_bytes(received)}/{_fmt_bytes(total)} · {_fmt_speed(speed_bps)}"
                    )
                else:
                    text = (
                        f"{prefix}已下 {_fmt_bytes(received)} · {_fmt_speed(speed_bps)}"
                    )
                emit(file_ratio, text)

        if total > 0 and part_path.stat().st_size < total:
            raise IOError(
                f"文件不完整：{part_path.stat().st_size}/{total}，将自动重试续传"
            )

        target = final_path
        if target.exists() and target.resolve() != part_path.resolve():
            stem, suffix = target.stem, target.suffix
            n = 1
            while True:
                candidate = target.with_name(f"{stem}_{n}{suffix}")
                if not candidate.exists():
                    target = candidate
                    break
                n += 1

        part_path.replace(target)
        self._cleanup_temp(part_path, meta_path)
        emit(1.0, f"{prefix}完成：{target.name}")
        return target

    @staticmethod
    def _cleanup_temp(part_path: Path, meta_path: Path) -> None:
        part_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)

    async def _album_messages(self, message: Message) -> list[Message]:
        if not message.grouped_id:
            return [message]
        chat = await message.get_chat()
        items: list[Message] = []
        async for m in self.client.iter_messages(
            chat,
            min_id=max(message.id - 40, 0),
            max_id=message.id + 40,
        ):
            if m.grouped_id == message.grouped_id:
                items.append(m)
        return items or [message]

    @staticmethod
    def _raw_text(message: Message | None) -> str:
        if message is None:
            return ""
        text = getattr(message, "raw_text", None) or getattr(message, "message", None) or ""
        text = str(text).strip()
        if text:
            return text
        entities = getattr(message, "entities", None) or []
        tags: list[str] = []
        full = str(getattr(message, "message", "") or "")
        for ent in entities:
            if isinstance(ent, MessageEntityHashtag) and full:
                frag = full[ent.offset : ent.offset + ent.length]
                if frag:
                    tags.append(frag)
        return " ".join(tags).strip()

    def _caption_from(
        self, origin: Message, media_msg: Message, album: list[Message]
    ) -> str:
        candidates = [self._raw_text(media_msg), self._raw_text(origin)]
        for m in album:
            t = self._raw_text(m)
            if t:
                candidates.append(t)
        texts = [c for c in candidates if c]
        if not texts:
            return ""
        return max(texts, key=len)

    async def _chat_name(self, ref: MessageRef, message: Message) -> str:
        if ref.username:
            return ref.username
        try:
            chat = await message.get_chat()
            return getattr(chat, "username", None) or getattr(chat, "title", None) or "chat"
        except Exception:  # noqa: BLE001
            return "chat"

    async def download_many(
        self,
        tasks: list[DownloadTask],
        concurrency: int | None = None,
    ) -> list[DownloadTask]:
        self._cancel = False
        self._active_cancel.clear()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        if concurrency is not None:
            self.concurrency = max(1, min(5, int(concurrency)))
            self._media_sem = asyncio.Semaphore(self.concurrency)
        self._log(f"媒体并发：{self.concurrency}（范围 1–5）")
        # 同时推进的「链接任务」也限制在 concurrency，避免上百协程一起抢 UI/网络
        task_sem = asyncio.Semaphore(self.concurrency)

        async def worker(task: DownloadTask) -> DownloadTask:
            async with task_sem:
                return await self.download_one(task)

        await asyncio.gather(*(worker(t) for t in tasks))
        return tasks


class _CancelledDownload(Exception):
    pass


def _fmt_bytes(n: float) -> str:
    if n >= 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024 * 1024):.2f} GB"
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n:.0f} B"


def _fmt_speed(bps: float) -> str:
    if bps <= 0:
        return "计算中…"
    return f"{_fmt_bytes(bps)}/s"


def _media_size(message: Message) -> int:
    try:
        if message.document and getattr(message.document, "size", None):
            return int(message.document.size)
        if message.file and getattr(message.file, "size", None):
            return int(message.file.size)
        if message.photo:
            sizes = getattr(message.photo, "sizes", None) or []
            best = 0
            for s in sizes:
                # PhotoSize / PhotoSizeProgressive
                size = getattr(s, "size", None)
                if size is None:
                    sizes_list = getattr(s, "sizes", None)
                    if sizes_list:
                        size = max(sizes_list)
                if isinstance(size, int) and size > best:
                    best = size
            return best
    except Exception:  # noqa: BLE001
        return 0
    return 0


def _has_video(message: Message) -> bool:
    return bool(message.video or (message.document and _is_video_document(message)))


def _has_image(message: Message) -> bool:
    if message.photo:
        return True
    return bool(message.document and _is_image_document(message))


def _media_kind(message: Message) -> str:
    if _has_video(message):
        return "视频"
    if _has_image(message):
        return "图片"
    return "媒体"


def _is_video_document(message: Message) -> bool:
    media = message.media
    if not isinstance(media, MessageMediaDocument) or not message.document:
        return False
    mime = (message.document.mime_type or "").lower()
    if mime.startswith("video/"):
        return True
    for attr in message.document.attributes or []:
        if isinstance(attr, DocumentAttributeVideo):
            return True
        if isinstance(attr, DocumentAttributeFilename):
            name = (attr.file_name or "").lower()
            if name.endswith((".mp4", ".mov", ".mkv", ".webm", ".avi")):
                return True
    return False


def _is_image_document(message: Message) -> bool:
    media = message.media
    if not isinstance(media, MessageMediaDocument) or not message.document:
        return False
    mime = (message.document.mime_type or "").lower()
    if mime.startswith("image/"):
        return True
    for attr in message.document.attributes or []:
        if isinstance(attr, DocumentAttributeFilename):
            name = (attr.file_name or "").lower()
            if name.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")):
                return True
    return False


def _guess_ext(message: Message) -> str:
    if message.file and message.file.ext:
        return message.file.ext
    if message.photo:
        return ".jpg"
    if message.document:
        for attr in message.document.attributes or []:
            if isinstance(attr, DocumentAttributeFilename) and attr.file_name:
                suffix = Path(attr.file_name).suffix
                if suffix:
                    return suffix
        mime = (message.document.mime_type or "").lower()
        if "webm" in mime:
            return ".webm"
        if "quicktime" in mime:
            return ".mov"
        if "png" in mime:
            return ".png"
        if "webp" in mime:
            return ".webp"
        if "gif" in mime:
            return ".gif"
        if "jpeg" in mime or "jpg" in mime:
            return ".jpg"
        if mime.startswith("image/"):
            return ".jpg"
    if _has_image(message):
        return ".jpg"
    return ".mp4"
