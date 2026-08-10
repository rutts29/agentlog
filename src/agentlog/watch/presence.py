"""In-memory live session presence for the watch daemon.

Ephemeral: no DB tables. The daemon writes ``presence.json``; the API reads it.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentlog.config import (
    PRESENCE_ACTIVE_SECONDS,
    presence_path_for_db,
)
from agentlog.ingest import claude as claude_adapter
from agentlog.ingest import codex as codex_adapter
from agentlog.ingest import cursor as cursor_adapter
from agentlog.safety.write_guard import assert_writable

log = logging.getLogger("agentlog.watch.presence")

PresenceState = str  # streaming | tool_running | waiting | unknown

_SKIP_NAMES = frozenset(
    {
        "skill-injections.jsonl",
        "journal.jsonl",
    }
)
_TAIL_BYTES = 16_384


def _utc_iso(ts: float | None = None) -> str:
    when = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return when.isoformat()


def is_presence_path(path: Path) -> bool:
    """Return True if ``path`` is a transcript we can track for presence."""
    name = path.name
    if name.endswith(("-journal", "-wal", "-shm")):
        return False
    if name in _SKIP_NAMES:
        return False
    return path.suffix == ".jsonl"


def external_id_for_path(harness: str, path: Path) -> str | None:
    """Derive adapter external_id from a source path. None if not trackable."""
    if not is_presence_path(path):
        return None
    try:
        if harness == "codex":
            return codex_adapter.external_id_from_path(path)
        if harness == "claude":
            return claude_adapter.external_id_from_path(path)
        if harness == "cursor":
            return cursor_adapter.external_id_from_path(path)
    except Exception:  # noqa: BLE001 - never break the daemon
        log.debug("external_id derivation failed for %s %s", harness, path, exc_info=True)
        return path.stem or None
    # Other harnesses: best-effort stem when we see a jsonl (rare).
    return path.stem or None


def session_id_for(harness: str, external_id: str) -> str:
    return f"{harness}:{external_id}"


def _classify_obj(obj: dict[str, Any]) -> PresenceState | None:
    """Map one transcript object to a presence state, or None to keep scanning."""
    role = obj.get("role")
    typ = obj.get("type")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

    # Cursor / Anthropic-style message envelopes
    if typ == "turn_ended":
        return "waiting"
    if role == "user" or typ in {"user", "user_message"}:
        return "waiting"
    if role == "assistant" or typ in {"assistant", "agent_message"}:
        content = None
        msg = obj.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
        if content is None:
            content = obj.get("content")
        if isinstance(content, list):
            kinds = {
                b.get("type")
                for b in content
                if isinstance(b, dict) and b.get("type")
            }
            if "tool_use" in kinds or "tool_call" in kinds:
                return "tool_running"
        return "streaming"

    # Codex rollout events
    if typ == "event_msg":
        ptype = payload.get("type")
        if ptype in {"user_message", "task_complete"}:
            return "waiting"
        if ptype in {"agent_message", "agent_reasoning"}:
            return "streaming"
        return None
    if typ == "response_item":
        ptype = payload.get("type")
        if ptype in {"function_call", "custom_tool_call"}:
            return "tool_running"
        if ptype in {"function_call_output", "custom_tool_call_output"}:
            return "streaming"
        if ptype == "message":
            if payload.get("role") == "user":
                return "waiting"
            return "streaming"
        return None

    # Claude Code: tool_use / tool_result at top level or in message
    if typ == "tool_use":
        return "tool_running"
    if typ in {"tool_result", "result"}:
        return "waiting"
    if typ == "last-prompt":
        return "waiting"

    return None


def _iter_tail_json_objects(path: Path, *, max_bytes: int = _TAIL_BYTES) -> list[dict[str, Any]]:
    """Return parsed JSON objects from the file tail (oldest → newest)."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size <= 0:
        return []
    try:
        with path.open("rb") as fh:
            start = max(0, size - max_bytes)
            fh.seek(start)
            raw = fh.read()
    except OSError:
        return []
    if start > 0:
        nl = raw.find(b"\n")
        if nl >= 0:
            raw = raw[nl + 1 :]
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def peek_transcript_state(path: Path) -> PresenceState:
    """Cheap, read-only state peek from a JSONL transcript tail."""
    try:
        objs = _iter_tail_json_objects(path)
    except Exception:  # noqa: BLE001
        return "unknown"
    for obj in reversed(objs):
        try:
            state = _classify_obj(obj)
        except Exception:  # noqa: BLE001
            continue
        if state is not None:
            return state
    return "unknown"


