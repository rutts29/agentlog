"""Join agent sessions to Git commits in the repos they worked in.

Descriptive joins only. Two methods, never conflated in rollups:

- ``explicit``: the session's recorded ``commit_sha`` appears in repo history.
- ``time_window``: commit author date falls in
  ``[started_at, ended_at + 30min]`` on the session branch (or any branch if
  unknown). Heuristic; weaker than explicit.

Git access is strictly read-only via ``subprocess`` argument lists.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

TIME_WINDOW_GRACE = timedelta(minutes=30)
GIT_TIMEOUT_S = 30

JOIN_METHOD_EXPLICIT = "explicit"
JOIN_METHOD_TIME_WINDOW = "time_window"

_EXPECTED_JOIN_ERRORS = (
    sqlite3.Error,
    OSError,
    subprocess.SubprocessError,
    ValueError,
    UnicodeDecodeError,
)

_NOTE = (
    "Descriptive session-to-commit joins only. "
    "time_window is a heuristic and must not be conflated with explicit. "
    "No causal authorship claims."
)


@dataclass(frozen=True)
class GitCommit:
    sha: str
    author_date: str
    subject: str
    files_changed: int
    insertions: int
    deletions: int


@dataclass
class RebuildStats:
    repos_seen: int = 0
    repos_resolved: int = 0
    repos_skipped: int = 0
    sessions_considered: int = 0
    explicit_joins: int = 0
    time_window_joins: int = 0
    sessions_with_join: int = 0
    sessions_no_joinable: int = 0
    sessions_failed: int = 0
    sessions_unresolved: int = 0
    sessions_published: int = 0
    published: bool = True
    errors: list[str] = field(default_factory=list)


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "--no-pager", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def is_git_repo(path: Path) -> bool:
    if not path.is_dir():
        return False
    proc = _run_git(path, "rev-parse", "--is-inside-work-tree")
    return proc is not None and proc.returncode == 0 and proc.stdout.strip() == "true"


def git_toplevel(path: Path) -> Path | None:
    if not path.is_dir():
        return None
    proc = _run_git(path, "rev-parse", "--show-toplevel")
    if proc is None or proc.returncode != 0:
        return None
    top = proc.stdout.strip()
    if not top:
        return None
    resolved = Path(top)
    return resolved if resolved.is_dir() else None


def _search_dashed_path(base: Path, remaining: list[str]) -> Path | None:
    """Greedy decode of '/'→'-' project slugs, also trying '_' joins."""
    if not remaining:
        return base if base.exists() else None
    for n in range(len(remaining), 0, -1):
        chunk = remaining[:n]
        rest = remaining[n:]
        for name in ("-".join(chunk), "_".join(chunk)) if n > 1 else (chunk[0],):
            candidate = base / name
            if not candidate.exists():
                continue
            found = _search_dashed_path(candidate, rest)
            if found is not None:
                return found
    return None


def decode_project_slug(slug: str) -> Path | None:
    """Decode Cursor/Claude project slugs like ``Users-…`` / ``-Users-…``."""
    text = slug.strip()
    if not text or text.startswith("http://") or text.startswith("https://"):
        return None
    if text.startswith("-"):
        text = text[1:]
    if not text:
        return None
    parts = [p for p in text.split("-") if p]
    if not parts:
        return None
    return _search_dashed_path(Path("/"), parts)


def resolve_local_repo_path(
    repo: str | None, cwd: str | None = None
) -> Path | None:
    """Resolve a sessions.repo value (and optional cwd) to a local git root."""
    candidates: list[Path] = []
    if cwd:
        c = Path(cwd)
        if c.is_dir():
            candidates.append(c)
    if repo:
        r = repo.strip()
        if r and not r.startswith("http://") and not r.startswith("https://"):
            p = Path(r)
            if p.is_dir():
                candidates.append(p)
            decoded = decode_project_slug(r)
            if decoded is not None:
                candidates.append(decoded)

    seen: set[str] = set()
    for cand in candidates:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        top = git_toplevel(cand)
        if top is not None:
            return top
    return None


def discover_repo_targets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Distinct sessions.repo values with a resolvable local git root, if any."""
    rows = conn.execute(
        """
        SELECT repo,
               COUNT(*) AS session_count,
               GROUP_CONCAT(DISTINCT cwd) AS cwds
        FROM sessions
        WHERE repo IS NOT NULL AND TRIM(repo) != ''
        GROUP BY repo
        ORDER BY session_count DESC, repo ASC
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        repo = str(row["repo"])
        cwds = [c for c in (row["cwds"] or "").split(",") if c]
        resolved: Path | None = None
        for cwd in cwds:
            resolved = resolve_local_repo_path(repo, cwd)
            if resolved is not None:
                break
        if resolved is None:
            resolved = resolve_local_repo_path(repo, None)
        out.append(
            {
                "repo": repo,
                "session_count": int(row["session_count"]),
                "resolved_path": str(resolved) if resolved else None,
                "on_disk": resolved is not None,
            }
        )
    return out


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_numstat_block(lines: list[str]) -> tuple[int, int, int]:
    files = 0
    insertions = 0
    deletions = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        ins, dele = parts[0], parts[1]
        if ins.isdigit():
            insertions += int(ins)
        if dele.isdigit():
            deletions += int(dele)
    return files, insertions, deletions


def load_commit(repo: Path, sha: str) -> GitCommit | None:
    """Return commit metadata if ``sha`` exists in ``repo`` (read-only)."""
    if not sha or not sha.strip():
        return None
    sha = sha.strip()
    verify = _run_git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
    if verify is None or verify.returncode != 0:
        return None
    full = verify.stdout.strip()
    meta = _run_git(
        repo,
        "log",
        "-1",
        "--format=%H%x00%aI%x00%s",
        "--numstat",
        full,
    )
    if meta is None or meta.returncode != 0 or not meta.stdout.strip():
        return None
    lines = meta.stdout.splitlines()
    head = lines[0].split("\x00")
    if len(head) < 3:
        return None
    files, ins, dele = _parse_numstat_block(lines[1:])
    return GitCommit(
        sha=head[0],
        author_date=head[1],
        subject=head[2],
        files_changed=files,
        insertions=ins,
        deletions=dele,
    )


def list_commits_in_window(
    repo: Path,
    *,
    start: datetime,
    end: datetime,
    branch: str | None = None,
) -> list[GitCommit]:
    """Commits whose author date falls in ``[start, end]`` (inclusive)."""
    args = [
        "log",
        "--format=%H%x00%aI%x00%s",
        "--numstat",
        f"--since={start.isoformat()}",
        f"--until={end.isoformat()}",
    ]
    br = (branch or "").strip()
    # Harnesses sometimes record the literal "HEAD"; treat as unknown → --all.
    if br and br.upper() != "HEAD":
        local = _run_git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{br}")
        if local is not None and local.returncode == 0:
            args.append(br)
        else:
            any_ref = _run_git(repo, "rev-parse", "--verify", "--quiet", br)
            if any_ref is not None and any_ref.returncode == 0:
                args.append(br)
            else:
                args.append("--all")
    else:
        args.append("--all")

    proc = _run_git(repo, *args)
    if proc is None or proc.returncode != 0:
        return []

    commits: list[GitCommit] = []
    current: list[str] | None = None
    meta_parts: list[str] | None = None
    for line in proc.stdout.splitlines():
        if "\x00" in line:
            if meta_parts is not None and current is not None:
                files, ins, dele = _parse_numstat_block(current)
                commits.append(
                    GitCommit(
                        sha=meta_parts[0],
                        author_date=meta_parts[1],
                        subject=meta_parts[2],
                        files_changed=files,
                        insertions=ins,
                        deletions=dele,
                    )
                )
            parts = line.split("\x00")
            if len(parts) < 3:
                meta_parts = None
                current = None
                continue
            meta_parts = parts[:3]
            current = []
        else:
            if current is not None:
                current.append(line)
    if meta_parts is not None and current is not None:
        files, ins, dele = _parse_numstat_block(current)
        commits.append(
            GitCommit(
                sha=meta_parts[0],
                author_date=meta_parts[1],
                subject=meta_parts[2],
                files_changed=files,
                insertions=ins,
                deletions=dele,
            )
        )

    # git --since/--until is second-granularity and exclusive on --until in
    # some versions; keep only author dates inside the closed window.
    kept: list[GitCommit] = []
    for c in commits:
        ad = _parse_iso(c.author_date)
        if ad is None:
            continue
        if start <= ad <= end:
            kept.append(c)
    return kept


class _StagedJoins:
    """Collects rows for one session so a failure never touches published data."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[Any, ...]] = {}

    def add(
        self,
        *,
        session_id: str,
        commit: GitCommit,
        join_method: str,
        repo_path: str,
    ) -> bool:
        key = (session_id, commit.sha)
        if key in self.rows:
            return False
        self.rows[key] = (
            session_id,
            commit.sha,
            join_method,
            commit.author_date,
            commit.subject,
            commit.files_changed,
            commit.insertions,
            commit.deletions,
            repo_path,
        )
        return True


def _insert_join(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    commit: GitCommit,
    join_method: str,
    repo_path: str,
    stage: _StagedJoins | None = None,
) -> bool:
    if stage is not None:
        return stage.add(
            session_id=session_id,
            commit=commit,
            join_method=join_method,
            repo_path=repo_path,
        )
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO session_commits (
            session_id, commit_sha, join_method, author_date, subject,
            files_changed, insertions, deletions, repo_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            commit.sha,
            join_method,
            commit.author_date,
            commit.subject,
            commit.files_changed,
            commit.insertions,
            commit.deletions,
            repo_path,
        ),
    )
    return cur.rowcount > 0


