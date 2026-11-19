from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# #tag / ＃tag / #多字标签；也兼容 #tag, #tag。等后面跟着标点
HASHTAG_RE = re.compile(r"[#＃]([^\s#＃]+)")
WHITESPACE_RE = re.compile(r"\s+")


def sanitize(name: str, max_len: int = 160) -> str:
    name = INVALID_CHARS.sub("_", name).strip(" .")
    name = re.sub(r"_+", "_", name)
    if not name:
        name = "file"
    return name[:max_len]


def _clean_tag(tag: str) -> str:
    return tag.strip(".,;:!?，。；：！？、)）]】》\"'”").rstrip("_-")


def _is_weak_title(text: str) -> bool:
    """Emoji / punctuation-only titles are useless for filenames."""
    # keep letters/digits/CJK ideographs
    useful = re.sub(r"[^\w\u4e00-\u9fff]+", "", text, flags=re.UNICODE)
    return not useful


def folder_name_from_caption(parts: dict[str, str], *, fallback: str = "untitled") -> str:
    """
    Build a single folder name from caption parts.
    Prefer title_tags, then title, tags, caption snippet.
    """
    title = (parts.get("title") or "").strip()
    tags = (parts.get("tags") or "").strip()
    caption = (parts.get("caption") or "").strip()
    if title and tags:
        raw = f"{title}_{tags}"
    elif title:
        raw = title
    elif tags:
        raw = tags
    elif caption:
        raw = caption[:60]
    else:
        raw = fallback
    name = sanitize(raw, 80)
    return name or sanitize(fallback, 80)


LINK_MARKER = ".tg_link_id"


def resolve_unique_folder(parent: Path, base_name: str, msg_id: int) -> str:
    """
    Avoid colliding caption folders from different links.
    Reuse existing folder only when its .tg_link_id matches msg_id;
    otherwise use base_name_{msg_id}.
    """
    base = sanitize(base_name, 80) or f"msg_{msg_id}"
    preferred = parent / base
    marker = preferred / LINK_MARKER
    if not preferred.exists():
        return base
    try:
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == str(msg_id):
            return base
    except Exception:  # noqa: BLE001
        pass
    alt = sanitize(f"{base}_{msg_id}", 100)
    return alt or f"msg_{msg_id}"


def write_link_marker(folder: Path, msg_id: int) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / LINK_MARKER).write_text(str(msg_id), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def extract_caption_parts(text: str | None) -> dict[str, str]:
    """
    From message caption/body:
      title  - first meaningful line (hashtags stripped; skip emoji-only)
      tags   - #标签 joined with _
      caption- short single-line summary
    """
    raw = (text or "").strip()
    if not raw:
        return {"title": "", "tags": "", "caption": ""}

    tags = HASHTAG_RE.findall(raw)
    # keep order, dedupe
    seen: set[str] = set()
    tag_list: list[str] = []
    for t in tags:
        t = _clean_tag(t)
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        tag_list.append(t)

    title = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        cleaned = HASHTAG_RE.sub("", line)
        cleaned = WHITESPACE_RE.sub(" ", cleaned).strip(" -_|·•")
        if cleaned and not _is_weak_title(cleaned):
            title = cleaned
            break
    if not title and tag_list:
        # 首行只有表情时，用第一个标签当标题，并从 tags 里去掉以免重复
        title = tag_list[0]
        tag_list = tag_list[1:]
    if not title:
        first = WHITESPACE_RE.sub(" ", raw.splitlines()[0]).strip()
        if first and not _is_weak_title(first):
            title = first[:40]

    caption = WHITESPACE_RE.sub(" ", raw.replace("\n", " ")).strip()
    return {
        "title": title[:80],
        "tags": "_".join(tag_list[:12]),
        "caption": caption[:120],
    }


def render_filename(
    template: str,
    *,
    index: int,
    chat: str,
    msg_id: int,
    date: datetime | None = None,
    title: str = "",
    tags: str = "",
    caption: str = "",
    ext: str = ".mp4",
) -> str:
    date = date or datetime.now()
    values = {
        "index": f"{index:03d}",
        "date": date.strftime("%Y%m%d"),
        "time": date.strftime("%H%M%S"),
        "chat": sanitize(chat or "chat"),
        "msg_id": str(msg_id),
        "title": sanitize(title, 80) if title else "",
        "tags": sanitize(tags, 120) if tags else "",
        "caption": sanitize(caption, 100) if caption else "",
    }
    name = template
    for key, value in values.items():
        name = name.replace("{" + key + "}", value)

    # collapse leftover empty placeholders / double separators
    name = re.sub(r"[_-]{2,}", "_", name)
    name = name.strip(" ._")
    name = sanitize(name, 180)
    if not name:
        name = f"{values['index']}_{values['msg_id']}"
    if not name.lower().endswith(ext.lower()):
        name += ext if ext.startswith(".") else f".{ext}"
    return name


def unique_path(directory: Path, filename: str) -> Path:
    path = directory / filename
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    i = 1
    while True:
        candidate = directory / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1
