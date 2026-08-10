from __future__ import annotations

import threading
import time
from collections.abc import Callable


class Debouncer:
    """Fire a callback once a key has been quiet for ``delay`` seconds."""

    def __init__(
        self,
        delay: float,
        on_fire: Callable[[str], None],
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.delay = delay
        self._on_fire = on_fire
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._lock = threading.Lock()
        self._deadlines: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def ping(self, key: str) -> None:
        with self._lock:
            self._deadlines[key] = self._clock() + self.delay

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
        """Return keys whose quiet period elapsed and remove them."""
        now = self._clock()
        ready: list[str] = []
        with self._lock:
            for key, deadline in list(self._deadlines.items()):
                if now >= deadline:
                    ready.append(key)
                    del self._deadlines[key]
        return ready

    def _loop(self) -> None:
        while not self._stop.is_set():
            for key in self.drain_ready():
                try:
                    self._on_fire(key)
                except Exception:  # noqa: BLE001 - never kill the loop
                    pass
            self._sleeper(0.05)
