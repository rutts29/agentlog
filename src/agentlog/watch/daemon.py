from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from agentlog.config import (
    DEFAULT_DB_PATH,
    PRESENCE_ACTIVE_SECONDS,
    PRESENCE_HEARTBEAT_SECONDS,
    WATCH_DEBOUNCE_SECONDS,
    WATCH_MAX_WAIT_SECONDS,
    WATCH_POLL_SECONDS,
    ensure_db_parent,
    presence_path_for_db,
)
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.ingest.base import is_sqlite_path
from agentlog.ingest.pipeline import (
    _changed_artifact_paths,
    adapter_for,
    ingest_harness,
)
from agentlog.watch.debounce import Debouncer
from agentlog.watch.events import record_ingest_event
from agentlog.watch.presence import PresenceMap
from agentlog.watch.sources import WatchSource, existing_watch_roots

log = logging.getLogger("agentlog.watch")

_MAX_INGEST_RETRIES = 3
_SQLITE_SIDECARS = ("-journal", "-wal", "-shm")
_WRITE_STATEMENTS = {
    "ALTER",
    "BEGIN",
    "CREATE",
    "DELETE",
    "DROP",
    "INSERT",
    "REINDEX",
    "REPLACE",
    "SAVEPOINT",
    "UPDATE",
    "VACUUM",
    "WITH",
}


@dataclass(frozen=True)
class _IngestOutcome:
    completed: bool
    changed_window_ids: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return self.completed


def _structured(
    event: str,
    *,
    harness: str | None = None,
    **fields: object,
) -> None:
    payload: dict[str, object] = {"event": event}
    if harness is not None:
        payload["harness"] = harness
    payload.update(fields)
    log.info("%s", json.dumps(payload, separators=(",", ":"), default=str))


class _HarnessHandler(FileSystemEventHandler):
    def __init__(self, harness: str, on_change: Callable[[str, str], None]) -> None:
        super().__init__()
        self.harness = harness
        self._on_change = on_change

    def _note_path(self, path: str) -> None:
        if not path:
            return
        name = Path(path).name
        if name.endswith(("-journal", "-wal", "-shm")):
            return
        self._on_change(self.harness, path)

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if getattr(event, "event_type", "") == "moved":
            return
        src = getattr(event, "src_path", "") or ""
        self._note_path(str(src))

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._note_path(str(getattr(event, "src_path", "") or ""))
        self._note_path(str(getattr(event, "dest_path", "") or ""))


class _WriteSerializedConnection:
    """Keep parsing concurrent while serializing SQLite write transactions."""

    def __init__(self, conn: sqlite3.Connection, write_lock: threading.Lock) -> None:
        self._conn = conn
        self._write_lock = write_lock
        self._owns_write_lock = False

    def _acquire_for(self, sql: str) -> bool:
        keyword = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if keyword not in _WRITE_STATEMENTS or self._owns_write_lock:
            return False
        self._write_lock.acquire()
        self._owns_write_lock = True
        return True

    def _release_write_lock(self) -> None:
        if self._owns_write_lock:
            self._owns_write_lock = False
            self._write_lock.release()

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        self._acquire_for(sql)
        try:
            cursor = self._conn.execute(sql, parameters)  # type: ignore[arg-type]
        except BaseException:
            if self._owns_write_lock and not self._conn.in_transaction:
                self._release_write_lock()
            raise
        if self._owns_write_lock and not self._conn.in_transaction:
            self._release_write_lock()
        return cursor

    def executemany(self, sql: str, parameters: object) -> sqlite3.Cursor:
        self._acquire_for(sql)
        try:
            cursor = self._conn.executemany(sql, parameters)  # type: ignore[arg-type]
        except BaseException:
            if self._owns_write_lock and not self._conn.in_transaction:
                self._release_write_lock()
            raise
        if self._owns_write_lock and not self._conn.in_transaction:
            self._release_write_lock()
        return cursor

    def executescript(self, sql: str) -> sqlite3.Cursor:
        self._acquire_for("BEGIN")
        try:
            cursor = self._conn.executescript(sql)
        except BaseException:
            if not self._conn.in_transaction:
                self._release_write_lock()
            raise
        if not self._conn.in_transaction:
            self._release_write_lock()
        return cursor

    def commit(self) -> None:
        try:
            self._conn.commit()
        except BaseException:
            try:
                self._conn.rollback()
            finally:
                self._release_write_lock()
            raise
        self._release_write_lock()

    def rollback(self) -> None:
        try:
            self._conn.rollback()
        finally:
            self._release_write_lock()

    def close(self) -> None:
        try:
            self._conn.close()
        finally:
            self._release_write_lock()

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


