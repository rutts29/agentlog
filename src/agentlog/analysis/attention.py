"""Deterministic Attention Inbox derivations.

Answers: what needs me right now, and what did I abandon that I might resume?
Computes on demand from sessions / messages / tool_events plus live presence.
No new tables. Every item carries a plain-English evidence sentence.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from agentlog.config import DEFAULT_DB_PATH, presence_path_for_db
from agentlog.watch.presence import read_presence_file
from agentlog.analysis.attention_signals import (
    assistant_asks_question,
    final_paragraph,
    incomplete_todo_in_text,
)

AttentionState = Literal[
    "live_waiting",
    "live_error",
    "waiting_on_user",
    "error_streak",
    "open_task",
    "long_running",
    "resumable",
]
Severity = Literal["warn", "info"]
Lane = Literal["urgent", "resumable"]

_STATE_RANK = {
    "live_waiting": 0,
    "live_error": 1,
    "waiting_on_user": 2,
    "error_streak": 3,
    "open_task": 4,
    "long_running": 5,
    "resumable": 6,
}
_SEVERITY_RANK = {"warn": 0, "info": 1}
_PLAN_TOOLS = frozenset({"TodoWrite", "update_plan"})
_UNVERIFIED_SOURCE_STATUSES = frozenset(
    {
        "source_changed",
        "source_unavailable",
        "frozen_diverged",
        "frozen_shrunk",
        "frozen_parser_upgrade",
    }
)
# Cursor/Codex path slugs that are not a real project identity for supersession.
_AMBIGUOUS_REPOS = frozenset({"empty-window", "unknown", ""})
_UUID_LEAF_RE = re.compile(
    r"(?:subagent:)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AttentionThresholds:
    """Tunable cutoffs for inbox derivation."""

    actionable_hours: float = 48.0
    waiting_hours: float = 2.0
    error_streak_n: int = 3
    long_running_hours: float = 4.0
    active_idle_minutes: float = 30.0
    warn_within_hours: float = 12.0
    resumable_max_days: float = 30.0
    recent_error_minutes: float = 30.0


@dataclass(frozen=True)
class AttentionItem:
    session_id: str
    state: AttentionState
    severity: Severity
    reason: str
    last_activity_at: str | None
    harness: str | None = None
    runtime_harness: str | None = None
    lane: Lane = "urgent"
    repo: str | None = None
    branch: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "severity": self.severity,
            "reason": self.reason,
            "last_activity_at": self.last_activity_at,
            "harness": self.harness,
            "runtime_harness": self.runtime_harness,
            "lane": self.lane,
            "repo": self.repo,
            "branch": self.branch,
        }


@dataclass
class AttentionStats:
    candidates: int = 0
    removed_by_horizon: int = 0
    removed_by_dedup: int = 0
    removed_by_resolution: int = 0
    kept_urgent: int = 0
    kept_resumable: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "candidates": self.candidates,
            "removed_by_horizon": self.removed_by_horizon,
            "removed_by_dedup": self.removed_by_dedup,
            "removed_by_resolution": self.removed_by_resolution,
            "kept_urgent": self.kept_urgent,
            "kept_resumable": self.kept_resumable,
        }


@dataclass
class _Candidate:
    session_id: str
    state: AttentionState
    severity: Severity
    reason: str
    last_activity_at: datetime | None
    harness: str | None
    lane: Lane
    runtime_harness: str | None = None
    repo: str | None = None
    branch: str | None = None
    rank: int = field(init=False)

    def __post_init__(self) -> None:
        self.rank = _STATE_RANK.get(self.state, 99)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _clip(text: str, limit: int = 100) -> str:
    """Clip to a readable excerpt; break on a word boundary when possible."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[: limit - 1]
    sp = cut.rfind(" ")
    if sp >= int(limit * 0.55):
        cut = cut[:sp]
    return cut.rstrip(".,;: ") + "…"


def _db_path_from_conn(conn: sqlite3.Connection) -> Path:
    try:
        rows = list(conn.execute("PRAGMA database_list"))
    except sqlite3.Error:
        return DEFAULT_DB_PATH
    for row in rows:
        # PRAGMA database_list: seq, name, file
        name = row[1] if not isinstance(row, sqlite3.Row) else row["name"]
        filename = row[2] if not isinstance(row, sqlite3.Row) else row["file"]
        if name == "main" and filename:
            return Path(str(filename))
    return DEFAULT_DB_PATH


