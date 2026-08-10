from __future__ import annotations

import hashlib
import sqlite3

import xxhash

WINDOW_ID_VERSION = "1"


def normalize_window_text(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n")


def compute_window_content_hash(
    session_id: str, request_text: str | None, response_text: str | None
) -> str:
    """Stable content fingerprint from session + turn texts (not message seqs)."""
    payload = "\n".join(
        [
            WINDOW_ID_VERSION,
            session_id,
            normalize_window_text(request_text),
            normalize_window_text(response_text),
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]


def window_id_for_content_hash(content_hash: str, occurrence: int) -> str:
    """Primary key: content hash, with stable disambiguation for duplicate turns."""
    if occurrence <= 0:
        return content_hash
    raw = f"{content_hash}:dup:{occurrence}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _is_tool_plumbing(msg: sqlite3.Row) -> bool:
    try:
        return bool(msg["is_tool_plumbing"])
    except (IndexError, KeyError):
        return False


def build_exchange_windows(
    messages: list[sqlite3.Row],
) -> list[tuple[str, str, str, str, str]]:
    """Pair each user message with the next assistant message.

    Returns
    (request_message_id, response_message_id, input_hash, content_hash, window_id).
    content_hash is the rematch key; window_id is the row PK (disambiguated).
    """
    windows: list[tuple[str, str, str, str, str]] = []
    pending_user: sqlite3.Row | None = None
    session_id = ""
    occurrence_by_hash: dict[str, int] = {}
    if messages:
        try:
            session_id = str(messages[0]["session_id"] or "")
        except (IndexError, KeyError):
            session_id = ""
    for msg in messages:
        if _is_tool_plumbing(msg):
            continue
        role = msg["role"]
        if role == "user":
            pending_user = msg
            continue
        if role == "assistant" and pending_user is not None:
            req_text = pending_user["text"] or ""
            resp_text = msg["text"] or ""
            input_hash = pending_user["content_hash"] or xxhash.xxh64(
                req_text.encode("utf-8", errors="replace")
            ).hexdigest()
            sid = session_id
            try:
                sid = str(pending_user["session_id"] or session_id)
            except (IndexError, KeyError):
                pass
            content_hash = compute_window_content_hash(sid, req_text, resp_text)
            occ = occurrence_by_hash.get(content_hash, 0)
            occurrence_by_hash[content_hash] = occ + 1
            wid = window_id_for_content_hash(content_hash, occ)
            windows.append(
                (pending_user["id"], msg["id"], input_hash, content_hash, wid)
            )
            pending_user = None
    return windows
