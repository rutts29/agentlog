from __future__ import annotations

import sqlite3

import xxhash


def _is_tool_plumbing(msg: sqlite3.Row) -> bool:
    try:
        return bool(msg["is_tool_plumbing"])
    except (IndexError, KeyError):
        return False


def build_exchange_windows(
    messages: list[sqlite3.Row],
) -> list[tuple[str, str, str]]:
    """Pair each user message with the next assistant message."""
    windows: list[tuple[str, str, str]] = []
    pending_user: sqlite3.Row | None = None
    for msg in messages:
        if _is_tool_plumbing(msg):
            continue
        role = msg["role"]
        if role == "user":
            pending_user = msg
            continue
        if role == "assistant" and pending_user is not None:
            input_hash = pending_user["content_hash"] or xxhash.xxh64(
                (pending_user["text"] or "").encode("utf-8", errors="replace")
            ).hexdigest()
            windows.append((pending_user["id"], msg["id"], input_hash))
            pending_user = None
    return windows