def _idle_hours(now: datetime, last_at: datetime | None) -> float | None:
    if last_at is None:
        return None
    return max(0.0, (now - last_at).total_seconds() / 3600.0)


def _severity_for_idle(
    idle_h: float | None,
    thresholds: AttentionThresholds,
    *,
    force_warn: bool = False,
) -> Severity:
    if force_warn:
        return "warn"
    if idle_h is None:
        return "info"
    if idle_h <= thresholds.warn_within_hours:
        return "warn"
    return "info"


def _session_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT
                s.id AS id,
                s.id AS session_id,
                s.harness AS harness,
                s.started_at AS started_at,
                s.ended_at AS ended_at,
                s.repo AS repo,
                s.branch AS branch,
                s.transcript_storage AS transcript_storage,
                s.source_sync_status AS source_sync_status,
                s.attention_final_question AS attention_final_question,
                s.attention_incomplete_todo AS attention_incomplete_todo,
                s.attention_last_plan_open AS attention_last_plan_open,
                s.attention_tail_revision AS attention_tail_revision,
                (
                    SELECT m.role FROM messages m
                    WHERE m.session_id = s.id
                      AND COALESCE(m.is_tool_plumbing, 0) = 0
                    ORDER BY m.seq DESC
                    LIMIT 1
                ) AS last_role,
                (
                    SELECT m.text FROM messages m
                    WHERE m.session_id = s.id
                      AND COALESCE(m.is_tool_plumbing, 0) = 0
                    ORDER BY m.seq DESC
                    LIMIT 1
                ) AS last_text,
                (
                    SELECT m.timestamp FROM messages m
                    WHERE m.session_id = s.id
                    ORDER BY m.seq DESC
                    LIMIT 1
                ) AS last_message_at,
                (
                    SELECT t.tool_name FROM tool_events t
                    WHERE t.session_id = s.id
                    ORDER BY t.seq DESC
                    LIMIT 1
                ) AS last_tool_name
            FROM sessions s
            """
        )
    )


def _logical_attention_rows(
    conn: sqlite3.Connection,
    rows: list[Any],
) -> tuple[
    list[dict[str, Any]],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    """Project physical sessions onto the inbox's logical navigation grain."""
    from agentlog.api.identity_aggregates import visible_logical_sessions

    visible = visible_logical_sessions(conn, rows)
    physical = {str(row["id"]): row for row in rows}
    projected: list[dict[str, Any]] = []
    display_by_physical: dict[str, str] = {}
    harness_by_display: dict[str, str] = {}
    runtime_harness_by_display: dict[str, str] = {}
    metric_by_display: dict[str, str] = {}
    for item in visible:
        metric_id = item.metric_session_id
        metric_row = physical.get(metric_id) or physical.get(item.session_id)
        if metric_row is None:
            continue
        display_id = item.session_id
        row = dict(metric_row)
        row["id"] = display_id
        row["session_id"] = display_id
        row["harness"] = item.logical_harness
        row["runtime_harness"] = item.runtime_harness
        row["metric_session_id"] = metric_id
        projected.append(row)
        display_by_physical[item.session_id] = display_id
        display_by_physical[metric_id] = display_id
        harness_by_display[display_id] = item.logical_harness
        runtime_harness_by_display[display_id] = item.runtime_harness
        metric_by_display[display_id] = metric_id
    return (
        projected,
        display_by_physical,
        harness_by_display,
        runtime_harness_by_display,
        metric_by_display,
    )


def _trailing_known_error_streak(
    conn: sqlite3.Connection, session_id: str
) -> list[str]:
    """Trailing consecutive failures among tool events with known success.

    ``success`` is NULL for most Codex/Cursor rows and for Claude ``call``
    rows. Only evaluate rows where adapters recorded a boolean outcome
    (today: Claude ``tool_result`` with ``is_error`` present).
    """
    rows = list(
        conn.execute(
            """
            SELECT tool_name, success
            FROM tool_events
            WHERE session_id = ?
              AND success IS NOT NULL
            ORDER BY seq DESC
            LIMIT 20
            """,
            (session_id,),
        )
    )
    streak: list[str] = []
    for row in rows:
        if int(row["success"]) == 0:
            streak.append(str(row["tool_name"]))
            continue
        break
    return streak


