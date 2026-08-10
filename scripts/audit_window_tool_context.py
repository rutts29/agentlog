"""Read-only audit: how many exchange windows had wrong tool context (H8).

Compares the legacy tool selection (message-seq range applied to tool seq) with
the linkage-based selection now used by the UX window-context loader, and
reports how many stored ux_observations were produced from a wrong tool view.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from pathlib import Path

from agentlog.analysis.extractors.window_context import _window_tools

DEFAULT_DB = Path.home() / ".agentlog" / "agentlog.db"


def _open_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _legacy_tools(
    conn: sqlite3.Connection, session_id: str, req_seq: int, end_seq: int
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT tool_name, action, success, seq
            FROM tool_events
            WHERE session_id = ? AND seq > ? AND seq < ?
            ORDER BY seq
            """,
            (session_id, req_seq, end_seq),
        )
    )


def _key(rows: list[sqlite3.Row]) -> list[tuple]:
    return [(r["tool_name"], r["action"], r["success"], r["seq"]) for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args()

    conn = _open_ro(args.db)
    windows = conn.execute(
        """
        SELECT ew.id, ew.session_id, ew.request_message_id, s.harness
        FROM exchange_windows ew
        JOIN sessions s ON s.id = ew.session_id
        """
    ).fetchall()

    labeled = {
        str(r["window_id"])
        for r in conn.execute("SELECT DISTINCT window_id FROM ux_observations")
    }

    changed: set[str] = set()
    was_blind: set[str] = set()
    by_harness: Counter[str] = Counter()
    blind_by_harness: Counter[str] = Counter()
    total_old = total_new = 0

    for win in windows:
        session_id = str(win["session_id"])
        req = conn.execute(
            "SELECT seq FROM messages WHERE id = ?", (win["request_message_id"],)
        ).fetchone()
        if req is None:
            continue
        req_seq = int(req["seq"])
        nxt = conn.execute(
            """
            SELECT seq FROM messages
            WHERE session_id = ? AND seq > ? AND role = 'user'
              AND COALESCE(is_tool_plumbing, 0) = 0
              AND COALESCE(authored_by_agent, 0) = 0
            ORDER BY seq LIMIT 1
            """,
            (session_id, req_seq),
        ).fetchone()
        end_seq = int(nxt["seq"]) if nxt is not None else 10**12

        old = _legacy_tools(conn, session_id, req_seq, end_seq)
        new = _window_tools(conn, session_id, req_seq=req_seq, end_seq=end_seq)
        total_old += len(old)
        total_new += len(new)
        if _key(old) != _key(new):
            wid = str(win["id"])
            changed.add(wid)
            by_harness[str(win["harness"])] += 1
            if not old and new:
                was_blind.add(wid)
                blind_by_harness[str(win["harness"])] += 1

    print(f"windows examined:              {len(windows)}")
    print(f"windows with wrong tool view:  {len(changed)}")
    print(f"  of which saw zero tools:     {len(was_blind)}")
    print(f"tool rows old/new:             {total_old} -> {total_new}")
    print(f"by harness (changed):          {dict(by_harness)}")
    print(f"by harness (was blind):        {dict(blind_by_harness)}")
    print(f"labeled windows (ux_observations): {len(labeled)}")
    print(f"  labeled AND wrong tool view: {len(labeled & changed)}")
    print(f"  labeled AND saw zero tools:  {len(labeled & was_blind)}")
    conn.close()


if __name__ == "__main__":
    main()
