from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agentlog.api.overview_cache import OverviewResponseCache


class OverviewResponseCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cache.sqlite"
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE values_table (value INTEGER NOT NULL)")
            conn.execute("INSERT INTO values_table VALUES (1)")
        self.cache = OverviewResponseCache(self.path, max_entries=2)

    def tearDown(self) -> None:
        self.cache.close()
        self.tmp.cleanup()

    def test_warm_hit_returns_independent_payload(self) -> None:
        calls = 0

        def build() -> dict:
            nonlocal calls
            calls += 1
            return {"items": [calls]}

        first = self.cache.get_or_compute(("7d", None, None), build)
        first["items"].append("caller mutation")
        second = self.cache.get_or_compute(("7d", None, None), build)

        self.assertEqual(calls, 1)
        self.assertEqual(second, {"items": [1]})

    def test_external_commit_invalidates_across_connections(self) -> None:
        calls = 0

        def build() -> dict:
            nonlocal calls
            calls += 1
            return {"value": calls}

        key = ("all", None, None)
        self.cache.get_or_compute(key, build)
        with sqlite3.connect(self.path) as writer:
            writer.execute("INSERT INTO values_table VALUES (2)")
        self.assertEqual(self.cache.get_or_compute(key, build), {"value": 2})
        self.assertEqual(calls, 2)

    def test_concurrent_cold_requests_single_flight(self) -> None:
        calls = 0
        calls_lock = threading.Lock()

        def build() -> dict:
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.05)
            return {"value": 42}

        results: list[dict] = []
        errors: list[BaseException] = []

        def request() -> None:
            try:
                results.append(self.cache.get_or_compute(("24h", None, None), build))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=request) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(calls, 1)
        self.assertEqual(results, [{"value": 42}] * 8)

    def test_eviction_is_bounded_and_old_key_recomputes(self) -> None:
        calls: dict[str, int] = {}

        def build(key: str) -> dict:
            calls[key] = calls.get(key, 0) + 1
            return {"key": key, "calls": calls[key]}

        for key in ("24h", "7d", "30d"):
            self.cache.get_or_compute((key, None, None), lambda key=key: build(key))

        self.assertEqual(self.cache.size, 2)
        self.cache.get_or_compute(("24h", None, None), lambda: build("24h"))
        self.assertEqual(calls["24h"], 2)
        self.assertEqual(self.cache.size, 2)

    def test_rolling_range_expires_without_database_commit(self) -> None:
        calls = 0
        cache = OverviewResponseCache(self.path, max_entries=2, ttl_seconds=0.5)

        def build() -> dict:
            nonlocal calls
            calls += 1
            return {"value": calls}

        with mock.patch("agentlog.api.overview_cache.monotonic", side_effect=[0.0, 1.0, 1.0]):
            key = ("24h", None, None)
            self.assertEqual(cache.get_or_compute(key, build), {"value": 1})
            self.assertEqual(cache.get_or_compute(key, build), {"value": 2})
        cache.close()
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
