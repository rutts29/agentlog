from __future__ import annotations

import threading
import time
from collections.abc import Callable


class Debouncer:
    """Fire after a quiet period, bounded by a maximum wait when configured."""

    def __init__(
        self,
        delay: float,
        on_fire: Callable[[str], None],
        *,
        max_wait: float | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if max_wait is not None and max_wait < 0:
            raise ValueError("max_wait must be non-negative")
        self.delay = delay
        self.max_wait = max_wait
        self._on_fire = on_fire
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._lock = threading.Lock()
        self._deadlines: dict[str, float] = {}
        self._first_seen: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def ping(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            first_seen = self._first_seen.setdefault(key, now)
            deadline = now + self.delay
            if self.max_wait is not None:
                deadline = min(deadline, first_seen + self.max_wait)
            self._deadlines[key] = deadline

    def pending(self) -> set[str]:
        with self._lock:
            return set(self._deadlines)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="agentlog-debouncer", daemon=True
        )
        self._thread.start()

    def stop(self, *, join_timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
            self._thread = None

    def drain_ready(self) -> list[str]:
        """Return keys whose quiet or maximum deadline elapsed and remove them."""
        now = self._clock()
        ready: list[str] = []
        with self._lock:
            for key, deadline in list(self._deadlines.items()):
                if now >= deadline:
                    ready.append(key)
                    del self._deadlines[key]
                    self._first_seen.pop(key, None)
        return ready

    def _loop(self) -> None:
        while not self._stop.is_set():
            for key in self.drain_ready():
                try:
                    self._on_fire(key)
                except Exception:  # noqa: BLE001 - never kill the loop
                    pass
            self._sleeper(0.05)