class WatchDaemon:
    """Monitor transcript sources and run incremental ingest per harness."""

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        sources: list[WatchSource] | None = None,
        debounce_seconds: float = WATCH_DEBOUNCE_SECONDS,
        max_wait_seconds: float = WATCH_MAX_WAIT_SECONDS,
        poll_seconds: float = WATCH_POLL_SECONDS,
        use_watchdog: bool = True,
        clock: Callable[[], float] | None = None,
        presence_active_seconds: float = PRESENCE_ACTIVE_SECONDS,
        presence_heartbeat_seconds: float = PRESENCE_HEARTBEAT_SECONDS,
        presence: PresenceMap | None = None,
    ) -> None:
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.sources = sources if sources is not None else existing_watch_roots()
        self.debounce_seconds = debounce_seconds
        self.max_wait_seconds = max_wait_seconds
        self.poll_seconds = poll_seconds
        self.use_watchdog = use_watchdog
        self._clock = clock or time.monotonic
        self._stop = threading.Event()
        self._changed_paths: dict[str, set[str]] = {}
        self._retry_counts: dict[str, int] = {}
        self._changed_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._ingest_threads: dict[str, threading.Thread] = {}
        self._ingest_reschedule: set[str] = set()
        self._derive_lock = threading.Lock()
        self._derive_thread: threading.Thread | None = None
        self._derive_pending: set[str] = set()
        self._derive_pending_window_ids: dict[str, set[str] | None] = {}
        self._derive_retry_count = 0
        self._poll_state: dict[str, tuple[int, ...]] = {}
        self._observer: Observer | None = None
        self._poll_thread: threading.Thread | None = None
        self._presence_thread: threading.Thread | None = None
        self._catchup_thread: threading.Thread | None = None
        self._presence_heartbeat_seconds = presence_heartbeat_seconds
        self.presence = presence or PresenceMap(
            active_seconds=presence_active_seconds,
            state_path=presence_path_for_db(self.db_path),
            db_path=self.db_path,
        )
        self._debouncer = Debouncer(
            debounce_seconds,
            self._schedule_ingest,
            max_wait=max_wait_seconds,
            clock=self._clock,
        )

    def _note_change(self, harness: str, path: str) -> None:
        # Presence updates immediately; ingest still waits for debounce.
        try:
            entry = self.presence.note_activity(harness, path)
            if entry is not None:
                _structured(
                    "presence",
                    harness=harness,
                    path=path,
                    external_id=entry.external_id,
                    state=entry.state,
                    pending_ingest=entry.pending_ingest,
                )
        except Exception:  # noqa: BLE001 - never block ingest pipeline
            log.exception("presence update failed for %s", path)
        with self._changed_lock:
            self._changed_paths.setdefault(harness, set()).add(path)
            if self._retry_counts.get(harness, 0) > _MAX_INGEST_RETRIES:
                self._retry_counts.pop(harness, None)
        self._debouncer.ping(harness)
        _structured(
            "change",
            harness=harness,
            path=path,
            debounce_s=self.debounce_seconds,
            max_wait_s=self.max_wait_seconds,
        )

    def _take_changed(self, harness: str) -> list[str]:
        with self._changed_lock:
            paths = sorted(self._changed_paths.pop(harness, set()))
        return paths

    def _canonical_changed_paths(
        self, harness: str, paths: list[str]
    ) -> list[str]:
        """Keep filesystem events inside configured roots and normalize sidecars."""
        if not paths:
            return []
        sources = [
            src
            for src in self.sources
            if src.harness == harness
        ]
        adapter = adapter_for(harness)
        if adapter is None:
            return []
        roots = [
            src.path.expanduser().resolve()
            for src in sources
        ]

        def sidecar_base(path: Path) -> Path | None:
            raw = str(path)
            for suffix in _SQLITE_SIDECARS:
                if raw.endswith(suffix):
                    base = Path(raw[: -len(suffix)])
                    if is_sqlite_path(base):
                        return base
            return None

        configured_files = {
            root
            for root, src in zip(roots, sources)
            if src.path.expanduser().is_file() and is_sqlite_path(root)
        }
        cursor_metadata_controls = {
            root
            for root, src in zip(roots, sources)
            if (
                harness == "cursor"
                and src.path.expanduser().is_file()
                and root.name == "state.vscdb"
            )
        }
        accepted: set[str] = set()
        controls: set[str] = set()
        for raw_path in paths:
            path = Path(raw_path).expanduser().resolve()
            base = sidecar_base(path)
            if base is None and path in cursor_metadata_controls:
                if path.is_file():
                    controls.add(str(path))
                continue
            if (
                base is not None
                and harness != "cursor"
                and base in configured_files
            ):
                if base.is_file() and adapter.accepts_watch_path(
                    base, base.parent
                ):
                    accepted.add(str(base))
                continue
            if base is None and not path.is_file():
                continue
            for root, src in zip(roots, sources):
                source_is_file = src.path.expanduser().is_file()
                if source_is_file:
                    allowed = path == root
                else:
                    try:
                        path.relative_to(root)
                    except ValueError:
                        allowed = False
                    else:
                        allowed = True
                if not allowed:
                    continue
                candidate = base if base is not None else path
                grammar_root = root.parent if source_is_file else root
                if not adapter.accepts_watch_path(candidate, grammar_root):
                    continue
                if base is not None:
                    if not base.exists() or not base.is_file():
                        break
                    try:
                        base.relative_to(root)
                    except ValueError:
                        break
                    if not is_sqlite_path(base):
                        break
                    accepted.add(str(base))
                else:
                    accepted.add(str(path))
                break
        artifacts = {
            str(path)
            for path in _changed_artifact_paths(adapter, sorted(accepted))
        }
        return sorted(artifacts | controls)

    def _cursor_metadata_control_path(
        self, harness: str, paths: list[str]
    ) -> Path | None:
        if harness != "cursor":
            return None
        changed = {Path(path).expanduser().resolve() for path in paths}
        for src in self.sources:
            if src.harness != "cursor" or not src.path.expanduser().is_file():
                continue
            configured = src.path.expanduser().resolve()
            if configured.name == "state.vscdb" and configured in changed:
                return configured
        return None

    def _requeue_changed(
        self,
        harness: str,
        paths: list[str],
        *,
        retry_empty: bool = False,
    ) -> None:
        if not paths and not retry_empty:
            return
        with self._changed_lock:
            self._changed_paths.setdefault(harness, set()).update(paths)
            attempts = self._retry_counts.get(harness, 0) + 1
            self._retry_counts[harness] = attempts
        if attempts <= _MAX_INGEST_RETRIES:
            self._debouncer.ping(harness)

    def _clear_retry(self, harness: str) -> None:
        with self._changed_lock:
            self._retry_counts.pop(harness, None)

    def _schedule_ingest(
        self, harness: str, *, catch_up_attention_tails: bool = False
    ) -> threading.Thread | None:
        with self._worker_lock:
            if self._stop.is_set():
                return None
            current = self._ingest_threads.get(harness)
            if current is not None:
                self._ingest_reschedule.add(harness)
                _structured("ingest_coalesced", harness=harness)
                return current
            worker = threading.Thread(
                target=self._ingest_worker,
                args=(harness, catch_up_attention_tails),
                name=f"agentlog-ingest-{harness}",
                daemon=True,
            )
            self._ingest_threads[harness] = worker
            worker.start()
        _structured("ingest_scheduled", harness=harness)
        return worker

    def _ingest_worker(
        self, harness: str, catch_up_attention_tails: bool = False
    ) -> None:
        try:
            while not self._stop.is_set():
                outcome = (
                    self._run_ingest(harness, catch_up_attention_tails=True)
                    if catch_up_attention_tails
                    else self._run_ingest(harness)
                )
                if outcome:
                    changed_window_ids = getattr(outcome, "changed_window_ids", ())
                    if changed_window_ids:
                        self._schedule_derive(harness, changed_window_ids)
                with self._worker_lock:
                    if self._stop.is_set():
                        self._ingest_threads.pop(harness, None)
                        self._ingest_reschedule.discard(harness)
                        return
                    if harness in self._ingest_reschedule:
                        self._ingest_reschedule.discard(harness)
                    else:
                        self._ingest_threads.pop(harness, None)
                        return
        finally:
            self._finish_ingest_worker(harness, threading.current_thread())

    def _finish_ingest_worker(
        self, harness: str, worker: threading.Thread
    ) -> None:
        with self._worker_lock:
            if self._ingest_threads.get(harness) is worker:
                self._ingest_threads.pop(harness, None)
                self._ingest_reschedule.discard(harness)

    def _run_ingest(
        self, harness: str, *, catch_up_attention_tails: bool = False
    ) -> _IngestOutcome:
        raw_changed = self._take_changed(harness)
        changed = self._canonical_changed_paths(harness, raw_changed)
        if raw_changed and not changed:
            self._clear_retry(harness)
            _structured(
                "ingest_ignored",
                harness=harness,
                changed=raw_changed,
                reason="no accepted paths",
            )
            return _IngestOutcome(False)
        started = self._clock()
        conn: _WriteSerializedConnection | None = None
        try:
            ensure_db_parent(self.db_path)
            raw_conn = connect(self.db_path)
            conn = _WriteSerializedConnection(raw_conn, self._write_lock)
            conn.execute("PRAGMA busy_timeout = 30000")
            repo = Repository(conn)  # type: ignore[arg-type]
            if changed:
                metadata_state = self._cursor_metadata_control_path(harness, changed)
                ingest_paths = [
                    path
                    for path in changed
                    if metadata_state is None or path != str(metadata_state)
                ]
                ingest_kwargs = (
                    {"cursor_metadata_state_db": metadata_state}
                    if metadata_state is not None
                    else {}
                )
                stats = ingest_harness(
                    repo,
                    harness,
                    changed_paths=ingest_paths,
                    **ingest_kwargs,
                )
            else:
                # An empty scope is reserved for startup catch-up and explicit
                # retries; the normal event path always supplies exact files.
                stats = (
                    ingest_harness(
                        repo, harness, catch_up_attention_tails=True
                    )
                    if catch_up_attention_tails
                    else ingest_harness(repo, harness)
                )
            if stats.failed:
                self._requeue_changed(
                    harness, changed, retry_empty=not changed
                )
                completed = False
            else:
                self._clear_retry(harness)
                completed = True
            event_id = None
            if not stats.failed:
                event = record_ingest_event(
                    conn,  # type: ignore[arg-type]
                    harness=harness,
                    sessions_added=stats.sessions_added,
                    sessions_updated=stats.sessions_updated,
                    messages_added=stats.messages_added,
                )
                event_id = event.id
            duration_ms = int((self._clock() - started) * 1000)
            _structured(
                "ingest_cycle",
                harness=harness,
                changed=changed,
                skipped=stats.skipped,
                parsed=stats.parsed,
                appended=stats.appended,
                failed=stats.failed,
                sessions_added=stats.sessions_added,
                sessions_updated=stats.sessions_updated,
                messages_added=stats.messages_added,
                event_id=event_id,
                duration_ms=duration_ms,
            )
            return _IngestOutcome(
                completed,
                frozenset(stats.changed_window_ids) if completed else frozenset(),
            )
        except Exception as exc:  # noqa: BLE001 - keep daemon alive
            self._requeue_changed(
                harness, changed, retry_empty=not changed
            )
            duration_ms = int((self._clock() - started) * 1000)
            _structured(
                "ingest_error",
                harness=harness,
                changed=changed,
                error=str(exc),
                duration_ms=duration_ms,
            )
            log.exception("ingest cycle failed for %s", harness)
            return _IngestOutcome(False)
        finally:
            if conn is not None:
                conn.close()

    def _queue_derive(
        self, harness: str, window_ids: set[str] | frozenset[str] | None
    ) -> None:
        if harness not in self._derive_pending_window_ids:
            self._derive_pending_window_ids[harness] = (
                None if window_ids is None else set(window_ids)
            )
        elif self._derive_pending_window_ids[harness] is None or window_ids is None:
            self._derive_pending_window_ids[harness] = None
        else:
            self._derive_pending_window_ids[harness].update(window_ids)
        self._derive_pending.add(harness)

    def _schedule_derive(
        self,
        harness: str,
        window_ids: set[str] | frozenset[str] | None = None,
    ) -> threading.Thread | None:
        if window_ids is not None and not window_ids:
            return None
        with self._derive_lock:
            if self._stop.is_set():
                return None
            self._queue_derive(harness, window_ids)
            if self._derive_thread is not None:
                return self._derive_thread
            worker = threading.Thread(
                target=self._derive_worker,
                name="agentlog-derive",
                daemon=True,
            )
            self._derive_thread = worker
            worker.start()
            return worker

    def _derive_worker(self) -> None:
        try:
            while True:
                with self._derive_lock:
                    if self._stop.is_set():
                        self._derive_pending.clear()
                        self._derive_pending_window_ids.clear()
                        self._derive_thread = None
                        return
                    if not self._derive_pending:
                        self._derive_thread = None
                        return
                    harnesses = tuple(sorted(self._derive_pending))
                    pending_window_ids = {
                        harness: self._derive_pending_window_ids.pop(harness, None)
                        for harness in harnesses
                    }
                    self._derive_pending.clear()
                batch_window_ids: set[str] | None
                if any(ids is None for ids in pending_window_ids.values()):
                    batch_window_ids = None
                else:
                    batch_window_ids = set().union(
                        *(ids or set() for ids in pending_window_ids.values())
                    )
                if self._run_derive(
                    harnesses, window_ids=batch_window_ids
                ) is not False:
                    self._derive_retry_count = 0
                    continue
                self._derive_retry_count += 1
                if self._derive_retry_count <= _MAX_INGEST_RETRIES:
                    with self._derive_lock:
                        for harness in harnesses:
                            self._queue_derive(
                                harness, pending_window_ids[harness]
                            )
                else:
                    _structured(
                        "derive_retry_exhausted",
                        harness=",".join(harnesses),
                        attempts=self._derive_retry_count,
                    )
                    self._derive_retry_count = 0
        finally:
            with self._derive_lock:
                if self._derive_thread is threading.current_thread():
                    self._derive_thread = None
                if self._stop.is_set():
                    self._derive_pending.clear()
                    self._derive_pending_window_ids.clear()

    def _run_derive(
        self,
        harnesses: tuple[str, ...],
        *,
        window_ids: set[str] | None = None,
    ) -> bool:
        from agentlog.analysis.derive import run_derive

        started = self._clock()
        harness = ",".join(harnesses)
        conn: _WriteSerializedConnection | None = None
        try:
            raw_conn = connect(self.db_path)
            conn = _WriteSerializedConnection(raw_conn, self._write_lock)
            result = run_derive(  # type: ignore[arg-type]
                conn, window_ids=window_ids
            )
            _structured(
                "derive_cycle",
                harness=harness,
                skipped=result.skipped,
                windows_total=result.windows_total,
                windows_classified=result.windows_classified,
                windows_updated=result.windows_updated,
                window_ids=len(window_ids) if window_ids is not None else None,
                run_id=result.run_id,
                duration_ms=int((self._clock() - started) * 1000),
            )
            return True
        except Exception as exc:  # noqa: BLE001 - ingest already succeeded
            _structured(
                "derive_error",
                harness=harness,
                error=str(exc),
                duration_ms=int((self._clock() - started) * 1000),
            )
            log.exception("derive cycle failed after %s ingest", harness)
            return False
        finally:
            if conn is not None:
                conn.close()

    def _snapshot(self, path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return st.st_size, st.st_mtime_ns

    def _poll_once(self, *, emit_changes: bool = True) -> None:
        for src in self.sources:
            if not src.poll:
                continue
            path = src.path
            if path.is_dir():
                for child in path.rglob("*"):
                    if not child.is_file():
                        continue
                    if child.suffix not in {".db", ".sqlite"} and not child.name.endswith(
                        (".db", ".sqlite")
                    ):
                        continue
                    key = f"{src.harness}:{child}"
                    snap = self._snapshot(child)
                    if snap is None:
                        continue
                    wal_snap = self._snapshot(Path(f"{child}-wal"))
                    composite = (
                        snap[0],
                        snap[1],
                        wal_snap[0] if wal_snap else 0,
                        wal_snap[1] if wal_snap else 0,
                    )
                    prev = self._poll_state.get(key)
                    self._poll_state[key] = composite
                    if emit_changes and prev is not None and prev != composite:
                        self._note_change(src.harness, str(child))
                continue
            key = f"{src.harness}:{path}"
            snap = self._snapshot(path)
            if snap is None:
                continue
            # Also check WAL sidecar — FSEvents often misses sqlite page writes.
            wal = Path(str(path) + "-wal")
            wal_snap = self._snapshot(wal)
            composite = (
                snap[0],
                snap[1],
                wal_snap[0] if wal_snap else 0,
                wal_snap[1] if wal_snap else 0,
            )
            prev_c = self._poll_state.get(key)
            self._poll_state[key] = composite
            if emit_changes and prev_c is not None and prev_c != composite:
                self._note_change(src.harness, str(path))

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001
                log.exception("poll cycle failed")

    def _start_watchdog(self) -> None:
        observer = Observer()
        scheduled = 0
        for src in self.sources:
            path = src.path
            if not path.exists():
                continue
            watch_dir = path if path.is_dir() else path.parent
            if not watch_dir.is_dir():
                continue
            handler = _HarnessHandler(src.harness, self._note_change)
            observer.schedule(handler, str(watch_dir), recursive=True)
            scheduled += 1
            _structured(
                "watch_schedule",
                harness=src.harness,
                path=str(watch_dir),
                recursive=True,
                mode="watchdog",
                poll_backstop=src.poll,
            )
        if scheduled:
            observer.start()
            self._observer = observer
        else:
            _structured("watch_schedule", mode="watchdog", scheduled=0)

    def start(self) -> None:
        ensure_db_parent(self.db_path)
        conn = connect(self.db_path)
        try:
            init_db(conn)
        finally:
            conn.close()

        _structured(
            "watch_start",
            db=str(self.db_path),
            sources=[
                {"harness": s.harness, "path": str(s.path), "poll": s.poll}
                for s in self.sources
            ],
            debounce_s=self.debounce_seconds,
            max_wait_s=self.max_wait_seconds,
            poll_s=self.poll_seconds,
            use_watchdog=self.use_watchdog,
            presence_path=str(self.presence.state_path or presence_path_for_db(self.db_path)),
            presence_active_s=self.presence.active_seconds,
            presence_heartbeat_s=self._presence_heartbeat_seconds,
        )
        self._stop.clear()
        self._debouncer.start()
        self._presence_thread = threading.Thread(
            target=self._presence_loop,
            name="agentlog-watch-presence",
            daemon=True,
        )
        self._presence_thread.start()
        try:
            self.presence.write_state_file()
        except Exception:  # noqa: BLE001
            log.exception("initial presence write failed")
        if any(source.poll for source in self.sources):
            try:
                self._poll_once(emit_changes=False)
            except Exception:  # noqa: BLE001
                log.exception("initial poll snapshot failed")
        if self.use_watchdog:
            try:
                self._start_watchdog()
            except Exception:  # noqa: BLE001
                log.exception("watchdog failed to start; continuing with poll only")
                self._observer = None
        if any(s.poll for s in self.sources) or self._observer is None:
            self._poll_thread = threading.Thread(
                target=self._poll_loop, name="agentlog-watch-poll", daemon=True
            )
            self._poll_thread.start()
            _structured("watch_schedule", mode="poll", interval_s=self.poll_seconds)
        self._catchup_thread = threading.Thread(
            target=self._catchup_ingest,
            name="agentlog-watch-catchup",
            daemon=True,
        )
        self._catchup_thread.start()

    def _catchup_ingest(self) -> None:
        """Incremental ingest for every configured harness on startup.

        Covers gaps while the machine was off or the daemon was dead so sessions
        are not lost waiting for the next filesystem event. Runs on a worker
        thread so presence heartbeats and the watch loop stay responsive.
        """
        harnesses = list(dict.fromkeys(src.harness for src in self.sources))
        _structured("catchup_start", harnesses=harnesses)
        workers: list[threading.Thread] = []
        for harness in harnesses:
            if self._stop.is_set():
                break
            worker = self._schedule_ingest(harness)
            if worker is not None and worker not in workers:
                workers.append(worker)
        for worker in workers:
            worker.join()
        with self._derive_lock:
            derive_worker = self._derive_thread
        if derive_worker is not None:
            derive_worker.join()
        _structured("catchup_done", harnesses=harnesses)

    def _presence_loop(self) -> None:
        while not self._stop.wait(self._presence_heartbeat_seconds):
            try:
                snap = self.presence.heartbeat()
                _structured(
                    "presence_heartbeat",
                    active=len(snap.get("sessions") or []),
                    removed=len(snap.get("removed") or []),
                    path=snap.get("path"),
                )
            except Exception:  # noqa: BLE001
                log.exception("presence heartbeat failed")

    def request_stop(self) -> None:
        self._stop.set()

    def _join_background_jobs(self, *, timeout: float = 5.0) -> list[str]:
        deadline = time.monotonic() + timeout
        with self._worker_lock:
            workers = list(self._ingest_threads.values())
        with self._derive_lock:
            if self._derive_thread is not None:
                workers.append(self._derive_thread)
        for worker in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=remaining)
        alive = [worker.name for worker in workers if worker.is_alive()]
        if alive:
            _structured("watch_stop_pending", workers=alive)
        return alive

    def stop(self, *, worker_timeout: float = 5.0) -> None:
        self._stop.set()
        self._debouncer.stop()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None
        if self._presence_thread is not None:
            self._presence_thread.join(timeout=5)
            self._presence_thread = None
        if self._catchup_thread is not None:
            self._catchup_thread.join(timeout=5)
            if not self._catchup_thread.is_alive():
                self._catchup_thread = None
        alive = self._join_background_jobs(timeout=worker_timeout)
        if self._catchup_thread is not None and self._catchup_thread.is_alive():
            alive.append(self._catchup_thread.name)
        if alive:
            names = sorted(set(alive))
            _structured("watch_stop_incomplete", workers=names)
            raise RuntimeError(
                "watcher shutdown incomplete; background workers still active: "
                + ", ".join(names)
            )
        try:
            self.presence.heartbeat()
        except Exception:  # noqa: BLE001
            log.exception("final presence write failed")
        _structured("watch_stop")

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
