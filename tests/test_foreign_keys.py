"""C1: foreign-key enforcement must survive fresh init and upgrades."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agentlog.db.migrations import MIGRATIONS, apply_migrations, current_version
from agentlog.db.migrations.fk import assert_foreign_keys_ok
from agentlog.db.schema import SCHEMA_SQL, connect, init_db, migrate_db


class ForeignKeyPragmaTests(unittest.TestCase):
    def test_fresh_init_leaves_foreign_keys_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "fresh.db")
            init_db(conn)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            assert_foreign_keys_ok(conn)
            conn.close()

    def test_upgrade_from_pre_v015_leaves_foreign_keys_on(self) -> None:
        """Reproduce the v015 trap: FK OFF mid-migration must not stick."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "upgrade.db"
            conn = connect(path)
            conn.executescript(SCHEMA_SQL)
            migrate_db(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            now = datetime.now(timezone.utc).isoformat()
            for version, fn in MIGRATIONS:
                if version >= 15:
                    break
                fn(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) "
                    "VALUES (?, ?)",
                    (version, now),
                )
            conn.commit()
            self.assertEqual(current_version(conn), 14)

            applied = apply_migrations(conn)
            self.assertIn(15, applied)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            # Intermediate: if someone applied only through v015's old body,
            # FK would stick at 0. The helper must restore it before return.
            assert_foreign_keys_ok(conn)
            conn.close()

    def test_cascade_delete_works_after_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "cascade.db")
            init_db(conn)
            conn.execute(
                """
                INSERT INTO artifacts
                (harness, path, size, mtime_ns, content_hash, parsed_offset,
                 parser_version)
                VALUES ('codex', '/tmp/a.jsonl', 1, 1, 'h', 0, '1')
                """
            )
            art = conn.execute("SELECT id FROM artifacts").fetchone()["id"]
            conn.execute(
                """
                INSERT INTO sessions
                (id, harness, external_id, artifact_id)
                VALUES ('codex:s', 'codex', 's', ?)
                """,
                (art,),
            )
            conn.execute(
                """
                INSERT INTO messages
                (id, session_id, seq, role, text, content_hash)
                VALUES ('m1', 'codex:s', 1, 'user', 'hi', 'h1')
                """
            )
            conn.commit()
            conn.execute("DELETE FROM sessions WHERE id = 'codex:s'")
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()
