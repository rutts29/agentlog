"""Measure durable-label survival across a forced mass re-ingest.

Usage:
  durability_check.py counts
  durability_check.py seed <n>
  durability_check.py force-reparse
"""

from __future__ import annotations

import sys
from pathlib import Path

from agentlog.config import DEFAULT_DB_PATH
from agentlog.db.schema import connect


def counts(conn) -> dict[str, tuple[int, int, int]]:
    out: dict[str, tuple[int, int, int]] = {}
    for table in ("adjudications", "ux_observations"):
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c,
                   SUM(link_status = 'linked') AS linked,
                   SUM(link_status = 'orphaned') AS orphaned
            FROM {table}
            """
        ).fetchone()
        out[table] = (int(row["c"]), int(row["linked"] or 0), int(row["orphaned"] or 0))
    for table in ("exchange_windows", "sessions", "artifacts", "messages"):
        c = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        out[table] = (int(c), 0, 0)
    return out


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "counts"
    conn = connect(Path(DEFAULT_DB_PATH))
    if cmd == "counts":
        for k, v in counts(conn).items():
            print(f"{k}\ttotal={v[0]}\tlinked={v[1]}\torphaned={v[2]}")
    elif cmd == "seed":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
        cur = conn.execute(
            """
            INSERT INTO adjudications (
                window_id, adjudicated_at, turn_kind, user_stance, agent_stance,
                prior_outcome, notes, source, content_hash, link_status
            )
            SELECT w.id, datetime('now'), '["human_task"]', 'neutral', 'executing',
                   'abstain', 'seeded for durability check', 'ad_hoc',
                   w.content_hash, 'linked'
            FROM exchange_windows w
            WHERE w.id NOT IN (
                SELECT window_id FROM adjudications WHERE window_id IS NOT NULL
            )
            ORDER BY w.id LIMIT ?
            """,
            (n,),
        )
        conn.commit()
        print(f"seeded {cur.rowcount}")
    elif cmd == "force-reparse":
        cur = conn.execute(
            "UPDATE artifacts SET parser_version = 'force-reingest-durability'"
        )
        conn.commit()
        print(f"artifacts marked for reparse: {cur.rowcount}")
    else:
        print(__doc__)
        return 2
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
