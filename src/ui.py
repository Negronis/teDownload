from __future__ import annotations

import asyncio
import os
import queue
import threading
import tkinter as tk
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Optional

import customtkinter as ctk
from PIL import Image

from .config_store import (
    ROOT,
    clamp_concurrency,
    export_credentials,
    import_credentials,
    load_config,
    resolve_download_dir,
    save_config,
)
from .download_history import DownloadHistoryLogger, resolve_history_log_dir
from .downloader import Downloader, DownloadTask, TaskStatus
from .link_parser import find_telegram_links, parse_links_block
from .proxy_detect import detect_windows_proxy
from .tg_session import build_client, ensure_login


# Clash Verge–like light theme
C_BG = "#F2F3F5"
C_SIDE = "#FFFFFF"
C_CARD = "#FFFFFF"
C_TEXT = "#1F2937"
C_MUTED = "#6B7280"
C_LINE = "#E5E7EB"
C_BLUE = "#3B82F6"
C_BLUE_HOVER = "#2563EB"
C_BLUE_SOFT = "#EFF6FF"
C_DANGER = "#EF4444"
C_DANGER_HOVER = "#DC2626"
C_GRAY_BTN = "#F3F4F6"
C_GRAY_HOVER = "#E5E7EB"
C_INPUT = "#F9FAFB"
SIDE_W = 168

ctk.set_appearance_mode("Light")
ctk.set_default_color_theme("blue")


def btn(master, text, command, *, primary=True, danger=False, width=None, height=34, **kwargs):
    if danger:
        fg, hover, tc = C_DANGER, C_DANGER_HOVER, "#fff"
    elif primary:
        fg, hover, tc = C_BLUE, C_BLUE_HOVER, "#fff"
    else:
        fg, hover, tc = C_GRAY_BTN, C_GRAY_HOVER, C_TEXT
    opts = dict(
        master=master,
        text=text,
        command=command,
        fg_color=fg,
        hover_color=hover,
        text_color=tc,
        corner_radius=10,
        height=height,
        font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
    )
    if width is not None:
        opts["width"] = width
    opts.update(kwargs)
    return ctk.CTkButton(**opts)


def card(parent, **kwargs) -> ctk.CTkFrame:
    return ctk.CTkFrame(
        parent,
        fg_color=C_CARD,
        corner_radius=14,
        border_width=1,
        border_color=C_LINE,
        **kwargs,
    )


