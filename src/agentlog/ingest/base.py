from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import xxhash

from agentlog.normalize.models import Harness, NormalizedMessage, ParseResult

log = logging.getLogger("agentlog.ingest")

_SQLITE_SUFFIXES = frozenset({".sqlite", ".db", ".vscdb"})
_FILE_STABILITY_ATTEMPTS = 3
_SQLITE_FINGERPRINT_ATTEMPTS = 3


def hash_bytes(data: bytes) -> str:
    return xxhash.xxh64(data).hexdigest()


def is_sqlite_path(path: Path) -> bool:
    return path.suffix.lower() in _SQLITE_SUFFIXES


def sqlite_fingerprint(path: Path) -> str:
    """Hash a consistent logical image without reading SQLite sidecars raw."""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    last_error: sqlite3.Error | None = None
    for attempt in range(_SQLITE_FINGERPRINT_ATTEMPTS):
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=30)
            conn.execute("PRAGMA busy_timeout = 30000")
            image = conn.serialize(name="main")
            digest = xxhash.xxh64()
            digest.update(b"agentlog-sqlite-logical-v1\0")
            digest.update(image)
            return digest.hexdigest()
        except sqlite3.Error as exc:
            last_error = exc
            if attempt + 1 < _SQLITE_FINGERPRINT_ATTEMPTS:
                time.sleep(0)
        finally:
            if conn is not None:
                conn.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"could not fingerprint SQLite source: {path}")


def _sqlite_revision(path: Path) -> tuple[int, int]:
    """Return a bounded revision token while tolerating WAL checkpoint races."""
    for attempt in range(_FILE_STABILITY_ATTEMPTS):
        main = path.stat()
        wal = Path(f"{path}-wal")
        try:
            wal_st = wal.stat()
        except OSError:
            wal_st = None
        main_after = path.stat()
        try:
            wal_after = wal.stat()
        except OSError:
            wal_after = None
        if (
            main.st_size == main_after.st_size
            and main.st_mtime_ns == main_after.st_mtime_ns
            and (wal_st is None) == (wal_after is None)
            and (
                wal_st is None
                or (
                    wal_st.st_size == wal_after.st_size
                    and wal_st.st_mtime_ns == wal_after.st_mtime_ns
                )
            )
        ):
            if wal_after is None:
                return main_after.st_size, main_after.st_mtime_ns
            revision = (
                (main_after.st_mtime_ns * 31 + wal_after.st_mtime_ns) * 31
                + wal_after.st_size
            ) & ((1 << 63) - 1)
            return main_after.st_size, revision
        if attempt + 1 < _FILE_STABILITY_ATTEMPTS:
            time.sleep(0)
    raise OSError(f"SQLite source remained unstable: {path}")


def hash_prefix(path: Path, nbytes: int) -> str:
    for attempt in range(_FILE_STABILITY_ATTEMPTS):
        main_st = path.stat()
        if nbytes <= 0:
            main = b""
        else:
            with path.open("rb") as f:
                main = f.read(nbytes)
        if not is_sqlite_path(path):
            return hash_bytes(main)

        wal = Path(f"{path}-wal")
        try:
            wal_st = wal.stat()
        except OSError:
            wal_st = None
        main_after = path.stat()
        try:
            wal_after = wal.stat()
        except OSError:
            wal_after = None
        stable = (
            main_st.st_size == main_after.st_size
            and main_st.st_mtime_ns == main_after.st_mtime_ns
            and (wal_st is None) == (wal_after is None)
            and (
                wal_st is None
                or (
                    wal_st.st_size == wal_after.st_size
                    and wal_st.st_mtime_ns == wal_after.st_mtime_ns
                )
            )
        )
        if stable:
            # The WAL revision is metadata here; hashing its raw bytes makes a
            # checkpoint look like content growth and causes needless reparses.
            wal_token = (
                b""
                if wal_after is None
                else f"{wal_after.st_size}:{wal_after.st_mtime_ns}".encode()
            )
            return hash_bytes(main + b"\0agentlog-wal-revision\0" + wal_token)
        if attempt + 1 < _FILE_STABILITY_ATTEMPTS:
            time.sleep(0)
    raise OSError(f"SQLite source remained unstable: {path}")


def file_stat(path: Path) -> tuple[int, int]:
    if not is_sqlite_path(path):
        st = path.stat()
        return st.st_size, st.st_mtime_ns
    return _sqlite_revision(path)


@dataclass(frozen=True)
class SourceSnapshot:
    """A stable, adapter-defined view of a multi-file transcript source."""

    data: bytes
    revision: tuple[int, int]
    content_hash: str


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
    uses_composite_source: bool = False

    def checkpoint_revision(self, path: Path) -> tuple[int, int]:
        return file_stat(path)

    def checkpoint_fingerprint(self, path: Path, parsed_offset: int) -> str:
        return hash_prefix(path, parsed_offset)

    def canonical_artifact_path(self, path: Path) -> Path:
        return path

    def capture_source(self, path: Path) -> SourceSnapshot:
        """Capture a stable composite source. Only composite adapters override this."""
        raise NotImplementedError("adapter does not define a composite source")

    def composite_snapshot_matches(
        self,
        path: Path,
        *,
        revision: tuple[int, int],
        content_hash: str,
    ) -> bool:
        snapshot = self.capture_source(path)
        return (
            snapshot.revision == revision
            and snapshot.content_hash == content_hash
        )

    def parse_source_snapshot(
        self, path: Path, snapshot: SourceSnapshot
    ) -> list[ParseResult]:
        """Parse captured bytes without rereading its dependencies."""
        return self.parse_path(path, snapshot.data, start_offset=0)

    @abstractmethod
    def discover(self) -> list[Path]:
        raise NotImplementedError

    @abstractmethod
    def parse_chunk(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> ParseResult:
        raise NotImplementedError

    def accepts_watch_path(self, path: Path, source_root: Path) -> bool:
        """Whether a changed path matches this adapter's directory grammar."""
        del path, source_root
        return True

    def parse_path(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> list[ParseResult]:
        """Parse one artifact path into one or more sessions."""
        return [self.parse_chunk(path, data, start_offset=start_offset)]
