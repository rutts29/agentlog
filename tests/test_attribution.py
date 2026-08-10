from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.analysis.attribution import (
    JOIN_METHOD_EXPLICIT,
    JOIN_METHOD_TIME_WINDOW,
    attribution_rollup,
    join_session_to_commits,
    rebuild_attribution,
    resolve_local_repo_path,
    session_attribution,
)
from agentlog.api.app import create_app
from agentlog.db.schema import connect, init_db


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "--no-pager", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _init_repo(root: Path) -> Path:
    repo = root / "proj"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    # Default branch name varies; normalize.
    _git(repo, "checkout", "-B", "main")
    return repo


def _commit(repo: Path, name: str, content: str, when: datetime) -> str:
    path = repo / name
    path.write_text(content, encoding="utf-8")
    env_date = when.strftime("%Y-%m-%dT%H:%M:%S")
    subprocess.run(
        ["git", "--no-pager", "-C", str(repo), "add", name],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "--no-pager",
            "-C",
            str(repo),
            "commit",
            "-m",
            f"add {name}",
            "--date",
            when.isoformat(),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={
            **dict(__import__("os").environ),
            "GIT_AUTHOR_DATE": when.isoformat(),
            "GIT_COMMITTER_DATE": when.isoformat(),
            "GIT_AUTHOR_NAME": "Test User",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test User",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    del env_date
    return _git(repo, "rev-parse", "HEAD")


class DecodeSlugTests(unittest.TestCase):
    def test_underscore_directory_decoded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Build /tmp/.../Users/<user>/side_projects/Plugin shape under tmp
            # by decoding against Path("/") we need real paths; test resolve via cwd instead.
            target = base / "side_projects" / "Plugin"
            target.mkdir(parents=True)
            _git(target, "init")
            slug = "Users-nobody-side-projects-Plugin"
            # decode against absolute / won't find tmp; resolve via cwd works:
            resolved = resolve_local_repo_path(slug, str(target))
            self.assertEqual(resolved, Path(_git(target, "rev-parse", "--show-toplevel")))


class JoinLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "t.db"
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.repo = _init_repo(self.root)
        self.t0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.sha_a = _commit(self.repo, "a.txt", "aaa\n", self.t0)
        self.sha_b = _commit(
            self.repo, "b.txt", "bbb\n", self.t0 + timedelta(hours=1)
        )
        self.sha_c = _commit(
            self.repo, "c.txt", "ccc\n", self.t0 + timedelta(days=2)
        )
        self.conn.execute(
            """
            INSERT INTO artifacts
            (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
            VALUES ('codex', '/tmp/a.jsonl', 1, 1, 'h', 0, '1')
            """
        )
        self.art = int(self.conn.execute("SELECT id FROM artifacts").fetchone()["id"])

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _session(
        self,
        sid: str,
        *,
        commit_sha: str | None,
        started: datetime,
        ended: datetime,
        branch: str | None = "main",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions
            (id, harness, external_id, artifact_id, started_at, ended_at,
             repo, cwd, branch, commit_sha, model)
            VALUES (?, 'codex', ?, ?, ?, ?, ?, ?, ?, ?, 'gpt-test')
            """,
            (
                sid,
                sid,
                self.art,
                started.isoformat(),
                ended.isoformat(),
                str(self.repo),
                str(self.repo),
                branch,
                commit_sha,
            ),
        )
        self.conn.commit()

    def test_explicit_join(self) -> None:
        self._session(
            "s-explicit",
            commit_sha=self.sha_a,
            started=self.t0 - timedelta(minutes=5),
            ended=self.t0 + timedelta(minutes=5),
        )
        counts = join_session_to_commits(
            self.conn,
            session_id="s-explicit",
            repo_path=self.repo,
            commit_sha=self.sha_a,
            branch="main",
            started_at=(self.t0 - timedelta(minutes=5)).isoformat(),
            ended_at=(self.t0 + timedelta(minutes=5)).isoformat(),
        )
        self.conn.commit()
        self.assertEqual(counts[JOIN_METHOD_EXPLICIT], 1)
        row = self.conn.execute(
            "SELECT join_method, subject FROM session_commits WHERE session_id=?",
            ("s-explicit",),
        ).fetchone()
        self.assertEqual(row["join_method"], JOIN_METHOD_EXPLICIT)
        self.assertIn("a.txt", row["subject"])

    def test_time_window_join_and_grace(self) -> None:
        # Session ends 10 min before commit B; 30 min grace still captures it.
        # branch=HEAD must not block the heuristic (harness quirk).
        started = self.t0 + timedelta(minutes=50)
        ended = self.t0 + timedelta(minutes=55)
        self._session(
            "s-window",
            commit_sha=None,
            started=started,
            ended=ended,
            branch="HEAD",
        )
        counts = join_session_to_commits(
            self.conn,
            session_id="s-window",
            repo_path=self.repo,
            commit_sha=None,
            branch="HEAD",
            started_at=started.isoformat(),
            ended_at=ended.isoformat(),
        )
        self.conn.commit()
        self.assertEqual(counts[JOIN_METHOD_EXPLICIT], 0)
        self.assertGreaterEqual(counts[JOIN_METHOD_TIME_WINDOW], 1)
        shas = {
            r["commit_sha"]
            for r in self.conn.execute(
                "SELECT commit_sha, join_method FROM session_commits WHERE session_id=?",
                ("s-window",),
            )
        }
        self.assertIn(self.sha_b, shas)
        self.assertNotIn(self.sha_c, shas)
        method = self.conn.execute(
            "SELECT join_method FROM session_commits WHERE session_id=? AND commit_sha=?",
            ("s-window", self.sha_b),
        ).fetchone()["join_method"]
        self.assertEqual(method, JOIN_METHOD_TIME_WINDOW)

    def test_honest_no_joinable_bucket(self) -> None:
        started = self.t0 + timedelta(days=10)
        ended = started + timedelta(minutes=30)
        self._session(
            "s-none",
            commit_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            started=started,
            ended=ended,
        )
        stats = rebuild_attribution(self.conn)
        self.assertEqual(stats.sessions_with_join, 0)
        self.assertEqual(stats.sessions_no_joinable, 1)
        rollup = attribution_rollup(self.conn)
        self.assertEqual(rollup["totals"]["sessions_no_joinable_commit"], 1)
        detail = session_attribution(self.conn, "s-none")
        assert detail is not None
        self.assertTrue(detail["no_joinable_commit"])
        self.assertEqual(detail["session_commits"], [])

    def test_explicit_preferred_over_time_window_same_sha(self) -> None:
        started = self.t0 - timedelta(minutes=1)
        ended = self.t0 + timedelta(minutes=1)
        self._session(
            "s-both",
            commit_sha=self.sha_a,
            started=started,
            ended=ended,
        )
        rebuild_attribution(self.conn)
        rows = list(
            self.conn.execute(
                "SELECT commit_sha, join_method FROM session_commits WHERE session_id=?",
                ("s-both",),
            )
        )
        by_sha = {r["commit_sha"]: r["join_method"] for r in rows}
        self.assertEqual(by_sha[self.sha_a], JOIN_METHOD_EXPLICIT)

    def test_rollup_separates_methods(self) -> None:
        self._session(
            "s1",
            commit_sha=self.sha_a,
            started=self.t0 - timedelta(minutes=5),
            ended=self.t0 + timedelta(minutes=5),
        )
        self._session(
            "s2",
            commit_sha=None,
            started=self.t0 + timedelta(minutes=50),
            ended=self.t0 + timedelta(minutes=55),
        )
        rebuild_attribution(self.conn)
        rollup = attribution_rollup(self.conn)
        self.assertGreaterEqual(rollup["totals"]["explicit_rows"], 1)
        self.assertGreaterEqual(rollup["totals"]["time_window_rows"], 1)
        repo_row = next(r for r in rollup["by_repo"] if r["repo"] == str(self.repo))
        self.assertGreaterEqual(
            repo_row["by_method"][JOIN_METHOD_EXPLICIT]["commits"], 1
        )
        self.assertGreaterEqual(
            repo_row["by_method"][JOIN_METHOD_TIME_WINDOW]["commits"], 1
        )
        self.assertIn("heuristic", rollup["join_methods"][JOIN_METHOD_TIME_WINDOW])


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "api.db"
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.repo = _init_repo(self.root)
        self.t0 = datetime(2026, 8, 2, 9, 0, 0, tzinfo=timezone.utc)
        self.sha = _commit(self.repo, "x.txt", "x\n", self.t0)
        self.conn.execute(
            """
            INSERT INTO artifacts
            (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
            VALUES ('codex', '/tmp/b.jsonl', 1, 1, 'h2', 0, '1')
            """
        )
        art = int(self.conn.execute("SELECT id FROM artifacts").fetchone()["id"])
        self.conn.execute(
            """
            INSERT INTO sessions
            (id, harness, external_id, artifact_id, started_at, ended_at,
             repo, cwd, branch, commit_sha, model)
            VALUES ('api-s1', 'codex', 'api-s1', ?, ?, ?, ?, ?, 'main', ?, 'm')
            """,
            (
                art,
                (self.t0 - timedelta(minutes=2)).isoformat(),
                (self.t0 + timedelta(minutes=2)).isoformat(),
                str(self.repo),
                str(self.repo),
                self.sha,
            ),
        )
        self.conn.commit()
        self.conn.close()
        self.client = TestClient(create_app(self.db_path))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rebuild_and_get(self) -> None:
        r = self.client.post("/api/attribution/rebuild")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(body["rebuild"]["explicit_joins"], 1)
        g = self.client.get("/api/attribution")
        self.assertEqual(g.status_code, 200)
        self.assertIn("by_repo", g.json())
        self.assertIn("by_harness", g.json())
        s = self.client.get("/api/attribution/session/api-s1")
        self.assertEqual(s.status_code, 200)
        detail = s.json()
        self.assertFalse(detail["no_joinable_commit"])
        self.assertEqual(detail["session_commits"][0]["join_method"], JOIN_METHOD_EXPLICIT)


class MigrationTests(unittest.TestCase):
    def test_v009_creates_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "m.db"
            conn = connect(db)
            init_db(conn)
            ver = conn.execute(
                "SELECT MAX(version) AS v FROM schema_migrations"
            ).fetchone()["v"]
            self.assertGreaterEqual(int(ver), 9)
            cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(session_commits)").fetchall()
            }
            self.assertIn("join_method", cols)
            self.assertIn("commit_sha", cols)
            conn.close()


if __name__ == "__main__":
    unittest.main()
