from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class IngestEvent:
    id: int
    ts: str
    harness: str
    sessions_added: int
    sessions_updated: int
    messages_added: int

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "harness": self.harness,
            "sessions_added": self.sessions_added,
            "sessions_updated": self.sessions_updated,
            "messages_added": self.messages_added,
        }


def record_ingest_event(
    conn: sqlite3.Connection,
    *,
    harness: str,
    sessions_added: int,
    sessions_updated: int,
    messages_added: int,
    ts: str | None = None,
) -> IngestEvent:
    when = ts or datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO ingest_events
            (ts, harness, sessions_added, sessions_updated, messages_added)
        VALUES (?, ?, ?, ?, ?)
        """,
        (when, harness, sessions_added, sessions_updated, messages_added),
    )
    conn.commit()
    event_id = int(cur.lastrowid)
    return IngestEvent(
        id=event_id,
        ts=when,
        harness=harness,
        sessions_added=sessions_added,
        sessions_updated=sessions_updated,
        messages_added=messages_added,
    )


def list_ingest_events(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    after_id: int | None = None,
    limit: int = 100,
) -> list[IngestEvent]:
    clauses: list[str] = []
    params: list[object] = []
    if since is not None:
        clauses.append("ts > ?")
        params.append(since)
    if after_id is not None:
        clauses.append("id > ?")
        params.append(after_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT id, ts, harness, sessions_added, sessions_updated, messages_added
        FROM ingest_events
        {where}
        ORDER BY id ASC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [
        IngestEvent(
            id=int(r["id"]),
            ts=str(r["ts"]),
            harness=str(r["harness"]),
            sessions_added=int(r["sessions_added"]),
            sessions_updated=int(r["sessions_updated"]),
            messages_added=int(r["messages_added"]),
        )
        for r in rows
    ]
