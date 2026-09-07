"""Permission checks use synthetic stores, never the owner's Agentlog data."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentlog.api.local_token import ensure_token_file, write_token_file
from agentlog.api.app import _startup_repair_model_identity
from agentlog.api.deps import get_write_conn, open_read_only
from agentlog.analysis.config_ledger import backup_agentlog_db
from agentlog.config import ensure_db_parent
from agentlog.db.schema import connect, init_db


@unittest.skipUnless(os.name == "posix", "POSIX filesystem permissions")
class PrivateStoragePermissionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def mode(self, path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    def permissive_umask(self):
        old = os.umask(0)
        self.addCleanup(os.umask, old)

    def test_default_directory_created_private_and_existing_mode_healed(self):
        self.permissive_umask()
        db = self.root / ".agentlog" / "agentlog.db"
        with mock.patch("agentlog.config.DEFAULT_DB_PATH", db):
            ensure_db_parent(db)
            self.assertEqual(self.mode(db.parent), 0o700)
            db.parent.chmod(0o755)
            ensure_db_parent(db)
            self.assertEqual(self.mode(db.parent), 0o700)

    def test_existing_custom_parent_and_ancestors_are_unchanged(self):
        self.root.chmod(0o755)
        parent = self.root / "shared"
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)
        ensure_db_parent(parent / "custom.db")
        self.assertEqual(self.mode(parent), 0o755)
        self.assertEqual(self.mode(self.root), 0o755)

    def test_default_directory_symlink_does_not_chmod_target(self):
        shared = self.root / "shared"
        shared.mkdir()
        shared.chmod(0o755)
        linked = self.root / ".agentlog"
        linked.symlink_to(shared, target_is_directory=True)
        db = linked / "agentlog.db"
        with mock.patch("agentlog.config.DEFAULT_DB_PATH", db):
            ensure_db_parent(db)
        self.assertTrue(linked.is_symlink())
        self.assertEqual(self.mode(shared), 0o755)

    def test_database_private_before_sqlite_opens_and_sidecars_stay_private(self):
        self.permissive_umask()
        db = self.root / "private.db"
        real_connect = sqlite3.connect

        def inspect_before_open(*args, **kwargs):
            self.assertEqual(self.mode(db), 0o600)
            return real_connect(*args, **kwargs)

        with mock.patch("agentlog.db.schema.sqlite3.connect", inspect_before_open):
            conn = connect(db)
        try:
            init_db(conn)
            for suffix in ("", "-wal", "-shm"):
                self.assertEqual(self.mode(Path(str(db) + suffix)), 0o600)
        finally:
            conn.close()
        # SQLite can recreate sidecars on later writes; their mode must remain
        # private without requiring another application-level chmod pass.
        conn = connect(db)
        try:
            conn.execute("CREATE TABLE permission_probe (value TEXT)")
            conn.commit()
            for suffix in ("-wal", "-shm"):
                self.assertEqual(self.mode(Path(str(db) + suffix)), 0o600)
        finally:
            conn.close()

    def test_connect_heals_default_directory_without_separate_setup(self):
        db = self.root / ".agentlog" / "agentlog.db"
        db.parent.mkdir(mode=0o755)
        db.parent.chmod(0o755)
        with mock.patch("agentlog.config.DEFAULT_DB_PATH", db):
            conn = connect(db)
            conn.close()
        self.assertEqual(self.mode(db.parent), 0o700)

    def test_api_writer_secures_storage_and_preserves_connection_settings(self):
        self.permissive_umask()
        db = self.root / "api.db"
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_path=db)))
        writer = get_write_conn(request)
        conn = next(writer)
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('api-record')")
        self.assertIs(conn.row_factory, sqlite3.Row)
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30_000)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        for suffix in ("", "-wal", "-shm"):
            self.assertEqual(self.mode(Path(str(db) + suffix)), 0o600)
        with self.assertRaises(StopIteration):
            next(writer)
        reader = open_read_only(db)
        try:
            self.assertEqual(reader.execute("SELECT value FROM sample").fetchone()[0], "api-record")
        finally:
            reader.close()

    def test_api_startup_heals_existing_database_permissions(self):
        db = self.root / "startup.db"
        conn = connect(db)
        init_db(conn)
        conn.close()
        db.chmod(0o644)
        _startup_repair_model_identity(db)
        self.assertEqual(self.mode(db), 0o600)

    def test_read_only_api_connection_does_not_heal_or_allow_writes(self):
        db = self.root / "readonly.db"
        sqlite3.connect(db).close()
        db.chmod(0o644)
        conn = open_read_only(db)
        try:
            self.assertEqual(self.mode(db), 0o644)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE refused (value TEXT)")
        finally:
            conn.close()

    def test_backup_of_permissive_database_is_private_and_preserves_data(self):
        self.permissive_umask()
        db = self.root / "legacy.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('backup-record')")
        conn.commit()
        conn.close()
        db.chmod(0o644)
        backup = backup_agentlog_db(db, reason="permissions")
        self.assertEqual(self.mode(backup), 0o600)
        self.assertEqual(self.mode(db), 0o644)
        restored = sqlite3.connect(backup)
        try:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(restored.execute("SELECT value FROM sample").fetchone()[0], "backup-record")
        finally:
            restored.close()

    def test_empty_and_missing_source_backups_stay_private(self):
        self.permissive_umask()
        for present in (False, True):
            with self.subTest(present=present):
                db = self.root / f"source-{present}.db"
                if present:
                    db.touch(mode=0o644)
                backup = backup_agentlog_db(db, reason=f"empty-{present}")
                self.assertEqual(self.mode(backup), 0o600)
                self.assertEqual(backup.stat().st_size, 0)

    def test_existing_database_and_live_sidecars_healed_without_data_loss(self):
        db = self.root / "existing.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE sample (value TEXT)")
            conn.execute("INSERT INTO sample VALUES ('synthetic-record')")
            conn.commit()
            for suffix in ("", "-wal", "-shm"):
                Path(str(db) + suffix).chmod(0o644)
            second = connect(db)
            try:
                self.assertEqual(
                    second.execute("SELECT value FROM sample").fetchone()[0],
                    "synthetic-record",
                )
                for suffix in ("", "-wal", "-shm"):
                    self.assertEqual(self.mode(Path(str(db) + suffix)), 0o600)
            finally:
                second.close()
        finally:
            conn.close()

    def test_database_symlink_preserved_and_actual_database_secured(self):
        db = self.root / "actual.db"
        original = sqlite3.connect(db)
        original.execute("CREATE TABLE sample (value TEXT)")
        original.commit()
        original.close()
        db.chmod(0o644)
        alias = self.root / "alias.db"
        alias.symlink_to(db)
        conn = connect(alias)
        try:
            conn.execute("SELECT * FROM sample").fetchall()
            self.assertTrue(alias.is_symlink())
            self.assertEqual(self.mode(db), 0o600)
        finally:
            conn.close()

    def test_sidecar_symlink_rejected_without_changing_target(self):
        db = self.root / "private.db"
        other = self.root / "unrelated"
        other.write_text("unchanged", encoding="utf-8")
        other.chmod(0o644)
        Path(str(db) + "-wal").symlink_to(other)
        with self.assertRaises(OSError):
            connect(db)
        self.assertEqual(other.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(self.mode(other), 0o644)

    def test_special_sqlite_database_names_do_not_create_files(self):
        for name in (":memory:", ""):
            with self.subTest(name=name):
                with mock.patch("agentlog.db.schema.os.open") as file_open:
                    conn = connect(name)
                    conn.close()
                file_open.assert_not_called()

    def test_token_private_before_publication_and_rotation_is_atomic(self):
        self.permissive_umask()
        target = self.root / "api_token"
        target.write_text("old-token\n", encoding="utf-8")
        target.chmod(0o600)
        real_replace = os.replace

        def inspect_before_publish(source, destination):
            self.assertEqual(Path(destination), target.resolve())
            self.assertEqual(self.mode(Path(source)), 0o600)
            self.assertEqual(Path(source).read_text(encoding="utf-8"), "new-token\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "old-token\n")
            real_replace(source, destination)

        with mock.patch("agentlog.api.local_token.os.replace", inspect_before_publish):
            self.assertEqual(write_token_file(target, " new-token "), target)
        self.assertEqual(target.read_text(encoding="utf-8"), "new-token\n")
        self.assertEqual(self.mode(target), 0o600)
        self.assertEqual(list(self.root.glob(".api-token-*")), [])

    def test_failed_token_publication_preserves_existing_and_removes_temporary(self):
        target = self.root / "api_token"
        write_token_file(target, "old-token")
        with mock.patch("agentlog.api.local_token.os.replace", side_effect=OSError("probe")):
            with self.assertRaises(OSError):
                write_token_file(target, "new-token")
        self.assertEqual(target.read_text(encoding="utf-8"), "old-token\n")
        self.assertEqual(list(self.root.glob(".api-token-*")), [])

    def test_token_custom_parent_unchanged_and_existing_mode_healed(self):
        self.root.chmod(0o755)
        target = self.root / "api_token"
        write_token_file(target, "same-token")
        target.chmod(0o644)
        token, _, created = ensure_token_file(target)
        self.assertEqual(token, "same-token")
        self.assertFalse(created)
        self.assertEqual(self.mode(target), 0o600)
        self.assertEqual(self.mode(self.root), 0o755)

    def test_token_symlink_preserved_through_healing_and_rotation(self):
        target = self.root / "actual_token"
        target.write_text("old-token\n", encoding="utf-8")
        target.chmod(0o644)
        alias = self.root / "api_token"
        alias.symlink_to(target)
        token, _, created = ensure_token_file(alias)
        self.assertEqual(token, "old-token")
        self.assertFalse(created)
        self.assertEqual(self.mode(target), 0o600)
        write_token_file(alias, "new-token")
        self.assertTrue(alias.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "new-token\n")
        self.assertEqual(self.mode(target), 0o600)


if __name__ == "__main__":
    unittest.main()
