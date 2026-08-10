from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agentlog.analysis import attribution
from agentlog.analysis.attribution import rebuild_attribution
from agentlog.db.schema import connect, init_db
from tests.test_attribution import _commit, _git

T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "checkout", "-B", "main")
    return repo


class RebuildSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.conn = connect(self.root / "t.db")
        init_db(self.conn)

        self.repo_ok = _make_repo(self.root, "ok")
        self.repo_bad = _make_repo(self.root, "bad")
        self.sha_ok = _commit(self.repo_ok, "a.txt", "a\n", T0)
        self.sha_bad = _commit(self.repo_bad, "b.txt", "b\n", T0)

        self.conn.execute(
            """
            INSERT INTO artifacts
            (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
            VALUES ('codex', '/tmp/a.jsonl', 1, 1, 'h', 0, '1')
            """
        )
        art = int(self.conn.execute("SELECT id FROM artifacts").fetchone()["id"])
        for sid, repo, sha in (
            ("s-ok", self.repo_ok, self.sha_ok),
            ("s-bad", self.repo_bad, self.sha_bad),
        ):
            self.conn.execute(
                """
                INSERT INTO sessions
                (id, harness, external_id, artifact_id, started_at, ended_at,
                 repo, cwd, branch, commit_sha, model)
                VALUES (?, 'codex', ?, ?, ?, ?, ?, ?, 'main', ?, 'gpt-test')
                """,
                (
                    sid,
                    sid,
                    art,
                    (T0 - timedelta(minutes=5)).isoformat(),
                    (T0 + timedelta(minutes=5)).isoformat(),
                    str(repo),
                    str(repo),
                    sha,
                ),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _seed_prior(self, session_id: str, sha: str) -> None:
        self.conn.execute(
            """
            INSERT INTO session_commits (
                session_id, commit_sha, join_method, author_date, subject,
                files_changed, insertions, deletions, repo_path
            ) VALUES (?, ?, 'explicit', ?, 'prior work', 1, 1, 0, '/prior')
            """,
            (session_id, sha, T0.isoformat()),
        )
        self.conn.commit()

    def _rows(self, session_id: str) -> list[tuple]:
        return [
            (r["commit_sha"], r["subject"], r["repo_path"])
            for r in self.conn.execute(
                "SELECT commit_sha, subject, repo_path FROM session_commits "
                "WHERE session_id = ? ORDER BY commit_sha",
                (session_id,),
            )
        ]

    def _fail_only(self, repo_name: str):
        real = attribution.list_commits_in_window

        def fake(repo_path, **kwargs):
            if Path(repo_path).name == repo_name:
                raise subprocess.SubprocessError("git exploded")
            return real(repo_path, **kwargs)

        return mock.patch.object(attribution, "list_commits_in_window", fake)

    def _fail_explicit(self, repo_name: str):
        real = attribution.load_commit

        def fake(repo_path, sha):
            if Path(repo_path).name == repo_name:
                raise subprocess.SubprocessError("git exploded")
            return real(repo_path, sha)

        return mock.patch.object(attribution, "load_commit", fake)

    def test_failed_session_keeps_prior_rows(self) -> None:
        self._seed_prior("s-ok", "deadbeef")
        self._seed_prior("s-bad", "cafebabe")

        with self._fail_only("bad"), self._fail_explicit("bad"):
            stats = rebuild_attribution(self.conn, max_failure_ratio=0.9)

        self.assertTrue(stats.published)
        self.assertEqual(stats.sessions_failed, 1)
        self.assertEqual(stats.sessions_published, 1)
        self.assertEqual(len(stats.errors), 1)
        self.assertIn("s-bad", stats.errors[0])

        self.assertEqual(
            self._rows("s-bad"), [("cafebabe", "prior work", "/prior")]
        )
        ok_rows = self._rows("s-ok")
        self.assertEqual([r[0] for r in ok_rows], [self.sha_ok])
        self.assertNotIn("prior work", [r[1] for r in ok_rows])

    def test_publish_aborts_past_failure_threshold(self) -> None:
        self._seed_prior("s-ok", "deadbeef")
        self._seed_prior("s-bad", "cafebabe")
        before = self._rows("s-ok") + self._rows("s-bad")

        def boom(*_args, **_kwargs):
            raise subprocess.SubprocessError("git exploded")

        with mock.patch.object(attribution, "list_commits_in_window", boom), \
                mock.patch.object(attribution, "load_commit", boom):
            stats = rebuild_attribution(self.conn)

        self.assertFalse(stats.published)
        self.assertEqual(stats.sessions_failed, 2)
        self.assertEqual(stats.sessions_published, 0)
        self.assertEqual(self._rows("s-ok") + self._rows("s-bad"), before)

    def test_clean_rebuild_replaces_stale_rows(self) -> None:
        self._seed_prior("s-ok", "deadbeef")
        self._seed_prior("s-bad", "cafebabe")
        stats = rebuild_attribution(self.conn)
        self.assertTrue(stats.published)
        self.assertEqual(stats.sessions_failed, 0)
        self.assertEqual(stats.sessions_published, 2)
        for sid, sha in (("s-ok", self.sha_ok), ("s-bad", self.sha_bad)):
            self.assertEqual([r[0] for r in self._rows(sid)], [sha])


if __name__ == "__main__":
    unittest.main()