def join_session_to_commits(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    repo_path: Path,
    commit_sha: str | None,
    branch: str | None,
    started_at: str | None,
    ended_at: str | None,
    stage: _StagedJoins | None = None,
) -> dict[str, int]:
    """Join one session; prefer explicit over time_window for the same sha."""
    counts = {JOIN_METHOD_EXPLICIT: 0, JOIN_METHOD_TIME_WINDOW: 0}
    repo_s = str(repo_path)
    seen: set[str] = set()

    if commit_sha:
        explicit = load_commit(repo_path, commit_sha)
        if explicit is not None:
            if _insert_join(
                conn,
                session_id=session_id,
                commit=explicit,
                join_method=JOIN_METHOD_EXPLICIT,
                repo_path=repo_s,
                stage=stage,
            ):
                counts[JOIN_METHOD_EXPLICIT] += 1
            seen.add(explicit.sha)

    start = _parse_iso(started_at)
    end = _parse_iso(ended_at)
    if start is not None and end is not None:
        if end < start:
            start, end = end, start
        window_end = end + TIME_WINDOW_GRACE
        for commit in list_commits_in_window(
            repo_path, start=start, end=window_end, branch=branch
        ):
            if commit.sha in seen:
                continue
            if _insert_join(
                conn,
                session_id=session_id,
                commit=commit,
                join_method=JOIN_METHOD_TIME_WINDOW,
                repo_path=repo_s,
                stage=stage,
            ):
                counts[JOIN_METHOD_TIME_WINDOW] += 1
            seen.add(commit.sha)

    return counts


