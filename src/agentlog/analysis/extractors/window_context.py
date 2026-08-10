from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from agentlog.analysis.extractors.models import WindowContext
from agentlog.analysis.extractors.patterns import (
    assistant_has_api_error,
    assistant_has_usage_limit,
    is_wait_loop_shape,
    mentions_cross_harness,
    unwrap_cursor_user_text,
)
from agentlog.analysis.extractors.taxonomy import (
    ASSISTANT_TEXT_CAP,
    NEXT_USER_TEXT_CAP,
    TOOL_TIMELINE_MAX_LINES,
    USER_TEXT_CAP,
)
from agentlog.safety.redaction import REDACTION_VERSION, RedactionReport, redact_text


def _trunc(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 1] + "…"


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        val = row[key]
    except (IndexError, KeyError):
        return default
    return default if val is None else val


def load_window_contexts(
    conn: sqlite3.Connection,
    *,
    window_ids: Iterable[str] | None = None,
) -> list[WindowContext]:
    """Reconstruct exchange windows with seq-range assistant/tool context."""
    if window_ids is not None:
        ids = list(window_ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        windows = list(
            conn.execute(
                f"""
                SELECT ew.*, s.harness, s.model AS session_model
                FROM exchange_windows ew
                JOIN sessions s ON s.id = ew.session_id
                WHERE ew.id IN ({placeholders})
                ORDER BY s.harness, ew.id
                """,
                ids,
            )
        )
    else:
        windows = list(
            conn.execute(
                """
                SELECT ew.*, s.harness, s.model AS session_model
                FROM exchange_windows ew
                JOIN sessions s ON s.id = ew.session_id
                ORDER BY s.harness, ew.id
                """
            )
        )

    out: list[WindowContext] = []
    for win in windows:
        ctx = _build_one(conn, win)
        out.append(ctx)
    return out


_MAX_SEQ = 9223372036854775807


def _session_has_linked_tools(conn: sqlite3.Connection, session_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM tool_events
        WHERE session_id = ? AND message_id IS NOT NULL
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    return row is not None


def _window_tools(
    conn: sqlite3.Connection, session_id: str, *, req_seq: int, end_seq: int
) -> list[sqlite3.Row]:
    """Tools belonging to a window, resolved through message linkage.

    Tool sequence numbers live in their own coordinate space and must never be
    compared against message sequence numbers. Linked tools are selected by the
    message they belong to; orphans are bounded by the tool sequences of their
    linked neighbours. Sessions with no linkage at all fall back to the raw
    sequence heuristic, which is the only ordering signal those sources carry.
    """
    if not _session_has_linked_tools(conn, session_id):
        return list(
            conn.execute(
                """
                SELECT tool_name, action, success, seq
                FROM tool_events
                WHERE session_id = ? AND seq > ? AND seq < ?
                ORDER BY seq
                """,
                (session_id, req_seq, end_seq),
            )
        )

    linked = list(
        conn.execute(
            """
            SELECT te.tool_name, te.action, te.success, te.seq
            FROM tool_events te
            JOIN messages m ON m.id = te.message_id
            WHERE te.session_id = ? AND m.session_id = ?
              AND m.seq >= ? AND m.seq < ?
            ORDER BY te.seq
            """,
            (session_id, session_id, req_seq, end_seq),
        )
    )
    # Orphan span in tool-sequence space: from this window's first linked tool
    # up to the next window's first linked tool.
    bounds = conn.execute(
        """
        SELECT
            COALESCE(MIN(CASE WHEN m.seq >= ? THEN te.seq END), ?) AS lo,
            COALESCE(MIN(CASE WHEN m.seq >= ? THEN te.seq END), ?) AS hi
        FROM tool_events te
        JOIN messages m ON m.id = te.message_id
        WHERE te.session_id = ? AND m.session_id = ?
        """,
        (req_seq, _MAX_SEQ, end_seq, _MAX_SEQ, session_id, session_id),
    ).fetchone()
    lo = int(bounds["lo"]) if bounds is not None else _MAX_SEQ
    hi = int(bounds["hi"]) if bounds is not None else _MAX_SEQ
    orphans = list(
        conn.execute(
            """
            SELECT tool_name, action, success, seq
            FROM tool_events
            WHERE session_id = ? AND message_id IS NULL
              AND seq >= ? AND seq < ?
            ORDER BY seq
            """,
            (session_id, lo, hi),
        )
    )
    if not orphans:
        return linked
    return sorted(linked + orphans, key=lambda r: int(r["seq"]))


def _build_one(conn: sqlite3.Connection, win: sqlite3.Row) -> WindowContext:
    session_id = win["session_id"]
    req = conn.execute(
        "SELECT * FROM messages WHERE id = ?", (win["request_message_id"],)
    ).fetchone()
    resp = conn.execute(
        "SELECT * FROM messages WHERE id = ?", (win["response_message_id"],)
    ).fetchone()
    if req is None or resp is None:
        return WindowContext(
            window_id=win["id"],
            session_id=session_id,
            harness=win["harness"],
            model=_row_get(win, "session_model"),
            request_message_id=win["request_message_id"],
            response_message_id=win["response_message_id"],
        )

    req_seq = int(req["seq"])
    # Next human user message after this request (non-plumbing).
    next_user = conn.execute(
        """
        SELECT * FROM messages
        WHERE session_id = ?
          AND seq > ?
          AND role = 'user'
          AND COALESCE(is_tool_plumbing, 0) = 0
          AND COALESCE(authored_by_agent, 0) = 0
        ORDER BY seq
        LIMIT 1
        """,
        (session_id, req_seq),
    ).fetchone()
    end_seq = int(next_user["seq"]) if next_user is not None else 10**12

    assistants = list(
        conn.execute(
            """
            SELECT * FROM messages
            WHERE session_id = ?
              AND seq > ?
              AND seq < ?
              AND role = 'assistant'
              AND COALESCE(is_tool_plumbing, 0) = 0
            ORDER BY seq
            """,
            (session_id, req_seq, end_seq),
        )
    )
    tools = _window_tools(conn, session_id, req_seq=req_seq, end_seq=end_seq)
    skills = list(
        conn.execute(
            """
            SELECT skill_name, exposure_type
            FROM skill_exposures
            WHERE session_id = ?
            """,
            (session_id,),
        )
    )

    assistant_parts: list[str] = []
    for msg in assistants:
        t = (msg["text"] or "").strip()
        if t:
            assistant_parts.append(t)
    # Prefer last narrations + final when over budget.
    assistant_joined = "\n---\n".join(assistant_parts)
    if len(assistant_joined) > ASSISTANT_TEXT_CAP and len(assistant_parts) > 2:
        keep = assistant_parts[:1] + assistant_parts[-3:]
        assistant_joined = "\n---\n".join(keep)

    timeline: list[str] = []
    for te in tools[:TOOL_TIMELINE_MAX_LINES]:
        succ = te["success"]
        succ_s = "?" if succ is None else ("1" if succ else "0")
        timeline.append(f"{te['tool_name']}|{te['action']}|{succ_s}")

    req_text = req["text"] or ""
    next_text = (next_user["text"] if next_user is not None else "") or ""

    return WindowContext(
        window_id=win["id"],
        session_id=session_id,
        harness=win["harness"],
        model=_row_get(resp, "model") or _row_get(win, "session_model"),
        request_text=req_text,
        assistant_text=assistant_joined,
        next_user_text=next_text,
        tool_timeline=timeline,
        skill_names=[s["skill_name"] for s in skills],
        skill_exposure_types=[s["exposure_type"] for s in skills],
        is_tool_plumbing=bool(_row_get(req, "is_tool_plumbing", 0)),
        authored_by_agent=bool(_row_get(req, "authored_by_agent", 0)),
        assistant_msg_count=len(assistants),
        tool_count=len(tools),
        request_message_id=win["request_message_id"],
        response_message_id=win["response_message_id"],
    )


def truncate_for_ux(
    ctx: WindowContext,
    *,
    report: RedactionReport | None = None,
) -> dict[str, Any]:
    """Bounded, redacted payload for the UX labeler.

    Redaction runs before truncation so a secret cannot survive by straddling a
    field cap, and before payload assembly so no caller can construct an
    unredacted payload by accident.
    """
    rep = report if report is not None else RedactionReport()
    user = redact_text(unwrap_cursor_user_text(ctx.request_text), rep)
    assistant = redact_text(ctx.assistant_text, rep)
    next_user = redact_text(unwrap_cursor_user_text(ctx.next_user_text), rep)
    return {
        "window_id": ctx.window_id,
        "harness": ctx.harness,
        "model": ctx.model,
        "user": _trunc(user, USER_TEXT_CAP),
        "assistant": _trunc(assistant, ASSISTANT_TEXT_CAP),
        "next_user": _trunc(next_user, NEXT_USER_TEXT_CAP),
        "tool_timeline": [
            redact_text(t, rep) for t in ctx.tool_timeline[:TOOL_TIMELINE_MAX_LINES]
        ],
        "skills_loaded": [
            redact_text(s, rep) for s in sorted(set(ctx.skill_names))[:40]
        ],
        "skill_exposure_types": sorted(set(ctx.skill_exposure_types))[:20],
        "redaction_version": REDACTION_VERSION,
    }


def structural_features(ctx: WindowContext) -> dict[str, Any]:
    return {
        "assistant_msg_count": ctx.assistant_msg_count,
        "tool_count": ctx.tool_count,
        "multi_assistant": ctx.assistant_msg_count > 1,
        "wait_loop_shape": is_wait_loop_shape(
            ctx.assistant_msg_count, ctx.tool_count
        ),
        "api_error": assistant_has_api_error(ctx.assistant_text),
        "usage_or_api_limit": assistant_has_usage_limit(ctx.assistant_text),
        "cross_harness_mention": mentions_cross_harness(ctx.request_text),
        "skill_attach_or_inject": any(
            t in ("matched", "injected", "tool_use", "attached")
            for t in ctx.skill_exposure_types
        ),
        "skill_names": sorted(set(ctx.skill_names))[:40],
        "request_chars": len((ctx.request_text or "").strip()),
        "is_tool_plumbing": ctx.is_tool_plumbing,
        "authored_by_agent": ctx.authored_by_agent,
        "image_only_prefix": (ctx.request_text or "").lstrip().startswith("[Image:"),
    }
