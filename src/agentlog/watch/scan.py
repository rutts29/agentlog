"""Filesystem scan for live agent presence.

The watch daemon only knows about sessions it received an inotify/FSEvents
callback for, and only for as long as they keep writing. This module derives
presence straight from transcript mtimes so ``/api/live`` stays authoritative
even when the daemon is restarting, an event was coalesced, or a worker has
been silent inside a long tool call.

Scan roots come from the harness registry, so a newly registered harness is
picked up without touching this file.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentlog.config import (
    HOME,
    PRESENCE_SCAN_CACHE_SECONDS,
    PRESENCE_SCAN_WINDOW_SECONDS,
)
from agentlog.registry.harnesses import list_harnesses

_TAIL_BYTES = 65_536
_MAX_PEEK_FILES = 60
_SKIP_NAMES = frozenset({"skill-injections.jsonl", "journal.jsonl"})

_STEP_TOOLS = ("UpdateCurrentStep", "TodoWrite")
_BOILERPLATE_LEAD = re.compile(
    r"^(?:you are working in|you are operating in|repo|repository|working directory|"
    r"context|project)\b[^.\n]*[.\n]?\s*",
    re.IGNORECASE,
)
_ROLE_LEAD = re.compile(r"^you are (?:an?\s+)?", re.IGNORECASE)
# Sentences that only describe the environment, never the job.
_SETUP_NOISE = re.compile(
    r"(?:/Users/|\bvenv\b|\bpython\b|\brun from\b|\bDB at\b|\bdatabase at\b|"
    r"\bport\b|\.db\b|\.venv|\brepo root\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScanRoot:
    harness: str
    root: Path
    suffix: str


@dataclass
class Peek:
    """What the tail of a transcript says the agent is doing."""

    state: str = "unknown"
    mid_turn: bool = False
    step: str | None = None
    tool: str | None = None
    brief: str | None = None


def scan_roots() -> list[ScanRoot]:
    """JSONL transcript roots declared by active harnesses in the registry."""
    out: list[ScanRoot] = []
    seen: set[tuple[str, str]] = set()
    for record in list_harnesses(ingest_status="active"):
        harness = str(record.get("id") or "")
        for location in record.get("transcript_locations") or []:
            text = str(location)
            if not text.endswith(".jsonl"):
                continue
            expanded = Path(text.replace("~", str(HOME), 1)) if text.startswith("~") else Path(text)
            parts = expanded.parts
            cut = next(
                (i for i, part in enumerate(parts) if "*" in part or "?" in part),
                len(parts),
            )
            root = Path(*parts[:cut]) if cut else Path("/")
            key = (harness, str(root))
            if key in seen:
                continue
            seen.add(key)
            out.append(ScanRoot(harness=harness, root=root, suffix=".jsonl"))
    return out


def _is_trackable(name: str) -> bool:
    if name in _SKIP_NAMES:
        return False
    return name.endswith(".jsonl") and not name.startswith(".")


def scan_recent(
    *,
    window_seconds: float = PRESENCE_SCAN_WINDOW_SECONDS,
    now: float | None = None,
    roots: list[ScanRoot] | None = None,
) -> list[tuple[str, Path, float, int]]:
    """Return (harness, path, mtime, size) for transcripts touched recently."""
    clock = now if now is not None else time.time()
    cutoff = clock - window_seconds
    found: list[tuple[str, Path, float, int]] = []
    for src in roots if roots is not None else scan_roots():
        if not src.root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(src.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if not _is_trackable(name):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_mtime < cutoff or st.st_size <= 0:
                    continue
                found.append((src.harness, Path(full), st.st_mtime, st.st_size))
    found.sort(key=lambda row: row[2], reverse=True)
    return found


def _tail_objects(path: Path, size: int) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as fh:
            start = max(0, size - _TAIL_BYTES)
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


def _blocks(obj: dict[str, Any]) -> list[dict[str, Any]]:
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else obj.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _text_of(obj: dict[str, Any]) -> str:
    parts = [
        str(b.get("text") or "")
        for b in _blocks(obj)
        if b.get("type") == "text" and b.get("text")
    ]
    if parts:
        return " ".join(parts)
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else obj.get("content")
    return content if isinstance(content, str) else ""


def _codex_peek(objs: list[dict[str, Any]]) -> Peek | None:
    peek = Peek()
    saw_codex = False
    for obj in reversed(objs):
        typ = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if typ not in {"event_msg", "response_item"}:
            continue
        saw_codex = True
        ptype = payload.get("type")
        if typ == "event_msg":
            if ptype == "task_complete":
                peek.state = "waiting"
                break
            if ptype == "user_message":
                peek.state = "thinking"
                peek.mid_turn = True
                break
            if ptype in {"agent_message", "agent_reasoning"}:
                peek.state = "streaming"
                peek.mid_turn = True
                break
        if typ == "response_item":
            if ptype in {"function_call", "custom_tool_call"}:
                peek.state = "tool_running"
                peek.mid_turn = True
                peek.tool = payload.get("name") if isinstance(payload.get("name"), str) else None
                break
            if ptype in {"function_call_output", "custom_tool_call_output"}:
                peek.state = "streaming"
                peek.mid_turn = True
                break
            if ptype == "message":
                peek.state = "thinking" if payload.get("role") == "user" else "streaming"
                peek.mid_turn = True
                break
    if not saw_codex:
        return None
    for obj in objs:
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                peek.brief = message.strip()
                break
    return peek


def peek_transcript(path: Path, size: int) -> Peek:
    """Read the tail once and derive state plus label material."""
    objs = _tail_objects(path, size)
    if not objs:
        return Peek()
    codex = _codex_peek(objs)
    if codex is not None:
        return codex

    peek = Peek()
    pending_tool = False
    for obj in reversed(objs):
        role = obj.get("role")
        typ = obj.get("type")
        blocks = _blocks(obj)
        kinds = {b.get("type") for b in blocks}
        if peek.step is None or peek.tool is None:
            for block in reversed(blocks):
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                if peek.tool is None and isinstance(name, str):
                    peek.tool = name
                if name in _STEP_TOOLS and peek.step is None:
                    inputs = block.get("input")
                    if isinstance(inputs, dict):
                        step = inputs.get("current_step") or inputs.get("completed_subtitle")
                        if isinstance(step, str) and step.strip():
                            peek.step = step.strip()
        if peek.state != "unknown":
            continue
        if typ == "turn_ended":
            peek.state = "waiting"
        elif "tool_result" in kinds:
            # Result arrived; the model is composing the next step.
            peek.state = "streaming"
            peek.mid_turn = True
        elif role == "assistant" or typ in {"assistant", "agent_message"}:
            if "tool_use" in kinds or "tool_call" in kinds:
                peek.state = "tool_running"
                pending_tool = True
            else:
                peek.state = "streaming"
            peek.mid_turn = True
        elif role == "user" or typ in {"user", "user_message"}:
            # No terminator after the prompt: the agent has the work and has not
            # answered yet. Nobody is waiting on the human here.
            peek.state = "thinking"
            peek.mid_turn = True
        elif typ == "tool_use":
            peek.state = "tool_running"
            peek.mid_turn = True
            pending_tool = True
        elif typ in {"tool_result", "result"}:
            peek.state = "waiting"

    if pending_tool:
        peek.mid_turn = True
    for obj in objs:
        if obj.get("role") == "user" or obj.get("type") in {"user", "user_message"}:
            text = _text_of(obj)
            if text.strip():
                peek.brief = text.strip()
                break
    return peek


@dataclass
class _PeekCache:
    """Memoise tail peeks by (path, mtime, size) — unchanged files stay free."""

    _entries: dict[str, tuple[float, int, Peek]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, path: Path, mtime: float, size: int) -> Peek:
        key = str(path)
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None and hit[0] == mtime and hit[1] == size:
                return hit[2]
        peek = peek_transcript(path, size)
        with self._lock:
            if len(self._entries) > 512:
                self._entries.clear()
            self._entries[key] = (mtime, size, peek)
        return peek

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


PEEK_CACHE = _PeekCache()


@dataclass
class _ScanCache:
    ttl: float = PRESENCE_SCAN_CACHE_SECONDS
    _at: float = 0.0
    _rows: list[tuple[str, Path, float, int]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def rows(self, *, now: float, window_seconds: float) -> list[tuple[str, Path, float, int]]:
        with self._lock:
            if self._rows and now - self._at < self.ttl:
                return self._rows
        rows = scan_recent(window_seconds=window_seconds, now=now)
        with self._lock:
            self._at = now
            self._rows = rows
        return rows

    def clear(self) -> None:
        with self._lock:
            self._at = 0.0
            self._rows = []


SCAN_CACHE = _ScanCache()


_CONTAINER_SEGMENTS = 6


def _resolve_project_dir(slug: str) -> Path | None:
    """Greedily rebuild a real directory from a hyphen-flattened path slug."""
    if not slug.startswith("Users-"):
        return None
    tokens = slug.split("-")[1:]
    current = Path("/Users")
    index = 0
    guard = 0
    while index < len(tokens) and guard < 32:
        guard += 1
        matched = 0
        for take in range(min(_CONTAINER_SEGMENTS, len(tokens) - index), 0, -1):
            chunk = tokens[index : index + take]
            for joiner in ("-", "_"):
                candidate = current / joiner.join(chunk)
                if candidate.is_dir():
                    current = candidate
                    matched = take
                    break
            if matched:
                break
        if not matched:
            return None
        index += matched
    return current if index == len(tokens) else None


_PROJECT_CACHE: dict[str, str] = {}
_PROJECT_LOCK = threading.Lock()


def project_label(slug: str | None) -> str | None:
    """Human project name for a repo slug or path."""
    if not slug:
        return None
    text = str(slug)
    if text.startswith("/"):
        return Path(text).name or text
    with _PROJECT_LOCK:
        hit = _PROJECT_CACHE.get(text)
    if hit is not None:
        return hit
    resolved = _resolve_project_dir(text)
    label = resolved.name if resolved is not None else text.rsplit("-", 1)[-1]
    with _PROJECT_LOCK:
        if len(_PROJECT_CACHE) > 256:
            _PROJECT_CACHE.clear()
        _PROJECT_CACHE[text] = label
    return label


def _clean_prompt(brief: str | None) -> str:
    if not brief:
        return ""
    text = re.sub(r"<timestamp>[\s\S]*?</timestamp>", " ", brief)
    query = re.search(r"<user_query>([\s\S]*?)</user_query>", text)
    if query:
        text = query.group(1)
    text = re.sub(r"<[^>\n]{0,80}>", " ", text)
    text = re.sub(r"[`*#>]", " ", text)
    # Leading links are context, not the ask — drop them so the rail shows work.
    text = re.sub(r"^(?:https?://\S+\s*)+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def prompt_label(brief: str | None) -> str | None:
    """First readable stretch of a prompt, with transcript markup stripped."""
    text = _clean_prompt(brief)
    if not text or _looks_like_clock(text):
        return None
    return text[:80].strip() or None


def task_label(brief: str | None) -> str | None:
    """Short task phrase from a subagent brief, minus prompt boilerplate."""
    text = _clean_prompt(brief)
    if not text:
        return None
    for _ in range(3):
        stripped = _BOILERPLATE_LEAD.sub("", text, count=1).strip()
        if stripped == text:
            break
        text = stripped
    text = _ROLE_LEAD.sub("", text, count=1).strip()
    if not text:
        return None
    parts = [
        part.strip().rstrip(".,;:")
        for part in re.split(r"(?<=[.!?])\s+|\s+—\s+|\n", text)[:6]
    ]
    sentence = next(
        (p for p in parts if len(p) >= 12 and not _SETUP_NOISE.search(p)),
        "",
    )
    if not sentence:
        sentence = next((p for p in parts if len(p) >= 8), text[:80].strip())
    if not sentence or _looks_like_clock(sentence):
        return None
    return sentence[:80] or None


def _looks_like_clock(text: str) -> bool:
    return bool(
        re.match(
            r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{4}-\d{2}-\d{2})",
            text,
            re.IGNORECASE,
        )
    )
