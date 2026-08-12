from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
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
from agentlog.ingest.pipeline import ingest_harness
from agentlog.watch.debounce import Debouncer
from agentlog.watch.events import record_ingest_event
from agentlog.watch.presence import PresenceMap
from agentlog.watch.sources import WatchSource, existing_watch_roots

log = logging.getLogger("agentlog.watch")

_MAX_INGEST_RETRIES = 3
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

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        src = getattr(event, "src_path", "") or ""
        # Ignore SQLite journal noise for directory watches; poll handles DBs.
        name = Path(str(src)).name
        if name.endswith(("-journal", "-wal", "-shm")):
            return
        self._on_change(self.harness, str(src))


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

    def _schedule_ingest(self, harness: str) -> threading.Thread | None:
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
                args=(harness,),
                name=f"agentlog-ingest-{harness}",
                daemon=True,
            )
            self._ingest_threads[harness] = worker
            worker.start()
        _structured("ingest_scheduled", harness=harness)
        return worker

    def _ingest_worker(self, harness: str) -> None:
        try:
            while not self._stop.is_set():
                completed = self._run_ingest(harness)
                if completed:
                    self._schedule_derive(harness)
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

    def _run_ingest(self, harness: str) -> bool:
        changed = self._take_changed(harness)
        started = self._clock()
        conn: _WriteSerializedConnection | None = None
        try:
            ensure_db_parent(self.db_path)
            raw_conn = connect(self.db_path)
            conn = _WriteSerializedConnection(raw_conn, self._write_lock)
            conn.execute("PRAGMA busy_timeout = 30000")
            repo = Repository(conn)  # type: ignore[arg-type]
            stats = ingest_harness(repo, harness)
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
            return completed
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
            return False
        finally:
            if conn is not None:
                conn.close()

    def _schedule_derive(self, harness: str) -> threading.Thread | None:
        with self._derive_lock:
            if self._stop.is_set():
                return None
            self._derive_pending.add(harness)
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
                        self._derive_thread = None
                        return
                    if not self._derive_pending:
                        self._derive_thread = None
                        return
                    harnesses = tuple(sorted(self._derive_pending))
                    self._derive_pending.clear()
                if self._run_derive(harnesses) is not False:
                    self._derive_retry_count = 0
                    continue
                self._derive_retry_count += 1
                if self._derive_retry_count <= _MAX_INGEST_RETRIES:
                    with self._derive_lock:
                        self._derive_pending.update(harnesses)
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

    def _run_derive(self, harnesses: tuple[str, ...]) -> bool:
        from agentlog.analysis.derive import run_derive

        started = self._clock()
        harness = ",".join(harnesses)
        conn: _WriteSerializedConnection | None = None
        try:
            raw_conn = connect(self.db_path)
            conn = _WriteSerializedConnection(raw_conn, self._write_lock)
            result = run_derive(conn)  # type: ignore[arg-type]
            _structured(
                "derive_cycle",
                harness=harness,
                skipped=result.skipped,
                windows_total=result.windows_total,
                windows_classified=result.windows_classified,
                windows_updated=result.windows_updated,
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

    def _poll_once(self) -> None:
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
                    prev = self._poll_state.get(key)
                    self._poll_state[key] = snap
                    if prev is not None and prev != snap:
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
            if prev_c is not None and prev_c != composite:
                self._note_change(src.harness, str(path))

    def _poll_loop(self) -> None:
        # Seed snapshots so the first pass does not fake a change storm.
        self._poll_once()
        with self._changed_lock:
            self._changed_paths.clear()
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
