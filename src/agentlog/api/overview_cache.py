"""Small, revision-aware cache for aggregate Overview responses."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from threading import Event, RLock
from time import monotonic
from typing import Any, Callable, Hashable


@dataclass
class _Entry:
    revision: tuple[Any, ...]
    payload: dict[str, Any]
    created_at: float


@dataclass
class _Flight:
    event: Event
    payload: dict[str, Any] | None = None
    revision: tuple[Any, ...] | None = None
    created_at: float | None = None
    error: BaseException | None = None


class OverviewResponseCache:
    """Bounded app-lifetime cache with single-flight misses.

    ``PRAGMA data_version`` is read from one long-lived read-only connection:
    unlike a fresh per-request connection, that connection observes commits
    made by other processes. File/WAL metadata is included to detect a replaced
    database even when a new file reuses the same SQLite data-version baseline.
    """

    def __init__(
        self, db_path: Path | str, *, max_entries: int = 6, ttl_seconds: float = 5.0
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._db_path = Path(db_path)
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[Hashable, _Entry] = OrderedDict()
        self._inflight: dict[Hashable, _Flight] = {}
        self._lock = RLock()
        self._revision_conn: sqlite3.Connection | None = None

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def close(self) -> None:
        with self._lock:
            conn, self._revision_conn = self._revision_conn, None
            self._entries.clear()
        if conn is not None:
            conn.close()

    def _revision(self) -> tuple[Any, ...]:
        with self._lock:
            conn = self._revision_conn
            if conn is None:
                uri = f"file:{self._db_path}?mode=ro"
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
                conn.execute("PRAGMA busy_timeout = 30000")
                self._revision_conn = conn
            data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])

        def stat(path: Path) -> tuple[int, int, int] | None:
            try:
                value = path.stat()
            except FileNotFoundError:
                return None
            return (value.st_ino, value.st_size, value.st_mtime_ns)

        return (data_version, stat(self._db_path), stat(Path(f"{self._db_path}-wal")))

    def get_or_compute(
        self, key: Hashable, builder: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        """Return an isolated payload, computing one miss per key at a time."""
        while True:
            revision = self._revision()
            with self._lock:
                entry = self._entries.get(key)
                if (
                    entry is not None
                    and entry.revision == revision
                    and monotonic() - entry.created_at < self._ttl_seconds
                ):
                    self._entries.move_to_end(key)
                    return deepcopy(entry.payload)
                if entry is not None:
                    self._entries.pop(key, None)
                flight = self._inflight.get(key)
                if flight is None:
                    flight = _Flight(event=Event())
                    self._inflight[key] = flight
                    owner = True
                else:
                    owner = False

            if owner:
                try:
                    payload = builder()
                    end_revision = self._revision()
                    with self._lock:
                        flight.payload = deepcopy(payload)
                        flight.revision = end_revision if end_revision == revision else None
                        flight.created_at = monotonic()
                        if flight.revision is not None:
                            self._entries[key] = _Entry(
                                end_revision, deepcopy(payload), flight.created_at
                            )
                            self._entries.move_to_end(key)
                            while len(self._entries) > self._max_entries:
                                self._entries.popitem(last=False)
                        self._inflight.pop(key, None)
                        flight.event.set()
                    return deepcopy(payload)
                except BaseException as exc:
                    with self._lock:
                        flight.error = exc
                        self._inflight.pop(key, None)
                        flight.event.set()
                    raise

            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            with self._lock:
                current_revision = self._revision()
                if (
                    flight.payload is not None
                    and flight.revision is not None
                    and current_revision == flight.revision
                    and flight.created_at is not None
                    and monotonic() - flight.created_at < self._ttl_seconds
                ):
                    return deepcopy(flight.payload)
