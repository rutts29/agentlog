"""Deterministic session briefs for structured handoffs.

Assembles compact context packages from evidence already in the DB.
No LLM calls. Parent/child links use sessions.parent_session_id;
cross-harness links are inferred on demand and labeled separately.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from agentlog.analysis.attention import derive_attention
from agentlog.analysis.attention_signals import (
    incomplete_todo_in_text as _incomplete_todo_in_text,
)
from agentlog.session_identity import (
    explicit_worker_parent_ids,
    implicit_parent_ids,
    is_suppressed_activity_session,
)
from agentlog.source_reader import CachedSourceTranscriptReader

# Soft budget for rendered Markdown (characters).
MARKDOWN_BUDGET = 2048
INFERRED_WINDOW_HOURS = 4.0
MAX_CHILDREN = 8
MAX_INFERRED = 5
MAX_TODOS = 5
MAX_SKILLS = 8
MAX_COMMITS = 5
MAX_MODELS = 4

_CLIP_SHORT = 80
_CLIP_MED = 160
_CLIP_LONG = 220

_USER_QUERY_RE = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>",
    re.DOTALL | re.IGNORECASE,
)
_TIMESTAMP_TAG_RE = re.compile(
    r"<timestamp>.*?</timestamp>\s*",
    re.DOTALL | re.IGNORECASE,
)
_TODO_LINE_RE = re.compile(
    r"^\s*[-*]\s+\[\s\]\s+(.+)$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class InferredLink:
    session_id: str
    harness: str
    direction: str  # predecessor | successor | overlapping
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "harness": self.harness,
            "kind": "inferred",
            "direction": self.direction,
            "evidence": self.evidence,
        }


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


def _clip(text: str | None, limit: int = _CLIP_MED) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "…"


def _humanize_user_text(text: str | None) -> str:
    raw = text or ""
    m = _USER_QUERY_RE.search(raw)
    if m:
        raw = m.group(1)
    raw = _TIMESTAMP_TAG_RE.sub("", raw)
    return raw.strip()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def resolve_session(
    conn: sqlite3.Connection, session_id: str
) -> sqlite3.Row | None:
    def visible(row: sqlite3.Row | None) -> sqlite3.Row | None:
        return None if row is not None and is_suppressed_activity_session(row) else row

    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is not None:
        return visible(row)
    if ":" not in session_id:
        for prefix in ("codex:", "claude:", "cursor:", "warp:", "hermes:", "grok:"):
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (prefix + session_id,),
            ).fetchone()
            if row is not None:
                return visible(row)
    return visible(conn.execute(
        """
        SELECT * FROM sessions
        WHERE external_id = ?
           OR id LIKE '%' || ?
        ORDER BY CASE WHEN external_id = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (session_id, session_id, session_id),
    ).fetchone())


def _parent_keys(row: sqlite3.Row) -> tuple[str, ...]:
    return (
        str(row["id"]),
        str(row["external_id"]),
        f"{row['harness']}:{row['external_id']}",
    )