def rebuild_attribution(
    conn: sqlite3.Connection, *, max_failure_ratio: float = 0.25
) -> RebuildStats:
    """Rebuild ``session_commits`` by staging first and publishing atomically.

    Prior rows survive for any session whose refresh fails, and nothing is
    published at all once failures exceed ``max_failure_ratio`` of the sessions
    considered — a broken Git environment must not erase valid attribution.
    """
    stats = RebuildStats()

    targets = discover_repo_targets(conn)
    path_by_repo = {
        t["repo"]: Path(t["resolved_path"])
        for t in targets
        if t["resolved_path"]
    }
    stats.repos_seen = len(targets)
    stats.repos_resolved = len(path_by_repo)
    stats.repos_skipped = stats.repos_seen - stats.repos_resolved

    sessions = conn.execute(
        """
        SELECT id, repo, cwd, branch, commit_sha, started_at, ended_at
        FROM sessions
        WHERE repo IS NOT NULL AND TRIM(repo) != ''
        """
    ).fetchall()

    staged: list[tuple[Any, ...]] = []
    refreshed: list[str] = []
    for s in sessions:
        stats.sessions_considered += 1
        session_id = str(s["id"])
        repo = s["repo"]
        repo_path = path_by_repo.get(repo)
        if repo_path is None:
            # Per-session cwd may still resolve when GROUP_CONCAT missed it.
            repo_path = resolve_local_repo_path(repo, s["cwd"])
        if repo_path is None:
            stats.sessions_no_joinable += 1
            stats.sessions_unresolved += 1
            continue
        stage = _StagedJoins()
        try:
            counts = join_session_to_commits(
                conn,
                session_id=session_id,
                repo_path=repo_path,
                commit_sha=s["commit_sha"],
                branch=s["branch"],
                started_at=s["started_at"],
                ended_at=s["ended_at"],
                stage=stage,
            )
        except _EXPECTED_JOIN_ERRORS as exc:
            stats.errors.append(f"{session_id}: {type(exc).__name__}: {exc}")
            stats.sessions_failed += 1
            continue
        refreshed.append(session_id)
        staged.extend(stage.rows.values())
        n = counts[JOIN_METHOD_EXPLICIT] + counts[JOIN_METHOD_TIME_WINDOW]
        stats.explicit_joins += counts[JOIN_METHOD_EXPLICIT]
        stats.time_window_joins += counts[JOIN_METHOD_TIME_WINDOW]
        if n > 0:
            stats.sessions_with_join += 1
        else:
            stats.sessions_no_joinable += 1

    if (
        stats.sessions_considered > 0
        and stats.sessions_failed / stats.sessions_considered > max_failure_ratio
    ):
        stats.published = False
        stats.errors.append(
            f"publish aborted: {stats.sessions_failed} of "
            f"{stats.sessions_considered} sessions failed to refresh"
        )
        conn.rollback()
        return stats

    try:
        for session_id in refreshed:
            conn.execute(
                "DELETE FROM session_commits WHERE session_id = ?", (session_id,)
            )
        conn.executemany(
            """
            INSERT OR IGNORE INTO session_commits (
                session_id, commit_sha, join_method, author_date, subject,
                files_changed, insertions, deletions, repo_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            staged,
        )
    except sqlite3.Error:
        conn.rollback()
        stats.published = False
        raise
    conn.commit()
    stats.sessions_published = len(refreshed)
    return stats


def session_commits(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT commit_sha, join_method, author_date, subject,
               files_changed, insertions, deletions, repo_path
        FROM session_commits
        WHERE session_id = ?
        ORDER BY
          CASE join_method WHEN 'explicit' THEN 0 ELSE 1 END,
          COALESCE(author_date, '') ASC,
          commit_sha ASC
        """,
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def session_attribution(
    conn: sqlite3.Connection, session_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, harness, repo, cwd, branch, commit_sha, started_at, ended_at "
        "FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    commits = session_commits(conn, session_id)
    by_method = {
        JOIN_METHOD_EXPLICIT: sum(
            1 for c in commits if c["join_method"] == JOIN_METHOD_EXPLICIT
        ),
        JOIN_METHOD_TIME_WINDOW: sum(
            1 for c in commits if c["join_method"] == JOIN_METHOD_TIME_WINDOW
        ),
    }
    return {
        "session_id": row["id"],
        "harness": row["harness"],
        "repo": row["repo"],
        "cwd": row["cwd"],
        "branch": row["branch"],
        "commit_sha": row["commit_sha"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "resolved_path": (
            str(resolve_local_repo_path(row["repo"], row["cwd"]))
            if row["repo"]
            else None
        ),
        "session_commits": commits,
        "joins_by_method": by_method,
        "no_joinable_commit": len(commits) == 0,
        "note": _NOTE,
    }


def _empty_method_bucket() -> dict[str, int]:
    return {
        "commits": 0,
        "insertions": 0,
        "deletions": 0,
        "files_changed": 0,
    }


def attribution_rollup(conn: sqlite3.Connection) -> dict[str, Any]:
    """Per-repo and per-harness rollups with join-method denominators."""
    # Sessions with a non-empty repo are the denominator for "joinable attempt".
    session_rows = conn.execute(
        """
        SELECT id, harness, repo, cwd
        FROM sessions
        WHERE repo IS NOT NULL AND TRIM(repo) != ''
        """
    ).fetchall()

    join_rows = conn.execute(
        """
        SELECT sc.session_id, sc.commit_sha, sc.join_method,
               sc.insertions, sc.deletions, sc.files_changed,
               sc.repo_path, s.harness, s.repo
        FROM session_commits sc
        JOIN sessions s ON s.id = sc.session_id
        """
    ).fetchall()

    sessions_with_join = {r["session_id"] for r in join_rows}

    def _ensure(
        store: dict[str, dict[str, Any]], key: str, *, label_key: str, label: str
    ) -> dict[str, Any]:
        if key not in store:
            store[key] = {
                label_key: label,
                "sessions": 0,
                "sessions_with_join": 0,
                "sessions_no_joinable_commit": 0,
                "by_method": {
                    JOIN_METHOD_EXPLICIT: _empty_method_bucket(),
                    JOIN_METHOD_TIME_WINDOW: _empty_method_bucket(),
                },
                "resolved_path": None,
            }
        return store[key]

    by_repo: dict[str, dict[str, Any]] = {}
    by_harness: dict[str, dict[str, Any]] = {}

    path_by_repo = {
        t["repo"]: t["resolved_path"] for t in discover_repo_targets(conn)
    }

    for s in session_rows:
        repo = s["repo"] or "(unknown)"
        harness = s["harness"] or "(unknown)"
        rb = _ensure(by_repo, repo, label_key="repo", label=repo)
        hb = _ensure(by_harness, harness, label_key="harness", label=harness)
        rb["sessions"] += 1
        hb["sessions"] += 1
        if path_by_repo.get(repo):
            rb["resolved_path"] = path_by_repo[repo]
        if s["id"] in sessions_with_join:
            rb["sessions_with_join"] += 1
            hb["sessions_with_join"] += 1
        else:
            rb["sessions_no_joinable_commit"] += 1
            hb["sessions_no_joinable_commit"] += 1

    # Distinct commits per (group, method) — a commit linked to many sessions
    # via time_window still counts once in the group's commit set for that method.
    seen_repo_method: dict[tuple[str, str], set[str]] = {}
    seen_harness_method: dict[tuple[str, str], set[str]] = {}

    for r in join_rows:
        repo = r["repo"] or "(unknown)"
        harness = r["harness"] or "(unknown)"
        method = r["join_method"]
        rb = _ensure(by_repo, repo, label_key="repo", label=repo)
        hb = _ensure(by_harness, harness, label_key="harness", label=harness)
        for bucket, key, seen_map in (
            (rb, repo, seen_repo_method),
            (hb, harness, seen_harness_method),
        ):
            m = bucket["by_method"][method]
            sk = (key, method)
            shas = seen_map.setdefault(sk, set())
            sha = r["commit_sha"]
            if sha not in shas:
                shas.add(sha)
                m["commits"] += 1
                m["insertions"] += int(r["insertions"] or 0)
                m["deletions"] += int(r["deletions"] or 0)
                m["files_changed"] += int(r["files_changed"] or 0)

    def _finalize(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for it in items:
            it["denominators"] = {
                "sessions": it["sessions"],
                "sessions_with_join": it["sessions_with_join"],
                "sessions_no_joinable_commit": it["sessions_no_joinable_commit"],
                "note": (
                    "sessions_no_joinable_commit includes sessions whose repo "
                    "is missing on disk, sessions with no matching commit_sha, "
                    "and sessions with no commit in the time-window heuristic."
                ),
            }
        items.sort(key=lambda x: (-int(x["sessions"]), str(x.get("repo") or x.get("harness"))))
        return items

    return {
        "by_repo": _finalize(list(by_repo.values())),
        "by_harness": _finalize(list(by_harness.values())),
        "totals": {
            "sessions_with_repo": len(session_rows),
            "sessions_with_join": len(sessions_with_join),
            "sessions_no_joinable_commit": len(session_rows) - len(sessions_with_join),
            "joined_rows": len(join_rows),
            "explicit_rows": sum(
                1 for r in join_rows if r["join_method"] == JOIN_METHOD_EXPLICIT
            ),
            "time_window_rows": sum(
                1 for r in join_rows if r["join_method"] == JOIN_METHOD_TIME_WINDOW
            ),
        },
        "join_methods": {
            JOIN_METHOD_EXPLICIT: "session.commit_sha found in repo history",
            JOIN_METHOD_TIME_WINDOW: (
                "commit author date in [started_at, ended_at+30min] "
                "(heuristic; weaker than explicit)"
            ),
        },
        "note": _NOTE,
    }


def project_label(repo: str | None) -> str:
    if not repo:
        return "(unknown)"
    text = repo.strip()
    if text.startswith("http"):
        path = urlparse(text).path.rstrip("/")
        name = path.split("/")[-1]
        return name.removesuffix(".git") or text
    if "/" in text or text.startswith("-") or text.startswith("Users-"):
        return text.split("/")[-1].lstrip("-") or text
    return text
