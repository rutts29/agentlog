from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentlog.analysis.claims.extract import link_supersessions
from agentlog.analysis.claims.models import Claim, Proposal
from agentlog.analysis.claims.proposals import refresh_learnings
from agentlog.analysis.claims.store import (
    count_proposals_by_status,
    set_proposal_status,
)
from agentlog.analysis.config_ledger import (
    backup_agentlog_db,
    find_supersession_cycles,
    proposal_correspondence,
    refresh_config_ledger,
    tracked_config_paths,
)
from agentlog.db.schema import connect, init_db
from agentlog.safety.write_guard import WriteGuardViolation, assert_writable


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "--no-pager", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


class SupersessionSafetyTests(unittest.TestCase):
    def test_same_id_does_not_self_supersede(self) -> None:
        prior = Claim(
            id="same",
            kind="recurring_instruction",
            subject="dont_act_yet_brake",
            predicate="observed_in_labeled_windows",
            value={"sessions": 5},
            scope_type="global",
            scope_id="global",
            derivation="llm_derived",
            sample_size=5,
            supersedes_id="same",  # corrupt prior
        )
        newer = Claim(
            id="same",
            kind="recurring_instruction",
            subject="dont_act_yet_brake",
            predicate="observed_in_labeled_windows",
            value={"sessions": 8},
            scope_type="global",
            scope_id="global",
            derivation="llm_derived",
            sample_size=8,
        )
        linked = link_supersessions([newer], [prior])
        self.assertIsNone(linked[0].supersedes_id)

    def test_distinct_ids_still_link(self) -> None:
        prior = Claim(
            id="old1",
            kind="skill_exposure",
            subject="x",
            predicate="session_exposure_rate",
            value={"exposure_count": 1},
            scope_type="skill",
            scope_id="sk",
            derivation="deterministic",
            sample_size=1,
        )
        newer = Claim(
            id="new1",
            kind="skill_exposure",
            subject="x",
            predicate="session_exposure_rate",
            value={"exposure_count": 5},
            scope_type="skill",
            scope_id="sk",
            derivation="deterministic",
            sample_size=5,
        )
        linked = link_supersessions([newer], [prior])
        self.assertEqual(linked[0].supersedes_id, "old1")

    def test_trigger_rejects_self_supersede_insert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            init_db(conn)
            now = "2026-08-09T12:00:00+00:00"
            with self.assertRaises(Exception):
                conn.execute(
                    """
                    INSERT INTO claims (
                        id, kind, subject, predicate, value_json, scope_type,
                        derivation, status, support_status, sample_size,
                        observed_at, extractor_name, extractor_version,
                        confidence_basis_json, does_not_prove, supersedes_id,
                        created_at, updated_at
                    ) VALUES (
                        'c1', 'k', 's', 'p', '{}', 'global',
                        'deterministic', 'candidate', 'ok', 1,
                        ?, 't', '1', '{}', '', 'c1', ?, ?
                    )
                    """,
                    (now, now, now),
                )
            conn.close()


class ProposalSupersededStatusTests(unittest.TestCase):
    def test_prune_uses_superseded_not_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            (home / "AGENTS.md").write_text("# g\n", encoding="utf-8")
            conn = connect(root / "t.db")
            init_db(conn)
            # Seed a pending proposal that will not be regenerated.
            conn.execute(
                """
                INSERT INTO proposals (
                    id, title, action, status, target_path, target_kind,
                    scope_type, scope_id, base_content_hash, unified_diff,
                    proposed_content, rationale, derivation_summary,
                    does_not_prove, sample_size, created_at, updated_at
                ) VALUES (
                    'stale1', 'Stale', 'add', 'pending', ?, 'agents_md',
                    'global', 'global', NULL, 'diff', 'body', 'r', '',
                    '', 0, '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00'
                )
                """,
                (str(home / "AGENTS.md"),),
            )
            conn.commit()
            refresh_learnings(conn, home=home, include_llm_derived=False)
            conn.commit()
            row = conn.execute(
                "SELECT status, decision_note FROM proposals WHERE id = 'stale1'"
            ).fetchone()
            self.assertEqual(row["status"], "superseded")
            self.assertIn("system-superseded", row["decision_note"] or "")
            counts = count_proposals_by_status(conn)
            self.assertGreaterEqual(counts["superseded"], 1)
            # Owner cannot set superseded via the public decision path.
            with self.assertRaises(ValueError):
                set_proposal_status(conn, "stale1", "superseded")
            conn.close()


