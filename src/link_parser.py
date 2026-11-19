from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


LINK_RE = re.compile(
    r"""(?xi)
    (?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/
    (?:
        c/(?P<private_id>\d+)/(?P<private_msg>\d+)
        |
        (?P<username>[A-Za-z0-9_]+)/(?P<public_msg>\d+)
    )
    (?:\?[^ \t\r\n]*)?
    """
)


@dataclass(frozen=True)
class MessageRef:
    raw: str
    username: Optional[str]
    chat_id: Optional[int]
    message_id: int

    @property
    def label(self) -> str:
        if self.username:
            return f"@{self.username}/{self.message_id}"
        return f"c/{abs(self.chat_id or 0) % 10**10}/{self.message_id}"

    def canonical_url(self) -> str:
        if self.username:
            return f"https://t.me/{self.username}/{self.message_id}"
        # internal -100xxxxxxxxxx → t.me/c/<id>/<msg>
        digits = str(abs(self.chat_id or 0))
        if digits.startswith("100") and len(digits) > 3:
            digits = digits[3:]
        return f"https://t.me/c/{digits}/{self.message_id}"


def parse_message_link(text: str) -> Optional[MessageRef]:
    text = text.strip()
    if not text:
        return None
    m = LINK_RE.search(text)
    if not m:
        return None

    if m.group("private_id"):
        # t.me/c/<id>/<msg> → internal id -100xxxxxxxxxx
        internal = int(f"-100{m.group('private_id')}")
        return MessageRef(
            raw=m.group(0).rstrip(".,;)]}"),
            username=None,
            chat_id=internal,
            message_id=int(m.group("private_msg")),
        )

    return MessageRef(
        raw=m.group(0).rstrip(".,;)]}"),
        username=m.group("username"),
        chat_id=None,
        message_id=int(m.group("public_msg")),
    )


def find_telegram_links(text: str) -> list[str]:
    """Extract raw telegram message URLs from free text (supports comma/newline)."""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for m in LINK_RE.finditer(text):
        raw = m.group(0).rstrip(".,;，、)]}")
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(raw)
    return found


def parse_links_block(block: str) -> list[MessageRef]:
    """
    Parse links from multi-line / comma-separated text.
    Supports: newline, English comma ,, Chinese comma ，
    """
    refs: list[MessageRef] = []
    seen: set[tuple] = set()

    # First pass: regex extract all TG links from whole block
    for raw in find_telegram_links(block):
        ref = parse_message_link(raw)
        if not ref:
            continue
        key = (ref.username, ref.chat_id, ref.message_id)
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)

    if refs:
        return refs

    # Fallback: split by newline / comma and try each fragment
    for part in re.split(r"[\n\r,，、;；]+", block or ""):
        part = part.strip()
        if not part or part.startswith("#"):
            continue
        ref = parse_message_link(part)
        if not ref:
            continue
        key = (ref.username, ref.chat_id, ref.message_id)
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    return refs
