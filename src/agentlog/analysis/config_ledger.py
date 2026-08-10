"""Dated history of the owner's agent-config instruction files.

Read-only on the source files. Snapshots land in ``~/.agentlog/agentlog.db``
(and DB backups under ``~/.agentlog/``), always through the write guard.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agentlog.analysis.skills import default_skill_roots, discover_skill_files
from agentlog.config import DEFAULT_DB_PATH
from agentlog.db.schema import BUSY_TIMEOUT_MS
from agentlog.safety.write_guard import assert_writable

CONTENT_STORE_CAP = 512_000
GIT_HISTORY_LIMIT = 200
MIN_POST_ACCEPT_HOURS = 24


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _sha1_text(text: str) -> str:
    return _sha1_bytes(text.encode("utf-8"))


def _snapshot_id(path: str, content_hash: str, source: str, git_commit: str | None) -> str:
    raw = f"{path}|{content_hash}|{source}|{git_commit or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _clip_content(text: str) -> tuple[str, int]:
    raw = text.encode("utf-8")
    n = len(raw)
    if n <= CONTENT_STORE_CAP:
        return text, n
    clipped = raw[:CONTENT_STORE_CAP].decode("utf-8", errors="ignore")
    return clipped + "\n/* agentlog: truncated */\n", n


def backup_agentlog_db(
    db_path: Path | None = None,
    *,
    reason: str,
) -> Path:
    """Back up SQLite consistently, or create an empty target if the source is absent."""
    src = Path(db_path or DEFAULT_DB_PATH).expanduser()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_reason = "".join(c if c.isalnum() or c in "-_" else "_" for c in reason)[:48]
    dest = src.parent / f"agentlog.db.bak_{safe_reason}_{stamp}"
    target = assert_writable(dest, purpose=f"db backup:{reason}")

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        assert_writable(tmp_path, purpose=f"db backup temp:{reason}")
        if src.is_file() and src.stat().st_size > 0:
            source_conn = sqlite3.connect(
                src.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=BUSY_TIMEOUT_MS / 1000,
            )
            destination_conn: sqlite3.Connection | None = None
            try:
                destination_conn = sqlite3.connect(
                    str(tmp_path),
                    timeout=BUSY_TIMEOUT_MS / 1000,
                )
                source_conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
                destination_conn.execute(
                    f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}"
                )
                source_conn.backup(destination_conn)
                destination_conn.commit()
            finally:
                if destination_conn is not None:
                    destination_conn.close()
                source_conn.close()
            shutil.copymode(src, tmp_path)
        else:
            # Keep missing and zero-byte sources as explicit empty backups.
            tmp_path.write_bytes(b"")
            if src.is_file():
                shutil.copymode(src, tmp_path)
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def _table_ready(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='config_snapshots'"
        ).fetchone()
        is not None
    )


def _git_root(path: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path.parent if path.is_file() else path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    root = Path(proc.stdout.strip())
    return root if root.is_dir() else None


def _rel_to_git(root: Path, path: Path) -> str | None:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return None


@dataclass
class LedgerStats:
    paths_scanned: int = 0
    live_inserted: int = 0
    git_inserted: int = 0
    git_repos: int = 0
    skipped_unchanged: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "paths_scanned": self.paths_scanned,
            "live_inserted": self.live_inserted,
            "git_inserted": self.git_inserted,
            "git_repos": self.git_repos,
            "skipped_unchanged": self.skipped_unchanged,
            "errors": self.errors[:20],
        }


def tracked_config_paths(home: Path | None = None) -> list[tuple[Path, str]]:
    """Return (path, path_kind) for instruction surfaces we care about."""
    from agentlog.analysis.claims.scope import discover_config_inventory

    base = home or Path.home()
    inv = discover_config_inventory(base)
    out: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for f in inv.files:
        if not f.exists:
            # Still track missing targets that inventory knows about? Skip.
            continue
        try:
            key = f.path.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append((f.path, f.kind))

    for _source, skill_path in discover_skill_files(default_skill_roots(base)):
        try:
            key = skill_path.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        # Skip plugin cache bulk — prefer user-owned skill homes.
        parts = skill_path.parts
        if "plugins" in parts and "cache" in parts:
            continue
        seen.add(key)
        out.append((skill_path, "skill"))
    return out


def _insert_snapshot(
    conn: sqlite3.Connection,
    *,
    path: Path,
    path_kind: str,
    content: str,
    observed_at: str,
    source: str,
    git_commit: str | None = None,
    git_committed_at: str | None = None,
    meta: dict[str, Any] | None = None,
) -> bool:
    stored, nbytes = _clip_content(content)
    content_hash = _sha1_text(content)
    sid = _snapshot_id(str(path), content_hash, source, git_commit)
    existing = conn.execute(
        "SELECT id FROM config_snapshots WHERE id = ?", (sid,)
    ).fetchone()
    if existing:
        return False
    conn.execute(
        """
        INSERT INTO config_snapshots (
            id, path, path_kind, content_hash, content, content_bytes,
            observed_at, source, git_commit, git_committed_at, meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            str(path),
            path_kind,
            content_hash,
            stored,
            nbytes,
            observed_at,
            source,
            git_commit,
            git_committed_at,
            json.dumps(meta or {}, sort_keys=True),
        ),
    )
    return True