def _recent_known_failures(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    since: datetime,
) -> list[str]:
    rows = list(
        conn.execute(
            """
            SELECT t.tool_name, m.timestamp
            FROM tool_events t
            LEFT JOIN messages m ON m.id = t.message_id
            WHERE t.session_id = ?
              AND t.success = 0
            ORDER BY t.seq DESC
            LIMIT 20
            """,
            (session_id,),
        )
    )
    names: list[str] = []
    for row in rows:
        ts = _parse_ts(row["timestamp"])
        if ts is not None and ts < since:
            continue
        names.append(str(row["tool_name"]))
    return names


def _last_activity(row: sqlite3.Row) -> datetime | None:
    candidates = [
        _parse_ts(row["last_message_at"]),
        _parse_ts(row["ended_at"]),
        _parse_ts(row["started_at"]),
    ]
    present = [c for c in candidates if c is not None]
    return max(present) if present else None


def _source_tail_is_unverified(row: dict[str, Any]) -> bool:
    return (
        row.get("transcript_storage") == "source_backed"
        and row.get("source_sync_status") in _UNVERIFIED_SOURCE_STATUSES
    )


def _tail_signal_coverage(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    thresholds: AttentionThresholds,
) -> dict[str, int | bool]:
    physical_rows = _session_rows(conn)
    rows, *_ = _logical_attention_rows(conn, physical_rows)
    resumable_max = timedelta(days=thresholds.resumable_max_days)
    eligible_sessions = 0
    covered_sessions = 0
    ignored_sessions = 0

    for row in rows:
        if row.get("transcript_storage") != "source_backed":
            continue
        last_at = _last_activity(row)
        if last_at is None or now - last_at > resumable_max:
            continue
        if _source_tail_is_unverified(row):
            ignored_sessions += 1
            continue
        eligible_sessions += 1
        if row.get("attention_tail_revision") == 1:
            covered_sessions += 1

    return {
        "eligible_sessions": eligible_sessions,
        "covered_sessions": covered_sessions,
        "missing_sessions": eligible_sessions - covered_sessions,
        "ignored_sessions": ignored_sessions,
        "complete": covered_sessions == eligible_sessions,
    }


def _has_open_plan(row: sqlite3.Row) -> bool:
    return row["last_tool_name"] in _PLAN_TOOLS


def _scope_key(repo: str | None, branch: str | None) -> tuple[str, str] | None:
    if not repo or repo in _AMBIGUOUS_REPOS:
        return None
    return (repo, branch or "")


def _mirror_key(session_id: str, harness: str | None) -> str | None:
    """Collapse Cursor path mirrors that share one composer UUID."""
    if harness != "cursor":
        return None
    leaf = session_id.rsplit("/", 1)[-1]
    match = _UUID_LEAF_RE.search(leaf)
    if match is None:
        return None
    return f"cursor-uuid:{match.group(1).lower()}"


def _build_successor_index(
    rows: list[sqlite3.Row],
) -> dict[tuple[str, str], list[tuple[datetime, str]]]:
    """Map (repo, branch) → [(started_at, session_id), ...] sorted ascending."""
    index: dict[tuple[str, str], list[tuple[datetime, str]]] = {}
    for row in rows:
        key = _scope_key(row["repo"], row["branch"])
        if key is None:
            continue
        started = _parse_ts(row["started_at"])
        if started is None:
            continue
        index.setdefault(key, []).append((started, str(row["session_id"])))
    for key in index:
        index[key].sort()
    return index


def _is_superseded(
    session_id: str,
    last_at: datetime | None,
    repo: str | None,
    branch: str | None,
    successors: dict[tuple[str, str], list[tuple[datetime, str]]],
) -> bool:
    """True when a later session in the same repo/branch continued the work."""
    if last_at is None:
        return False
    key = _scope_key(repo, branch)
    if key is None:
        return False
    for started, other_id in successors.get(key, []):
        if other_id == session_id:
            continue
        if started > last_at:
            return True
    return False