class ConfigLedgerTests(unittest.TestCase):
    def test_backup_includes_committed_rows_left_in_wal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agentlog.db"
            writer = sqlite3.connect(source)
            writer.execute("CREATE TABLE entries (value TEXT NOT NULL)")
            writer.commit()
            self.assertEqual(
                writer.execute("PRAGMA journal_mode = WAL").fetchone()[0],
                "wal",
            )
            writer.execute("PRAGMA wal_autocheckpoint = 1000000")
            writer.execute("INSERT INTO entries(value) VALUES ('committed')")
            writer.commit()
            wal_path = Path(f"{source}-wal")
            self.assertTrue(wal_path.is_file())
            self.assertGreater(wal_path.stat().st_size, 0)

            main_only = root / "main-only.db"
            shutil.copy2(source, main_only)
            main_conn = sqlite3.connect(main_only)
            self.assertEqual(
                main_conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0],
                0,
            )
            main_conn.close()

            backup = backup_agentlog_db(source, reason="wal test")
            backup_conn = sqlite3.connect(backup)
            self.assertEqual(
                backup_conn.execute("SELECT value FROM entries").fetchone()[0],
                "committed",
            )
            self.assertEqual(
                backup_conn.execute("PRAGMA quick_check").fetchone()[0],
                "ok",
            )
            backup_conn.close()
            writer.close()

    def test_backup_missing_source_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "missing.db"
            backup = backup_agentlog_db(source, reason="missing")
            self.assertEqual(backup.read_bytes(), b"")

    def test_git_history_and_live_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            repo = home / "proj"
            repo.mkdir()
            _git(repo, "init")
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "t")
            _git(repo, "checkout", "-B", "main")
            agents = repo / "AGENTS.md"
            agents.write_text("# v1\n", encoding="utf-8")
            _git(repo, "add", "AGENTS.md")
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "v1"],
                check=True,
                capture_output=True,
                env={
                    **dict(__import__("os").environ),
                    "GIT_AUTHOR_DATE": "2026-01-01T10:00:00",
                    "GIT_COMMITTER_DATE": "2026-01-01T10:00:00",
                },
            )
            agents.write_text("# v2\nrule\n", encoding="utf-8")
            _git(repo, "add", "AGENTS.md")
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "v2"],
                check=True,
                capture_output=True,
                env={
                    **dict(__import__("os").environ),
                    "GIT_AUTHOR_DATE": "2026-02-01T10:00:00",
                    "GIT_COMMITTER_DATE": "2026-02-01T10:00:00",
                },
            )
            # Point inventory at this home via AGENTS.md symlink-style file.
            (home / "AGENTS.md").write_text("# global\n", encoding="utf-8")

            conn = connect(root / "t.db")
            init_db(conn)
            # Track the repo AGENTS by scanning with home that includes it
            # via discover — put it under side_projects-style path used by inventory.
            # Directly exercise ledger APIs on known paths.
            from agentlog.analysis.config_ledger import (
                import_git_history_for_path,
                scan_live_path,
            )

            n, err = import_git_history_for_path(
                conn, agents, path_kind="agents_md"
            )
            self.assertIsNone(err)
            self.assertGreaterEqual(n, 2)
            tag = scan_live_path(conn, agents, path_kind="agents_md")
            self.assertIn(tag, {"inserted", "duplicate", "unchanged"})
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM config_snapshots WHERE path = ?"
                , (str(agents),)
            ).fetchone()["c"]
            self.assertGreaterEqual(int(total), 2)
            conn.close()

    def test_write_guard_blocks_config_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agents = home / "AGENTS.md"
            agents.write_text("x", encoding="utf-8")
            # AGENTS.md is always harness config by name.
            with self.assertRaises(WriteGuardViolation):
                assert_writable(agents, purpose="test")

    def test_correspondence_association_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "AGENTS.md"
            target.write_text("# before\n", encoding="utf-8")
            conn = connect(root / "t.db")
            init_db(conn)
            proposed = "# before\n\n## New\n\n- rule\n"
            prop = Proposal(
                id="p1",
                title="t",
                action="add",
                status="accepted",
                target_path=str(target),
                target_kind="agents_md",
                scope_type="global",
                scope_id="global",
                base_content_hash="x",
                unified_diff="d",
                proposed_content=proposed,
                rationale="r",
                decided_at=(
                    datetime.now(timezone.utc) - timedelta(hours=48)
                ).isoformat(),
            )
            from agentlog.analysis.config_ledger import _insert_snapshot

            _insert_snapshot(
                conn,
                path=target,
                path_kind="agents_md",
                content=proposed,
                observed_at=datetime.now(timezone.utc).isoformat(),
                source="live_scan",
            )
            corr = proposal_correspondence(conn, prop)
            self.assertEqual(corr["status"], "observed_match")
            self.assertIn("does not prove", corr["does_not_prove"].lower())
            conn.close()


if __name__ == "__main__":
    unittest.main()
