"""Human adjudication of LLM-labeled exchange windows."""

from __future__ import annotations

import json
import random
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agentlog.analysis.extractors.patterns import (
    classify_request_text,
    unwrap_cursor_user_text,
)
from agentlog.analysis.extractors.packets import (
    ALLOWED_AGENT_STANCE,
    ALLOWED_PRIOR_OUTCOME,
    ALLOWED_TURN_KINDS,
    ALLOWED_USER_STANCE,
)
from agentlog.api.deps import (
    get_conn,
    get_write_conn,
    is_sqlite_busy,
    with_busy_retry,
)
from agentlog.normalize.model_identity import display_model
from agentlog.safety.write_guard import assert_writable

router = APIRouter(tags=["adjudication"])

REPO_ROOT = Path(__file__).resolve().parents[3]
ORIGINAL_AUDIT_PACK = (
    REPO_ROOT / ".research" / "extraction-verification" / "audit_pack_unlabeled.jsonl"
)
DEFAULT_AUDIT_PACK = (
    REPO_ROOT
    / ".research"
    / "extraction-verification"
    / "audit_pack_adjudicable.jsonl"
)
MIN_REPORT_N = 20
SCALAR_FIELDS = ("user_stance", "agent_stance", "prior_outcome")
# Below 4 chars after unwrap are stubs ("ok", "y") too thin for stance/kind
# judgments; "lgtm"/"okay"/"yes." remain. Synthetic envelopes are filtered
# separately regardless of length.
MIN_HUMAN_CHARS = 4
QUEUE_TARGET = 100
QUEUE_SEED = 42
SYNTHETIC_REQUEST_KINDS = frozenset(
    {
        "empty",
        "auto_review",
        "task_notification",
        "continue_stub",
        "realtime_delegation",
        "skill_body",
    }
)
_SYSTEM_REMINDER_ONLY = re.compile(
    r"^\s*<system-reminder>[\s\S]*?</system-reminder>\s*$",
    re.IGNORECASE,
)
_HARNESS_ENVELOPE = re.compile(
    r"<(system-reminder|system_reminder|system_notification|task_notification|"
    r"additional_context|environment_context)\b",
    re.IGNORECASE,
)

# Plain-language options for progressive triage (canonical enum underneath).
TURN_KIND_OPTIONS: list[dict[str, str]] = [
    {"value": "human_task", "label": "asked for something new", "key": "1"},
    {"value": "human_followup", "label": "continued an existing thread", "key": "2"},
    {"value": "clarifying_question", "label": "asked the agent a clarifying question", "key": "3"},
    {"value": "soft_approval", "label": "gave a light go-ahead", "key": "4"},
    {"value": "correction", "label": "corrected something the agent did", "key": "5"},
    {"value": "redirect_or_brake", "label": "redirected or braked the agent", "key": "6"},
    {"value": "dont_act_yet", "label": "told the agent to stop or wait", "key": "7"},
    {"value": "inter_agent_handoff", "label": "handed work between agents", "key": "8"},
    {"value": "worker_brief", "label": "briefed a worker/subagent", "key": "9"},
    {"value": "coordinator_nudge", "label": "nudged a coordinator/lead", "key": "0"},
    {"value": "skill_invocation", "label": "invoked a skill", "key": "a"},
    {"value": "slash_command", "label": "used a slash command", "key": "b"},
    {"value": "image_only", "label": "sent an image only", "key": "c"},
]
USER_STANCE_OPTIONS: list[dict[str, str]] = [
    {"value": "neutral", "label": "calm / matter-of-fact", "key": "1"},
    {"value": "approving", "label": "approving", "key": "2"},
    {"value": "correcting", "label": "correcting", "key": "3"},
    {"value": "redirecting", "label": "redirecting", "key": "4"},
    {"value": "skeptical", "label": "skeptical", "key": "5"},
    {"value": "frustrated", "label": "frustrated", "key": "6"},
    {"value": "confused", "label": "confused", "key": "7"},
    {"value": "blocked_waiting_on_user", "label": "blocked, waiting on the user", "key": "8"},
]
AGENT_STANCE_OPTIONS: list[dict[str, str]] = [
    {"value": "executing", "label": "executing the task", "key": "1"},
    {"value": "investigating", "label": "investigating", "key": "2"},
    {"value": "narrating_wait", "label": "narrating / waiting", "key": "3"},
    {"value": "asking_clarification", "label": "asking for clarification", "key": "4"},
    {"value": "pushing_back", "label": "pushing back", "key": "5"},
    {"value": "handing_off", "label": "handing off", "key": "6"},
    {"value": "failing_tooling", "label": "hitting tool failures", "key": "7"},
]
PRIOR_OUTCOME_OPTIONS: list[dict[str, str]] = [
    {"value": "accepted_continue", "label": "accepted and continued", "key": "1"},
    {"value": "accepted_done", "label": "accepted as done", "key": "2"},
    {"value": "partial_accept", "label": "partially accepted", "key": "3"},
    {"value": "rejected_redo", "label": "rejected / asked to redo", "key": "4"},
    {"value": "ignored_by_user_topic_shift", "label": "ignored; topic shifted", "key": "5"},
]
HUMAN_PRESENT_OPTIONS: list[dict[str, str]] = [
    {"value": "yes", "label": "yes — a real human turn", "key": "1"},
    {"value": "no", "label": "no — agent or harness content", "key": "2"},
    {"value": "unclear", "label": "unclear", "key": "3"},
]
VAGUE_OPTION = {"value": "abstain", "label": "too vague to judge", "key": "v"}


