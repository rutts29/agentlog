from __future__ import annotations

import hashlib
import sqlite3


WINDOW_ID_VERSION = "1"


def _normalize(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n")


def _content_hash(session_id: str, request_text: str | None, response_text: str | None) -> str:
    payload = "\n".join(
        [
            WINDOW_ID_VERSION,
            session_id,
            _normalize(request_text),
            _normalize(response_text),
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]


def _rebuild_exchange_windows(conn: sqlite3.Connection) -> dict[str, str]:
    """Rebuild windows with content_hash identity. Returns old_id -> new_id."""
    conn.execute(
        """
        CREATE TABLE exchange_windows_v012 (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            request_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            response_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            input_hash TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            UNIQUE (session_id, request_message_id, response_message_id)
        )
        """
    )
    old_to_new: dict[str, str] = {}
    rows = conn.execute(
        """
        SELECT w.id, w.session_id, w.request_message_id, w.response_message_id,
               w.input_hash, req.text AS req_text, resp.text AS resp_text
        FROM exchange_windows w
        LEFT JOIN messages req ON req.id = w.request_message_id
        LEFT JOIN messages resp ON resp.id = w.response_message_id
        """
    ).fetchall()
    for row in rows:
        old_id = str(row["id"])
        session_id = str(row["session_id"])
        ch = _content_hash(session_id, row["req_text"], row["resp_text"])
        old_to_new[old_id] = ch
        conn.execute(
            """
            INSERT INTO exchange_windows_v012 (
                id, session_id, request_message_id, response_message_id,
                input_hash, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                request_message_id = excluded.request_message_id,
                response_message_id = excluded.response_message_id,
                input_hash = excluded.input_hash,
                content_hash = excluded.content_hash
            """,
            (
                ch,
                session_id,
                row["request_message_id"],
                row["response_message_id"],
                row["input_hash"],
                ch,
            ),
        )
    conn.execute("DROP TABLE exchange_windows")
    conn.execute("ALTER TABLE exchange_windows_v012 RENAME TO exchange_windows")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_exchange_windows_session "
        "ON exchange_windows(session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_exchange_windows_content_hash "
        "ON exchange_windows(content_hash)"
    )
    return old_to_new


def _rebuild_soft_label_table(
    conn: sqlite3.Connection,
    *,
    table: str,
    create_sql: str,
    copy_columns: list[str],
    old_to_new: dict[str, str],
) -> None:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone():
        return
    conn.executescript(create_sql)
    new_table = f"{table}_v012"
    cols = ", ".join(copy_columns)
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    for row in rows:
        values = dict(row)
        old_wid = str(values.get("window_id") or "")
        new_wid = old_to_new.get(old_wid, old_wid)
        values["window_id"] = new_wid
        if not values.get("content_hash"):
            live = conn.execute(
                "SELECT content_hash FROM exchange_windows WHERE id = ?",
                (new_wid,),
            ).fetchone()
            values["content_hash"] = str(live["content_hash"]) if live else ""
        values.setdefault("link_status", "linked" if new_wid in {
            r["id"] for r in conn.execute("SELECT id FROM exchange_windows")
        } else "orphaned")
        values.setdefault("orphaned_at", None)
        placeholders = ", ".join("?" for _ in copy_columns)
        conn.execute(
            f"INSERT INTO {new_table} ({cols}) VALUES ({placeholders})",
            [values.get(c) for c in copy_columns],
        )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {new_table} RENAME TO {table}")


def apply(conn: sqlite3.Connection) -> None:
    from agentlog.db.migrations.fk import run_without_foreign_keys

    def _body() -> None:
        _apply_body(conn)

    run_without_foreign_keys(conn, _body)


def _apply_body(conn: sqlite3.Connection) -> None:
    old_to_new = _rebuild_exchange_windows(conn)

    # Cheap deterministic classifications keep CASCADE (regenerable).
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name = 'window_det_classifications'"
    ).fetchone():
        conn.execute(
            """
            CREATE TABLE window_det_classifications_v012 (
                id TEXT PRIMARY KEY,
                window_id TEXT NOT NULL REFERENCES exchange_windows(id) ON DELETE CASCADE,
                run_id TEXT NOT NULL REFERENCES derivation_runs(id) ON DELETE CASCADE,
                turn_kinds_json TEXT NOT NULL,
                request_kind TEXT NOT NULL,
                route TEXT NOT NULL,
                drop_rules_json TEXT NOT NULL DEFAULT '[]',
                features_json TEXT NOT NULL DEFAULT '{}',
                extractor_name TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                model TEXT,
                prompt_hash TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (window_id, run_id)
            )
            """
        )
        for src in conn.execute("SELECT * FROM window_det_classifications"):
            new_wid = old_to_new.get(str(src["window_id"]), str(src["window_id"]))
            if not conn.execute(
                "SELECT 1 FROM exchange_windows WHERE id = ?", (new_wid,)
            ).fetchone():
                continue
            conn.execute(
                """
                INSERT INTO window_det_classifications_v012 (
                    id, window_id, run_id, turn_kinds_json, request_kind, route,
                    drop_rules_json, features_json, extractor_name, extractor_version,
                    model, prompt_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    src["id"],
                    new_wid,
                    src["run_id"],
                    src["turn_kinds_json"],
                    src["request_kind"],
                    src["route"],
                    src["drop_rules_json"],
                    src["features_json"],
                    src["extractor_name"],
                    src["extractor_version"],
                    src["model"],
                    src["prompt_hash"],
                    src["created_at"],
                ),
            )
        conn.execute("DROP TABLE window_det_classifications")
        conn.execute(
            "ALTER TABLE window_det_classifications_v012 "
            "RENAME TO window_det_classifications"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_det_class_window "
            "ON window_det_classifications(window_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_det_class_route "
            "ON window_det_classifications(route)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_det_class_run "
            "ON window_det_classifications(run_id)"
        )

    ux_cols = [
        "id",
        "window_id",
        "run_id",
        "turn_kinds_json",
        "user_stance",
        "agent_stance",
        "prior_outcome",
        "flags_json",
        "spans_json",
        "confidence_json",
        "abstain_reasons_json",
        "novel_observations_json",
        "extractor_name",
        "extractor_version",
        "model",
        "prompt_hash",
        "batch_size",
        "raw_json",
        "created_at",
        "content_hash",
        "link_status",
        "orphaned_at",
    ]
    _rebuild_soft_label_table(
        conn,
        table="ux_observations",
        create_sql="""
        CREATE TABLE ux_observations_v012 (
            id TEXT PRIMARY KEY,
            window_id TEXT,
            run_id TEXT NOT NULL REFERENCES derivation_runs(id) ON DELETE CASCADE,
            turn_kinds_json TEXT NOT NULL,
            user_stance TEXT,
            agent_stance TEXT,
            prior_outcome TEXT,
            flags_json TEXT NOT NULL,
            spans_json TEXT NOT NULL,
            confidence_json TEXT NOT NULL,
            abstain_reasons_json TEXT NOT NULL,
            novel_observations_json TEXT NOT NULL,
            extractor_name TEXT NOT NULL,
            extractor_version TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            batch_size INTEGER NOT NULL DEFAULT 1,
            raw_json TEXT,
            created_at TEXT NOT NULL,
            content_hash TEXT NOT NULL DEFAULT '',
            link_status TEXT NOT NULL DEFAULT 'linked'
                CHECK (link_status IN ('linked', 'orphaned')),
            orphaned_at TEXT,
            UNIQUE (window_id, run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_ux_obs_window ON ux_observations_v012(window_id);
        CREATE INDEX IF NOT EXISTS idx_ux_obs_run ON ux_observations_v012(run_id);
        CREATE INDEX IF NOT EXISTS idx_ux_obs_content_hash
            ON ux_observations_v012(content_hash);
        CREATE INDEX IF NOT EXISTS idx_ux_obs_link_status
            ON ux_observations_v012(link_status);
        """,
        copy_columns=ux_cols,
        old_to_new=old_to_new,
    )

    for table in (
        "auto_review_observations",
        "worker_task_observations",
        "skill_compliance_observations",
    ):
        _rebuild_soft_label_table(
            conn,
            table=table,
            create_sql=f"""
            CREATE TABLE {table}_v012 (
                id TEXT PRIMARY KEY,
                window_id TEXT,
                run_id TEXT NOT NULL REFERENCES derivation_runs(id) ON DELETE CASCADE,
                payload_json TEXT NOT NULL,
                extractor_name TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                model TEXT,
                prompt_hash TEXT,
                created_at TEXT NOT NULL,
                content_hash TEXT NOT NULL DEFAULT '',
                link_status TEXT NOT NULL DEFAULT 'linked'
                    CHECK (link_status IN ('linked', 'orphaned')),
                orphaned_at TEXT,
                UNIQUE (window_id, run_id)
            );
            """,
            copy_columns=[
                "id",
                "window_id",
                "run_id",
                "payload_json",
                "extractor_name",
                "extractor_version",
                "model",
                "prompt_hash",
                "created_at",
                "content_hash",
                "link_status",
                "orphaned_at",
            ],
            old_to_new=old_to_new,
        )

    _rebuild_soft_label_table(
        conn,
        table="adjudications",
        create_sql="""
        CREATE TABLE adjudications_v012 (
            window_id TEXT PRIMARY KEY,
            adjudicated_at TEXT NOT NULL,
            turn_kind TEXT NOT NULL,
            user_stance TEXT,
            agent_stance TEXT,
            prior_outcome TEXT,
            notes TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL CHECK (source IN ('audit_pack', 'ad_hoc')),
            content_hash TEXT NOT NULL DEFAULT '',
            link_status TEXT NOT NULL DEFAULT 'linked'
                CHECK (link_status IN ('linked', 'orphaned')),
            orphaned_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_adjudications_at
            ON adjudications_v012(adjudicated_at);
        CREATE INDEX IF NOT EXISTS idx_adjudications_source
            ON adjudications_v012(source);
        CREATE INDEX IF NOT EXISTS idx_adjudications_content_hash
            ON adjudications_v012(content_hash);
        CREATE INDEX IF NOT EXISTS idx_adjudications_link_status
            ON adjudications_v012(link_status);
        """,
        copy_columns=[
            "window_id",
            "adjudicated_at",
            "turn_kind",
            "user_stance",
            "agent_stance",
            "prior_outcome",
            "notes",
            "source",
            "content_hash",
            "link_status",
            "orphaned_at",
        ],
        old_to_new=old_to_new,
    )