def import_git_history_for_path(
    conn: sqlite3.Connection,
    path: Path,
    *,
    path_kind: str,
    limit: int = GIT_HISTORY_LIMIT,
) -> tuple[int, str | None]:
    """Import historical blobs for ``path`` from git. Returns (inserted, error)."""
    root = _git_root(path)
    if root is None:
        return 0, None
    rel = _rel_to_git(root, path)
    if rel is None:
        return 0, None
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                "--follow",
                f"-n{limit}",
                "--format=%H%x00%cI",
                "--",
                rel,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 0, f"{path}: git log failed: {exc}"
    if proc.returncode != 0:
        return 0, f"{path}: git log exit {proc.returncode}"

    inserted = 0
    for line in proc.stdout.splitlines():
        if "\x00" not in line:
            continue
        commit, committed_at = line.split("\x00", 1)
        commit = commit.strip()
        committed_at = committed_at.strip() or _utc_now()
        try:
            show = subprocess.run(
                ["git", "-C", str(root), "show", f"{commit}:{rel}"],
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if show.returncode != 0:
            continue
        try:
            text = show.stdout.decode("utf-8", errors="replace")
        except Exception:
            continue
        if _insert_snapshot(
            conn,
            path=path,
            path_kind=path_kind,
            content=text,
            observed_at=committed_at,
            source="git_history",
            git_commit=commit,
            git_committed_at=committed_at,
            meta={"git_root": str(root), "rel_path": rel},
        ):
            inserted += 1
    return inserted, None


def scan_live_path(
    conn: sqlite3.Connection,
    path: Path,
    *,
    path_kind: str,
    now: str | None = None,
) -> str:
    """Snapshot current file contents if the hash is new. Returns status tag."""
    ts = now or _utc_now()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unreadable"
    content_hash = _sha1_text(text)
    latest = conn.execute(
        """
        SELECT content_hash FROM config_snapshots
        WHERE path = ? AND source = 'live_scan'
        ORDER BY observed_at DESC LIMIT 1
        """,
        (str(path),),
    ).fetchone()
    if latest and str(latest["content_hash"]) == content_hash:
        return "unchanged"
    if _insert_snapshot(
        conn,
        path=path,
        path_kind=path_kind,
        content=text,
        observed_at=ts,
        source="live_scan",
        meta={},
    ):
        return "inserted"
    return "duplicate"


def refresh_config_ledger(
    conn: sqlite3.Connection,
    *,
    home: Path | None = None,
    include_git_history: bool = True,
    git_limit: int = GIT_HISTORY_LIMIT,
) -> dict[str, Any]:
    """Scan tracked configs and optionally backfill git history."""
    if not _table_ready(conn):
        return {"error": "config_snapshots table missing; run migrations"}
    stats = LedgerStats()
    now = _utc_now()
    paths = tracked_config_paths(home)
    stats.paths_scanned = len(paths)
    repos_seen: set[Path] = set()

    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    for path, kind in paths:
        if include_git_history:
            root = _git_root(path)
            if root is not None and root not in repos_seen:
                repos_seen.add(root)
            n, err = import_git_history_for_path(
                conn, path, path_kind=kind, limit=git_limit
            )
            stats.git_inserted += n
            if err:
                stats.errors.append(err)
        tag = scan_live_path(conn, path, path_kind=kind, now=now)
        if tag == "inserted":
            stats.live_inserted += 1
        elif tag == "unchanged":
            stats.skipped_unchanged += 1
        elif tag == "unreadable":
            stats.errors.append(f"{path}: unreadable")
    stats.git_repos = len(repos_seen)
    return stats.to_dict()


def list_snapshots(
    conn: sqlite3.Connection,
    *,
    path: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if not _table_ready(conn):
        return []
    if path:
        rows = conn.execute(
            """
            SELECT id, path, path_kind, content_hash, content_bytes,
                   observed_at, source, git_commit, git_committed_at
            FROM config_snapshots
            WHERE path = ?
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (path, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, path, path_kind, content_hash, content_bytes,
                   observed_at, source, git_commit, git_committed_at
            FROM config_snapshots
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def ledger_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_ready(conn):
        return {"paths": 0, "snapshots": 0, "by_source": {}, "oldest": None, "newest": None}
    total = conn.execute("SELECT COUNT(*) AS c FROM config_snapshots").fetchone()
    paths = conn.execute(
        "SELECT COUNT(DISTINCT path) AS c FROM config_snapshots"
    ).fetchone()
    by_source = {
        str(r["source"]): int(r["c"])
        for r in conn.execute(
            "SELECT source, COUNT(*) AS c FROM config_snapshots GROUP BY source"
        )
    }
    bounds = conn.execute(
        """
        SELECT MIN(observed_at) AS oldest, MAX(observed_at) AS newest
        FROM config_snapshots
        """
    ).fetchone()
    return {
        "paths": int(paths["c"]) if paths else 0,
        "snapshots": int(total["c"]) if total else 0,
        "by_source": by_source,
        "oldest": bounds["oldest"] if bounds else None,
        "newest": bounds["newest"] if bounds else None,
    }


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def proposal_correspondence(
    conn: sqlite3.Connection,
    proposal: Any,
) -> dict[str, Any]:
    """Observed config-file correspondence for an accepted proposal.

    Reports whether a matching (or any) change appeared after the decision.
    Association only — never causation. Abstains when evidence is thin.
    """
    base = {
        "status": "n/a",
        "language": "correspondence is only reported for accepted proposals",
        "path": proposal.target_path,
        "decided_at": proposal.decided_at,
        "matching_snapshot": None,
        "first_change_after": None,
        "does_not_prove": (
            "A matching snapshot after acceptance does not prove this proposal "
            "caused the edit; the owner may have written something equivalent "
            "for unrelated reasons."
        ),
    }
    if proposal.status != "accepted":
        return base
    if not _table_ready(conn):
        return {
            **base,
            "status": "unavailable",
            "language": "config_snapshots table not present",
        }

    decided = _parse_ts(proposal.decided_at)
    proposed_hash = (
        _sha1_text(proposal.proposed_content)
        if proposal.proposed_content is not None
        else None
    )
    rows = conn.execute(
        """
        SELECT id, content_hash, observed_at, source, git_commit
        FROM config_snapshots
        WHERE path = ?
        ORDER BY observed_at ASC
        """,
        (proposal.target_path,),
    ).fetchall()

    after: list[sqlite3.Row] = []
    matching: sqlite3.Row | None = None
    for r in rows:
        obs = _parse_ts(str(r["observed_at"]))
        if decided is not None and obs is not None and obs < decided:
            continue
        after.append(r)
        if proposed_hash and str(r["content_hash"]) == proposed_hash and matching is None:
            matching = r

    # Live file check (read-only) for current match.
    live_match = False
    live_hash = None
    path = Path(proposal.target_path)
    if path.is_file() and proposed_hash:
        try:
            live_hash = _sha1_text(path.read_text(encoding="utf-8", errors="replace"))
            live_match = live_hash == proposed_hash
        except OSError:
            pass

    if matching is None and live_match:
        status = "observed_match_live"
        language = (
            "current file content hash matches the proposed content; "
            "no dated ledger snapshot of that hash after acceptance yet"
        )
    elif matching is not None:
        status = "observed_match"
        language = (
            f"a config snapshot after acceptance matches the proposed content "
            f"(observed_at={matching['observed_at']}, source={matching['source']})"
        )
    elif after:
        # Changed but not to the exact proposed bytes.
        first = after[0]
        changed_from_base = bool(
            proposal.base_content_hash
            and any(
                str(r["content_hash"]) != proposal.base_content_hash for r in after
            )
        )
        if changed_from_base or (
            proposal.base_content_hash and live_hash and live_hash != proposal.base_content_hash
        ):
            status = "observed_change_other"
            language = (
                "the target file changed after acceptance, but not to the "
                "exact proposed content hash"
            )
        else:
            status = "no_match_yet"
            language = "snapshots exist after acceptance but none match the proposal"
    else:
        # Time gate: abstain if acceptance is too recent for a fair look.
        if decided is not None:
            age = datetime.now(timezone.utc) - decided.astimezone(timezone.utc)
            if age < timedelta(hours=MIN_POST_ACCEPT_HOURS):
                return {
                    **base,
                    "status": "abstain",
                    "language": (
                        f"fewer than {MIN_POST_ACCEPT_HOURS}h since acceptance; "
                        "too early to judge correspondence"
                    ),
                    "matching_snapshot": None,
                    "first_change_after": None,
                }
        status = "no_match_yet"
        language = "no config snapshot after acceptance matches the proposal yet"

    return {
        **base,
        "status": status,
        "language": language,
        "matching_snapshot": (
            {
                "id": matching["id"],
                "observed_at": matching["observed_at"],
                "source": matching["source"],
                "git_commit": matching["git_commit"],
                "content_hash": matching["content_hash"],
            }
            if matching is not None
            else None
        ),
        "first_change_after": (
            {
                "id": after[0]["id"],
                "observed_at": after[0]["observed_at"],
                "source": after[0]["source"],
                "content_hash": after[0]["content_hash"],
            }
            if after
            else None
        ),
        "live_matches_proposed": live_match,
        "proposed_content_hash": proposed_hash,
        "snapshots_after_decision": len(after),
    }


def find_supersession_cycles(conn: sqlite3.Connection) -> list[list[str]]:
    """Return cycles in claims.supersedes_id, including self-loops."""
    rows = conn.execute(
        "SELECT id, supersedes_id FROM claims WHERE supersedes_id IS NOT NULL"
    ).fetchall()
    edges = {str(r["id"]): str(r["supersedes_id"]) for r in rows}
    cycles: list[list[str]] = []
    seen_global: set[str] = set()
    for start in edges:
        if start in seen_global:
            continue
        path: list[str] = []
        node: str | None = start
        index: dict[str, int] = {}
        while node is not None and node in edges:
            if node in index:
                cycles.append(path[index[node] :] + [node])
                break
            if node in seen_global:
                break
            index[node] = len(path)
            path.append(node)
            seen_global.add(node)
            node = edges.get(node)
    return cycles
