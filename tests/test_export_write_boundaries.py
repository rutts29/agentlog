"""Owner exports and daemon logs must obey the advisory-only write boundary."""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentlog.analysis.owner_notes import (
    OwnerInsightBatch,
    write_owner_batch_export,
    write_owner_fact_packet,
)
from agentlog.safety.write_guard import WriteGuardViolation, assert_writable
from agentlog.service.logging_setup import configure_daemon_logging, ensure_log_dir


class ExportWriteBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.batch = OwnerInsightBatch("owner:batch-1", "synthetic-hash", (), ())
        root_logger = logging.getLogger()
        handlers, level = list(root_logger.handlers), root_logger.level

        def restore_logging() -> None:
            for handler in root_logger.handlers:
                if handler not in handlers:
                    handler.close()
            root_logger.handlers[:] = handlers
            root_logger.setLevel(level)

        self.addCleanup(restore_logging)

    def test_shared_guard_resolves_dangling_symlink_targets(self) -> None:
        target = self.root / "missing" / "AGENTS.md"
        link = self.root / "output.json"
        link.symlink_to(target)
        with self.assertRaises(WriteGuardViolation):
            assert_writable(link)
        self.assertFalse(target.parent.exists())

    def test_shared_guard_keeps_missing_parent_support(self) -> None:
        target = self.root / "missing" / "nested" / "output.json"
        self.assertEqual(assert_writable(target), target)
        self.assertFalse(target.parent.exists())

    def test_fact_packet_refuses_protected_names_before_mkdir(self) -> None:
        for name in ("AGENTS.md", "SKILL.md", "config.toml", "hooks.json"):
            with self.subTest(name=name):
                target = self.root / name / "nested" / name
                with self.assertRaises(WriteGuardViolation):
                    write_owner_fact_packet(target, run_id="synthetic", items=[])
                self.assertFalse(target.parent.exists())

    def test_fact_packet_refuses_existing_and_dangling_symlinks(self) -> None:
        for exists in (True, False):
            with self.subTest(exists=exists):
                protected = self.root / str(exists) / "AGENTS.md"
                protected.parent.mkdir()
                if exists:
                    protected.write_text("original", encoding="utf-8")
                link = self.root / f"packet-{exists}.json"
                link.symlink_to(protected)
                with self.assertRaises(WriteGuardViolation):
                    write_owner_fact_packet(link, run_id="synthetic", items=[])
                self.assertEqual(protected.exists(), exists)
                if exists:
                    self.assertEqual(protected.read_text(encoding="utf-8"), "original")

    def test_batch_export_refuses_config_directories_before_mkdir(self) -> None:
        for suffix in (".cursor/rules/new", ".claude/skills/new", ".codex/plugins/new"):
            with self.subTest(suffix=suffix):
                target = self.root / suffix
                with self.assertRaises(WriteGuardViolation):
                    write_owner_batch_export(target, [self.batch])
                self.assertFalse(target.exists())

    def test_batch_export_checks_every_child_before_writing(self) -> None:
        for name in ("owner_insights_prompt.md", "batch-1.json", "proposal_targets.json", "manifest.json"):
            for exists in (True, False):
                with self.subTest(name=name, exists=exists):
                    export = self.root / f"{name}-{exists}"
                    export.mkdir()
                    protected = export / "SKILL.md"
                    if exists:
                        protected.write_text("original", encoding="utf-8")
                    (export / name).symlink_to(protected)
                    before = set(export.iterdir())
                    with self.assertRaises(WriteGuardViolation):
                        write_owner_batch_export(export, [self.batch])
                    self.assertEqual(set(export.iterdir()), before)
                    self.assertEqual(protected.exists(), exists)
                    if exists:
                        self.assertEqual(protected.read_text(encoding="utf-8"), "original")

    def test_batch_id_cannot_escape_export_directory(self) -> None:
        for suffix in ("../escaped", str(self.root / "absolute"), "nested/file", "..\\escaped", ""):
            with self.subTest(suffix=suffix):
                export = self.root / "export"
                batch = OwnerInsightBatch(f"owner:{suffix}", "hash", (), ())
                with self.assertRaises(ValueError):
                    write_owner_batch_export(export, [batch])
                self.assertFalse(export.exists())
                self.assertFalse((self.root / "escaped.json").exists())
                self.assertFalse((self.root / "absolute.json").exists())

    def test_exports_and_logs_refuse_outside_allowed_roots(self) -> None:
        allowed = self.root / "allowed"
        outside = self.root / "outside"
        with patch("agentlog.safety.write_guard.allowed_roots", return_value=(allowed,)):
            for operation in (
                lambda: write_owner_batch_export(outside, [self.batch]),
                lambda: write_owner_fact_packet(outside / "facts.json", run_id="synthetic", items=[]),
                lambda: configure_daemon_logging(outside / "daemon.log"),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaises(WriteGuardViolation):
                        operation()
                    self.assertFalse(outside.exists())

    def test_exports_and_logs_work_in_each_working_root(self) -> None:
        for root_name in ("data", "project", "temp"):
            with self.subTest(root=root_name):
                working = self.root / root_name
                with patch("agentlog.safety.write_guard.allowed_roots", return_value=(working,)):
                    manifest = write_owner_batch_export(working / "export", [self.batch])
                    self.assertEqual(manifest["batch_count"], 1)
                    self.assertEqual(json.loads((working / "export/batch-1.json").read_text())["batch_id"], self.batch.id)
                    write_owner_fact_packet(working / "facts.json", run_id="synthetic", items=[])
                    self.assertEqual(json.loads((working / "facts.json").read_text())["run_id"], "synthetic")
                    log = working / "logs/daemon.log"
                    self.assertEqual(configure_daemon_logging(log), log)
                    logging.info("synthetic message")
                    self.assertIn("synthetic message", log.read_text())
                    for handler in logging.getLogger().handlers:
                        handler.close()

    def test_ensure_log_dir_refuses_protected_targets_before_mkdir(self) -> None:
        for suffix in ("nested/AGENTS.md", "nested/SKILL.md", ".claude/hooks/daemon.log"):
            with self.subTest(suffix=suffix):
                target = self.root / suffix
                with self.assertRaises(WriteGuardViolation):
                    ensure_log_dir(target)
                self.assertFalse(target.parent.exists())

    def test_logging_refuses_existing_and_dangling_symlinks(self) -> None:
        for exists in (True, False):
            with self.subTest(exists=exists):
                protected = self.root / str(exists) / "AGENTS.md"
                protected.parent.mkdir()
                if exists:
                    protected.write_text("original", encoding="utf-8")
                link = self.root / f"daemon-{exists}.log"
                link.symlink_to(protected)
                with self.assertRaises(WriteGuardViolation):
                    configure_daemon_logging(link)
                self.assertEqual(protected.exists(), exists)
                if exists:
                    self.assertEqual(protected.read_text(encoding="utf-8"), "original")

    def test_logging_checks_backup_symlinks_before_open(self) -> None:
        for index in (1, 5):
            for exists in (True, False):
                with self.subTest(index=index, exists=exists):
                    directory = self.root / f"{index}-{exists}"
                    directory.mkdir()
                    protected = directory / "AGENTS.md"
                    if exists:
                        protected.write_text("original", encoding="utf-8")
                    log = directory / "daemon.log"
                    backup = directory / f"daemon.log.{index}"
                    backup.symlink_to(protected)
                    with self.assertRaises(WriteGuardViolation):
                        configure_daemon_logging(log)
                    self.assertFalse(log.exists())
                    self.assertTrue(backup.is_symlink())
                    self.assertEqual(protected.exists(), exists)
                    if exists:
                        self.assertEqual(protected.read_text(), "original")

    def test_logging_rechecks_backups_at_rollover(self) -> None:
        log = self.root / "daemon.log"
        configure_daemon_logging(log)
        logging.info("synthetic before rotation")
        protected = self.root / "SKILL.md"
        protected.write_text("original", encoding="utf-8")
        backup = self.root / "daemon.log.1"
        backup.symlink_to(protected)
        handler = logging.getLogger().handlers[0]
        with self.assertRaises(WriteGuardViolation):
            handler.doRollover()
        self.assertTrue(backup.is_symlink())
        self.assertEqual(protected.read_text(), "original")
        self.assertIn("synthetic before rotation", log.read_text())

    def test_log_symlink_checks_resolved_backup_targets(self) -> None:
        log = self.root / "daemon.log"
        link = self.root / "alias.log"
        link.symlink_to(log)
        protected = self.root / "AGENTS.md"
        backup = self.root / "daemon.log.1"
        backup.symlink_to(protected)
        with self.assertRaises(WriteGuardViolation):
            configure_daemon_logging(link)
        self.assertFalse(log.exists())
        self.assertFalse(protected.exists())

    def test_rollover_checks_backups_at_original_base_after_base_symlink_change(self) -> None:
        log = self.root / "daemon.log"
        configure_daemon_logging(log)
        log.unlink()
        log.symlink_to(self.root / "other.log")
        protected = self.root / "AGENTS.md"
        backup = self.root / "daemon.log.1"
        backup.symlink_to(protected)
        with self.assertRaises(WriteGuardViolation):
            logging.getLogger().handlers[0].doRollover()
        self.assertTrue(backup.is_symlink())
        self.assertFalse(protected.exists())

    def test_ordinary_log_rotation_preserves_messages(self) -> None:
        log = self.root / "logs/daemon.log"
        configure_daemon_logging(log)
        logging.info("synthetic before rotation")
        logging.getLogger().handlers[0].doRollover()
        logging.info("synthetic after rotation")
        self.assertIn("synthetic before rotation", (log.parent / "daemon.log.1").read_text())
        self.assertIn("synthetic after rotation", log.read_text())


if __name__ == "__main__":
    unittest.main()
