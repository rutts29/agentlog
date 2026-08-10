from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import xxhash

from agentlog.normalize.models import Harness, NormalizedMessage, ParseResult

log = logging.getLogger("agentlog.ingest")


def hash_bytes(data: bytes) -> str:
    return xxhash.xxh64(data).hexdigest()


def hash_prefix(path: Path, nbytes: int) -> str:
    if nbytes <= 0:
        return hash_bytes(b"")
    with path.open("rb") as f:
        return hash_bytes(f.read(nbytes))


def file_stat(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


_CURSOR_TS_RE = re.compile(
    r"^[A-Za-z]+, ([A-Za-z]+) (\d{1,2}), (\d{4}), "
    r"(\d{1,2}):(\d{2}) (AM|PM) \(UTC([+-]\d{1,2}):(\d{2})\)$"
)
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def parse_cursor_wrapper_ts(value: str) -> datetime | None:
    """Parse Cursor `<timestamp>` human strings like 'Thursday, Jul 23, 2026, 11:09 PM (UTC+5:30)'."""
    m = _CURSOR_TS_RE.match(value.strip())
    if not m:
        return None
    mon_s, day_s, year_s, hour_s, minute_s, ampm, tzh, tzm = m.groups()
    month = _MONTHS.get(mon_s)
    if month is None:
        return None
    hour = int(hour_s) % 12
    if ampm == "PM":
        hour += 12
    sign = 1 if tzh.startswith("+") else -1
    offset = timezone(
        sign * timedelta(hours=abs(int(tzh)), minutes=int(tzm))
    )
    return datetime(
        int(year_s), month, int(day_s), hour, int(minute_s), tzinfo=offset
    )


def parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Heuristic: ms vs s
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return parse_cursor_wrapper_ts(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if "text" in content and isinstance(content["text"], str):
            return content["text"]
        if "content" in content:
            return extract_text(content["content"])
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("type")
                if t in ("text", "input_text", "output_text") and isinstance(
                    item.get("text"), str
                ):
                    parts.append(item["text"])
                elif t == "thinking" and isinstance(item.get("thinking"), str):
                    parts.append(item["thinking"])
                elif isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return "\n".join(p for p in parts if p)
    return str(content)


_TOOL_PLUMBING_TYPES = frozenset(
    {"tool_result", "tool_use", "server_tool_use"}
)
_SUBSTANTIVE_TEXT_TYPES = frozenset({"text", "input_text", "output_text"})
_CHROME_TYPES = frozenset(
    {"tool_result", "tool_use", "server_tool_use", "thinking"}
)


def _substantive_text(content: list) -> str:
    """Visible text only — thinking is chrome, not conversational substance."""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in _SUBSTANTIVE_TEXT_TYPES and isinstance(
            item.get("text"), str
        ):
            parts.append(item["text"])
    return "\n".join(p for p in parts if p)


def content_is_tool_plumbing(content: Any) -> bool:
    """True when content has no substantive text — only tool/thinking chrome."""
    if not isinstance(content, list) or not content:
        return False
    if _substantive_text(content).strip():
        return False
    saw_chrome = False
    for item in content:
        if not isinstance(item, dict):
            return False
        btype = item.get("type")
        if btype in _CHROME_TYPES:
            saw_chrome = True
        elif btype in _SUBSTANTIVE_TEXT_TYPES:
            continue
        else:
            return False
    return saw_chrome


def content_hash_text(text: str) -> str:
    return hash_bytes(text.encode("utf-8", errors="replace"))


def flag_parent_authored_prompt(
    messages: list[NormalizedMessage],
    *,
    leading_users: bool = False,
) -> None:
    """Mark parent/harness-authored user turn(s) in a child/subagent session.

    Default: first non-plumbing user only. leading_users=True marks every
    user turn before the first assistant (Codex preamble + task brief).
    """
    for msg in messages:
        if msg.role == "assistant":
            return
        if msg.role == "user" and not msg.is_tool_plumbing:
            msg.authored_by_agent = True
            if not leading_users:
                return


def iter_jsonl_bytes(
    data: bytes, *, source: str
) -> Iterator[tuple[int, int, dict[str, Any] | None, str | None]]:
    """Yield (line_start, safe_offset, obj|None, error|None) over a byte slice.

    ``safe_offset`` is what a caller may checkpoint at. An unterminated trailing
    line that does not parse is an in-progress write, so its own start offset is
    reported and those bytes stay unconsumed until the writer finishes the line.
    """
    offset = 0
    size = len(data)
    while offset < size:
        nl = data.find(b"\n", offset)
        terminated = nl != -1
        if terminated:
            line_end = nl + 1
            line = data[offset:nl]
        else:
            line_end = size
            line = data[offset:line_end]
        if line.strip():
            obj: dict[str, Any] | None = None
            error: str | None = None
            try:
                parsed = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                error = f"{source}: {exc}"
            else:
                if isinstance(parsed, dict):
                    obj = parsed
                else:
                    error = f"{source}: non-object JSON"
            if error is not None and not terminated:
                yield offset, offset, None, f"{error} (incomplete trailing line)"
            else:
                yield offset, line_end, obj, error
        offset = line_end


class TranscriptAdapter(ABC):
    harness: Harness
    # Byte-offset append is for line-oriented JSONL. SQLite sources always reparse.
    supports_byte_append: bool = True

    @abstractmethod
    def discover(self) -> list[Path]:
        raise NotImplementedError

    @abstractmethod
    def parse_chunk(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> ParseResult:
        raise NotImplementedError

    def parse_path(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> list[ParseResult]:
        """Parse one artifact path into one or more sessions."""
        return [self.parse_chunk(path, data, start_offset=start_offset)]