def _hydrated_message_text(
    conn: sqlite3.Connection,
    session_id: str,
    reader: CachedSourceTranscriptReader,
) -> dict[str, str]:
    row = conn.execute(
        "SELECT transcript_storage FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None or row["transcript_storage"] != "source_backed":
        return {}
    source = reader(conn, session_id)
    if not source.ready:
        raise RuntimeError(
            f"canonical source unavailable for {session_id}: "
            f"{source.warning or source.status}"
        )
    return {str(message["id"]): str(message["text"]) for message in source.messages}


def _first_user_text(
    conn: sqlite3.Connection,
    session_id: str,
    reader: CachedSourceTranscriptReader,
) -> str | None:
    row = conn.execute(
        """
        SELECT id, text FROM messages
        WHERE session_id = ?
          AND role = 'user'
          AND COALESCE(is_tool_plumbing, 0) = 0
          AND COALESCE(authored_by_agent, 0) = 0
          AND TRIM(text) != ''
        ORDER BY seq ASC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return _hydrated_message_text(conn, session_id, reader).get(
        str(row["id"]), str(row["text"] or "")
    )


def _last_role_text(
    conn: sqlite3.Connection, session_id: str, role: str,
    reader: CachedSourceTranscriptReader,
) -> str | None:
    row = conn.execute(
        """
        SELECT id, text FROM messages
        WHERE session_id = ?
          AND role = ?
          AND COALESCE(is_tool_plumbing, 0) = 0
          AND COALESCE(authored_by_agent, 0) = 0
          AND TRIM(text) != ''
        ORDER BY seq DESC
        LIMIT 1
        """,
        (session_id, role),
    ).fetchone()
    if row is None:
        return None
    return _hydrated_message_text(conn, session_id, reader).get(
        str(row["id"]), str(row["text"] or "")
    )


def _message_models(conn: sqlite3.Connection, session_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT model FROM messages
        WHERE session_id = ?
          AND model IS NOT NULL
          AND TRIM(model) != ''
        ORDER BY model
        LIMIT ?
        """,
        (session_id, MAX_MODELS),
    ).fetchall()
    return [str(r["model"]) for r in rows]


def _counts(conn: sqlite3.Connection, session_id: str) -> tuple[int, int]:
    msg = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    tools = conn.execute(
        "SELECT COUNT(*) AS c FROM tool_events WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return (
        int(msg["c"]) if msg else 0,
        int(tools["c"]) if tools else 0,
    )


def _skills(conn: sqlite3.Connection, session_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT skill_name, COUNT(*) AS c
        FROM skill_exposures
        WHERE session_id = ?
        GROUP BY skill_name
        ORDER BY c DESC, skill_name ASC
        LIMIT ?
        """,
        (session_id, MAX_SKILLS),
    ).fetchall()
    return [str(r["skill_name"]) for r in rows]


def _commits(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, "session_commits"):
        return []
    rows = conn.execute(
        """
        SELECT commit_sha, join_method, author_date, subject
        FROM session_commits
        WHERE session_id = ?
        ORDER BY COALESCE(author_date, '') DESC
        LIMIT ?
        """,
        (session_id, MAX_COMMITS),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "sha": str(r["commit_sha"])[:12],
                "join_method": r["join_method"],
                "author_date": r["author_date"],
                "subject": _clip(r["subject"], _CLIP_SHORT),
            }
        )
    return out


def _unresolved_todos(text: str | None) -> list[str]:
    if not text or not _incomplete_todo_in_text(text):
        return []
    found = [_clip(m.group(1), _CLIP_SHORT) for m in _TODO_LINE_RE.finditer(text)]
    if found:
        return found[:MAX_TODOS]
    # JSON-style pending markers without clean markdown lines.
    return ["(incomplete todo markers present)"]


def _duration_seconds(
    started: str | None, ended: str | None, last_msg_at: str | None
) -> int | None:
    start = _parse_ts(started)
    end = _parse_ts(ended) or _parse_ts(last_msg_at)
    if start is None or end is None:
        return None
    secs = int((end - start).total_seconds())
    return max(0, secs)


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    mins = seconds // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    rem = mins % 60
    return f"{hours}h {rem}m" if rem else f"{hours}h"


def _one_line_desc(
    conn: sqlite3.Connection, session_id: str, reader: CachedSourceTranscriptReader
) -> str:
    text = _first_user_text(conn, session_id, reader)
    return _clip(_humanize_user_text(text), _CLIP_SHORT) or "(no user message)"


def _recorded_parent(
    conn: sqlite3.Connection, row: sqlite3.Row, reader: CachedSourceTranscriptReader
) -> dict[str, Any] | None:
    session_id = str(row["id"])
    parent_id = implicit_parent_ids(conn).get(session_id)
    kind = "recorded"
    if parent_id is None:
        parent_id = explicit_worker_parent_ids(conn).get(session_id)
        kind = "observed_worker"
    if parent_id is None:
        return None
    parent = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (parent_id,)
    ).fetchone()
    if parent is None:
        return None
    return {
        "id": str(parent["id"]),
        "harness": parent["harness"],
        "kind": kind,
        "description": _one_line_desc(conn, str(parent["id"]), reader),
    }


def _recorded_children(
    conn: sqlite3.Connection, row: sqlite3.Row, reader: CachedSourceTranscriptReader
) -> tuple[list[dict[str, Any]], int]:
    session_id = str(row["id"])
    parents = implicit_parent_ids(conn)
    explicit = explicit_worker_parent_ids(conn)
    child_ids = {
        child_id
        for child_id, parent_id in {**explicit, **parents}.items()
        if parent_id == session_id
    }
    kids = [
        child
        for child in conn.execute(
            "SELECT id, harness, started_at FROM sessions"
        ).fetchall()
        if str(child["id"]) in child_ids
    ]
    kids.sort(key=lambda child: (str(child["started_at"] or ""), str(child["id"])))
    out: list[dict[str, Any]] = []
    for kid in kids[:MAX_CHILDREN]:
        out.append(
            {
                "id": str(kid["id"]),
                "harness": kid["harness"],
                "kind": "recorded",
                "description": _one_line_desc(conn, str(kid["id"]), reader),
            }
        )
    return out, len(kids)


def _project_tokens(repo: str | None, cwd: str | None) -> set[str]:
    """Strong identity tokens for cross-harness project matching."""
    tokens: set[str] = set()
    if cwd:
        path = cwd.replace("\\", "/").rstrip("/").lower()
        if path:
            tokens.add(f"cwd:{path}")
            dashed = path.lstrip("/").replace("/", "-").replace("_", "-")
            tokens.add(f"slug:{dashed}")
            base = path.rsplit("/", 1)[-1].replace("_", "-")
            if len(base) >= 8:
                tokens.add(f"base:{base}")
    if repo:
        r = repo.strip().lower()
        if r.endswith(".git"):
            r = r[:-4]
        if "github.com/" in r:
            r = r.split("github.com/", 1)[1].strip("/")
            parts = [p.replace("_", "-") for p in r.split("/") if p]
            if parts:
                if len(parts[-1]) >= 3:
                    tokens.add(f"base:{parts[-1]}")
                tokens.add(f"slug:{'-'.join(parts)}")
        else:
            slug = r.lstrip("-").replace("/", "-").replace("_", "-")
            if slug:
                tokens.add(f"slug:{slug}")
                # Trailing path-ish segment (e.g. …-side-projects-plugin).
                segs = [s for s in slug.split("-") if s]
                if len(segs) >= 2:
                    tail = "-".join(segs[-2:])
                    if len(tail) >= 8:
                        tokens.add(f"tail:{tail}")
                if segs and len(segs[-1]) >= 8:
                    tokens.add(f"base:{segs[-1]}")
    return tokens


def _branch_compatible(a: str | None, b: str | None) -> bool:
    def norm(v: str | None) -> str | None:
        if not v:
            return None
        t = v.strip()
        if not t or t.upper() == "HEAD":
            return None
        return t

    na, nb = norm(a), norm(b)
    if na is None or nb is None:
        return True
    return na == nb


def _session_bounds(
    row: sqlite3.Row, last_msg_at: str | None
) -> tuple[datetime | None, datetime | None]:
    start = _parse_ts(row["started_at"])
    end = _parse_ts(row["ended_at"]) or _parse_ts(last_msg_at) or start
    return start, end


def infer_cross_harness_links(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    window_hours: float = INFERRED_WINDOW_HOURS,
    limit: int = MAX_INFERRED,
) -> list[InferredLink]:
    """Infer cross-harness continuations via repo/cwd + branch + time window."""
    self_tokens = _project_tokens(row["repo"], row["cwd"])
    if not self_tokens:
        return []

    last_msg = conn.execute(
        """
        SELECT timestamp FROM messages
        WHERE session_id = ?
        ORDER BY seq DESC LIMIT 1
        """,
        (row["id"],),
    ).fetchone()
    self_start, self_end = _session_bounds(
        row, last_msg["timestamp"] if last_msg else None
    )
    if self_start is None or self_end is None:
        return []

    window = timedelta(hours=window_hours)
    implicit_parents = implicit_parent_ids(conn)
    explicit_parents = explicit_worker_parent_ids(conn)
    resolved_parents = {**explicit_parents, **implicit_parents}
    lineage = set(_parent_keys(row))
    parent_id = resolved_parents.get(str(row["id"]))
    if parent_id:
        lineage.add(parent_id)
    lineage.update(
        child_id
        for child_id, candidate_parent in resolved_parents.items()
        if candidate_parent == str(row["id"])
    )

    candidates = conn.execute(
        """
        SELECT
            s.id, s.harness, s.external_id, s.repo, s.cwd, s.branch,
            s.started_at, s.ended_at,
            (
                SELECT m.timestamp FROM messages m
                WHERE m.session_id = s.id
                ORDER BY m.seq DESC LIMIT 1
            ) AS last_message_at
        FROM sessions s
        WHERE s.harness != ?
          AND s.id != ?
        """,
        (row["harness"], row["id"]),
    ).fetchall()

    scored: list[tuple[float, InferredLink]] = []
    for cand in candidates:
        cid = str(cand["id"])
        if cid in resolved_parents:
            continue
        if cid in lineage or str(cand["external_id"]) in lineage:
            continue
        # Skip inventory/skills pseudo-sessions.
        if ":skills:" in cid or str(cand["external_id"]).startswith("skills:"):
            continue
        if not _branch_compatible(row["branch"], cand["branch"]):
            continue
        other_tokens = _project_tokens(cand["repo"], cand["cwd"])
        shared = self_tokens & other_tokens
        # Require a strong token (cwd exact, slug, or long base/tail).
        strong = {
            t
            for t in shared
            if t.startswith("cwd:")
            or t.startswith("slug:")
            or t.startswith("tail:")
            or (t.startswith("base:") and len(t) >= 13)
        }
        if not strong:
            continue
        c_start, c_end = _session_bounds(cand, cand["last_message_at"])
        if c_start is None or c_end is None:
            continue

        overlap = self_start <= c_end and c_start <= self_end
        if overlap:
            direction = "overlapping"
            gap = 0.0
        elif c_start >= self_end and (c_start - self_end) <= window:
            direction = "successor"
            gap = (c_start - self_end).total_seconds() / 3600.0
        elif self_start >= c_end and (self_start - c_end) <= window:
            direction = "predecessor"
            gap = (self_start - c_end).total_seconds() / 3600.0
        else:
            continue

        evidence = {
            "method": "repo_branch_time",
            "shared_tokens": sorted(strong)[:4],
            "branch_self": row["branch"],
            "branch_other": cand["branch"],
            "time_gap_hours": round(gap, 3),
            "window_hours": window_hours,
        }
        link = InferredLink(
            session_id=str(cand["id"]),
            harness=str(cand["harness"]),
            direction=direction,
            evidence=evidence,
        )
        scored.append((gap if direction != "overlapping" else -1.0, link))

    scored.sort(key=lambda x: (x[0], x[1].session_id))
    return [link for _, link in scored[:limit]]


def _attention_for_session(
    conn: sqlite3.Connection, session_id: str
) -> list[dict[str, Any]]:
    items = derive_attention(conn)
    return [
        {
            "state": i.state,
            "severity": i.severity,
            "reason": _clip(i.reason, _CLIP_MED),
        }
        for i in items
        if i.session_id == session_id
    ]


def build_session_brief(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    include_inferred: bool = False,
) -> dict[str, Any] | None:
    row = resolve_session(conn, session_id)
    if row is None:
        return None
    sid = str(row["id"])
    source_reader = CachedSourceTranscriptReader()

    models = _message_models(conn, sid)
    if row["model"] and str(row["model"]) not in models:
        models = [str(row["model"]), *models][:MAX_MODELS]
    elif not models and row["model"]:
        models = [str(row["model"])]

    msg_n, tool_n = _counts(conn, sid)
    last_msg = conn.execute(
        """
        SELECT timestamp FROM messages
        WHERE session_id = ?
        ORDER BY seq DESC LIMIT 1
        """,
        (sid,),
    ).fetchone()
    last_msg_at = last_msg["timestamp"] if last_msg else None
    duration = _duration_seconds(row["started_at"], row["ended_at"], last_msg_at)

    first_human = _clip(
        _humanize_user_text(_first_user_text(conn, sid, source_reader)), _CLIP_LONG
    )
    last_human = _clip(
        _humanize_user_text(_last_role_text(conn, sid, "user", source_reader)), _CLIP_LONG
    )
    last_assistant_raw = _last_role_text(conn, sid, "assistant", source_reader)
    last_assistant = _clip(last_assistant_raw, _CLIP_LONG)
    todos = _unresolved_todos(last_assistant_raw)
    attention = _attention_for_session(conn, sid)

    work: dict[str, Any] = {
        "first_human": first_human or None,
        "message_count": msg_n,
        "tool_event_count": tool_n,
        "skills": _skills(conn, sid),
    }
    commits = _commits(conn, sid)
    if commits:
        work["commits"] = commits

    parent = _recorded_parent(conn, row, source_reader)
    children, child_total = _recorded_children(conn, row, source_reader)
    inferred = (
        [link.to_dict() for link in infer_cross_harness_links(conn, row)]
        if include_inferred
        else []
    )

    brief: dict[str, Any] = {
        "session_id": sid,
        "header": {
            "harness": row["harness"],
            "models": models,
            "effort": row["effort"],
            "repo": row["repo"],
            "branch": row["branch"],
            "cwd": row["cwd"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "duration_seconds": duration,
            "attention": attention,
        },
        "work": work,
        "orchestration": {
            "parent": parent,
            "children": children,
            "child_total": child_total,
            "inferred_links": inferred,
        },
        "open_loops": {
            "last_human": last_human or None,
            "last_assistant": last_assistant or None,
            "unresolved_todos": todos,
            "attention": attention,
        },
    }
    if not source_reader.verify_current():
        raise RuntimeError("canonical source changed during brief construction")
    return brief


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _fit_utf8(text: str, budget: int = MARKDOWN_BUDGET) -> str:
    if _utf8_len(text) <= budget:
        return text
    # Walk back so we never split a multibyte codepoint.
    raw = text.encode("utf-8")[: max(0, budget - 3)]
    return raw.decode("utf-8", errors="ignore").rstrip() + "…\n"


def render_brief_markdown(brief: dict[str, Any]) -> str:
    h = brief["header"]
    w = brief["work"]
    o = brief["orchestration"]
    loops = brief["open_loops"]

    models = ", ".join(h.get("models") or []) or "-"
    att = h.get("attention") or []
    att_s = (
        ", ".join(f"{a['state']}({a['severity']})" for a in att) if att else "none"
    )

    children = list(o.get("children") or [])
    inferred = list(o.get("inferred_links") or [])

    def _assemble(
        *,
        child_n: int,
        inferred_n: int,
        first_lim: int,
        last_h_lim: int,
        last_a_lim: int,
    ) -> str:
        lines: list[str] = [
            f"# Session brief: {brief['session_id']}",
            "",
            "## Header",
            f"- harness: {h.get('harness')}",
            f"- model(s): {models}",
            f"- effort: {h.get('effort') or '-'}",
            f"- repo: {h.get('repo') or '-'}",
            f"- branch: {h.get('branch') or '-'}",
            f"- started: {h.get('started_at') or '-'}",
            f"- ended: {h.get('ended_at') or '-'}",
            f"- duration: {_format_duration(h.get('duration_seconds'))}",
            f"- attention: {att_s}",
            "",
            "## Work",
            f"- first human: {_clip(w.get('first_human'), first_lim) or '-'}",
            (
                f"- messages: {w.get('message_count', 0)}; "
                f"tools: {w.get('tool_event_count', 0)}"
            ),
        ]
        skills = w.get("skills") or []
        if skills:
            lines.append(f"- skills: {', '.join(skills)}")
        commits = w.get("commits") or []
        if commits:
            bits = [f"{c['sha']} {_clip(c.get('subject'), 40)}" for c in commits]
            lines.append(f"- commits: {'; '.join(bits)}")

        lines.extend(["", "## Orchestration"])
        parent = o.get("parent")
        if parent:
            lines.append(
                f"- parent ({parent.get('kind', 'recorded')}): "
                f"{parent.get('id')} — {parent.get('description')}"
            )
        else:
            lines.append("- parent: none")
        shown_children = children[:child_n]
        child_total = int(o.get("child_total") or len(children))
        if child_total:
            lines.append(f"- children ({child_total}):")
            for ch in shown_children:
                lines.append(
                    f"  - {ch.get('id')} — {ch.get('description')}"
                )
            omitted = child_total - len(shown_children)
            if omitted > 0:
                lines.append(f"  - …and {omitted} more")
        else:
            lines.append("- children: none")
        shown_inf = inferred[:inferred_n]
        if shown_inf:
            lines.append("- inferred (cross-harness):")
            for link in shown_inf:
                ev = link.get("evidence") or {}
                lines.append(
                    f"  - {link.get('direction')}: {link.get('session_id')} "
                    f"[{link.get('harness')}] gap={ev.get('time_gap_hours')}h "
                    f"via {', '.join((ev.get('shared_tokens') or [])[:2])}"
                )

        lines.extend(
            [
                "",
                "## Open loops",
                f"- last human: {_clip(loops.get('last_human'), last_h_lim) or '-'}",
                (
                    f"- last assistant: "
                    f"{_clip(loops.get('last_assistant'), last_a_lim) or '-'}"
                ),
            ]
        )
        todos = loops.get("unresolved_todos") or []
        if todos:
            lines.append("- unresolved todos:")
            for t in todos:
                lines.append(f"  - [ ] {t}")
        else:
            lines.append("- unresolved todos: none")
        if att:
            lines.append("- attention items:")
            for a in att[:3]:
                lines.append(f"  - {a['state']}: {a['reason']}")
        return "\n".join(lines).rstrip() + "\n"

    # Progressive compaction until under budget (UTF-8 bytes ≈ 2KB).
    plans = [
        (MAX_CHILDREN, MAX_INFERRED, _CLIP_LONG, _CLIP_LONG, _CLIP_LONG),
        (5, 3, _CLIP_MED, _CLIP_MED, _CLIP_MED),
        (3, 2, _CLIP_SHORT, _CLIP_SHORT, _CLIP_SHORT),
        (2, 1, 60, 60, 60),
        (1, 0, 40, 40, 40),
    ]
    text = _assemble(
        child_n=plans[0][0],
        inferred_n=plans[0][1],
        first_lim=plans[0][2],
        last_h_lim=plans[0][3],
        last_a_lim=plans[0][4],
    )
    for child_n, inferred_n, first_lim, last_h_lim, last_a_lim in plans[1:]:
        if _utf8_len(text) <= MARKDOWN_BUDGET:
            break
        text = _assemble(
            child_n=child_n,
            inferred_n=inferred_n,
            first_lim=first_lim,
            last_h_lim=last_h_lim,
            last_a_lim=last_a_lim,
        )
    return _fit_utf8(text)


def session_brief_payload(
    conn: sqlite3.Connection, session_id: str
) -> dict[str, Any] | None:
    brief = build_session_brief(conn, session_id)
    if brief is None:
        return None
    return {**brief, "markdown": render_brief_markdown(brief)}
