from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from agentlog.db.migrations.v002_extraction import apply as apply_v002
from agentlog.db.migrations.v003_experiments import apply as apply_v003
from agentlog.db.migrations.v004_token_usage import apply as apply_v004
from agentlog.db.migrations.v005_effort_source import apply as apply_v005
from agentlog.db.migrations.v006_ingest_events import apply as apply_v006
from agentlog.db.migrations.v007_skills import apply as apply_v007
from agentlog.db.migrations.v008_adjudications import apply as apply_v008
from agentlog.db.migrations.v009_session_commits import apply as apply_v009
from agentlog.db.migrations.v010_authored_by_agent import apply as apply_v010
from agentlog.db.migrations.v011_api_query_indexes import apply as apply_v011
from agentlog.db.migrations.v012_durable_labels import apply as apply_v012
from agentlog.db.migrations.v013_cursor_canonical_ids import apply as apply_v013
from agentlog.db.migrations.v014_model_identity import apply as apply_v014
from agentlog.db.migrations.v015_derive_watermarks import apply as apply_v015
from agentlog.db.migrations.v016_claims_proposals import apply as apply_v016
from agentlog.db.migrations.v017_proposal_decisions import apply as apply_v017
from agentlog.db.migrations.v018_skill_inventory import apply as apply_v018
from agentlog.db.migrations.v019_run_publication import apply as apply_v019
from agentlog.db.migrations.v020_proposal_superseded import apply as apply_v020
from agentlog.db.migrations.v021_config_ledger import apply as apply_v021
from agentlog.db.migrations.v022_proposal_llm_provenance import apply as apply_v022
from agentlog.db.migrations.v023_message_model_index import apply as apply_v023
from agentlog.db.migrations.v024_session_links import apply as apply_v024
from agentlog.db.migrations.v025_session_link_roles import apply as apply_v025
from agentlog.db.migrations.v026_tool_operation_kind import apply as apply_v026
from agentlog.db.migrations.v027_transcript_storage import apply as apply_v027
from agentlog.db.migrations.v028_session_source_identity import apply as apply_v028
from agentlog.db.migrations.v029_legacy_continuation import apply as apply_v029

# Version 1 = base SCHEMA_SQL in schema.py (implicit).
MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (2, apply_v002),
    (3, apply_v003),
    (4, apply_v004),
    (5, apply_v005),
    (6, apply_v006),
    (7, apply_v007),
    (8, apply_v008),
    (9, apply_v009),
    (10, apply_v010),
    (11, apply_v011),
    (12, apply_v012),
    (13, apply_v013),
    (14, apply_v014),
    (15, apply_v015),
    (16, apply_v016),
    (17, apply_v017),
    (18, apply_v018),
    (19, apply_v019),
    (20, apply_v020),
    (21, apply_v021),
    (22, apply_v022),
    (23, apply_v023),
    (24, apply_v024),
    (25, apply_v025),
    (26, apply_v026),
    (27, apply_v027),
    (28, apply_v028),
    (29, apply_v029),
]


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def current_version(conn: sqlite3.Connection) -> int:
    _ensure_migrations_table(conn)
    row = conn.execute("SELECT COALESCE(MAX(version), 1) AS v FROM schema_migrations").fetchone()
    if row is None:
        return 1
    # Fresh DB with empty migrations table still has base schema from SCHEMA_SQL.
    count = conn.execute("SELECT COUNT(*) AS c FROM schema_migrations").fetchone()
    if count is not None and int(count["c"]) == 0:
        return 1
    return int(row["v"])


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply pending numbered migrations. Returns versions applied."""
    from agentlog.db.migrations.fk import assert_foreign_keys_ok

    _ensure_migrations_table(conn)
    applied: list[int] = []
    have = {
        int(r["version"])
        for r in conn.execute("SELECT version FROM schema_migrations")
    }
    now = datetime.now(timezone.utc).isoformat()
    for version, fn in MIGRATIONS:
        if version in have:
            continue
        fn(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, now),
        )
        applied.append(version)
    # Rebuild migrations toggle FK mid-flight; refuse to hand back a connection
    # that still has enforcement off or that carries unresolved FK violations.
    assert_foreign_keys_ok(conn)
    return applied