def peek_title_hint(path: Path) -> str | None:
    """Best-effort short title from a user message in the tail (pending ingest)."""
    try:
        objs = _iter_tail_json_objects(path)
    except Exception:  # noqa: BLE001
        return None
    for obj in objs:
        try:
            text = _user_text_from_obj(obj)
        except Exception:  # noqa: BLE001
            continue
        if text:
            return text[:120]
    return None


def _user_text_from_obj(obj: dict[str, Any]) -> str | None:
    role = obj.get("role")
    typ = obj.get("type")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    if role == "user" or typ == "user":
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else obj.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str) and t.strip():
                        parts.append(t.strip())
            if parts:
                return " ".join(parts)
    if typ == "event_msg" and payload.get("type") == "user_message":
        msg = payload.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return None


@dataclass
class PresenceEntry:
    harness: str
    external_id: str
    source_path: str
    last_activity_at: float
    state: PresenceState = "unknown"
    session_id: str | None = None
    title: str | None = None
    repo: str | None = None
    pending_ingest: bool = True

    @property
    def key(self) -> str:
        return session_id_for(self.harness, self.external_id)

    def age_seconds(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.last_activity_at)

    def to_dict(self, *, now: float | None = None) -> dict[str, Any]:
        clock = now if now is not None else time.time()
        return {
            "harness": self.harness,
            "external_id": self.external_id,
            "session_id": self.session_id,
            "source_path": self.source_path,
            "state": self.state,
            "last_activity_at": _utc_iso(self.last_activity_at),
            "age_seconds": round(self.age_seconds(clock), 3),
            "pending_ingest": self.pending_ingest,
            "title": self.title,
            "repo": self.repo,
        }