def _audit_pack_path(request: Request) -> Path:
    override = getattr(request.app.state, "audit_pack_path", None)
    if override is not None:
        return Path(override)
    return DEFAULT_AUDIT_PACK


def _original_pack_path(request: Request) -> Path:
    override = getattr(request.app.state, "original_audit_pack_path", None)
    if override is not None:
        return Path(override)
    return ORIGINAL_AUDIT_PACK


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def human_core_text(text: str) -> str:
    return unwrap_cursor_user_text(text or "").strip()


def is_adjudicable_request(
    text: str,
    *,
    authored_by_agent: bool,
    is_tool_plumbing: bool,
) -> tuple[bool, str]:
    """Return (ok, reason). reason is 'ok' or an ineligibility code."""
    if authored_by_agent:
        return False, "authored_by_agent"
    if is_tool_plumbing:
        return False, "tool_plumbing"
    raw = text or ""
    hit = classify_request_text(raw)
    if hit.kind in SYNTHETIC_REQUEST_KINDS:
        return False, hit.kind
    if _SYSTEM_REMINDER_ONLY.match(raw):
        return False, "system_reminder"
    core = human_core_text(raw)
    if not core:
        return False, "empty_core"
    # Envelope-only: harness tags with no human core beyond the tags themselves.
    if _HARNESS_ENVELOPE.search(raw) and "<user_query>" not in raw.lower():
        # Allow if substantial non-tag prose remains after stripping reminder blocks.
        stripped = re.sub(
            r"<system-reminder>[\s\S]*?</system-reminder>",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip()
        stripped = re.sub(r"<[^>]+>", "", stripped).strip()
        if len(stripped) < MIN_HUMAN_CHARS:
            return False, "harness_envelope"
    if len(core) < MIN_HUMAN_CHARS:
        return False, "too_short"
    return True, "ok"


def taxonomy_payload() -> dict[str, Any]:
    return {
        "human_present": HUMAN_PRESENT_OPTIONS,
        "turn_kind": TURN_KIND_OPTIONS,
        "user_stance": USER_STANCE_OPTIONS + [VAGUE_OPTION],
        "agent_stance": AGENT_STANCE_OPTIONS + [VAGUE_OPTION],
        "prior_outcome": PRIOR_OUTCOME_OPTIONS + [VAGUE_OPTION],
        "vague_key": VAGUE_OPTION["key"],
        "enums": {
            "turn_kind": sorted(ALLOWED_TURN_KINDS),
            "user_stance": sorted(ALLOWED_USER_STANCE),
            "agent_stance": sorted(ALLOWED_AGENT_STANCE),
            "prior_outcome": sorted(ALLOWED_PRIOR_OUTCOME),
        },
    }


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


def _llm_labels(conn: sqlite3.Connection, window_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not window_ids:
        return {}
    placeholders = ",".join("?" * len(window_ids))
    rows = conn.execute(
        f"""
        SELECT window_id, turn_kinds_json, user_stance, agent_stance, prior_outcome,
               created_at
        FROM ux_observations
        WHERE window_id IN ({placeholders})
        ORDER BY created_at DESC
        """,
        window_ids,
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        wid = str(row["window_id"])
        if wid in out:
            continue
        out[wid] = {
            "turn_kind": _parse_json_list(row["turn_kinds_json"]),
            "user_stance": row["user_stance"],
            "agent_stance": row["agent_stance"],
            "prior_outcome": row["prior_outcome"],
        }
    return out


def _adjudications(
    conn: sqlite3.Connection, window_ids: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    if window_ids is not None and not window_ids:
        return {}
    if window_ids is None:
        rows = conn.execute(
            """
            SELECT window_id, adjudicated_at, turn_kind, user_stance, agent_stance,
                   prior_outcome, notes, source
            FROM adjudications
            """
        ).fetchall()
    else:
        placeholders = ",".join("?" * len(window_ids))
        rows = conn.execute(
            f"""
            SELECT window_id, adjudicated_at, turn_kind, user_stance, agent_stance,
                   prior_outcome, notes, source
            FROM adjudications
            WHERE window_id IN ({placeholders})
            """,
            window_ids,
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out[str(row["window_id"])] = {
            "adjudicated_at": row["adjudicated_at"],
            "turn_kind": _parse_json_list(row["turn_kind"]),
            "user_stance": row["user_stance"],
            "agent_stance": row["agent_stance"],
            "prior_outcome": row["prior_outcome"],
            "notes": row["notes"] or "",
            "source": row["source"],
        }
    return out


def _month_key(started_at: str | None) -> str:
    if not started_at:
        return "unknown"
    return str(started_at)[:7]


def _primary_kind(turn_kinds_json: str | None, request_kind: str | None) -> str:
    kinds = _parse_json_list(turn_kinds_json)
    if kinds:
        return kinds[0]
    return request_kind or "unknown"


def window_resolves(conn: sqlite3.Connection, window_id: str) -> bool:
    """True when the window row exists and both message FKs resolve."""
    row = conn.execute(
        """
        SELECT 1
        FROM exchange_windows w
        JOIN messages req ON req.id = w.request_message_id
        JOIN messages resp ON resp.id = w.response_message_id
        WHERE w.id = ?
        """,
        (window_id,),
    ).fetchone()
    return row is not None


def payload_has_human_turn(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    for turn in payload.get("turns") or []:
        if turn.get("slot") == "human" and str(turn.get("text") or "").strip():
            return True
    return False


def list_eligible_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    # INNER JOIN request+response messages so candidates cannot dangle.
    rows = conn.execute(
        """
        SELECT w.id AS window_id, w.session_id, s.harness, s.started_at,
               s.model_canonical,
               m.text AS request_text,
               COALESCE(m.authored_by_agent, 0) AS authored_by_agent,
               COALESCE(m.is_tool_plumbing, 0) AS is_tool_plumbing,
               (
                 SELECT d.turn_kinds_json FROM window_det_classifications d
                 WHERE d.window_id = w.id
                 ORDER BY d.created_at DESC LIMIT 1
               ) AS turn_kinds_json,
               (
                 SELECT d.request_kind FROM window_det_classifications d
                 WHERE d.window_id = w.id
                 ORDER BY d.created_at DESC LIMIT 1
               ) AS request_kind
        FROM exchange_windows w
        JOIN sessions s ON s.id = w.session_id
        JOIN messages m ON m.id = w.request_message_id
        JOIN messages resp ON resp.id = w.response_message_id
        WHERE m.role = 'user'
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        ok, _reason = is_adjudicable_request(
            row["request_text"] or "",
            authored_by_agent=bool(row["authored_by_agent"]),
            is_tool_plumbing=bool(row["is_tool_plumbing"]),
        )
        if not ok:
            continue
        out.append(
            {
                "window_id": str(row["window_id"]),
                "session_id": row["session_id"],
                "harness": row["harness"] or "other",
                "started_at": row["started_at"],
                "model": display_model(row["model_canonical"]),
                "month": _month_key(row["started_at"]),
                "turn_kind": _primary_kind(row["turn_kinds_json"], row["request_kind"]),
            }
        )
    return out


def evaluate_pack_eligibility(
    conn: sqlite3.Connection, pack_ids: list[str]
) -> dict[str, Any]:
    if not pack_ids:
        return {
            "total": 0,
            "eligible": 0,
            "ineligible": 0,
            "ineligible_rate": None,
            "reasons": {},
        }
    placeholders = ",".join("?" * len(pack_ids))
    rows = {
        str(r["id"]): r
        for r in conn.execute(
            f"""
            SELECT w.id, m.text, COALESCE(m.authored_by_agent, 0) AS authored_by_agent,
                   COALESCE(m.is_tool_plumbing, 0) AS is_tool_plumbing
            FROM exchange_windows w
            JOIN messages m ON m.id = w.request_message_id
            WHERE w.id IN ({placeholders})
            """,
            pack_ids,
        )
    }
    reasons: Counter[str] = Counter()
    eligible = 0
    for wid in pack_ids:
        row = rows.get(wid)
        if row is None:
            reasons["missing_window"] += 1
            continue
        ok, reason = is_adjudicable_request(
            row["text"] or "",
            authored_by_agent=bool(row["authored_by_agent"]),
            is_tool_plumbing=bool(row["is_tool_plumbing"]),
        )
        if ok:
            eligible += 1
        else:
            reasons[reason] += 1
    total = len(pack_ids)
    ineligible = total - eligible
    return {
        "total": total,
        "eligible": eligible,
        "ineligible": ineligible,
        "ineligible_rate": (ineligible / total) if total else None,
        "reasons": dict(reasons.most_common()),
        "min_human_chars": MIN_HUMAN_CHARS,
    }


def stratified_pick(
    candidates: list[dict[str, Any]],
    *,
    n: int,
    seed: int,
    pinned: list[str] | None = None,
) -> list[str]:
    """Stratify by harness × month × turn_kind; pin existing adjudications first."""
    pinned = list(pinned or [])
    by_id = {c["window_id"]: c for c in candidates}
    chosen: list[str] = []
    seen: set[str] = set()
    for wid in pinned:
        if wid in by_id and wid not in seen:
            chosen.append(wid)
            seen.add(wid)
    remaining_n = max(0, n - len(chosen))
    pool = [c for c in candidates if c["window_id"] not in seen]
    if remaining_n <= 0 or not pool:
        return chosen[:n] if n < len(chosen) else chosen

    rng = random.Random(seed)
    # Nest: harness -> month -> turn_kind
    buckets: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for c in pool:
        buckets[c["harness"]][c["month"]][c["turn_kind"]].append(c)

    # Flatten leaf buckets with proportional harness allocation.
    harnesses = sorted(buckets)
    total = len(pool)
    alloc: dict[str, int] = {}
    left = remaining_n
    for i, h in enumerate(harnesses):
        h_count = sum(
            len(v) for months in buckets[h].values() for v in months.values()
        )
        if i == len(harnesses) - 1:
            alloc[h] = left
        else:
            share = max(1, round(remaining_n * h_count / total)) if total else 0
            share = min(share, h_count, left - (len(harnesses) - i - 1))
            alloc[h] = max(0, share)
            left -= alloc[h]

    picked: list[dict[str, Any]] = []
    for h in harnesses:
        need = alloc[h]
        leaves: list[list[dict[str, Any]]] = []
        for month in sorted(buckets[h]):
            for kind in sorted(buckets[h][month]):
                leaf = list(buckets[h][month][kind])
                rng.shuffle(leaf)
                leaves.append(leaf)
        # Round-robin across month×kind leaves for time/kind spread.
        while need > 0 and leaves:
            progressed = False
            next_leaves: list[list[dict[str, Any]]] = []
            for leaf in leaves:
                if need <= 0:
                    next_leaves.append(leaf)
                    continue
                if leaf:
                    picked.append(leaf.pop())
                    need -= 1
                    progressed = True
                if leaf:
                    next_leaves.append(leaf)
            leaves = next_leaves
            if not progressed:
                break

    rng.shuffle(picked)
    for c in picked:
        if c["window_id"] not in seen:
            chosen.append(c["window_id"])
            seen.add(c["window_id"])
    # Never drop pinned adjudications even if over target.
    min_keep = len([w for w in pinned if w in by_id])
    return chosen if len(chosen) <= max(n, min_keep) else chosen[: max(n, min_keep)]


def _turn_model(message: sqlite3.Row, win: sqlite3.Row) -> str:
    canonical = message["model_canonical"] if message is not None else None
    if not (canonical and str(canonical).strip()):
        canonical = win["session_model_canonical"]
    return display_model(canonical)


def load_window_turns(conn: sqlite3.Connection, window_id: str) -> dict[str, Any] | None:
    """Full turns for adjudication UI: prior agent, human, agent, next human."""
    win = conn.execute(
        """
        SELECT w.*, s.harness, s.model_canonical AS session_model_canonical
        FROM exchange_windows w
        JOIN sessions s ON s.id = w.session_id
        WHERE w.id = ?
        """,
        (window_id,),
    ).fetchone()
    if win is None:
        return None
    req = conn.execute(
        "SELECT * FROM messages WHERE id = ?", (win["request_message_id"],)
    ).fetchone()
    resp = conn.execute(
        "SELECT * FROM messages WHERE id = ?", (win["response_message_id"],)
    ).fetchone()
    if req is None or resp is None:
        return None

    prior = conn.execute(
        """
        SELECT * FROM messages
        WHERE session_id = ? AND seq < ? AND role = 'assistant'
          AND COALESCE(is_tool_plumbing, 0) = 0
        ORDER BY seq DESC LIMIT 1
        """,
        (win["session_id"], int(req["seq"])),
    ).fetchone()

    next_user = conn.execute(
        """
        SELECT * FROM messages
        WHERE session_id = ?
          AND seq > ?
          AND role = 'user'
          AND COALESCE(is_tool_plumbing, 0) = 0
          AND COALESCE(authored_by_agent, 0) = 0
        ORDER BY seq LIMIT 1
        """,
        (win["session_id"], int(req["seq"])),
    ).fetchone()
    end_seq = int(next_user["seq"]) if next_user is not None else 10**12

    assistants = list(
        conn.execute(
            """
            SELECT * FROM messages
            WHERE session_id = ? AND seq > ? AND seq < ?
              AND role = 'assistant' AND COALESCE(is_tool_plumbing, 0) = 0
            ORDER BY seq
            """,
            (win["session_id"], int(req["seq"]), end_seq),
        )
    )
    assistant_text = "\n---\n".join(
        (m["text"] or "").strip() for m in assistants if (m["text"] or "").strip()
    )
    if not assistant_text:
        assistant_text = resp["text"] or ""

    turns: list[dict[str, Any]] = []
    if prior is not None and (prior["text"] or "").strip():
        turns.append(
            {
                "id": prior["id"],
                "role": "assistant",
                "slot": "prior_agent",
                "text": prior["text"] or "",
                "model": _turn_model(prior, win),
                "authored_by_agent": False,
                "is_tool_plumbing": bool(prior["is_tool_plumbing"] or 0),
            }
        )
    turns.append(
        {
            "id": req["id"],
            "role": "user",
            "slot": "human",
            "text": req["text"] or "",
            "model": None,
            "authored_by_agent": bool(req["authored_by_agent"] or 0),
            "is_tool_plumbing": bool(req["is_tool_plumbing"] or 0),
        }
    )
    turns.append(
        {
            "id": resp["id"],
            "role": "assistant",
            "slot": "agent",
            "text": assistant_text,
            "model": _turn_model(resp, win),
            "authored_by_agent": False,
            "is_tool_plumbing": False,
        }
    )
    if next_user is not None and (next_user["text"] or "").strip():
        turns.append(
            {
                "id": next_user["id"],
                "role": "user",
                "slot": "next_human",
                "text": next_user["text"] or "",
                "model": None,
                "authored_by_agent": bool(next_user["authored_by_agent"] or 0),
                "is_tool_plumbing": bool(next_user["is_tool_plumbing"] or 0),
            }
        )

    return {
        "window_id": window_id,
        "session_id": win["session_id"],
        "harness": win["harness"],
        "model": _turn_model(resp, win),
        "turns": turns,
        # Compatibility fields for older clients / tests.
        "user": human_core_text(req["text"] or ""),
        "assistant": assistant_text,
        "next_user": human_core_text(next_user["text"] if next_user else ""),
    }


def rebuild_adjudicable_pack(
    conn: sqlite3.Connection,
    pack_path: Path,
    *,
    n: int = QUEUE_TARGET,
    seed: int = QUEUE_SEED,
) -> dict[str, Any]:
    candidates = list_eligible_candidates(conn)
    adj = _adjudications(conn)
    # Only pin adjudications whose windows still resolve — stale ids are dropped
    # from the queue (the adjudication row itself is preserved in DB).
    pinned = [
        wid
        for wid in adj
        if wid in {c["window_id"] for c in candidates} or window_resolves(conn, wid)
    ]
    for wid in pinned:
        if wid in {c["window_id"] for c in candidates}:
            continue
        row = conn.execute(
            """
            SELECT w.id AS window_id, w.session_id, s.harness, s.started_at,
                   s.model_canonical
            FROM exchange_windows w
            JOIN sessions s ON s.id = w.session_id
            WHERE w.id = ?
            """,
            (wid,),
        ).fetchone()
        if row is not None:
            candidates.append(
                {
                    "window_id": wid,
                    "session_id": row["session_id"],
                    "harness": row["harness"] or "other",
                    "started_at": row["started_at"],
                    "model": display_model(row["model_canonical"]),
                    "month": _month_key(row["started_at"]),
                    "turn_kind": "adjudicated",
                }
            )

    picked = stratified_pick(candidates, n=n, seed=seed, pinned=pinned)
    # Final gate: every selected id must assemble a human turn right now.
    verified: list[str] = []
    for wid in picked:
        payload = load_window_turns(conn, wid)
        if payload_has_human_turn(payload):
            verified.append(wid)
    if len(verified) < n:
        have = set(verified)
        for c in candidates:
            if len(verified) >= n:
                break
            wid = c["window_id"]
            if wid in have:
                continue
            payload = load_window_turns(conn, wid)
            if payload_has_human_turn(payload):
                verified.append(wid)
                have.add(wid)
    picked = verified[: max(n, len([w for w in pinned if w in verified]))]

    by_id = {c["window_id"]: c for c in candidates}
    pack_path = assert_writable(pack_path, purpose="adjudication pack")
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    with pack_path.open("w", encoding="utf-8") as f:
        for wid in picked:
            meta = by_id.get(wid) or {
                "window_id": wid,
                "session_id": None,
                "harness": None,
            }
            f.write(
                json.dumps(
                    {
                        "window_id": wid,
                        "harness": meta.get("harness"),
                        "session_id": meta.get("session_id"),
                        "month": meta.get("month"),
                        "turn_kind": meta.get("turn_kind"),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return {
        "path": str(pack_path),
        "selected": len(picked),
        "eligible_population": len(candidates),
        "pinned_adjudications": len([w for w in pinned if w in set(picked)]),
        "seed": seed,
        "target": n,
        "rebuilt": True,
    }


def _pack_staleness(conn: sqlite3.Connection, pack: list[dict[str, Any]]) -> dict[str, int]:
    missing = 0
    empty = 0
    ok = 0
    for row in pack:
        wid = str(row["window_id"])
        if not window_resolves(conn, wid):
            missing += 1
            continue
        payload = load_window_turns(conn, wid)
        if payload_has_human_turn(payload):
            ok += 1
        else:
            empty += 1
    return {"missing_window": missing, "empty_turns": empty, "ok": ok}


def _ensure_pack(conn: sqlite3.Connection, pack_path: Path, *, rebuild: bool) -> dict[str, Any]:
    if rebuild or not pack_path.is_file():
        return rebuild_adjudicable_pack(conn, pack_path)
    rows = _load_jsonl(pack_path)
    stale = _pack_staleness(conn, rows)
    # Re-ingest rewrites window ids; refresh when any pack entry no longer assembles.
    if stale["missing_window"] or stale["empty_turns"] or len(rows) < QUEUE_TARGET:
        meta = rebuild_adjudicable_pack(conn, pack_path)
        meta["stale_before_rebuild"] = stale
        return meta
    return {
        "path": str(pack_path),
        "selected": len(rows),
        "eligible_population": None,
        "pinned_adjudications": None,
        "seed": QUEUE_SEED,
        "target": QUEUE_TARGET,
        "rebuilt": False,
        "stale_before_rebuild": stale,
    }


def _validate_labels(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    triage = body.get("triage") or body.get("human_present")
    if triage is not None and triage not in ("yes", "no", "unclear"):
        raise HTTPException(status_code=400, detail="triage must be yes, no, or unclear")

    raw_kinds = body.get("turn_kind")
    if raw_kinds is None:
        raw_kinds = []
    if not isinstance(raw_kinds, list):
        raise HTTPException(status_code=400, detail="turn_kind must be an array")
    turn_kind = [str(k) for k in raw_kinds]
    unknown = [k for k in turn_kind if k not in ALLOWED_TURN_KINDS]
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"unknown turn_kind: {sorted(set(unknown))}"
        )

    def _opt_enum(field: str, allowed: frozenset[str]) -> str | None:
        val = body.get(field)
        if val is None or val == "":
            return None
        text = str(val)
        if text not in allowed:
            raise HTTPException(status_code=400, detail=f"invalid {field}: {text}")
        return text

    source = body.get("source") or "audit_pack"
    if source not in ("audit_pack", "ad_hoc"):
        raise HTTPException(status_code=400, detail="source must be audit_pack or ad_hoc")

    notes = body.get("notes")
    if notes is None:
        notes = ""
    if not isinstance(notes, str):
        raise HTTPException(status_code=400, detail="notes must be a string")

    # Progressive triage shortcuts: no/unclear → abstain fields, empty turn_kind.
    if triage in ("no", "unclear"):
        turn_kind = []
        user_stance = "abstain"
        agent_stance = "abstain"
        prior_outcome = "abstain"
        tag = "triage:no_human" if triage == "no" else "triage:unclear_human"
        if tag not in notes:
            notes = f"{tag}\n{notes}".strip()
    else:
        user_stance = _opt_enum("user_stance", ALLOWED_USER_STANCE)
        agent_stance = _opt_enum("agent_stance", ALLOWED_AGENT_STANCE)
        prior_outcome = _opt_enum("prior_outcome", ALLOWED_PRIOR_OUTCOME)
        if triage == "yes" and "triage:has_human" not in notes:
            notes = f"triage:has_human\n{notes}".strip()

    vague_fields = body.get("vague_fields") or []
    if isinstance(vague_fields, list) and vague_fields:
        tag = "vague:" + ",".join(str(x) for x in vague_fields)
        if tag not in notes:
            notes = f"{tag}\n{notes}".strip()

    return {
        "turn_kind": turn_kind,
        "user_stance": user_stance,
        "agent_stance": agent_stance,
        "prior_outcome": prior_outcome,
        "notes": notes,
        "source": source,
        "triage": triage,
    }


def _rate(matches: int, n: int) -> dict[str, Any]:
    return {
        "matches": matches,
        "n": n,
        "rate": (matches / n) if n else None,
    }


def _precision(tp: int, denom: int) -> dict[str, Any]:
    return {
        "tp": tp,
        "denominator": denom,
        "rate": (tp / denom) if denom else None,
    }


def build_report(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    queue_total: int,
    adjudicated: int,
    with_llm: int,
    min_n: int = MIN_REPORT_N,
) -> dict[str, Any]:
    base = {
        "adjudicated": adjudicated,
        "with_llm": with_llm,
        "total_queue": queue_total,
        "min_required": min_n,
    }
    if with_llm < min_n:
        return {**base, "insufficient_data": True}

    fields: dict[str, Any] = {}
    kind_matches = 0
    label_tp: Counter[str] = Counter()
    label_pred: Counter[str] = Counter()
    kind_confusion: Counter[tuple[str, str]] = Counter()
    for human, llm in pairs:
        h_set = set(human.get("turn_kind") or [])
        l_set = set(llm.get("turn_kind") or [])
        if h_set == l_set:
            kind_matches += 1
        else:
            kind_confusion[
                (",".join(sorted(h_set)) or "(empty)", ",".join(sorted(l_set)) or "(empty)")
            ] += 1
        for lab in l_set:
            label_pred[lab] += 1
            if lab in h_set:
                label_tp[lab] += 1

    fields["turn_kind"] = {
        "exact_match": _rate(kind_matches, with_llm),
        "llm_precision": {
            lab: _precision(label_tp[lab], label_pred[lab]) for lab in sorted(label_pred)
        },
        "confusion_pairs": [
            {"human": h, "llm": l, "count": c}
            for (h, l), c in kind_confusion.most_common(40)
        ],
    }

    for field in SCALAR_FIELDS:
        matches = 0
        confusion: Counter[tuple[str, str]] = Counter()
        pred_tp: Counter[str] = Counter()
        pred_n: Counter[str] = Counter()
        for human, llm in pairs:
            h = human.get(field)
            l = llm.get(field)
            h_s = "null" if h is None else str(h)
            l_s = "null" if l is None else str(l)
            if h == l:
                matches += 1
            else:
                confusion[(h_s, l_s)] += 1
            pred_n[l_s] += 1
            if h == l:
                pred_tp[l_s] += 1
        fields[field] = {
            "exact_match": _rate(matches, with_llm),
            "llm_precision": {
                lab: _precision(pred_tp[lab], pred_n[lab]) for lab in sorted(pred_n)
            },
            "confusion_pairs": [
                {"human": h, "llm": l, "count": c}
                for (h, l), c in confusion.most_common(40)
            ],
        }

    return {**base, "insufficient_data": False, "fields": fields}


@router.get("/api/adjudication/taxonomy")
def adjudication_taxonomy() -> dict:
    return taxonomy_payload()


@router.post("/api/adjudication/rebuild")
def adjudication_rebuild(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    pack_path = _audit_pack_path(request)
    meta = rebuild_adjudicable_pack(conn, pack_path)
    original_ids = [
        str(r["window_id"]) for r in _load_jsonl(_original_pack_path(request))
    ]
    return {
        **meta,
        "original_pack_eligibility": evaluate_pack_eligibility(conn, original_ids),
        "rebuilt": True,
    }


@router.get("/api/adjudication/queue")
def adjudication_queue(
    request: Request,
    rebuild: bool = Query(False),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    pack_path = _audit_pack_path(request)
    pack_meta = _ensure_pack(conn, pack_path, rebuild=rebuild)
    pack = _load_jsonl(pack_path)
    if not pack:
        pack_meta = rebuild_adjudicable_pack(conn, pack_path)
        pack = _load_jsonl(pack_path)

    window_ids = [str(r["window_id"]) for r in pack]
    llm_by_id = _llm_labels(conn, window_ids)
    adj_by_id = _adjudications(conn, window_ids)

    items: list[dict[str, Any]] = []
    skipped_stale = 0
    for row in pack:
        wid = str(row["window_id"])
        payload = load_window_turns(conn, wid)
        if not payload_has_human_turn(payload):
            skipped_stale += 1
            continue
        assert payload is not None
        adj = adj_by_id.get(wid)
        # Blind-first: never send LLM labels until the human has committed.
        items.append(
            {
                "window_id": wid,
                "index": len(items),
                "position": len(items) + 1,
                "harness": payload.get("harness") or row.get("harness"),
                "session_id": payload.get("session_id") or row.get("session_id"),
                "payload": payload,
                "llm": llm_by_id.get(wid) if adj is not None else None,
                "adjudication": adj,
                "adjudicated": adj is not None,
            }
        )

    # If filtering hollowed the queue, rebuild once to a full adjudicable 100.
    if skipped_stale or len(items) < QUEUE_TARGET:
        pack_meta = rebuild_adjudicable_pack(conn, pack_path)
        pack = _load_jsonl(pack_path)
        window_ids = [str(r["window_id"]) for r in pack]
        llm_by_id = _llm_labels(conn, window_ids)
        adj_by_id = _adjudications(conn, window_ids)
        items = []
        skipped_stale = 0
        for row in pack:
            wid = str(row["window_id"])
            payload = load_window_turns(conn, wid)
            if not payload_has_human_turn(payload):
                skipped_stale += 1
                continue
            assert payload is not None
            adj = adj_by_id.get(wid)
            items.append(
                {
                    "window_id": wid,
                    "index": len(items),
                    "position": len(items) + 1,
                    "harness": payload.get("harness") or row.get("harness"),
                    "session_id": payload.get("session_id") or row.get("session_id"),
                    "payload": payload,
                    "llm": llm_by_id.get(wid) if adj is not None else None,
                    "adjudication": adj,
                    "adjudicated": adj is not None,
                }
            )

    done = sum(1 for i in items if i["adjudicated"])
    original_ids = [
        str(r["window_id"]) for r in _load_jsonl(_original_pack_path(request))
    ]
    return {
        "items": items,
        "progress": {
            "done": done,
            "total": len(items),
            "remaining": max(0, len(items) - done),
        },
        "audit_pack": str(pack_path),
        "pack": {**pack_meta, "skipped_stale": skipped_stale},
        "original_pack_eligibility": evaluate_pack_eligibility(conn, original_ids),
        "integrity": {
            "note": (
                "exchange_windows.id = content hash over session + request/response "
                "texts (stable across re-parse). ux_observations and adjudications "
                "use soft refs with content_hash + link_status; re-ingest upserts "
                "windows and re-links labels instead of CASCADE-deleting them."
            ),
        },
    }


def _window_content_hash(conn: sqlite3.Connection, window_id: str) -> str | None:
    win = conn.execute(
        "SELECT * FROM exchange_windows WHERE id = ?",
        (window_id,),
    ).fetchone()
    if win is None:
        return None
    keys = set(win.keys())
    if "content_hash" in keys and win["content_hash"]:
        return str(win["content_hash"])
    # After v012, id itself is the content hash identity.
    return window_id


def _upsert_adjudication(
    conn: sqlite3.Connection,
    *,
    window_id: str,
    now: str,
    labels: dict[str, Any],
    content_hash: str,
) -> None:
    adj_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(adjudications)")}
    if "content_hash" in adj_cols and "link_status" in adj_cols:
        conn.execute(
            """
            INSERT INTO adjudications (
                window_id, adjudicated_at, turn_kind, user_stance, agent_stance,
                prior_outcome, notes, source, content_hash, link_status, orphaned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'linked', NULL)
            ON CONFLICT(window_id) DO UPDATE SET
                adjudicated_at = excluded.adjudicated_at,
                turn_kind = excluded.turn_kind,
                user_stance = excluded.user_stance,
                agent_stance = excluded.agent_stance,
                prior_outcome = excluded.prior_outcome,
                notes = excluded.notes,
                source = excluded.source,
                content_hash = excluded.content_hash,
                link_status = 'linked',
                orphaned_at = NULL
            """,
            (
                window_id,
                now,
                json.dumps(labels["turn_kind"], ensure_ascii=False),
                labels["user_stance"],
                labels["agent_stance"],
                labels["prior_outcome"],
                labels["notes"],
                labels["source"],
                content_hash,
            ),
        )
        return
    conn.execute(
        """
        INSERT INTO adjudications (
            window_id, adjudicated_at, turn_kind, user_stance, agent_stance,
            prior_outcome, notes, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(window_id) DO UPDATE SET
            adjudicated_at = excluded.adjudicated_at,
            turn_kind = excluded.turn_kind,
            user_stance = excluded.user_stance,
            agent_stance = excluded.agent_stance,
            prior_outcome = excluded.prior_outcome,
            notes = excluded.notes,
            source = excluded.source
        """,
        (
            window_id,
            now,
            json.dumps(labels["turn_kind"], ensure_ascii=False),
            labels["user_stance"],
            labels["agent_stance"],
            labels["prior_outcome"],
            labels["notes"],
            labels["source"],
        ),
    )


@router.post("/api/adjudication/{window_id}")
def save_adjudication(
    window_id: str,
    body: dict[str, Any],
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    labels = _validate_labels(body)
    now = _utc_now()

    def _write() -> str:
        content_hash = _window_content_hash(conn, window_id)
        if content_hash is None:
            raise HTTPException(status_code=404, detail="window not found")
        _upsert_adjudication(
            conn,
            window_id=window_id,
            now=now,
            labels=labels,
            content_hash=content_hash,
        )
        return content_hash

    try:
        content_hash = with_busy_retry(_write, conn=conn)
    except HTTPException:
        raise
    except sqlite3.OperationalError as exc:
        if is_sqlite_busy(exc):
            raise HTTPException(
                status_code=503,
                detail="database busy — retry save; your labels were not lost",
            ) from exc
        raise

    llm = _llm_labels(conn, [window_id]).get(window_id)
    return {
        "window_id": window_id,
        "adjudicated_at": now,
        "turn_kind": labels["turn_kind"],
        "user_stance": labels["user_stance"],
        "agent_stance": labels["agent_stance"],
        "prior_outcome": labels["prior_outcome"],
        "notes": labels["notes"],
        "source": labels["source"],
        "triage": labels.get("triage"),
        "content_hash": content_hash,
        "link_status": "linked",
        "llm": llm,
    }


@router.get("/api/adjudication/report")
def adjudication_report(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    pack_path = _audit_pack_path(request)
    if not pack_path.is_file():
        rebuild_adjudicable_pack(conn, pack_path)
    pack = _load_jsonl(pack_path)
    window_ids = [str(r["window_id"]) for r in pack]
    llm_by_id = _llm_labels(conn, window_ids)
    adj_by_id = _adjudications(conn, window_ids)

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for wid in window_ids:
        human = adj_by_id.get(wid)
        llm = llm_by_id.get(wid)
        if human is None or llm is None:
            continue
        # Triage no/unclear clears enums on purpose — not a label disagreement.
        notes = str(human.get("notes") or "")
        if "triage:no_human" in notes or "triage:unclear_human" in notes:
            continue
        pairs.append((human, llm))

    return build_report(
        pairs,
        queue_total=len(window_ids),
        adjudicated=sum(1 for wid in window_ids if wid in adj_by_id),
        with_llm=len(pairs),
    )
