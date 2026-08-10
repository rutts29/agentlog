"""Read-only audit: do stored messages match a full re-parse of each artifact?

Detects silent loss from checkpointing past a partially written JSONL tail (H2)
and from equal-length duplicate replacement (M3). Never writes to the database.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from pathlib import Path

from agentlog.ingest.pipeline import adapter_for

DEFAULT_DB = Path.home() / ".agentlog" / "agentlog.db"
JSONL_HARNESSES = ("codex", "claude", "cursor", "t3code")


def _open_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _stored_count(conn: sqlite3.Connection, session_id: str) -> int | None:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()
    exists = conn.execute(
        "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if exists is None:
        return None
    return int(row["c"])


def _candidate_ids(harness: str, external_id: str) -> list[str]:
    ids = [f"{harness}:{external_id}"]
    if harness == "cursor":
        from agentlog.ingest.cursor import canonical_external_id

        canon = canonical_external_id(external_id)
        if canon != external_id:
            ids.append(f"cursor:{canon}")
    return ids


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--harness", action="append", default=None)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = _open_ro(args.db)
    harnesses = args.harness or list(JSONL_HARNESSES)
    placeholders = ",".join("?" * len(harnesses))
    artifacts = conn.execute(
        f"SELECT id, harness, path, size, parsed_offset FROM artifacts "
        f"WHERE harness IN ({placeholders}) ORDER BY id",
        harnesses,
    ).fetchall()
    if args.limit:
        artifacts = artifacts[: args.limit]

    checked = 0
    missing_file = 0
    unmatched_session = 0
    short_by_harness: Counter[str] = Counter()
    lost_by_harness: Counter[str] = Counter()
    gaps: list[tuple[int, str, str, int, int]] = []

    for art in artifacts:
        harness = str(art["harness"])
        adapter = adapter_for(harness)
        if adapter is None or not adapter.supports_byte_append:
            continue
        path = Path(str(art["path"]))
        if not path.is_file():
            missing_file += 1
            continue
        try:
            data = path.read_bytes()
            results = adapter.parse_path(path, data, start_offset=0)
        except Exception as exc:  # noqa: BLE001 - audit must not abort
            print(f"parse error {path}: {exc}")
            continue
        checked += 1
        for result in results:
            if not result.messages:
                continue
            ext = result.session.external_id
            stored: int | None = None
            matched: str | None = None
            for sid in _candidate_ids(harness, ext):
                n = _stored_count(conn, sid)
                if n is not None:
                    stored, matched = n, sid
                    break
            if stored is None or matched is None:
                unmatched_session += 1
                continue
            on_disk = len(result.messages)
            if stored < on_disk:
                short_by_harness[harness] += 1
                lost_by_harness[harness] += on_disk - stored
                gaps.append((on_disk - stored, harness, matched, stored, on_disk))

    print(f"artifacts checked:        {checked}")
    print(f"files missing on disk:    {missing_file}")
    print(f"sessions not in database: {unmatched_session}")
    print(f"sessions short by harness: {dict(short_by_harness)}")
    print(f"messages missing by harness: {dict(lost_by_harness)}")
    for gap, harness, sid, stored, on_disk in sorted(gaps, reverse=True)[:25]:
        print(f"  -{gap:5d}  {harness:7s} {sid}  stored={stored} disk={on_disk}")
    conn.close()


if __name__ == "__main__":
    main()