@dataclass
class PresenceMap:
    """Thread-safe map of recently-active sessions."""

    active_seconds: float = PRESENCE_ACTIVE_SECONDS
    state_path: Path | None = None
    db_path: Path | None = None
    clock: Callable[[], float] = field(default_factory=lambda: time.time)
    _entries: dict[str, PresenceEntry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _generation: int = 0

    def note_activity(self, harness: str, path: str | Path) -> PresenceEntry | None:
        """Update presence for a changed source path. Snappy (no ingest debounce)."""
        p = Path(path)
        external_id = external_id_for_path(harness, p)
        if external_id is None:
            return None
        now = self.clock()
        state = peek_transcript_state(p)
        title_hint = peek_title_hint(p)
        key = session_id_for(harness, external_id)
        meta = self._lookup_db(harness, external_id)
        with self._lock:
            prev = self._entries.get(key)
            entry = PresenceEntry(
                harness=harness,
                external_id=external_id,
                source_path=str(p),
                last_activity_at=now,
                state=state,
                session_id=meta["session_id"] if meta else None,
                title=(meta["title"] if meta and meta.get("title") else title_hint),
                repo=meta["repo"] if meta else None,
                pending_ingest=meta is None,
            )
            # Preserve a better title if peek fails but we had one.
            if entry.title is None and prev is not None:
                entry.title = prev.title
            self._entries[key] = entry
            self._generation += 1
            snapshot = entry
        self.write_state_file()
        return snapshot

    def expire(self, *, now: float | None = None) -> list[str]:
        """Drop entries past the active window. Returns removed keys."""
        clock = now if now is not None else self.clock()
        cutoff = clock - self.active_seconds
        removed: list[str] = []
        with self._lock:
            for key, entry in list(self._entries.items()):
                if entry.last_activity_at < cutoff:
                    del self._entries[key]
                    removed.append(key)
            if removed:
                self._generation += 1
        return removed

    def active(self, *, now: float | None = None) -> list[PresenceEntry]:
        clock = now if now is not None else self.clock()
        self.expire(now=clock)
        with self._lock:
            items = sorted(
                self._entries.values(),
                key=lambda e: e.last_activity_at,
                reverse=True,
            )
            return list(items)

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        clock = now if now is not None else self.clock()
        sessions = [e.to_dict(now=clock) for e in self.active(now=clock)]
        with self._lock:
            gen = self._generation
        return {
            "ts": _utc_iso(clock),
            "generation": gen,
            "active_seconds": self.active_seconds,
            "sessions": sessions,
        }

    def write_state_file(self, path: Path | None = None) -> Path:
        """Atomically replace the presence JSON state file."""
        dest = Path(path or self.state_path or presence_path_for_db(self.db_path))
        payload = self.snapshot()
        atomic_write_json(dest, payload)
        return dest

    def heartbeat(self) -> dict[str, Any]:
        """Expire idle sessions and rewrite the state file."""
        removed = self.expire()
        path = self.write_state_file()
        snap = self.snapshot()
        snap["removed"] = removed
        snap["path"] = str(path)
        return snap

    def _lookup_db(self, harness: str, external_id: str) -> dict[str, Any] | None:
        if self.db_path is None:
            return None
        sid = session_id_for(harness, external_id)
        try:
            uri = Path(self.db_path).resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    """
                    SELECT id, repo, cwd FROM sessions
                    WHERE id = ? OR (harness = ? AND external_id = ?)
                    LIMIT 1
                    """,
                    (sid, harness, external_id),
                ).fetchone()
                if row is None:
                    return None
                title = None
                trow = conn.execute(
                    """
                    SELECT text FROM messages
                    WHERE session_id = ? AND role = 'user'
                      AND COALESCE(is_tool_plumbing, 0) = 0
                    ORDER BY seq ASC
                    LIMIT 1
                    """,
                    (row["id"],),
                ).fetchone()
                if trow is not None and trow["text"]:
                    title = str(trow["text"]).strip()[:120] or None
                return {
                    "session_id": str(row["id"]),
                    "repo": row["repo"] or row["cwd"],
                    "title": title,
                }
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - presence must stay resilient
            log.debug("presence db lookup failed", exc_info=True)
            return None


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via temp file + os.replace (same-directory rename)."""
    path = assert_writable(path, purpose="presence state")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_presence_file(path: Path) -> dict[str, Any]:
    """Load presence.json; return empty snapshot on missing/corrupt file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {
            "ts": _utc_iso(),
            "generation": 0,
            "active_seconds": PRESENCE_ACTIVE_SECONDS,
            "sessions": [],
        }
    if not isinstance(data, dict):
        return {
            "ts": _utc_iso(),
            "generation": 0,
            "active_seconds": PRESENCE_ACTIVE_SECONDS,
            "sessions": [],
        }
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        data["sessions"] = []
    return data


def enrich_presence_sessions(
    db_path: Path,
    sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fill session_id/title/repo from DB when ingest has landed."""
    if not sessions:
        return sessions
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except Exception:  # noqa: BLE001
        return sessions
    try:
        out: list[dict[str, Any]] = []
        for raw in sessions:
            item = dict(raw)
            harness = str(item.get("harness") or "")
            external_id = str(item.get("external_id") or "")
            if not harness or not external_id:
                out.append(item)
                continue
            sid = item.get("session_id") or session_id_for(harness, external_id)
            row = conn.execute(
                """
                SELECT id, repo, cwd FROM sessions
                WHERE id = ? OR (harness = ? AND external_id = ?)
                LIMIT 1
                """,
                (sid, harness, external_id),
            ).fetchone()
            if row is None:
                item["pending_ingest"] = True
                item.setdefault("session_id", None)
                out.append(item)
                continue
            item["session_id"] = str(row["id"])
            item["pending_ingest"] = False
            if not item.get("repo"):
                item["repo"] = row["repo"] or row["cwd"]
            if not item.get("title"):
                trow = conn.execute(
                    """
                    SELECT text FROM messages
                    WHERE session_id = ? AND role = 'user'
                      AND COALESCE(is_tool_plumbing, 0) = 0
                    ORDER BY seq ASC
                    LIMIT 1
                    """,
                    (row["id"],),
                ).fetchone()
                if trow is not None and trow["text"]:
                    item["title"] = str(trow["text"]).strip()[:120] or None
            out.append(item)
        return out
    except Exception:  # noqa: BLE001
        return sessions
    finally:
        conn.close()
