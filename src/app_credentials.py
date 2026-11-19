"""
Built-in app credentials (fallback).

Telegram requires every MTProto client to present an api_id/api_hash.
These defaults come from Telegram Desktop's open-source client, so you can
use this tool without my.telegram.org when that site rate-limits you.

Prefer replacing them with your own from https://my.telegram.org when possible.
For personal use on your own account this is usually fine; don't redistribute
as a mass-market client using these shared IDs.
"""

# Telegram Desktop (public in tdesktop source)
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"