class TaskRow(ctk.CTkFrame):
    def __init__(self, master, task: DownloadTask, on_retry, on_open, on_delete, on_pause, **kwargs):
        super().__init__(
            master,
            fg_color=C_INPUT,
            corner_radius=12,
            border_width=1,
            border_color=C_LINE,
            **kwargs,
        )
        self.task = task
        self.on_retry = on_retry
        self.on_open = on_open
        self.on_delete = on_delete
        self.on_pause = on_pause
        self.grid_columnconfigure(1, weight=1)

        self.idx = ctk.CTkLabel(self, text=f"{task.index:03d}", width=40, text_color=C_MUTED, anchor="w")
        self.idx.grid(row=0, column=0, padx=(12, 4), pady=(10, 0), sticky="w")

        self.title = ctk.CTkLabel(
            self, text=self._title_text(), anchor="w", text_color=C_TEXT,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
        )
        self.title.grid(row=0, column=1, padx=4, pady=(10, 0), sticky="ew")

        self.status = ctk.CTkLabel(self, text=task.status.value, width=64, anchor="e", text_color=C_BLUE)
        self.status.grid(row=0, column=2, padx=4, pady=(10, 0), sticky="e")

        self.action_btn = btn(self, "重试", self._on_action, primary=False, width=54, height=28)
        self.action_btn.grid(row=0, column=3, padx=2, pady=(10, 0))
        self.open_btn = btn(self, "打开", lambda: self.on_open(self.task), primary=False, width=54, height=28)
        self.open_btn.grid(row=0, column=4, padx=2, pady=(10, 0))
        self.del_btn = btn(
            self, "删除", lambda: self.on_delete(self.task), danger=True, width=54, height=28
        )
        self.del_btn.grid(row=0, column=5, padx=(2, 10), pady=(10, 0))

        self.bar = ctk.CTkProgressBar(self, progress_color=C_BLUE, fg_color=C_LINE, height=6, corner_radius=4)
        self.bar.grid(row=1, column=0, columnspan=6, padx=12, pady=(8, 2), sticky="ew")
        self.bar.set(0)

        self.detail = ctk.CTkLabel(self, text="", anchor="w", text_color=C_MUTED)
        self.detail.grid(row=2, column=0, columnspan=6, padx=12, pady=(0, 10), sticky="ew")
        self._last_status = ""
        self._last_detail = ""
        self._last_title = ""
        self._last_progress = -1.0
        self._sync_buttons()
        self._refresh_title()

    def _title_text(self) -> str:
        if self.task.filename:
            return self.task.filename
        if self.task.custom_name:
            return self.task.custom_name
        return self.task.ref.label

    def _refresh_title(self) -> None:
        text = self._title_text()
        if text != self._last_title:
            self.title.configure(text=text)
            self._last_title = text

    def _on_action(self) -> None:
        if self.task.status == TaskStatus.RUNNING:
            self.on_pause(self.task)
        else:
            self.on_retry(self.task)

    def update_view(self, status: str, progress: float, detail: str) -> None:
        self.task.status = TaskStatus(status) if status in TaskStatus._value2member_map_ else self.task.status
        self.task.progress = progress
        self.task.message = detail
        progress = max(0.0, min(1.0, progress))
        self._refresh_title()
        if status != self._last_status:
            self.status.configure(text=status)
            self._last_status = status
            self._sync_buttons()
        if abs(progress - self._last_progress) >= 0.005 or progress >= 1.0:
            self.bar.set(progress)
            self._last_progress = progress
        if detail != self._last_detail:
            self.detail.configure(text=detail)
            self._last_detail = detail

    def sync_from_task(self) -> None:
        self.update_view(self.task.status.value, self.task.progress, self.task.message)

    def _sync_buttons(self) -> None:
        st = self.task.status
        if st == TaskStatus.RUNNING:
            self.action_btn.configure(text="暂停", state="normal", fg_color=C_DANGER, hover_color=C_DANGER_HOVER, text_color="#fff")
        elif st == TaskStatus.ERROR:
            self.action_btn.configure(
                text="重试", state="normal", fg_color=C_GRAY_BTN, hover_color=C_GRAY_HOVER, text_color=C_TEXT
            )
        elif st in {TaskStatus.CANCELLED, TaskStatus.SKIPPED}:
            self.action_btn.configure(
                text="继续", state="normal", fg_color=C_GRAY_BTN, hover_color=C_GRAY_HOVER, text_color=C_TEXT
            )
        else:
            self.action_btn.configure(
                text="重试", state="disabled", fg_color=C_GRAY_BTN, hover_color=C_GRAY_HOVER, text_color=C_TEXT
            )
        self.open_btn.configure(
            state="normal" if self.task.saved_path and Path(self.task.saved_path).exists() else "disabled"
        )
        self.del_btn.configure(state="disabled" if st == TaskStatus.RUNNING else "normal")


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Telegram 批量视频下载器")
        self.geometry("1040x760")
        self.minsize(920, 680)
        self.configure(fg_color=C_BG)

        self.cfg = load_config()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.loop_thread: Optional[threading.Thread] = None
        self.client = None
        self.downloader: Optional[Downloader] = None
        self.tasks: list[DownloadTask] = []
        self.task_rows: dict[str, TaskRow] = {}
        self._busy = False
        self._filter = tk.StringVar(value="全部")
        self._qr_window: Optional[ctk.CTkToplevel] = None
        self._qr_label: Optional[ctk.CTkLabel] = None
        self._pending_progress: dict[str, tuple[str, float, str]] = {}
        self._progress_dirty = False
        self._nav_btns: dict[str, ctk.CTkButton] = {}
        # 下载线程禁止直接碰 Tk；统一进队列由主线程消化，避免暂停时卡死
        self._ui_q: queue.Queue[tuple[str, Any]] = queue.Queue()

        self._logged_in = False
        self._init_vars()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_ui_queue)
        self.after(100, self._bootstrap_async)
        self.after(1500, self._poll_clipboard)

    def _init_vars(self) -> None:
        self.api_id_var = tk.StringVar(value=str(self.cfg.get("api_id") or ""))
        self.api_hash_var = tk.StringVar(value=str(self.cfg.get("api_hash") or ""))
        saved_proxy = str(self.cfg.get("proxy") or "").strip() or (detect_windows_proxy() or "")
        self.proxy_var = tk.StringVar(value=saved_proxy)
        self.dir_var = tk.StringVar(value=str(self.cfg.get("download_dir") or "downloads"))
        self.template_var = tk.StringVar(
            value=str(self.cfg.get("naming_template") or "{index}_{title}_{tags}")
        )
        mode = str(self.cfg.get("naming_mode") or "auto").strip().lower()
        self.naming_mode_var = tk.StringVar(value="manual" if mode == "manual" else "auto")
        self.retry_var = tk.StringVar(value=str(self.cfg.get("max_retries", 3)))
        self.conc_var = tk.StringVar(
            value=str(clamp_concurrency(self.cfg.get("concurrency", 3)))
        )
        self.clipboard_auto_var = tk.BooleanVar(
            value=bool(self.cfg.get("clipboard_auto_fill", True))
        )
        self.folder_by_caption_var = tk.BooleanVar(
            value=bool(self.cfg.get("folder_by_caption", True))
        )
        mt = str(self.cfg.get("media_types") or "both").strip().lower()
        if mt not in ("both", "video", "image"):
            mt = "both"
        self.media_types_var = tk.StringVar(value=mt)
        self.history_log_enabled_var = tk.BooleanVar(
            value=bool(self.cfg.get("history_log_enabled", True))
        )
        self.history_log_dir_var = tk.StringVar(
            value=str(self.cfg.get("history_log_dir") or "")
        )
        self._naming_mode_btns: dict[str, ctk.CTkButton] = {}
        self._media_type_btns: dict[str, ctk.CTkButton] = {}
        self._last_clipboard = ""
        self._clipboard_fill_cooldown = False

    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- sidebar ---
        side = ctk.CTkFrame(self, fg_color=C_SIDE, width=SIDE_W, corner_radius=0, border_width=0)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)

        brand = ctk.CTkFrame(side, fg_color="transparent")
        brand.pack(fill="x", padx=16, pady=(18, 20))
        ctk.CTkLabel(
            brand,
            text="TG Downloader",
            text_color=C_TEXT,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=15, weight="bold"),
            anchor="w",
        ).pack(fill="x")
        ctk.CTkLabel(
            brand,
            text="批量视频下载",
            text_color=C_MUTED,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=11),
            anchor="w",
        ).pack(fill="x", pady=(2, 0))

        self._nav_btns["home"] = self._nav_item(side, "首页", lambda: self._show_page("home"))
        self._nav_btns["settings"] = self._nav_item(side, "设置", lambda: self._show_page("settings"))

        self.status_label = ctk.CTkLabel(
            side,
            text="正在检查登录…",
            text_color=C_MUTED,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=11),
            wraplength=SIDE_W - 28,
            justify="left",
            anchor="w",
        )
        self.status_label.pack(side="bottom", fill="x", padx=14, pady=16)

        # --- main ---
        main = ctk.CTkFrame(self, fg_color=C_BG, corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(0, weight=1)

        self.pages = ctk.CTkFrame(main, fg_color=C_BG)
        self.pages.grid(row=0, column=0, sticky="nsew")
        self.pages.grid_columnconfigure(0, weight=1)
        self.pages.grid_rowconfigure(0, weight=1)

        self.home_page = ctk.CTkFrame(self.pages, fg_color=C_BG)
        self.settings_page = ctk.CTkFrame(self.pages, fg_color=C_BG)
        for p in (self.home_page, self.settings_page):
            p.grid(row=0, column=0, sticky="nsew")
            p.grid_columnconfigure(0, weight=1)

        self._build_home()
        self._build_settings()
        self._show_page("home")

    def _nav_item(self, parent, text: str, command) -> ctk.CTkButton:
        b = ctk.CTkButton(
            parent,
            text=f"  {text}",
            command=command,
            anchor="w",
            height=40,
            corner_radius=10,
            fg_color="transparent",
            hover_color=C_BLUE_SOFT,
            text_color=C_TEXT,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
        )
        b.pack(fill="x", padx=10, pady=3)
        return b

    def _set_nav_active(self, key: str) -> None:
        for k, b in self._nav_btns.items():
            if k == key:
                b.configure(fg_color=C_BLUE_SOFT, text_color=C_BLUE)
            else:
                b.configure(fg_color="transparent", text_color=C_TEXT)

    def _show_page(self, key: str) -> None:
        self._set_nav_active(key)
        if key == "home":
            self.home_page.tkraise()
        else:
            self.settings_page.tkraise()

    def _page_header(self, parent, title: str, subtitle: str = "") -> None:
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 10))
        ctk.CTkLabel(
            wrap,
            text=title,
            text_color=C_TEXT,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=20, weight="bold"),
            anchor="w",
        ).pack(anchor="w")
        if subtitle:
            ctk.CTkLabel(
                wrap,
                text=subtitle,
                text_color=C_MUTED,
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
                anchor="w",
            ).pack(anchor="w", pady=(2, 0))

    def _build_home(self) -> None:
        page = self.home_page
        page.grid_rowconfigure(2, weight=1)
        self._page_header(
            page,
            "首页",
            "支持视频/图片；可按文案自动建文件夹；链接可用换行/逗号分隔",
        )

        # links card
        c1 = card(page)
        c1.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 12))
        c1.grid_columnconfigure(0, weight=1)
        link_head = ctk.CTkFrame(c1, fg_color="transparent")
        link_head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        ctk.CTkLabel(
            link_head, text="消息链接", text_color=C_TEXT,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=14, weight="bold"), anchor="w",
        ).pack(side="left")
        self.clipboard_hint = ctk.CTkLabel(
            link_head, text="", text_color=C_MUTED,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=11),
        )
        self.clipboard_hint.pack(side="right")
        self._refresh_clipboard_hint()
        self.links_box = ctk.CTkTextbox(
            c1, height=108, fg_color=C_INPUT, border_color=C_LINE, border_width=1,
            corner_radius=10, text_color=C_TEXT,
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.links_box.grid(row=1, column=0, padx=16, sticky="ew")
        actions = ctk.CTkFrame(c1, fg_color="transparent")
        actions.grid(row=2, column=0, padx=12, pady=12, sticky="ew")
        btn(actions, "开始下载", self._on_start, width=100).pack(side="left", padx=4)
        btn(actions, "暂停", self._on_cancel, danger=True, width=72).pack(side="left", padx=4)
        btn(actions, "重试失败", self._on_retry_failed, primary=False, width=88).pack(side="left", padx=4)
        btn(actions, "清除完成", self._clear_done, primary=False, width=88).pack(side="left", padx=4)
        btn(actions, "打开目录", self._open_dir, primary=False, width=88).pack(side="left", padx=4)
        btn(actions, "读取剪贴板", self._fill_from_clipboard, primary=False, width=100).pack(
            side="left", padx=4
        )

        # tasks card
        c2 = card(page)
        c2.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 12))
        c2.grid_columnconfigure(0, weight=1)
        c2.grid_rowconfigure(1, weight=1)
        head = ctk.CTkFrame(c2, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        ctk.CTkLabel(
            head, text="任务", text_color=C_TEXT,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=14, weight="bold"),
        ).pack(side="left")
        ctk.CTkOptionMenu(
            head, values=["全部", "下载中", "失败", "已取消", "完成"], variable=self._filter,
            width=100, height=30, corner_radius=8, fg_color=C_GRAY_BTN, button_color=C_BLUE,
            button_hover_color=C_BLUE_HOVER, text_color=C_TEXT,
            command=lambda _=None: self._render_tasks(),
        ).pack(side="right")
        self.task_host = ctk.CTkScrollableFrame(c2, fg_color=C_CARD, corner_radius=8)
        self.task_host.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.task_host.grid_columnconfigure(0, weight=1)

        # download log card
        c3 = card(page)
        c3.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 18))
        c3.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            c3, text="下载日志", text_color=C_MUTED,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12), anchor="w",
        ).grid(row=0, column=0, padx=14, pady=(10, 2), sticky="w")
        self.dl_log_box = ctk.CTkTextbox(
            c3, height=90, fg_color=C_INPUT, border_width=0, corner_radius=10,
            text_color=C_MUTED, font=ctk.CTkFont(family="Consolas", size=11),
        )
        self.dl_log_box.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="ew")
        self.dl_log_box.configure(state="disabled")

    def _build_settings(self) -> None:
        page = self.settings_page
        page.grid_rowconfigure(1, weight=1)
        self._page_header(page, "设置", "登录、代理、目录与凭证")

        body = ctk.CTkScrollableFrame(page, fg_color=C_BG)
        body.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        body.grid_columnconfigure(0, weight=1)

        # login
        s1 = card(body)
        s1.pack(fill="x", padx=8, pady=8)
        self._section_title(s1, "账号登录")
        state_row = ctk.CTkFrame(s1, fg_color=C_INPUT, corner_radius=10, border_width=1, border_color=C_LINE)
        state_row.pack(fill="x", padx=16, pady=(0, 10))
        self.login_state_dot = ctk.CTkLabel(
            state_row, text="●", text_color=C_MUTED, width=24,
            font=ctk.CTkFont(size=14),
        )
        self.login_state_dot.pack(side="left", padx=(12, 4), pady=12)
        self.login_state_label = ctk.CTkLabel(
            state_row,
            text="正在检查本地会话…",
            text_color=C_TEXT,
            anchor="w",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold"),
        )
        self.login_state_label.pack(side="left", fill="x", expand=True, padx=(0, 12), pady=12)
        ctk.CTkLabel(
            s1, text="推荐扫码：手机 Telegram → 设置 → 设备 → 扫描二维码",
            text_color=C_MUTED, anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 8))
        row = ctk.CTkFrame(s1, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 8))
        btn(row, "扫码登录", lambda: self._on_login(True), width=110).pack(side="left", padx=4)
        btn(row, "手机号登录", lambda: self._on_login(False), primary=False, width=110).pack(side="left", padx=4)
        btn(row, "刷新状态", self._try_auto_connect, primary=False, width=100).pack(side="left", padx=4)
        ctk.CTkLabel(
            s1, text="登录日志", text_color=C_MUTED, anchor="w",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
        ).pack(fill="x", padx=16, pady=(4, 2))
        self.login_log_box = ctk.CTkTextbox(
            s1, height=110, fg_color=C_INPUT, border_width=1, border_color=C_LINE,
            corner_radius=10, text_color=C_MUTED,
            font=ctk.CTkFont(family="Consolas", size=11),
        )
        self.login_log_box.pack(fill="x", padx=16, pady=(0, 14))
        self.login_log_box.configure(state="disabled")

        # clipboard
        s_clip = card(body)
        s_clip.pack(fill="x", padx=8, pady=8)
        self._section_title(s_clip, "剪贴板识别")
        clip_row = ctk.CTkFrame(s_clip, fg_color="transparent")
        clip_row.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkSwitch(
            clip_row,
            text="自动识别复制的 Telegram 链接并填入",
            variable=self.clipboard_auto_var,
            command=self._on_clipboard_auto_toggle,
            progress_color=C_BLUE,
            button_color="#fff",
            button_hover_color="#F3F4F6",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
        ).pack(side="left")
        ctk.CTkLabel(
            s_clip,
            text="开启后监听剪贴板；也支持用英文/中文逗号分隔多条链接后点「开始下载」。",
            text_color=C_MUTED, anchor="w", wraplength=620, justify="left",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
        ).pack(fill="x", padx=16, pady=(0, 14))

        # media types
        s_media = card(body)
        s_media.pack(fill="x", padx=8, pady=8)
        self._section_title(s_media, "下载内容")
        media_row = ctk.CTkFrame(s_media, fg_color="transparent")
        media_row.pack(fill="x", padx=12, pady=(0, 6))
        for key, label in (
            ("both", "图片+视频"),
            ("video", "仅视频"),
            ("image", "仅图片"),
        ):
            self._media_type_btns[key] = ctk.CTkButton(
                media_row,
                text=label,
                height=36,
                corner_radius=10,
                command=lambda k=key: self._set_media_types(k),
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
            )
            self._media_type_btns[key].pack(side="left", fill="x", expand=True, padx=4)
        self._set_media_types(self.media_types_var.get(), persist=False)
        ctk.CTkLabel(
            s_media,
            text="按链接筛选要保存的媒体类型；相册中不符合的条目会自动跳过。",
            text_color=C_MUTED, anchor="w", wraplength=620, justify="left",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
        ).pack(fill="x", padx=16, pady=(0, 14))

        # folder by caption
        s_folder = card(body)
        s_folder.pack(fill="x", padx=8, pady=8)
        self._section_title(s_folder, "按文案建文件夹")
        folder_row = ctk.CTkFrame(s_folder, fg_color="transparent")
        folder_row.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkSwitch(
            folder_row,
            text="每条链接按消息文案自动创建文件夹",
            variable=self.folder_by_caption_var,
            command=self._on_folder_by_caption_toggle,
            progress_color=C_BLUE,
            button_color="#fff",
            button_hover_color="#F3F4F6",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
        ).pack(side="left")
        ctk.CTkLabel(
            s_folder,
            text="开启后：用标题/标签建子目录；图片进「图片」、视频进「视频」。重名则追加消息 ID。",
            text_color=C_MUTED, anchor="w", wraplength=620, justify="left",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
        ).pack(fill="x", padx=16, pady=(0, 14))

        # proxy
        s2 = card(body)
        s2.pack(fill="x", padx=8, pady=8)
        self._section_title(s2, "网络代理")
        prow = ctk.CTkFrame(s2, fg_color="transparent")
        prow.pack(fill="x", padx=12, pady=(0, 14))
        prow.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(
            prow, textvariable=self.proxy_var, placeholder_text="http://127.0.0.1:7897",
            height=36, corner_radius=10, border_color=C_LINE, fg_color=C_INPUT,
        ).grid(row=0, column=0, sticky="ew", padx=(4, 8))
        btn(prow, "检测系统代理", self._detect_proxy, primary=False, width=120).grid(row=0, column=1)

        # download
        s3 = card(body)
        s3.pack(fill="x", padx=8, pady=8)
        self._section_title(s3, "下载与命名")
        self._labeled_entry(s3, "下载目录", self.dir_var)
        brow = ctk.CTkFrame(s3, fg_color="transparent")
        brow.pack(fill="x", padx=12, pady=(0, 6))
        btn(brow, "选择文件夹", self._pick_dir, primary=False, width=110).pack(side="left", padx=4)
        btn(brow, "打开目录", self._open_dir, primary=False, width=100).pack(side="left", padx=4)
        ctk.CTkLabel(
            s3, text="文件命名方式", text_color=C_TEXT, anchor="w",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
        ).pack(fill="x", padx=16, pady=(4, 6))
        mode_row = ctk.CTkFrame(s3, fg_color="transparent")
        mode_row.pack(fill="x", padx=12, pady=(0, 6))
        self._naming_mode_btns["auto"] = ctk.CTkButton(
            mode_row, text="自动获取文件名", height=36, corner_radius=10,
            command=lambda: self._set_naming_mode("auto"),
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
        )
        self._naming_mode_btns["auto"].pack(side="left", fill="x", expand=True, padx=4)
        self._naming_mode_btns["manual"] = ctk.CTkButton(
            mode_row, text="下载前批量重命名", height=36, corner_radius=10,
            command=lambda: self._set_naming_mode("manual"),
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
        )
        self._naming_mode_btns["manual"].pack(side="left", fill="x", expand=True, padx=4)
        self._set_naming_mode(self.naming_mode_var.get(), persist=False)
        self.naming_mode_hint = ctk.CTkLabel(
            s3, text="", text_color=C_MUTED, anchor="w", wraplength=620, justify="left",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
        )
        self.naming_mode_hint.pack(fill="x", padx=16, pady=(0, 8))
        self._refresh_naming_mode_hint()

        self._labeled_entry(s3, "命名模板", self.template_var)
        ctk.CTkLabel(
            s3,
            text="文件名完全按此模板生成。要用标签请包含 {title}/{tags}，例如 {index}_{title}_{tags}",
            text_color=C_MUTED, anchor="w",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
        ).pack(fill="x", padx=16, pady=(0, 4))
        ctk.CTkLabel(
            s3,
            text="可用变量：{title} {tags} {caption} {index} {date} {chat} {msg_id}",
            text_color=C_BLUE, anchor="w",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
        ).pack(fill="x", padx=16, pady=(0, 4))
        reset_row = ctk.CTkFrame(s3, fg_color="transparent")
        reset_row.pack(fill="x", padx=12, pady=(0, 8))
        btn(reset_row, "恢复默认模板", self._reset_naming_template, primary=False, width=120).pack(
            side="left", padx=4
        )
        nums = ctk.CTkFrame(s3, fg_color="transparent")
        nums.pack(fill="x", padx=12, pady=(0, 14))
        ctk.CTkLabel(nums, text="失败重试", text_color=C_TEXT).pack(side="left", padx=(4, 6))
        ctk.CTkEntry(
            nums, textvariable=self.retry_var, width=64, height=32, corner_radius=8, fg_color=C_INPUT
        ).pack(side="left")
        ctk.CTkLabel(nums, text="同时下载", text_color=C_TEXT).pack(side="left", padx=(16, 6))
        ctk.CTkEntry(
            nums, textvariable=self.conc_var, width=64, height=32, corner_radius=8, fg_color=C_INPUT
        ).pack(side="left")
        ctk.CTkLabel(
            nums, text="（1–5，图片/视频并行）", text_color=C_MUTED,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
        ).pack(side="left", padx=(8, 0))

        # download history log
        s_hist = card(body)
        s_hist.pack(fill="x", padx=8, pady=8)
        self._section_title(s_hist, "下载记录日志")
        hist_row = ctk.CTkFrame(s_hist, fg_color="transparent")
        hist_row.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkSwitch(
            hist_row,
            text="完成后写入下载记录（按时间命名 .log）",
            variable=self.history_log_enabled_var,
            command=self._on_history_log_toggle,
            progress_color=C_BLUE,
            button_color="#fff",
            button_hover_color="#F3F4F6",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
        ).pack(side="left")
        self._labeled_entry(s_hist, "日志目录", self.history_log_dir_var)
        ctk.CTkLabel(
            s_hist,
            text="留空则写到软件根目录；文件名如 download_20261118_235712.log",
            text_color=C_MUTED, anchor="w", wraplength=620, justify="left",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=12),
        ).pack(fill="x", padx=16, pady=(0, 6))
        hrow = ctk.CTkFrame(s_hist, fg_color="transparent")
        hrow.pack(fill="x", padx=12, pady=(0, 14))
        btn(hrow, "选择文件夹", self._pick_history_dir, primary=False, width=110).pack(side="left", padx=4)
        btn(hrow, "打开日志目录", self._open_history_dir, primary=False, width=110).pack(side="left", padx=4)
        btn(hrow, "恢复默认", self._reset_history_dir, primary=False, width=100).pack(side="left", padx=4)

        # credentials
        s4 = card(body)
        s4.pack(fill="x", padx=8, pady=8)
        self._section_title(s4, "API 凭证")
        ctk.CTkLabel(
            s4, text="可导出/导入 JSON 备份，勿发给他人。",
            text_color=C_MUTED, anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 6))
        self._labeled_entry(s4, "API ID", self.api_id_var)
        self._labeled_entry(s4, "API Hash", self.api_hash_var)
        crow = ctk.CTkFrame(s4, fg_color="transparent")
        crow.pack(fill="x", padx=12, pady=(4, 14))
        btn(crow, "导出凭证", self._export_credentials, width=100).pack(side="left", padx=4)
        btn(crow, "导入凭证", self._import_credentials, primary=False, width=100).pack(side="left", padx=4)
        btn(crow, "my.telegram.org", self._open_my_telegram, primary=False, width=130).pack(side="left", padx=4)

        save_row = ctk.CTkFrame(body, fg_color="transparent")
        save_row.pack(fill="x", padx=8, pady=10)
        btn(save_row, "保存设置", self._save_settings, width=120).pack(side="left", padx=4)

    def _section_title(self, parent, text: str) -> None:
        ctk.CTkLabel(
            parent, text=text, text_color=C_TEXT, anchor="w",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=14, weight="bold"),
        ).pack(fill="x", padx=16, pady=(14, 8))

    def _labeled_entry(self, parent, label: str, variable: tk.StringVar) -> None:
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(fill="x", padx=12, pady=4)
        wrap.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(wrap, text=label, width=80, anchor="w", text_color=C_TEXT).grid(
            row=0, column=0, padx=(4, 8), sticky="w"
        )
        ctk.CTkEntry(
            wrap, textvariable=variable, height=36, corner_radius=10,
            border_color=C_LINE, fg_color=C_INPUT,
        ).grid(row=0, column=1, sticky="ew")

    # ---- settings / files ----
    def _open_my_telegram(self) -> None:
        try:
            os.startfile("https://my.telegram.org")  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc))

    def _save_settings(self) -> None:
        try:
            self._save_cfg_from_ui()
            messagebox.showinfo("已保存", "设置已保存。")
            self._append_login_log("设置已保存")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))

    def _export_credentials(self) -> None:
        try:
            self._ensure_api_ready()
            path = filedialog.asksaveasfilename(
                title="导出凭证", defaultextension=".json",
                filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
                initialfile="tg_credentials.json",
            )
            if not path:
                return
            export_credentials(
                Path(path),
                api_id=self.api_id_var.get().strip(),
                api_hash=self.api_hash_var.get().strip(),
                proxy=self.proxy_var.get().strip(),
                include_proxy=True,
            )
            self._append_login_log(f"凭证已导出：{path}")
            messagebox.showinfo("已导出", f"凭证已保存到：\n{path}\n\n请妥善保管，不要发给他人。")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导出失败", str(exc))

    def _import_credentials(self) -> None:
        try:
            path = filedialog.askopenfilename(
                title="导入凭证", filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
            )
            if not path:
                return
            data = import_credentials(Path(path))
            self.api_id_var.set(str(data["api_id"]))
            self.api_hash_var.set(str(data["api_hash"]))
            if "proxy" in data:
                self.proxy_var.set(str(data.get("proxy") or ""))
            self._save_cfg_from_ui()
            self._append_login_log(f"凭证已导入：{path}")
            messagebox.showinfo("已导入", "凭证已导入并保存。如需重新登录，请再点扫码登录。")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导入失败", str(exc))

    def _bootstrap_async(self) -> None:
        self.loop = asyncio.new_event_loop()

        def runner() -> None:
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()

        self.loop_thread = threading.Thread(target=runner, daemon=True)
        self.loop_thread.start()
        self.after(50, self._try_auto_connect)

    def _run_coro(self, coro):
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _pick_dir(self) -> None:
        initial = str(resolve_download_dir(self.dir_var.get()))
        path = filedialog.askdirectory(initialdir=initial)
        if path:
            self.dir_var.set(path)
            try:
                cfg = self._save_cfg_from_ui()
                self._append_dl_log(f"下载目录已保存：{cfg['download_dir']}")
            except Exception:  # noqa: BLE001
                pass

    def _open_dir(self) -> None:
        path = resolve_download_dir(self.dir_var.get())
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc))

    def _on_history_log_toggle(self) -> None:
        try:
            self._save_cfg_from_ui()
        except Exception:  # noqa: BLE001
            pass
        state = "已开启" if self.history_log_enabled_var.get() else "已关闭"
        self._append_login_log(f"下载记录日志{state}")

    def _pick_history_dir(self) -> None:
        initial = str(resolve_history_log_dir(self.history_log_dir_var.get()))
        path = filedialog.askdirectory(initialdir=initial)
        if path:
            self.history_log_dir_var.set(path)
            try:
                self._save_cfg_from_ui()
                self._append_login_log(f"日志目录已保存：{path}")
            except Exception:  # noqa: BLE001
                pass

    def _open_history_dir(self) -> None:
        path = resolve_history_log_dir(self.history_log_dir_var.get())
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc))

    def _reset_history_dir(self) -> None:
        self.history_log_dir_var.set("")
        try:
            self._save_cfg_from_ui()
            self._append_login_log(f"日志目录已恢复为软件根目录：{ROOT}")
            messagebox.showinfo("已恢复", f"日志将写到：\n{ROOT}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))

    def _ui_put(self, kind: str, payload: Any = None) -> None:
        self._ui_q.put((kind, payload))

    def _drain_ui_queue(self) -> None:
        processed = 0
        logs_this_tick = 0
        try:
            while processed < 40:
                kind, payload = self._ui_q.get_nowait()
                processed += 1
                if kind == "log_dl":
                    # 文本框写入很贵：每拍最多刷几条，防止并发时界面卡死
                    if logs_this_tick >= 6:
                        continue
                    logs_this_tick += 1
                    self._write_log_box(self.dl_log_box, str(payload))
                elif kind == "log_login":
                    if logs_this_tick >= 6:
                        continue
                    logs_this_tick += 1
                    self._write_log_box(self.login_log_box, str(payload))
                elif kind == "status":
                    self.status_label.configure(text=str(payload))
                elif kind == "progress_flush":
                    self._flush_pending_progress()
                elif kind == "login_ui":
                    state, detail = payload
                    self._apply_login_ui(state, detail)
                elif kind == "render_tasks":
                    self._render_tasks()
                elif kind == "msg_error":
                    messagebox.showerror(str(payload[0]), str(payload[1]))
                elif kind == "msg_info":
                    messagebox.showinfo(str(payload[0]), str(payload[1]))
                elif kind == "call":
                    fn = payload
                    if callable(fn):
                        fn()
        except queue.Empty:
            pass
        if self._progress_dirty:
            self._flush_pending_progress()
        self.after(120, self._drain_ui_queue)

    def _flush_pending_progress(self) -> None:
        self._progress_dirty = False
        pending = self._pending_progress
        if not pending:
            return
        # 只刷最新快照，避免队列堆积
        items = list(pending.items())
        for task_id, (status, ratio, text) in items:
            self._apply_progress(task_id, status, ratio, text)

    @staticmethod
    def _write_log_box(box: ctk.CTkTextbox, text: str, *, max_lines: int = 200) -> None:
        box.configure(state="normal")
        box.insert("end", text + "\n")
        try:
            end_line = int(float(box.index("end-1c").split(".")[0]))
            if end_line > max_lines:
                box.delete("1.0", f"{end_line - max_lines}.0")
        except Exception:  # noqa: BLE001
            pass
        box.see("end")
        box.configure(state="disabled")

    def _append_dl_log(self, text: str) -> None:
        self._ui_put("log_dl", text)

    def _append_login_log(self, text: str) -> None:
        self._ui_put("log_login", text)

    def _append_log(self, text: str) -> None:
        """Download-related log (kept for downloader callback)."""
        # 队列堆积时丢弃过程日志，优先保证界面可点
        try:
            if self._ui_q.qsize() > 60:
                return
        except Exception:  # noqa: BLE001
            pass
        self._append_dl_log(text)

    def _set_status(self, text: str) -> None:
        self._ui_put("status", text)

    def _apply_login_ui(self, state: str, detail: str = "") -> None:
        if state == "logged_in":
            color = "#16A34A"
            text = detail or "已登录"
            side = text if text.startswith("已") else f"已登录：{text}"
        elif state == "checking":
            color = C_MUTED
            text = detail or "正在检查登录…"
            side = text
        elif state == "error":
            color = C_DANGER
            text = detail or "检查失败"
            side = text
        else:
            color = C_DANGER
            text = detail or "未登录"
            side = text
        self.login_state_dot.configure(text_color=color)
        self.login_state_label.configure(text=text, text_color=C_TEXT)
        self.status_label.configure(text=side, text_color=color if state == "logged_in" else C_MUTED)

    def _set_login_ui(self, state: str, detail: str = "") -> None:
        """state: checking | logged_in | logged_out | error"""
        self._ui_put("login_ui", (state, detail))

    def _detect_proxy(self) -> None:
        found = detect_windows_proxy()
        if not found:
            messagebox.showwarning(
                "未检测到",
                "没有读到 Windows 系统代理。\n请先打开 Clash Verge 并开启「系统代理」。",
            )
            return
        self.proxy_var.set(found)
        try:
            self._save_cfg_from_ui()
        except Exception:  # noqa: BLE001
            pass
        self._append_login_log(f"已填入系统代理：{found}")
        messagebox.showinfo("已填入", f"检测到系统代理：\n{found}")

    def _ensure_api_ready(self) -> bool:
        from .config_store import _normalize_api

        probe = _normalize_api({"api_id": self.api_id_var.get(), "api_hash": self.api_hash_var.get()})
        self.api_id_var.set(str(probe["api_id"]))
        self.api_hash_var.set(str(probe["api_hash"]))
        return True

    def _refresh_clipboard_hint(self) -> None:
        if not hasattr(self, "clipboard_hint"):
            return
        if self.clipboard_auto_var.get():
            self.clipboard_hint.configure(text="剪贴板自动填入：开", text_color=C_BLUE)
        else:
            self.clipboard_hint.configure(text="剪贴板自动填入：关", text_color=C_MUTED)

    def _on_clipboard_auto_toggle(self) -> None:
        self._refresh_clipboard_hint()
        try:
            self._save_cfg_from_ui()
        except Exception:  # noqa: BLE001
            pass
        state = "已开启" if self.clipboard_auto_var.get() else "已关闭"
        self._append_login_log(f"剪贴板自动识别{state}")

    def _on_folder_by_caption_toggle(self) -> None:
        try:
            self._save_cfg_from_ui()
        except Exception:  # noqa: BLE001
            pass
        state = "已开启" if self.folder_by_caption_var.get() else "已关闭"
        self._append_login_log(f"按文案建文件夹{state}")

    def _set_media_types(self, mode: str, *, persist: bool = True) -> None:
        if mode not in ("both", "video", "image"):
            mode = "both"
        self.media_types_var.set(mode)
        for key, b in self._media_type_btns.items():
            if key == mode:
                b.configure(fg_color=C_BLUE, hover_color=C_BLUE_HOVER, text_color="#fff")
            else:
                b.configure(fg_color=C_GRAY_BTN, hover_color=C_GRAY_HOVER, text_color=C_TEXT)
        if persist:
            try:
                self._save_cfg_from_ui()
            except Exception:  # noqa: BLE001
                pass
            label = {"both": "图片+视频", "video": "仅视频", "image": "仅图片"}[mode]
            self._append_login_log(f"下载内容：{label}")

    def _read_clipboard_text(self) -> str:
        try:
            return str(self.clipboard_get() or "")
        except tk.TclError:
            return ""

    def _fill_links_from_text(self, text: str, *, source: str = "剪贴板") -> int:
        refs = parse_links_block(text)
        if not refs:
            return 0
        lines = [r.canonical_url() for r in refs]
        self.links_box.delete("1.0", "end")
        self.links_box.insert("1.0", "\n".join(lines))
        self._append_dl_log(f"已从{source}填入 {len(refs)} 条链接")
        return len(refs)

    def _fill_from_clipboard(self) -> None:
        text = self._read_clipboard_text()
        if not text.strip():
            messagebox.showinfo("提示", "剪贴板为空")
            return
        n = self._fill_links_from_text(text, source="剪贴板")
        if not n:
            messagebox.showinfo("提示", "剪贴板中没有识别到 Telegram 消息链接")

    def _poll_clipboard(self) -> None:
        try:
            # 窗口失焦时不轮询，减少空转
            focused = bool(self.focus_displayof())
            if (
                focused
                and self.clipboard_auto_var.get()
                and not self._clipboard_fill_cooldown
            ):
                text = self._read_clipboard_text()
                if text and text != self._last_clipboard:
                    self._last_clipboard = text
                    if find_telegram_links(text):
                        n = self._fill_links_from_text(text, source="剪贴板")
                        if n:
                            self._clipboard_fill_cooldown = True
                            self.after(1500, self._clear_clipboard_cooldown)
        except Exception:  # noqa: BLE001
            pass
        self.after(1500, self._poll_clipboard)

    def _clear_clipboard_cooldown(self) -> None:
        self._clipboard_fill_cooldown = False

    def _reset_naming_template(self) -> None:
        self.template_var.set("{index}_{title}_{tags}")
        try:
            self._save_cfg_from_ui()
            self._append_login_log("命名模板已恢复为 {index}_{title}_{tags}")
            messagebox.showinfo("已恢复", "命名模板已设为：{index}_{title}_{tags}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))

    def _set_naming_mode(self, mode: str, *, persist: bool = True) -> None:
        mode = "manual" if mode == "manual" else "auto"
        self.naming_mode_var.set(mode)
        for key, b in self._naming_mode_btns.items():
            if key == mode:
                b.configure(fg_color=C_BLUE, hover_color=C_BLUE_HOVER, text_color="#fff")
            else:
                b.configure(fg_color=C_GRAY_BTN, hover_color=C_GRAY_HOVER, text_color=C_TEXT)
        self._refresh_naming_mode_hint()
        if persist:
            try:
                self._save_cfg_from_ui()
            except Exception:  # noqa: BLE001
                pass

    def _refresh_naming_mode_hint(self) -> None:
        if not hasattr(self, "naming_mode_hint"):
            return
        if self.naming_mode_var.get() == "manual":
            text = "开始下载前会先读取每条消息，弹出批量重命名窗口；确认后再下载。"
        else:
            text = "下载时按命名模板自动生成文件名（读取消息正文与 #标签）。"
        self.naming_mode_hint.configure(text=text)

    def _save_cfg_from_ui(self) -> dict:
        from .config_store import _normalize_api

        try:
            max_retries = max(0, int(self.retry_var.get().strip() or "3"))
        except ValueError as exc:
            raise ValueError("失败重试次数必须是数字") from exc
        concurrency = clamp_concurrency(self.conc_var.get().strip() or "3")
        self.conc_var.set(str(concurrency))

        naming_mode = "manual" if self.naming_mode_var.get() == "manual" else "auto"
        data = _normalize_api(
            {
                "api_id": self.api_id_var.get().strip(),
                "api_hash": self.api_hash_var.get().strip(),
                "download_dir": self.dir_var.get().strip() or "downloads",
                "naming_template": self.template_var.get().strip() or "{index}_{title}_{tags}",
                "naming_mode": naming_mode,
                "session_name": self.cfg.get("session_name") or "tg_downloader",
                "max_retries": max_retries,
                "concurrency": concurrency,
                "prefer_qr_login": True,
                "proxy": self.proxy_var.get().strip(),
                "clipboard_auto_fill": bool(self.clipboard_auto_var.get()),
                "folder_by_caption": bool(self.folder_by_caption_var.get()),
                "media_types": (
                    self.media_types_var.get()
                    if self.media_types_var.get() in ("both", "video", "image")
                    else "both"
                ),
                "history_log_enabled": bool(self.history_log_enabled_var.get()),
                "history_log_dir": self.history_log_dir_var.get().strip(),
            }
        )
        self.api_id_var.set(str(data["api_id"]))
        self.api_hash_var.set(str(data["api_hash"]))
        save_config(data)
        self.cfg = data
        return data

    def _ask_dialog(self, title: str, prompt: str, show: Optional[str] = None, *, height: int = 180) -> str:
        result: dict[str, str] = {"value": ""}
        done = threading.Event()

        def show_dialog() -> None:
            win = ctk.CTkToplevel(self)
            win.title(title)
            win.geometry(f"480x{height}")
            win.configure(fg_color=C_BG)
            win.transient(self)
            win.grab_set()
            box = card(win)
            box.pack(fill="both", expand=True, padx=12, pady=12)
            ctk.CTkLabel(box, text=prompt, wraplength=420, justify="left", text_color=C_TEXT).pack(
                padx=14, pady=(14, 8), anchor="w"
            )
            var = tk.StringVar()
            entry = ctk.CTkEntry(
                box, textvariable=var, show=show, height=34, corner_radius=10,
                fg_color=C_INPUT, border_color=C_LINE,
            )
            entry.pack(padx=14, pady=8, fill="x")
            entry.focus_set()

            def ok() -> None:
                result["value"] = var.get().strip()
                win.destroy()
                done.set()

            def cancel() -> None:
                result["value"] = ""
                win.destroy()
                done.set()

            btns = ctk.CTkFrame(box, fg_color="transparent")
            btns.pack(padx=14, pady=12, fill="x")
            btn(btns, "确定", ok, width=80).pack(side="right", padx=4)
            btn(btns, "取消", cancel, primary=False, width=80).pack(side="right", padx=4)
            win.protocol("WM_DELETE_WINDOW", cancel)
            entry.bind("<Return>", lambda _e: ok())

        self._ui_put("call", show_dialog)
        done.wait()
        return result["value"]

    async def _ask_phone(self) -> str:
        return await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._ask_dialog("登录", "请输入手机号（含国际区号，如 +8613xxxxxxxxx）"),
        )

    async def _ask_code(self, hint: str = "") -> str:
        prompt = hint.strip() or "请输入验证码（多在 Telegram 官方对话里，不是短信）"
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._ask_dialog("验证码", prompt, height=220)
        )

    async def _ask_password(self) -> str:
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._ask_dialog("两步验证", "请输入两步验证密码", show="*")
        )

    def _ensure_qr_window(self) -> None:
        if self._qr_window and self._qr_window.winfo_exists():
            try:
                self._qr_window.lift()
                self._qr_window.attributes("-topmost", True)
            except Exception:  # noqa: BLE001
                pass
            return
        win = ctk.CTkToplevel(self)
        win.title("扫码登录")
        win.geometry("440x600")
        win.configure(fg_color=C_BG)
        win.transient(self)
        try:
            win.attributes("-topmost", True)
            win.lift()
            win.focus_force()
        except Exception:  # noqa: BLE001
            pass
        box = card(win)
        box.pack(fill="both", expand=True, padx=14, pady=14)
        ctk.CTkLabel(
            box,
            text="用手机 Telegram 扫码\n设置 → 设备 → 扫描二维码\n若一直无码，到设置填代理",
            wraplength=380, justify="left", text_color=C_TEXT,
        ).pack(padx=14, pady=12, anchor="w")
        self._qr_label = ctk.CTkLabel(box, text="正在生成二维码…", text_color=C_MUTED)
        self._qr_label.pack(padx=14, pady=8)
        self._qr_url_var = tk.StringVar(value="")
        ctk.CTkEntry(
            box, textvariable=self._qr_url_var, height=32, corner_radius=10,
            fg_color=C_INPUT, border_color=C_LINE,
        ).pack(padx=14, pady=4, fill="x")

        def copy_url() -> None:
            text = self._qr_url_var.get()
            if not text:
                return
            self.clipboard_clear()
            self.clipboard_append(text)
            self._append_login_log("已复制登录链接")

        btn(box, "复制链接", copy_url, primary=False, width=100).pack(padx=14, pady=10, anchor="w")
        ctk.CTkLabel(box, text="二维码约 30 秒刷新一次", text_color=C_MUTED).pack(padx=14, pady=4, anchor="w")
        self._qr_window = win

    def _open_qr_placeholder(self) -> None:
        def _do() -> None:
            self._ensure_qr_window()
            if self._qr_label:
                self._qr_label.configure(image=None, text="正在连接 Telegram / 生成二维码…")

        self._ui_put("call", _do)

    async def _show_qr(self, url: str) -> None:
        pil = None
        err = ""
        try:
            import qrcode

            def make_image() -> Image.Image:
                img = qrcode.make(url)
                bio = BytesIO()
                img.save(bio, format="PNG")
                bio.seek(0)
                return Image.open(bio).copy()

            pil = await asyncio.get_event_loop().run_in_executor(None, make_image)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            self._append_login_log(f"二维码图片生成失败，改显示链接：{exc}")

        done = threading.Event()

        def render() -> None:
            self._ensure_qr_window()
            if hasattr(self, "_qr_url_var"):
                self._qr_url_var.set(url)
            assert self._qr_label is not None
            if pil is not None:
                ctk_img = ctk.CTkImage(light_image=pil, dark_image=pil, size=(260, 260))
                self._qr_label.configure(image=ctk_img, text="")
                self._qr_label.image = ctk_img
            else:
                self._qr_label.configure(image=None, text=f"图片生成失败：{err}\n请复制下方链接")
            done.set()

        self._ui_put("call", render)
        await asyncio.get_event_loop().run_in_executor(None, done.wait)

    async def _clear_qr(self) -> None:
        done = threading.Event()

        def close() -> None:
            if self._qr_window and self._qr_window.winfo_exists():
                self._qr_window.destroy()
            self._qr_window = None
            self._qr_label = None
            done.set()

        self._ui_put("call", close)
        await asyncio.get_event_loop().run_in_executor(None, done.wait)

    def _try_auto_connect(self) -> None:
        if self.loop is None:
            self.after(100, self._try_auto_connect)
            return
        if self._busy:
            return

        async def job() -> None:
            self._set_login_ui("checking", "正在检查本地会话…")
            self._append_login_log("正在读取登录状态…")
            try:
                cfg = load_config()
                if not cfg.get("api_id") or not cfg.get("api_hash"):
                    self._logged_in = False
                    self._set_login_ui("logged_out", "未登录（缺少 API 配置）")
                    self._append_login_log("未登录：缺少 API 配置")
                    return
                if str(cfg.get("api_hash")) in ("", "your_api_hash_here"):
                    self._logged_in = False
                    self._set_login_ui("logged_out", "未登录")
                    return
                self.api_id_var.set(str(cfg.get("api_id")))
                self.api_hash_var.set(str(cfg.get("api_hash")))
                proxy = self.proxy_var.get().strip() or str(cfg.get("proxy") or "")
                if self.client:
                    try:
                        await self.client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    self.client = None
                client = build_client(
                    cfg["api_id"], cfg["api_hash"], cfg["session_name"], proxy_url=proxy
                )
                await asyncio.wait_for(client.connect(), timeout=15)
                if await client.is_user_authorized():
                    self.client = client
                    me = await client.get_me()
                    uname = getattr(me, "username", None)
                    name = f"@{uname}" if uname else (getattr(me, "first_name", "") or "已登录")
                    self._logged_in = True
                    self._set_login_ui("logged_in", f"已登录：{name}")
                    self._append_login_log(f"已恢复本地会话：{name}")
                else:
                    await client.disconnect()
                    self.client = None
                    self._logged_in = False
                    self._set_login_ui("logged_out", "未登录（本地无有效会话）")
                    self._append_login_log("本地会话无效，需要重新登录")
            except Exception as exc:  # noqa: BLE001
                self._logged_in = False
                self._set_login_ui("error", f"检查失败：{exc}")
                self._append_login_log(f"检查登录状态失败：{exc}")

        self._run_coro(job())

    def _login_status(self, msg: str) -> None:
        self._set_login_ui("checking", msg)
        self._append_login_log(msg)

    def _on_login(self, prefer_qr: bool = True) -> None:
        if self._busy:
            return
        if self._logged_in and self.client:
            if not messagebox.askyesno("重新登录", "当前已登录，是否重新登录？"):
                return
        self._ensure_api_ready()
        if prefer_qr:
            self._open_qr_placeholder()

        async def job() -> None:
            self._busy = True
            try:
                cfg = self._save_cfg_from_ui()
                self._set_login_ui("checking", "连接中…")
                self._append_login_log(
                    "开始登录…" + (f"（代理 {cfg.get('proxy')}）" if cfg.get("proxy") else "（直连）")
                )
                if self.client:
                    await self.client.disconnect()
                self.client = build_client(
                    cfg["api_id"], cfg["api_hash"], cfg["session_name"],
                    proxy_url=str(cfg.get("proxy") or ""),
                )
                mode = await ensure_login(
                    self.client,
                    prefer_qr=prefer_qr,
                    ask_phone=self._ask_phone,
                    ask_code=self._ask_code,
                    ask_password=self._ask_password,
                    show_qr=self._show_qr,
                    clear_qr=self._clear_qr,
                    on_status=self._login_status,
                )
                me = await self.client.get_me()
                uname = getattr(me, "username", None)
                name = f"@{uname}" if uname else (getattr(me, "first_name", "") or "已登录")
                self._logged_in = True
                self._set_login_ui("logged_in", f"已登录：{name}")
                self._append_login_log(f"登录成功（{mode}）：{name}")
            except Exception as exc:  # noqa: BLE001
                self._logged_in = False
                self._set_login_ui("error", f"登录失败：{exc}")
                self._append_login_log(f"登录失败：{exc}")
                msg = str(exc)
                self._ui_put("msg_error", ("登录失败", msg))
                try:
                    await self._clear_qr()
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self._busy = False

        self._run_coro(job())

    def _filtered_tasks(self) -> list[DownloadTask]:
        mapping = {
            "全部": None,
            "下载中": {TaskStatus.RUNNING, TaskStatus.PENDING},
            "失败": {TaskStatus.ERROR},
            "已取消": {TaskStatus.CANCELLED, TaskStatus.SKIPPED},
            "完成": {TaskStatus.DONE},
        }
        wanted = mapping.get(self._filter.get())
        if not wanted:
            return list(self.tasks)
        return [t for t in self.tasks if t.status in wanted]

    def _render_tasks(self) -> None:
        for child in self.task_host.winfo_children():
            child.destroy()
        self.task_rows.clear()
        for i, task in enumerate(self._filtered_tasks()):
            row = TaskRow(
                self.task_host,
                task,
                on_retry=self._retry_one,
                on_open=self._open_one,
                on_delete=self._delete_one,
                on_pause=self._pause_one,
            )
            row.grid(row=i, column=0, sticky="ew", pady=5)
            row.sync_from_task()
            self.task_rows[str(task.index)] = row

    def _on_progress(self, task_id: str, status: str, ratio: float, text: str) -> None:
        # 下载线程：只写快照；由主线程定时 drain 刷新，不再每次入队 flush
        self._pending_progress[task_id] = (status, ratio, text)
        self._progress_dirty = True

    def _apply_progress(self, task_id: str, status: str, ratio: float, text: str) -> None:
        for t in self.tasks:
            if str(t.index) == task_id:
                if status in TaskStatus._value2member_map_:
                    t.status = TaskStatus(status)
                t.progress = ratio
                t.message = text
                break
        row = self.task_rows.get(task_id)
        if row:
            row.update_view(status, ratio, text)

    def _make_downloader(self, cfg: dict) -> Downloader:
        mt = str(cfg.get("media_types") or "both").strip().lower()
        if mt not in ("both", "video", "image"):
            mt = "both"
        history = None
        if bool(cfg.get("history_log_enabled", True)):
            history = DownloadHistoryLogger(
                resolve_history_log_dir(str(cfg.get("history_log_dir") or "")),
                enabled=True,
            )
        return Downloader(
            client=self.client,
            download_dir=resolve_download_dir(cfg["download_dir"]),
            naming_template=cfg["naming_template"],
            max_retries=int(cfg.get("max_retries", 3)),
            concurrency=clamp_concurrency(cfg.get("concurrency", 3)),
            folder_by_caption=bool(cfg.get("folder_by_caption", True)),
            media_types=mt,
            history=history,
            on_progress=self._on_progress,
            on_log=self._append_log,
        )

    async def _run_tasks(self, tasks: list[DownloadTask], cfg: dict) -> None:
        if not tasks:
            return
        downloader = self._make_downloader(cfg)
        self.downloader = downloader
        conc = clamp_concurrency(cfg.get("concurrency", 3))
        self._set_status(f"下载中（{len(tasks)}，并发 {conc}）…")
        self._append_dl_log(f"开始下载 {len(tasks)} 个任务（同时下载 {conc}）")
        await downloader.download_many(tasks, concurrency=conc)

        def finish() -> None:
            self._render_tasks()
            ok = sum(1 for t in self.tasks if t.status == TaskStatus.DONE)
            fail = sum(1 for t in self.tasks if t.status == TaskStatus.ERROR)
            cancelled = sum(
                1 for t in self.tasks if t.status in {TaskStatus.CANCELLED, TaskStatus.SKIPPED}
            )
            self._write_log_box(self.dl_log_box, f"批次结束：完成 {ok}，失败 {fail}，取消 {cancelled}")
            # 侧栏恢复登录状态，下载结果写在下载日志里
            if self._logged_in:
                cur = self.login_state_label.cget("text")
                self.status_label.configure(text=cur, text_color="#16A34A")
            else:
                self.status_label.configure(text=f"完成 {ok} / 失败 {fail} / 取消 {cancelled}")

        self._ui_put("call", finish)
        self.downloader = None

    def _batch_rename_dialog(
        self, items: list[tuple[int, str, str]]
    ) -> Optional[list[str]]:
        """Show editable filenames. items = [(index, link_label, suggested_name), ...]"""
        result: dict[str, Optional[list[str]]] = {"names": None}
        done = threading.Event()

        def show_dialog() -> None:
            win = ctk.CTkToplevel(self)
            win.title("批量重命名")
            win.geometry("720x520")
            win.minsize(640, 420)
            win.configure(fg_color=C_BG)
            win.transient(self)
            win.grab_set()
            try:
                win.lift()
                win.focus_force()
            except Exception:  # noqa: BLE001
                pass

            box = card(win)
            box.pack(fill="both", expand=True, padx=14, pady=14)
            box.grid_columnconfigure(0, weight=1)
            box.grid_rowconfigure(1, weight=1)

            ctk.CTkLabel(
                box,
                text="确认或修改文件名后开始下载",
                text_color=C_TEXT,
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=15, weight="bold"),
                anchor="w",
            ).grid(row=0, column=0, padx=16, pady=(14, 6), sticky="w")

            scroll = ctk.CTkScrollableFrame(box, fg_color=C_CARD, corner_radius=8)
            scroll.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
            scroll.grid_columnconfigure(1, weight=1)

            vars_list: list[tk.StringVar] = []
            for i, (idx, label, suggested) in enumerate(items):
                ctk.CTkLabel(
                    scroll, text=f"{idx:03d}", width=40, text_color=C_MUTED, anchor="w",
                ).grid(row=i * 2, column=0, padx=(8, 4), pady=(8, 0), sticky="w")
                ctk.CTkLabel(
                    scroll, text=label, text_color=C_MUTED, anchor="w",
                    font=ctk.CTkFont(family="Microsoft YaHei UI", size=11),
                ).grid(row=i * 2, column=1, padx=4, pady=(8, 0), sticky="ew")
                var = tk.StringVar(value=suggested)
                vars_list.append(var)
                ctk.CTkEntry(
                    scroll, textvariable=var, height=34, corner_radius=10,
                    fg_color=C_INPUT, border_color=C_LINE,
                ).grid(row=i * 2 + 1, column=0, columnspan=2, padx=8, pady=(2, 6), sticky="ew")

            def ok() -> None:
                result["names"] = [v.get().strip() for v in vars_list]
                win.destroy()
                done.set()

            def cancel() -> None:
                result["names"] = None
                win.destroy()
                done.set()

            btns = ctk.CTkFrame(box, fg_color="transparent")
            btns.grid(row=2, column=0, sticky="ew", padx=12, pady=12)
            btn(btns, "开始下载", ok, width=110).pack(side="right", padx=4)
            btn(btns, "取消", cancel, primary=False, width=80).pack(side="right", padx=4)
            win.protocol("WM_DELETE_WINDOW", cancel)

        self._ui_put("call", show_dialog)
        done.wait()
        return result["names"]

    async def _prepare_manual_names(
        self, tasks: list[DownloadTask], cfg: dict
    ) -> bool:
        """Preview auto names, let user edit. Returns False if cancelled."""
        downloader = self._make_downloader(cfg)
        self._set_status(f"读取消息命名信息（{len(tasks)}）…")
        items: list[tuple[int, str, str]] = []
        for task in tasks:
            try:
                suggested = await downloader.suggest_filename(task)
                task.message = f"建议：{suggested}"
            except Exception as exc:  # noqa: BLE001
                suggested = f"{task.index:03d}.mp4"
                task.message = f"预览失败，可手填：{exc}"
                self._append_log(f"[{task.index}] 预览文件名失败：{exc}")
            items.append((task.index, task.ref.label, suggested))
            self._emit_task_message(task)

        names = await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._batch_rename_dialog(items)
        )
        if names is None:
            return False
        for task, name in zip(tasks, names):
            task.custom_name = name or None
            if name:
                task.filename = name if Path(name).suffix else name
                task.message = f"将保存为：{name}"
            self._emit_task_message(task)
        return True

    def _emit_task_message(self, task: DownloadTask) -> None:
        self._on_progress(str(task.index), task.status.value, task.progress, task.message)

    @staticmethod
    def _ref_key(ref) -> tuple:
        return (ref.username, ref.chat_id, ref.message_id)

    def _on_start(self) -> None:
        if self._busy:
            return
        refs = parse_links_block(self.links_box.get("1.0", "end"))
        if not refs:
            messagebox.showwarning("提示", "请先粘贴至少一条有效的消息链接")
            return

        async def job() -> None:
            self._busy = True
            to_run: list[DownloadTask] = []
            brand_new: list[DownloadTask] = []
            try:
                cfg = self._save_cfg_from_ui()
                if not self.client or not await self.client.is_user_authorized():
                    raise RuntimeError("请先到「设置」里扫码或手机号登录")

                existing = {self._ref_key(t.ref): t for t in self.tasks}
                start_index = (self.tasks[-1].index + 1) if self.tasks else 1
                next_index = start_index

                for ref in refs:
                    key = self._ref_key(ref)
                    old = existing.get(key)
                    if old is not None:
                        if old.status == TaskStatus.DONE:
                            self._append_dl_log(f"已完成，跳过：{ref.label}")
                            continue
                        if old.status == TaskStatus.RUNNING:
                            self._append_dl_log(f"下载中，跳过重复：{ref.label}")
                            continue
                        if old.status == TaskStatus.PENDING:
                            to_run.append(old)
                            continue
                        # 失败 / 已取消 → 续传同一条，不新建
                        old.status = TaskStatus.PENDING
                        old.message = "等待继续"
                        to_run.append(old)
                        self._append_dl_log(f"继续已有任务：{ref.label}")
                        continue

                    task = DownloadTask(index=next_index, ref=ref)
                    next_index += 1
                    self.tasks.append(task)
                    existing[key] = task
                    brand_new.append(task)
                    to_run.append(task)

                self._ui_put("render_tasks")
                if not to_run:
                    self._append_dl_log("没有新的可下载任务（链接已在列表中）")
                    self._ui_put("msg_info", ("提示", "这些链接已在任务列表中，无需重复添加。"))
                    return

                if cfg.get("naming_mode") == "manual" and brand_new:
                    ok = await self._prepare_manual_names(brand_new, cfg)
                    if not ok:
                        for t in brand_new:
                            t.status = TaskStatus.CANCELLED
                            t.message = "已取消重命名"
                        self._ui_put("render_tasks")
                        self._set_status("已取消")
                        self._append_dl_log("已取消批量重命名，未开始下载")
                        return

                await self._run_tasks(to_run, cfg)
            except Exception as exc:  # noqa: BLE001
                self._set_status("下载中断")
                self._append_dl_log(f"下载中断：{exc}")
                self._ui_put("msg_error", ("下载失败", str(exc)))
            finally:
                self._busy = False

        self._run_coro(job())

    def _retry_one(self, task: DownloadTask) -> None:
        if self._busy:
            messagebox.showinfo("提示", "当前有任务在执行，请稍后再试")
            return

        async def job() -> None:
            self._busy = True
            try:
                cfg = self._save_cfg_from_ui()
                if not self.client or not await self.client.is_user_authorized():
                    raise RuntimeError("请先登录")
                is_resume = task.status in {TaskStatus.CANCELLED, TaskStatus.SKIPPED}
                task.status = TaskStatus.PENDING
                task.message = "等待继续" if is_resume else "等待重试"
                self._ui_put("render_tasks")
                await self._run_tasks([task], cfg)
            except Exception as exc:  # noqa: BLE001
                self._ui_put("msg_error", ("操作失败", str(exc)))
            finally:
                self._busy = False

        self._run_coro(job())

    def _delete_one(self, task: DownloadTask) -> None:
        if task.status == TaskStatus.RUNNING:
            messagebox.showinfo("提示", "下载中的任务请先暂停，再删除")
            return
        self.tasks = [t for t in self.tasks if t.index != task.index]
        self._append_dl_log(f"已删除任务：{task.ref.label}")
        self._render_tasks()

    def _pause_one(self, task: DownloadTask) -> None:
        if task.status != TaskStatus.RUNNING:
            return
        if not self.downloader:
            messagebox.showinfo("提示", "当前没有进行中的下载")
            return
        self.downloader.cancel_task(str(task.index))
        task.message = "正在暂停…"
        self._append_dl_log(f"[{task.index}] 已请求暂停（保留断点）")
        row = self.task_rows.get(str(task.index))
        if row:
            row.update_view(task.status.value, task.progress, task.message)

    def _on_retry_failed(self) -> None:
        failed = [
            t for t in self.tasks
            if t.status in {TaskStatus.ERROR, TaskStatus.CANCELLED, TaskStatus.SKIPPED}
        ]
        if not failed:
            messagebox.showinfo("提示", "没有可重试的失败/取消任务")
            return
        if self._busy:
            messagebox.showinfo("提示", "当前有任务在执行，请稍后再试")
            return

        async def job() -> None:
            self._busy = True
            try:
                cfg = self._save_cfg_from_ui()
                if not self.client or not await self.client.is_user_authorized():
                    raise RuntimeError("请先登录")
                for t in failed:
                    t.status = TaskStatus.PENDING
                    t.message = "等待重试"
                self._ui_put("render_tasks")
                await self._run_tasks(failed, cfg)
            except Exception as exc:  # noqa: BLE001
                self._ui_put("msg_error", ("重试失败", str(exc)))
            finally:
                self._busy = False

        self._run_coro(job())

    def _open_one(self, task: DownloadTask) -> None:
        if not task.saved_path:
            return
        path = Path(task.saved_path)
        if not path.exists():
            messagebox.showwarning("提示", "文件或文件夹不存在")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", str(exc))

    def _clear_done(self) -> None:
        self.tasks = [t for t in self.tasks if t.status != TaskStatus.DONE]
        self._render_tasks()

    def _on_cancel(self) -> None:
        if not self.downloader:
            return
        self.downloader.cancel()
        for t in self.tasks:
            if t.status == TaskStatus.RUNNING:
                t.message = "正在暂停…"
                row = self.task_rows.get(str(t.index))
                if row:
                    row.update_view(t.status.value, t.progress, t.message)
        self._append_dl_log("已请求暂停：进行中任务会保留 .part 以便断点续传")
        self._set_status("暂停中…")

    def _on_close(self) -> None:
        async def shutdown() -> None:
            if self.downloader:
                self.downloader.cancel()
            if self.client:
                await self.client.disconnect()

        try:
            if self.loop and self.loop.is_running():
                fut = self._run_coro(shutdown())
                fut.result(timeout=5)
                self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


def run_app() -> None:
    app = App()
    app.mainloop()