def _load_presence_by_session(
    conn: sqlite3.Connection,
    *,
    presence_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    path = presence_path or presence_path_for_db(_db_path_from_conn(conn))
    data = read_presence_file(path)
    out: dict[str, dict[str, Any]] = {}
    for raw in data.get("sessions") or []:
        if not isinstance(raw, dict):
            continue
        sid = raw.get("session_id")
        if not sid:
            harness = raw.get("harness")
            external_id = raw.get("external_id")
            if harness and external_id:
                sid = f"{harness}:{external_id}"
        if not sid:
            continue
        out[str(sid)] = raw
    return out


def _logical_presence(
    raw_presence: dict[str, dict[str, Any]],
    display_by_physical: dict[str, str],
    harness_by_display: dict[str, str],
    runtime_harness_by_display: dict[str, str],
) -> dict[str, dict[str, Any]]:
    state_rank = {
        "waiting": 3,
        "tool_running": 2,
        "streaming": 1,
    }
    out: dict[str, dict[str, Any]] = {}
    for physical_id, raw in raw_presence.items():
        display_id = display_by_physical.get(physical_id, physical_id)
        entry = dict(raw)
        entry["session_id"] = display_id
        logical_harness = harness_by_display.get(display_id)
        if logical_harness:
            entry["harness"] = logical_harness
        runtime_harness = runtime_harness_by_display.get(display_id)
        if runtime_harness:
            entry["runtime_harness"] = runtime_harness
        previous = out.get(display_id)
        if previous is None:
            out[display_id] = entry
            continue
        old_state = state_rank.get(str(previous.get("state") or ""), 0)
        new_state = state_rank.get(str(entry.get("state") or ""), 0)
        old_at = _parse_ts(previous.get("last_activity_at"))
        new_at = _parse_ts(entry.get("last_activity_at"))
        if (new_state, new_at or datetime.min.replace(tzinfo=timezone.utc)) > (
            old_state,
            old_at or datetime.min.replace(tzinfo=timezone.utc),
        ):
            out[display_id] = entry
    return out


def _candidate_to_item(c: _Candidate) -> AttentionItem:
    return AttentionItem(
        session_id=c.session_id,
        state=c.state,
        severity=c.severity,
        reason=c.reason,
        last_activity_at=c.last_activity_at.isoformat() if c.last_activity_at else None,
        harness=c.harness,
        runtime_harness=c.runtime_harness,
        lane=c.lane,
        repo=c.repo,
        branch=c.branch,
    )


def _sort_items(items: list[AttentionItem]) -> list[AttentionItem]:
    return sorted(
        items,
        key=lambda i: (
            0 if i.lane == "urgent" else 1,
            _STATE_RANK.get(i.state, 9),
            _SEVERITY_RANK.get(i.severity, 9),
            -(
                _parse_ts(i.last_activity_at).timestamp()
                if _parse_ts(i.last_activity_at)
                else 0.0
            ),
            i.session_id,
        ),
    )


def derive_attention(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    thresholds: AttentionThresholds | None = None,
    state: AttentionState | None = None,
    include_resumable: bool = False,
    presence_path: Path | None = None,
    return_stats: bool = False,
) -> list[AttentionItem] | tuple[list[AttentionItem], AttentionStats]:
    """Derive attention items.

    By default returns the urgent lane only (MCP / brief callers). Pass
    ``include_resumable=True`` for dormant resume candidates, or use
    ``attention_payload`` for the full inbox response with stats.
    """
    thresholds = thresholds or AttentionThresholds()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    actionable = timedelta(hours=thresholds.actionable_hours)
    waiting_delta = timedelta(hours=thresholds.waiting_hours)
    long_delta = timedelta(hours=thresholds.long_running_hours)
    active_idle = timedelta(minutes=thresholds.active_idle_minutes)
    resumable_max = timedelta(days=thresholds.resumable_max_days)
    recent_error_delta = timedelta(minutes=thresholds.recent_error_minutes)

    physical_rows = _session_rows(conn)
    (
        rows,
        display_by_physical,
        harness_by_display,
        runtime_harness_by_display,
        metric_by_display,
    ) = _logical_attention_rows(conn, physical_rows)
    successors = _build_successor_index(rows)
    presence = _logical_presence(
        _load_presence_by_session(conn, presence_path=presence_path),
        display_by_physical,
        harness_by_display,
        runtime_harness_by_display,
    )

    stats = AttentionStats()
    per_session: dict[str, list[_Candidate]] = {}

    def add(c: _Candidate) -> None:
        stats.candidates += 1
        per_session.setdefault(c.session_id, []).append(c)

    # Live presence signals (highest priority).
    for sid, entry in presence.items():
        pstate = str(entry.get("state") or "unknown")
        harness = harness_by_display.get(sid) or entry.get("harness")
        runtime_harness = runtime_harness_by_display.get(sid) or entry.get(
            "runtime_harness"
        )
        metric_session_id = metric_by_display.get(sid, sid)
        last_at = _parse_ts(entry.get("last_activity_at")) or now
        repo = entry.get("repo")
        if pstate == "waiting":
            age = entry.get("age_seconds")
            age_txt = (
                f"{float(age):.0f}s ago"
                if isinstance(age, (int, float))
                else "just now"
            )
            add(
                _Candidate(
                    session_id=sid,
                    state="live_waiting",
                    severity="warn",
                    reason=(
                        f"Live session is waiting on you "
                        f"(presence age {age_txt})."
                    ),
                    last_activity_at=last_at,
                    harness=str(harness) if harness else None,
                    runtime_harness=(
                        str(runtime_harness) if runtime_harness else None
                    ),
                    lane="urgent",
                    repo=str(repo) if repo else None,
                )
            )
        recent_fails = _recent_known_failures(
            conn, metric_session_id, since=now - recent_error_delta
        )
        if recent_fails and pstate in {"streaming", "tool_running", "waiting", "unknown"}:
            names = ", ".join(recent_fails[:3])
            add(
                _Candidate(
                    session_id=sid,
                    state="live_error",
                    severity="warn",
                    reason=(
                        f"Live session has {len(recent_fails)} recent tool "
                        f"failure(s) ({names})."
                    ),
                    last_activity_at=last_at,
                    harness=str(harness) if harness else None,
                    runtime_harness=(
                        str(runtime_harness) if runtime_harness else None
                    ),
                    lane="urgent",
                    repo=str(repo) if repo else None,
                )
            )

    for row in rows:
        session_id = str(row["session_id"])
        metric_session_id = str(row.get("metric_session_id") or session_id)
        harness = row["harness"]
        runtime_harness = row.get("runtime_harness")
        repo = row["repo"]
        branch = row["branch"]
        last_at = _last_activity(row)
        idle = (now - last_at) if last_at is not None else None
        idle_h = _idle_hours(now, last_at)
        last_role = row["last_role"]
        last_text = row["last_text"] or ""
        source_unverified = _source_tail_is_unverified(row)
        source_tail_current = row.get("attention_tail_revision") == 1
        if (
            row.get("transcript_storage") == "source_backed"
            and source_tail_current
            and not source_unverified
        ):
            asks = bool(row.get("attention_final_question"))
            incomplete_todo = bool(row.get("attention_incomplete_todo"))
        elif row.get("transcript_storage") == "source_backed":
            # Source-backed text stays at its canonical source. Without a
            # trusted compact signal, skip text-derived states for this row.
            asks = False
            incomplete_todo = False
        else:
            asks = last_role == "assistant" and assistant_asks_question(last_text)
            incomplete_todo = last_role == "assistant" and incomplete_todo_in_text(
                last_text
            )
        if row.get("transcript_storage") == "source_backed":
            open_plan = (
                source_tail_current
                and not source_unverified
                and bool(row.get("attention_last_plan_open"))
            )
        else:
            open_plan = _has_open_plan(row) and last_role != "user"
        open_task = incomplete_todo or open_plan
        resolved_by_reply = last_role == "user"
        superseded = _is_superseded(
            session_id, last_at, repo, branch, successors
        )

        # Transcript waiting / open-task signals.
        if asks or open_task:
            if resolved_by_reply or superseded:
                stats.candidates += 1
                stats.removed_by_resolution += 1
            elif idle is None:
                pass
            elif idle > resumable_max:
                stats.candidates += 1
                stats.removed_by_horizon += 1
            elif idle >= actionable:
                # Dormant: abandoned, not urgent.
                if asks:
                    excerpt = _clip(final_paragraph(last_text))
                    evidence = (
                        f"last turn still asks: {excerpt!r}"
                        if last_text
                        else "last turn still asks a question"
                    )
                elif incomplete_todo:
                    evidence = "last message still lists incomplete todos"
                else:
                    tool = row["last_tool_name"] or "plan tool"
                    evidence = f"ended on open {tool}"
                add(
                    _Candidate(
                        session_id=session_id,
                        state="resumable",
                        severity="info",
                        reason=(
                            f"Abandoned {idle_h:.1f}h ago ({evidence}). "
                            f"Not urgent — resume only if you still care."
                        ),
                        last_activity_at=last_at,
                        harness=harness,
                        runtime_harness=runtime_harness,
                        lane="resumable",
                        repo=repo,
                        branch=branch,
                    )
                )
            else:
                # Within actionable horizon.
                if asks and idle >= waiting_delta:
                    excerpt = _clip(final_paragraph(last_text))
                    waiting_reason = (
                        f"Assistant is waiting on your answer "
                        f"({idle_h:.1f}h idle): {excerpt!r}"
                        if excerpt
                        else f"Assistant is waiting on your answer ({idle_h:.1f}h idle)."
                    )
                    add(
                        _Candidate(
                            session_id=session_id,
                            state="waiting_on_user",
                            severity=_severity_for_idle(idle_h, thresholds),
                            reason=waiting_reason,
                            last_activity_at=last_at,
                            harness=harness,
                            runtime_harness=runtime_harness,
                            lane="urgent",
                            repo=repo,
                            branch=branch,
                        )
                    )
                if open_task and idle >= waiting_delta:
                    if incomplete_todo:
                        evidence = "incomplete todos remain in the last assistant message"
                    else:
                        tool = row["last_tool_name"] or "plan tool"
                        evidence = (
                            f"session ends on open {tool} with no later user reply"
                        )
                    add(
                        _Candidate(
                            session_id=session_id,
                            state="open_task",
                            severity=_severity_for_idle(idle_h, thresholds),
                            reason=(
                                f"Unfinished work from {idle_h:.1f}h ago: "
                                f"{evidence}."
                            ),
                            last_activity_at=last_at,
                            harness=harness,
                            runtime_harness=runtime_harness,
                            lane="urgent",
                            repo=repo,
                            branch=branch,
                        )
                    )

        streak = _trailing_known_error_streak(conn, metric_session_id)
        if len(streak) >= thresholds.error_streak_n:
            if superseded:
                stats.candidates += 1
                stats.removed_by_resolution += 1
            elif idle is not None and idle > actionable:
                stats.candidates += 1
                stats.removed_by_horizon += 1
            else:
                names = ", ".join(streak[: thresholds.error_streak_n])
                add(
                    _Candidate(
                        session_id=session_id,
                        state="error_streak",
                        severity=_severity_for_idle(
                            idle_h, thresholds, force_warn=True
                        ),
                        reason=(
                            f"Session ends with {len(streak)} consecutive "
                            f"failed tool results ({names})."
                        ),
                        last_activity_at=last_at,
                        harness=harness,
                        runtime_harness=runtime_harness,
                        lane="urgent",
                        repo=repo,
                        branch=branch,
                    )
                )

        started = _parse_ts(row["started_at"])
        if (
            started is not None
            and last_at is not None
            and idle is not None
            and idle <= active_idle
        ):
            duration = last_at - started
            # Zombie composers that started weeks ago are not "long running".
            if duration >= long_delta and (now - started) <= actionable:
                hours = duration.total_seconds() / 3600.0
                add(
                    _Candidate(
                        session_id=session_id,
                        state="long_running",
                        severity="info",
                        reason=(
                            f"Active session has been running for "
                            f"{hours:.1f}h (started {started.isoformat()})."
                        ),
                        last_activity_at=last_at,
                        harness=harness,
                        runtime_harness=runtime_harness,
                        lane="urgent",
                        repo=repo,
                        branch=branch,
                    )
                )

    def _pick_best(cands: list[_Candidate]) -> _Candidate:
        urgent = [c for c in cands if c.lane == "urgent"]
        pool = urgent or cands
        return min(
            pool,
            key=lambda c: (
                0 if c.lane == "urgent" else 1,
                c.rank,
                _SEVERITY_RANK.get(c.severity, 9),
                -(c.last_activity_at.timestamp() if c.last_activity_at else 0.0),
                c.session_id,
            ),
        )

    # Dedup: one item per session, strongest state wins.
    chosen: list[_Candidate] = []
    for sid, cands in per_session.items():
        if not cands:
            continue
        stats.removed_by_dedup += max(0, len(cands) - 1)
        chosen.append(_pick_best(cands))

    # Second pass: collapse Cursor composer mirrors (same UUID, different path).
    by_mirror: dict[str, list[_Candidate]] = {}
    unmatched: list[_Candidate] = []
    for cand in chosen:
        key = _mirror_key(cand.session_id, cand.harness)
        if key is None:
            unmatched.append(cand)
            continue
        by_mirror.setdefault(key, []).append(cand)
    collapsed: list[_Candidate] = list(unmatched)
    for group in by_mirror.values():
        if len(group) > 1:
            stats.removed_by_dedup += len(group) - 1
        collapsed.append(_pick_best(group))
    chosen = collapsed

    items = [_candidate_to_item(c) for c in chosen]
    urgent_items = [i for i in items if i.lane == "urgent"]
    resumable_items = [i for i in items if i.lane == "resumable"]
    stats.kept_urgent = len(urgent_items)
    stats.kept_resumable = len(resumable_items)

    if include_resumable or state == "resumable":
        out = _sort_items(urgent_items + resumable_items)
    else:
        out = _sort_items(urgent_items)

    if state is not None:
        out = [i for i in out if i.state == state]

    if return_stats:
        return out, stats
    return out


def attention_payload(
    conn: sqlite3.Connection,
    *,
    state: AttentionState | None = None,
    now: datetime | None = None,
    thresholds: AttentionThresholds | None = None,
    presence_path: Path | None = None,
    resumable_limit: int = 20,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    thresholds = thresholds or AttentionThresholds()
    tail_signal_coverage = _tail_signal_coverage(
        conn, now=now, thresholds=thresholds
    )
    result = derive_attention(
        conn,
        now=now,
        thresholds=thresholds,
        state=state,
        include_resumable=True,
        presence_path=presence_path,
        return_stats=True,
    )
    assert isinstance(result, tuple)
    items, stats = result
    if state is not None:
        # Filtered view: put matches in ``items`` regardless of lane.
        return {
            "generated_at": now.isoformat(),
            "count": len(items),
            "items": [i.to_dict() for i in items],
            "resumable_count": stats.kept_resumable,
            "resumable": [],
            "stats": stats.to_dict(),
            "tail_signal_coverage": tail_signal_coverage,
            "thresholds": {
                "actionable_hours": thresholds.actionable_hours,
                "waiting_hours": thresholds.waiting_hours,
                "error_streak_n": thresholds.error_streak_n,
                "long_running_hours": thresholds.long_running_hours,
                "warn_within_hours": thresholds.warn_within_hours,
                "resumable_max_days": thresholds.resumable_max_days,
            },
        }

    urgent = [i for i in items if i.lane == "urgent"]
    resumable_all = [i for i in items if i.lane == "resumable"]
    resumable = resumable_all[:resumable_limit]
    return {
        "generated_at": now.isoformat(),
        "count": len(urgent),
        "items": [i.to_dict() for i in urgent],
        "resumable_count": len(resumable_all),
        "resumable": [i.to_dict() for i in resumable],
        "stats": stats.to_dict(),
        "tail_signal_coverage": tail_signal_coverage,
        "thresholds": {
            "actionable_hours": thresholds.actionable_hours,
            "waiting_hours": thresholds.waiting_hours,
            "error_streak_n": thresholds.error_streak_n,
            "long_running_hours": thresholds.long_running_hours,
            "warn_within_hours": thresholds.warn_within_hours,
            "resumable_max_days": thresholds.resumable_max_days,
        },
    }
