"""Deterministic evidence preparation for the harness coach.

This module deliberately stops at a file-based handoff. It reads the local
SQLite corpus, redacts text, and writes run-local packets. It does not infer a
claim, persist labels, or call a model.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from agentlog.session_identity import (
    build_identity_context,
    logical_root_session_id,
    logical_projection,
    provider_backing_shadow_ids,
    provider_canonical_root_backing_ids,
    provider_root_shadow_ids,
    resolve_implicit_parent_ids,
    is_internal_approval_guardian,
)
from agentlog.source_reader import CachedSourceTranscriptReader
from agentlog.analysis.coach.proof import (
    EVIDENCE_MESSAGE,
    EVIDENCE_SKILL,
    EVIDENCE_TOOL,
    is_causally_later,
    is_successful_artifact_result,
    supports_verification_result,
    supports_bounded_gap,
    supports_skill_action,
    supports_successful_result,
)
from agentlog.analysis.extractors.patterns import classify_request_text
from agentlog.normalize.synthetic import classify_synthetic_user_text
from agentlog.registry import supports as harness_supports
from agentlog.analysis.coach.redaction import COACH_REDACTION_VERSION, redact_locator_text
from agentlog.safety.redaction import REDACTION_VERSION, RedactionReport, redact_text
from agentlog.safety.write_guard import assert_writable, write_text

SCHEMA_VERSION = "coach.preprocess.v1"
PROMPT_VERSION = "coach.result.v1"
COACH_KINDS = (
    "instruction_follow",
    "instruction_miss",
    "repeated_ask",
    "skill_use",
    "delivery_gap",
    "verification",
    "process_fact",
)
_EXCLUDED_MARKERS = ("auto-review", "auto_review", "auto review", "worker-brief", "worker_brief", "worker brief")
_SENTIMENT_MARKERS = ("sentiment", "mood", "emotion", "tone", "feelings", "happy", "angry")
_REQUIRED_ARCS = {
    "instruction_follow": {"request", "response", "outcome"},
    "instruction_miss": {"request", "response", "gap"},
    "repeated_ask": {"request_1", "request_2"},
    "skill_use": {"skill_request", "skill_evidence", "skill_action"},
    "delivery_gap": {"expectation", "delivery"},
    "verification": {"verification_request", "verification_result"},
    "process_fact": {"action", "artifact"},
}
_ARC_EVIDENCE_RULES = {
    "request": {("message", "user")}, "response": {("message", "assistant")},
    "outcome": {("message", "user"), ("tool", "")},
    "gap": {("message", "user"), ("tool", "")},
    "request_1": {("message", "user")}, "request_2": {("message", "user")},
    "skill_request": {("message", "user")},
    "skill_evidence": {("skill", "")},
    "skill_action": {("tool", "")},
    "expectation": {("message", "user")},
    "delivery": {("message", "user"), ("tool", "")},
    "verification_request": {("message", "user")},
    "verification_result": {("message", "user"), ("tool", "")},
    "action": {("tool", "")},
    "artifact": {("tool", "")},
}
_SIGNAL_TERMS = {
    "verify": 5, "test": 4, "requested": 3, "must": 3, "only": 2,
    "follow": 3, "miss": 5, "didn't": 5, "did not": 5, "still": 3,
    "again": 3, "already": 2, "skill": 3, "done": 2, "complete": 2,
    "commit": 2, "diff": 2, "proof": 3,
}
_MAX_TOOL_EVENTS_PER_WINDOW = 64
_MAX_SKILL_EXPOSURES_PER_WINDOW = 64
_DROP_REQUEST_KINDS = frozenset({
    "task_notification", "continue_stub", "realtime_delegation", "auto_review",
    "worker_brief", "inter_agent_handoff", "image_only",
})
_URL_RE = re.compile(r"\b(?:https?|ssh|git)://[^\s\"'<>]+", re.IGNORECASE)
_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:[A-Za-z0-9._~-]+/)+[A-Za-z0-9._~-]+")

COACH_RESULT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["packet_id", "result_id", "abstain", "producer", "window_dispositions"],
    "properties": {
        "packet_id": {"type": "string"},
        "result_id": {"type": "string"},
        "abstain": {"type": "boolean"},
        "abstain_reason": {"type": "string"},
        "producer": {
            "type": "object",
            "required": ["provider", "model", "prompt_hash"],
            "properties": {"provider": {"type": "string"}, "model": {"type": "string"}, "prompt_hash": {"type": "string"}, "worker_id": {"type": "string"}, "assignment_id": {"type": "string"}},
        },
        "window_dispositions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["window_id", "observation_ids", "no_supported_observation"],
                "properties": {
                    "window_id": {"type": "string"},
                    "observation_ids": {"type": "array", "items": {"type": "string"}},
                    "no_supported_observation": {"type": "boolean"},
                },
            },
        },
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["observation_id", "kind", "assertion_key", "confidence", "does_not_prove", "evidence", "proof_arcs"],
                "properties": {
                    "observation_id": {"type": "string"},
                    "kind": {"enum": list(COACH_KINDS)},
                    "assertion_key": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "does_not_prove": {"type": "string"},
                    "evidence": {"type": "array"},
                    "proof_arcs": {"type": "array"},
                },
            },
        },
    },
    "x-required-proof-arcs": {kind: sorted(arcs) for kind, arcs in _REQUIRED_ARCS.items()},
}

COACH_PROMPT = """You are an evidence-bound harness coach. Use only the redacted packet.
Return JSON matching coach.result.v1. You may abstain. Every observation needs
an assertion_key, confidence in [0,1], a concrete does_not_prove limitation,
exact evidence references (message_id, window_id, role, seq, quote), and proof_arcs.
Tool-event and skill-exposure references may use their immutable event ID and
fact string; use these deterministic facts for delivery, verification, skill,
and artifact/process proof instead of trusting an assistant self-report. A
skill_use requires both exposure and attributable action/tool evidence.
For a process_fact, both action and artifact arcs must cite the same successful
artifact-producing tool fact; an assistant message cannot fill either arc.
Verification results and completion/outcome arcs require a deterministic tool
result or later owner evidence; an assistant self-report alone is insufficient.
Miss and delivery gaps require a failed deterministic result or later owner correction.
Each proof arc has an arc label and evidence_refs; use the required labels for
the observation kind. Do not infer intention from a request alone: follow,
miss, delivery, and verification require response/outcome evidence. Do not
emit sentiment, mood, emotion, or tone observations. Keep each packet's root
independent; never cite another root. Abstain when the packet cannot support a
complete proof arc.
Every result must include window_dispositions with exactly one entry for every
local packet window (never root_request_index context). Give each observation a
unique observation_id. A disposition lists the exact observation_ids supported
by that local window, or sets no_supported_observation true with an empty list.
If abstaining, every local window disposition must explicitly declare no
supported observation.
"""


@dataclass(frozen=True)
class CoachPreprocessConfig:
    publication_mode: str = "full"
    max_windows_per_root: int | None = None
    max_windows_per_packet: int = 24
    max_packet_chars: int | None = 1_500_000
    max_quote_chars: int | None = None
    max_packets: int | None = None
    producer_provider: str = "openai"
    producer_model: str = "gpt-5.6-luna"
    producer_worker_id: str = "luna-extraction"
    producer_assignment_id: str = "luna-extraction"
    source_transcript_reader: Callable[[sqlite3.Connection, str], Any] | None = None


_SAMPLED_WINDOWS_PER_ROOT = 8
_SAMPLED_QUOTE_CHARS = 800


@dataclass(frozen=True)
class ValidationFailure:
    reason: str
    observation_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "observation_index": self.observation_index}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _short_hash(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:24]


def _path_label(kind: str, value: str) -> str:
    return f"{kind}:{_short_hash(value)}" if value else ""


def _repo_label(value: str) -> str:
    return _path_label("repo", value) if value else ""


def _external_label(value: str) -> str:
    return _path_label("external", value) if value else ""


def _redact_transcript(text: str, report: RedactionReport) -> str:
    return redact_locator_text(text, report)


redact_coach_text = redact_locator_text


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _excluded_label(*values: Any) -> bool:
    blob = " ".join(str(v or "").lower().replace("/", " ") for v in values)
    return any(marker in blob for marker in _EXCLUDED_MARKERS)


def _synthetic_request_kind(text: str) -> str:
    flags = classify_synthetic_user_text(text)
    if flags.authored_by_agent or flags.is_tool_plumbing:
        return "shared_synthetic"
    kind = classify_request_text(text).kind
    return kind if kind in _DROP_REQUEST_KINDS else ""


def _repeated_request_theme_matches(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    aliases = {
        "test": "verification", "tests": "verification", "testing": "verification",
        "verify": "verification", "verified": "verification",
    }
    noise = {"again", "and", "please", "run", "the", "this", "that", "then", "to"}
    def terms(entry: Mapping[str, Any]) -> set[str]:
        return {
            aliases.get(token, token)
            for token in re.findall(r"[a-z0-9][a-z0-9_-]*", str(entry.get("quote") or "").lower())
            if len(token) >= 4 and token not in noise
        }
    return len(terms(first) & terms(second)) >= 2


def _aware_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _timestamp_sort_key(value: Any) -> tuple[int, str]:
    parsed = _aware_timestamp(value)
    return (0, parsed.isoformat()) if parsed is not None else (1, str(value or ""))


def _session_roots(rows: list[sqlite3.Row]) -> tuple[dict[str, str], dict[str, sqlite3.Row]]:
    by_id = {str(r["id"]): r for r in rows}
    parents = resolve_implicit_parent_ids(rows)
    roots: dict[str, str] = {}
    for sid in sorted(by_id):
        cur, seen = sid, set()
        while cur in parents and cur not in seen:
            seen.add(cur)
            cur = parents[cur]
        roots[sid] = cur if cur in by_id else sid
    return roots, by_id


def _source_backed_session_ids(sessions: Iterable[sqlite3.Row]) -> set[str]:
    source_backed: set[str] = set()
    for session in sessions:
        columns = set(session.keys())
        if "transcript_storage" not in columns:
            continue
        mode = session["transcript_storage"]
        if mode not in {"legacy_materialized", "source_backed"}:
            raise ValueError(
                "coach_source_transcript_storage_invalid: "
                f"session_id={session['id']}"
            )
        if mode == "source_backed":
            source_backed.add(str(session["id"]))
    return source_backed


def _source_messages(
    conn: sqlite3.Connection,
    session_ids: Iterable[str],
    *,
    reader: Callable[[sqlite3.Connection, str], Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, str]]]:
    messages_by_session: dict[str, list[dict[str, Any]]] = {}
    provenance: dict[str, dict[str, str]] = {}
    required = {
        "id", "seq", "role", "timestamp", "model", "model_canonical", "effort",
        "text", "content_hash", "is_tool_plumbing", "authored_by_agent",
    }
    for session_id in sorted(set(session_ids)):
        result = reader(conn, session_id)
        status = str(getattr(result, "status", "unreadable"))
        if status != "ready":
            raise ValueError(
                "coach_source_transcript_unavailable: "
                f"session_id={session_id} status={status}"
            )
        source_identity = str(getattr(result, "source_identity", "") or "")
        source_hash = str(getattr(result, "source_hash", "") or "")
        if not source_identity or not source_hash:
            raise ValueError(
                "coach_source_transcript_provenance_missing: "
                f"session_id={session_id}"
            )
        raw_messages = getattr(result, "messages", None)
        if not isinstance(raw_messages, list):
            raise ValueError(
                "coach_source_transcript_messages_invalid: "
                f"session_id={session_id}"
            )
        parsed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw in raw_messages:
            if not isinstance(raw, Mapping) or not required.issubset(raw):
                raise ValueError(
                    "coach_source_transcript_message_invalid: "
                    f"session_id={session_id}"
                )
            message_id = str(raw["id"] or "")
            text = raw["text"]
            if not message_id or message_id in seen_ids or not isinstance(text, str):
                raise ValueError(
                    "coach_source_transcript_message_invalid: "
                    f"session_id={session_id}"
                )
            seen_ids.add(message_id)
            parsed.append(dict(raw, id=message_id, session_id=session_id, text=text))
        parsed.sort(key=lambda message: (int(message["seq"]), str(message["id"])))
        messages_by_session[session_id] = parsed
        provenance[session_id] = {
            "source_identity": source_identity,
            "source_hash": source_hash,
        }
    return messages_by_session, provenance


def _validate_source_message_ledger(
    conn: sqlite3.Connection,
    messages_by_session: Mapping[str, list[dict[str, Any]]],
) -> None:
    for session_id, messages in messages_by_session.items():
        persisted = conn.execute(
            "SELECT id, seq, role, timestamp, content_hash, is_tool_plumbing, authored_by_agent "
            "FROM messages WHERE session_id = ? ORDER BY seq, id",
            (session_id,),
        ).fetchall()
        if len(persisted) != len(messages):
            raise ValueError(
                "coach_source_transcript_ledger_mismatch: "
                f"session_id={session_id}"
            )
        for stored, source in zip(persisted, messages):
            if (
                str(stored["id"]) != str(source["id"])
                or int(stored["seq"]) != int(source["seq"])
                or str(stored["role"] or "") != str(source["role"] or "")
                or str(stored["timestamp"] or "") != str(source["timestamp"] or "")
                or str(stored["content_hash"] or "") != str(source["content_hash"] or "")
                or bool(stored["is_tool_plumbing"]) != bool(source["is_tool_plumbing"])
                or bool(stored["authored_by_agent"]) != bool(source["authored_by_agent"])
            ):
                raise ValueError(
                    "coach_source_transcript_ledger_mismatch: "
                    f"session_id={session_id}"
                )


def _linked_t3_sessions(conn: sqlite3.Connection) -> set[str]:
    if not _table_exists(conn, "session_links"):
        return set()
    cols = [str(r[1]) for r in conn.execute("PRAGMA table_info(session_links)")]
    if not cols:
        return set()
    rows = conn.execute("SELECT * FROM session_links").fetchall()
    out: set[str] = set()
    for row in rows:
        vals = {c: str(row[c] or "") for c in cols}
        blob = " ".join(vals.values()).lower()
        duplicate = any(x in blob for x in ("duplicate", "mirror", "same_content", "t3_duplicate"))
        if ("t3" in blob and duplicate) or any(k in vals and vals[k].lower() in {"1", "true", "yes"} for k in ("is_duplicate", "duplicate")):
            for key in ("session_id", "linked_session_id", "root_session_id", "source_session_id", "target_session_id"):
                if vals.get(key):
                    out.add(vals[key])
    return out


def _signal_score(user: str, assistant: str) -> int:
    blob = f"{user} {assistant}".lower()
    score = sum(weight for term, weight in _SIGNAL_TERMS.items() if term in blob)
    return score + min(len(user) // 160, 4) + min(len(assistant) // 240, 4)


def _artifact_meta(conn: sqlite3.Connection, artifact_id: Any) -> dict[str, Any]:
    if artifact_id is None or not _table_exists(conn, "artifacts"):
        return {"artifact_id": artifact_id, "artifact_hash": None, "parser_version": None, "artifact_path": None}
    row = conn.execute("SELECT id, path, content_hash, parser_version FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    if row is None:
        return {"artifact_id": artifact_id, "artifact_hash": None, "parser_version": None, "artifact_path": None}
    return {"artifact_id": row["id"], "artifact_hash": row["content_hash"], "parser_version": row["parser_version"], "artifact_path": str(row["path"] or "")}


def _session_context(conn: sqlite3.Connection) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    tools: dict[str, list[dict[str, Any]]] = {}
    skills: dict[str, list[dict[str, Any]]] = {}
    if _table_exists(conn, "tool_events"):
        tool_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(tool_events)")
        }
        has_operation_kind = "operation_kind" in tool_columns
        operation_select = ", operation_kind" if has_operation_kind else ""
        for row in conn.execute(
            "SELECT id, session_id, message_id, seq, tool_name, action, success, duration_ms"
            f"{operation_select} FROM tool_events ORDER BY session_id, seq, id"
        ):
            tools.setdefault(str(row["session_id"]), []).append(
                {
                    "tool_event_id": str(row["id"]), "message_id": row["message_id"],
                    "seq": row["seq"], "tool_name": str(row["tool_name"] or ""),
                    "action": str(row["action"] or ""), "success": row["success"],
                    "duration_ms": row["duration_ms"],
                    "operation_kind": (
                        str(row["operation_kind"] or "unknown")
                        if has_operation_kind else "unknown"
                    ),
                }
            )
    if _table_exists(conn, "skill_exposures"):
        for row in conn.execute(
            "SELECT id, session_id, message_id, skill_name, exposure_type "
            "FROM skill_exposures ORDER BY session_id, id"
        ):
            skills.setdefault(str(row["session_id"]), []).append(
                {
                    "skill_exposure_id": str(row["id"]), "message_id": row["message_id"],
                    "skill_name": str(row["skill_name"] or ""),
                    "exposure_type": str(row["exposure_type"] or ""),
                }
            )
    return tools, skills


def _projection_source_ids(
    conn: sqlite3.Connection,
    sessions: dict[str, sqlite3.Row],
    *,
    identity: Any,
) -> set[str]:
    excluded: set[str] = set()
    for sid, session in sessions.items():
        if str(session["harness"] or "") != "t3code":
            continue
        projection = logical_projection(conn, sid, "t3code", context=identity)
        transcript_id = projection["transcript_session_id"]
        if transcript_id:
            excluded.add(sid)
            continue
        if session["transcript_storage"] == "source_backed":
            canonical_backing = identity.canonical_root_backing_by_source.get(sid)
            if canonical_backing:
                excluded.add(canonical_backing)
    return excluded


def _copied_history_duplicate_windows(
    rows: Iterable[sqlite3.Row],
    *,
    logical_roots: Mapping[str, str],
    sessions: Mapping[str, sqlite3.Row],
) -> dict[str, str]:
    parents = resolve_implicit_parent_ids(sessions.values())

    def parent(session_id: str) -> str:
        return parents.get(session_id, "")

    def ancestor(older: str, newer: str) -> bool:
        current = newer
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            current = parent(current)
            if current == older:
                return True
        return False

    grouped: dict[tuple[str, str, str, int, str], list[sqlite3.Row]] = {}
    for row in rows:
        session_id = str(row["session_id"] or "")
        request_hash = str(row["req_hash"] or "")
        response_hash = str(row["resp_hash"] or "")
        timestamp = str(row["req_timestamp"] or "")
        if not session_id or not request_hash or not response_hash or not timestamp:
            continue
        grouped.setdefault(
            (
                str(logical_roots.get(session_id) or ""), request_hash, response_hash,
                int(row["req_seq"]), timestamp,
            ),
            [],
        ).append(row)
    duplicates: dict[str, str] = {}
    for values in grouped.values():
        for older in values:
            for newer in values:
                older_session = str(older["session_id"] or "")
                newer_session = str(newer["session_id"] or "")
                if older_session == newer_session or not ancestor(older_session, newer_session):
                    continue
                duplicates[str(newer["window_id"])] = str(older["window_id"])
    return dict(sorted(duplicates.items()))


def _corpus_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    counts: dict[str, int] = {}
    high_water: dict[str, dict[str, Any]] = {}
    for table in ("artifacts", "sessions", "messages", "exchange_windows"):
        if not _table_exists(conn, table):
            counts[table] = 0
            high_water[table] = {}
            continue
        row = conn.execute(f"SELECT COUNT(*) AS count, MAX(rowid) AS max_rowid FROM {table}").fetchone()
        counts[table] = int(row["count"] or 0)
        high_water[table] = {"max_rowid": row["max_rowid"]}
        if table == "artifacts":
            extra = conn.execute("SELECT MAX(id) AS max_id, MAX(mtime_ns) AS max_mtime_ns FROM artifacts").fetchone()
            high_water[table].update({"max_id": extra["max_id"], "max_mtime_ns": extra["max_mtime_ns"]})
        elif table in {"sessions", "messages"}:
            extra = conn.execute(f"SELECT MAX(seq) AS max_seq, MAX(timestamp) AS max_timestamp FROM {table}" if table == "messages" else "SELECT MAX(started_at) AS max_started_at FROM sessions").fetchone()
            high_water[table].update(dict(extra))
        elif table == "exchange_windows":
            extra = conn.execute("SELECT MAX(id) AS max_id FROM exchange_windows").fetchone()
            high_water[table]["max_id"] = extra["max_id"]
    artifact_rows = []
    if _table_exists(conn, "artifacts"):
        artifact_rows = [
            {"id": row["id"], "content_hash": row["content_hash"], "parser_version": row["parser_version"]}
            for row in conn.execute("SELECT id, content_hash, parser_version FROM artifacts ORDER BY id")
        ]
    body = {"counts": counts, "high_water": high_water, "artifacts": artifact_rows}
    body["snapshot_hash"] = _short_hash(json.dumps(body, sort_keys=True, ensure_ascii=False))
    return body


build_corpus_snapshot = _corpus_snapshot


def _publication_limits(cfg: CoachPreprocessConfig) -> tuple[int | None, int | None]:
    mode = str(cfg.publication_mode or "").strip().lower()
    if mode not in {"full", "sampled"}:
        raise ValueError("publication_mode must be full or sampled")
    window_limit = cfg.max_windows_per_root
    quote_limit = cfg.max_quote_chars
    if mode == "sampled":
        window_limit = _SAMPLED_WINDOWS_PER_ROOT if window_limit is None else window_limit
        quote_limit = _SAMPLED_QUOTE_CHARS if quote_limit is None else quote_limit
    if window_limit is not None and (
        not isinstance(window_limit, int) or isinstance(window_limit, bool) or window_limit < 1
    ):
        raise ValueError("max_windows_per_root must be positive when bounded")
    if quote_limit is not None and (
        not isinstance(quote_limit, int) or isinstance(quote_limit, bool) or quote_limit < 1
    ):
        raise ValueError("max_quote_chars must be positive when bounded")
    if (
        not isinstance(cfg.max_windows_per_packet, int)
        or isinstance(cfg.max_windows_per_packet, bool)
        or cfg.max_windows_per_packet < 1
    ):
        raise ValueError("max_windows_per_packet must be positive")
    if cfg.max_packet_chars is not None and (
        not isinstance(cfg.max_packet_chars, int)
        or isinstance(cfg.max_packet_chars, bool)
        or cfg.max_packet_chars < 1
    ):
        raise ValueError("max_packet_chars must be positive when bounded")
    if cfg.max_packets is not None and (
        not isinstance(cfg.max_packets, int)
        or isinstance(cfg.max_packets, bool)
        or cfg.max_packets < 0
    ):
        raise ValueError("max_packets must be nonnegative when bounded")
    return window_limit, quote_limit


def _select_per_root(items: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    if limit is None:
        return sorted(items, key=lambda x: (str(x["timestamp"] or ""), str(x["window_id"])))
    if len(items) <= limit:
        return sorted(items, key=lambda x: (str(x["timestamp"] or ""), str(x["window_id"])))
    ordered = sorted(items, key=lambda x: (str(x["timestamp"] or ""), str(x["window_id"])))
    chosen: list[dict[str, Any]] = []
    # Temporal strata preserve old, middle, and recent behavior alongside peaks.
    indices = [0, len(ordered) // 2, len(ordered) - 1]
    for idx in indices:
        if ordered[idx] not in chosen:
            chosen.append(ordered[idx])
    for item in sorted(items, key=lambda x: (-int(x["signal_score"]), str(x["window_id"]))):
        if item not in chosen:
            chosen.append(item)
        if len(chosen) >= limit:
            break
    return sorted(chosen[:limit], key=lambda x: (str(x["timestamp"] or ""), str(x["window_id"])))


def _window_rows(
    conn: sqlite3.Connection,
    report: RedactionReport,
    *,
    source_transcript_reader: Callable[[sqlite3.Connection, str], Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    sessions = list(conn.execute("SELECT * FROM sessions ORDER BY id"))
    roots, by_id = _session_roots(sessions)
    source_backed = _source_backed_session_ids(sessions)
    tool_context, skill_context = _session_context(conn)
    linked = _linked_t3_sessions(conn)
    identity = build_identity_context(conn)
    backing_shadows = provider_backing_shadow_ids(conn, context=identity)
    projection_sources = _projection_source_ids(conn, by_id, identity=identity)
    historical_root_backings = provider_root_shadow_ids(
        conn, context=identity
    ) - provider_canonical_root_backing_ids(conn, context=identity)
    logical_roots = {
        sid: logical_root_session_id(
            conn, roots.get(sid, sid), context=identity
        )
        for sid in by_id
    }
    excluded_roots: set[str] = set()
    excluded_sessions: set[str] = set()
    excluded_synthetic_windows: set[str] = set()
    excluded_synthetic_by_kind: dict[str, int] = {}
    for sid, row in by_id.items():
        root = roots[sid]
        if is_internal_approval_guardian(row):
            excluded_sessions.add(sid)
            if sid == root:
                excluded_roots.add(root)
            continue
        if _excluded_label(row["harness"], row["external_id"], row["model"], row["model_canonical"], row["agent_profile"]):
            excluded_sessions.add(sid)
            if sid == root:
                excluded_roots.add(root)
        if sid in linked:
            excluded_sessions.add(sid)
            if sid == root:
                excluded_roots.add(root)

    source_sessions = {
        session_id
        for session_id in source_backed
        if session_id not in excluded_sessions
        and session_id not in projection_sources
        and session_id not in historical_root_backings
        and roots.get(session_id, session_id) not in excluded_roots
    }
    source_messages, source_provenance = _source_messages(
        conn,
        source_sessions,
        reader=source_transcript_reader or CachedSourceTranscriptReader(),
    )
    _validate_source_message_ledger(conn, source_messages)
    legacy_sessions = sorted(set(by_id) - source_backed)
    legacy_placeholders = ", ".join("?" for _ in legacy_sessions)
    rows = conn.execute(
        """
        SELECT w.id AS window_id, w.session_id, w.input_hash, w.*,
               s.harness, s.external_id, s.repo, s.cwd, s.started_at, s.artifact_id,
               s.parent_session_id, s.model, s.model_canonical, s.provider, s.agent_profile,
               req.id AS req_id, req.role AS req_role, req.seq AS req_seq,
               req.timestamp AS req_timestamp, req.content_hash AS req_hash,
               req.model_canonical AS req_model_canonical, req.effort AS req_effort,
               req.is_tool_plumbing AS req_tool, req.authored_by_agent AS req_agent,
               resp.id AS resp_id, resp.role AS resp_role, resp.seq AS resp_seq,
               resp.timestamp AS resp_timestamp, resp.content_hash AS resp_hash,
               resp.model_canonical AS resp_model_canonical, resp.effort AS resp_effort,
               resp.is_tool_plumbing AS resp_tool, resp.authored_by_agent AS resp_agent
        FROM exchange_windows w
        JOIN sessions s ON s.id = w.session_id
        JOIN messages req ON req.id = w.request_message_id
        JOIN messages resp ON resp.id = w.response_message_id
        ORDER BY w.session_id, req.seq, w.id
        """
    ).fetchall()
    text_by_id: dict[str, str] = {}
    if legacy_sessions:
        text_rows = conn.execute(
            "SELECT id, text FROM messages WHERE session_id IN (" + legacy_placeholders + ")",
            legacy_sessions,
        ).fetchall()
        text_by_id = {str(message["id"]): str(message["text"] or "") for message in text_rows}
    source_text_by_id = {
        str(message["id"]): str(message["text"])
        for messages in source_messages.values()
        for message in messages
    }
    rows = [
        dict(
            row,
            req_text=(source_text_by_id if str(row["session_id"]) in source_backed else text_by_id).get(str(row["req_id"]), ""),
            resp_text=(source_text_by_id if str(row["session_id"]) in source_backed else text_by_id).get(str(row["resp_id"]), ""),
        )
        for row in rows
    ]
    total = len(rows)
    copied_duplicate_windows = _copied_history_duplicate_windows(
        rows, logical_roots=logical_roots, sessions=by_id
    )
    out: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    inspected = 0
    session_messages: dict[str, list[dict[str, Any]]] = {}
    message_seq_by_id: dict[str, int] = {}
    message_by_id: dict[str, dict[str, Any]] = {}
    for message in conn.execute(
        "SELECT id, session_id, seq, role, timestamp, content_hash, model_canonical, effort, "
        "is_tool_plumbing, authored_by_agent FROM messages ORDER BY session_id, seq, id"
    ):
        session_id = str(message["session_id"])
        if session_id in source_backed:
            continue
        copied = dict(message)
        copied["text"] = str(text_by_id.get(str(message["id"]), "")) if legacy_sessions else ""
        session_messages.setdefault(session_id, []).append(copied)
        message_seq_by_id[str(message["id"])] = int(message["seq"])
        message_by_id[str(message["id"])] = copied
    for session_id, messages in source_messages.items():
        session_messages[session_id] = messages
        for message in messages:
            message_id = str(message["id"])
            message_seq_by_id[message_id] = int(message["seq"])
            message_by_id[message_id] = message
    session_models: dict[str, list[str]] = {}
    for session_id, messages in session_messages.items():
        for message in messages:
            if str(message["role"] or "") != "assistant":
                continue
            model = str(message.get("model_canonical") or message.get("model") or "")
            if model and model not in session_models.setdefault(session_id, []):
                session_models[session_id].append(model)
    for row in rows:
        sid = str(row["session_id"])
        if str(row["window_id"]) in copied_duplicate_windows:
            continue
        physical_root = roots.get(sid, sid)
        root = logical_roots.get(sid, physical_root)
        if (
            physical_root in excluded_roots
            or sid in excluded_sessions
            or sid in projection_sources
            or sid in historical_root_backings
        ):
            continue
        if row["req_role"] != "user" or row["resp_role"] != "assistant":
            continue
        if row["req_tool"] or row["resp_tool"] or row["req_agent"] or row["resp_agent"]:
            continue
        user_raw, asst_raw = str(row["req_text"] or ""), str(row["resp_text"] or "")
        if not user_raw.strip() or not asst_raw.strip():
            continue
        synthetic_kind = _synthetic_request_kind(user_raw)
        if synthetic_kind:
            excluded_synthetic_windows.add(str(row["window_id"]))
            excluded_synthetic_by_kind[synthetic_kind] = excluded_synthetic_by_kind.get(synthetic_kind, 0) + 1
            continue
        inspected += 1
        user, asst = _redact_transcript(user_raw, report), _redact_transcript(asst_raw, report)
        runtime_harness = str(row["harness"] or "")
        logical_harness = "t3code" if sid in backing_shadows else runtime_harness
        artifact = _artifact_meta(conn, row["artifact_id"])
        artifact_path = str(artifact.get("artifact_path") or "")
        artifact["artifact_path"] = _path_label("artifact", artifact_path)
        cwd_raw = str(row["cwd"] or "")
        cwd_label = _path_label("cwd", cwd_raw)
        request_seq = int(row["req_seq"])
        next_owner_request_seq = next(
            (
                int(message["seq"])
                for message in session_messages.get(sid, [])
                if int(message["seq"]) > request_seq
                and str(message["role"] or "") == "user"
                and not bool(message["is_tool_plumbing"])
                and not bool(message["authored_by_agent"])
                and not _synthetic_request_kind(str(message["text"] or ""))
            ),
            None,
        )
        window_end_seq = next_owner_request_seq if next_owner_request_seq is not None else 2**63 - 1
        timeline_messages: list[dict[str, Any]] = []
        for message in session_messages.get(sid, []):
            sequence = int(message["seq"])
            if sequence < request_seq or sequence >= window_end_seq:
                continue
            if not (
                (sequence == request_seq and str(message["role"] or "") == "user")
                or str(message["role"] or "") == "assistant"
            ):
                continue
            raw_text = str(message["text"] or "")
            redacted_text = _redact_transcript(raw_text, report)
            timeline_messages.append(
                {
                    "message_id": str(message["id"]), "role": str(message["role"] or ""),
                    "seq": sequence, "timestamp": message["timestamp"],
                    "source_text": redacted_text, "source_hash": _sha256(raw_text),
                    "content_hash": str(message["content_hash"] or _sha256(raw_text)),
                    "model_canonical": message["model_canonical"], "effort": message["effort"],
                }
            )
        tool_events: list[dict[str, Any]] = []
        for event in tool_context.get(sid, []):
            event_message_id = str(event.get("message_id") or "")
            event_message_seq = message_seq_by_id.get(event_message_id)
            bound_message = message_by_id.get(event_message_id)
            if event_message_seq is None or bound_message is None:
                continue
            role = str(bound_message["role"] or "")
            plumbing = bool(bound_message["is_tool_plumbing"])
            if (
                event_message_seq < request_seq
                or event_message_seq >= window_end_seq
                or not (role == "assistant" or (role == "user" and plumbing))
            ):
                continue
            tool_events.append(
                dict(
                    event,
                    tool_name=_redact_transcript(event["tool_name"], report),
                    action=_redact_transcript(event["action"], report),
                    message_seq=event_message_seq,
                    message_role=role,
                    message_is_tool_plumbing=plumbing,
                    message_timestamp=bound_message["timestamp"],
                    request_seq=request_seq,
                    window_end_seq=None if next_owner_request_seq is None else next_owner_request_seq - 1,
                )
            )
            if len(tool_events) >= _MAX_TOOL_EVENTS_PER_WINDOW:
                break
        skill_exposures: list[dict[str, Any]] = []
        for exposure in skill_context.get(sid, []):
            message_id = exposure.get("message_id")
            message_seq = message_seq_by_id.get(str(message_id or ""))
            bound_message = message_by_id.get(str(message_id or ""))
            if (
                message_seq is None
                or bound_message is None
                or message_seq < request_seq
                or message_seq >= window_end_seq
            ):
                continue
            role = str(bound_message["role"] or "")
            plumbing = bool(bound_message["is_tool_plumbing"])
            agent_authored = bool(bound_message["authored_by_agent"])
            exposure_type = str(exposure.get("exposure_type") or "").strip().lower().replace("-", "_")
            attributable = (
                exposure_type == "attached"
                and str(message_id or "") == str(row["req_id"])
                and role == "user"
                and not plumbing
                and not agent_authored
            ) or (
                exposure_type == "injected"
                and role == "user"
                and plumbing
            ) or (
                exposure_type in {"tool_use", "loaded", "activated", "invoked"}
                and role == "assistant"
            )
            if not attributable:
                continue
            skill_exposures.append(
                dict(
                    exposure,
                    skill_name=_redact_transcript(exposure["skill_name"], report),
                    exposure_type=_redact_transcript(exposure["exposure_type"], report),
                    message_seq=message_seq,
                    message_role=role,
                    message_is_tool_plumbing=plumbing,
                    message_timestamp=bound_message["timestamp"],
                    request_message_id=str(row["req_id"]),
                )
            )
            if len(skill_exposures) >= _MAX_SKILL_EXPOSURES_PER_WINDOW:
                break
        item = {
            "window_id": str(row["window_id"]), "session_id": sid, "root_session_id": root,
            "physical_root_session_id": physical_root,
            "harness": logical_harness, "logical_harness": logical_harness, "runtime_harness": runtime_harness,
            "repo": _repo_label(str(row["repo"] or "")),
            "cwd": cwd_label, "timestamp": row["req_timestamp"] or row["started_at"],
            "request_seq": request_seq, "response_seq": int(row["resp_seq"]),
            "window_end_seq": None if next_owner_request_seq is None else next_owner_request_seq - 1,
            "input_hash": str(row["input_hash"] or ""),
            "content_hash": str(row["content_hash"] or _short_hash(f"{sid}\n{user_raw}\n{asst_raw}")),
            "artifact": artifact, "signal_score": _signal_score(user_raw, asst_raw),
            "tool_timeline": tool_events, "skill_exposures": skill_exposures,
            "skills_loaded": sorted({str(exposure["skill_name"]) for exposure in skill_exposures}),
            "source_provenance": dict(source_provenance.get(sid, {})),
            "session": {
                "session_id": sid, "external_id": _external_label(str(row["external_id"] or "")),
                "harness": runtime_harness, "logical_harness": logical_harness, "runtime_harness": runtime_harness,
                "parent_session_id": _external_label(str(row["parent_session_id"] or "")),
                "root_session_id": root, "physical_root_session_id": physical_root,
                "artifact_id": artifact.get("artifact_id"),
                "repo": _repo_label(str(row["repo"] or "")), "model": row["model"],
                "model_canonical": row["model_canonical"], "provider": row["provider"],
                "agent_profile": row["agent_profile"],
                "models_seen": session_models.get(sid, []),
                "source_provenance": dict(source_provenance.get(sid, {})),
            },
            "messages": [
                {"message_id": str(row["req_id"]), "role": "user", "seq": int(row["req_seq"]), "timestamp": row["req_timestamp"], "source_text": user, "source_hash": _sha256(user_raw), "content_hash": str(row["req_hash"] or _sha256(user_raw)), "model_canonical": None, "effort": None},
                {"message_id": str(row["resp_id"]), "role": "assistant", "seq": int(row["resp_seq"]), "timestamp": row["resp_timestamp"], "source_text": asst, "source_hash": _sha256(asst_raw), "content_hash": str(row["resp_hash"] or _sha256(asst_raw)), "model_canonical": row["resp_model_canonical"], "effort": row["resp_effort"]},
            ],
            "message_timeline": timeline_messages,
        }
        item["user"], item["assistant"] = user, asst
        out.append(item)
        key = f"{item['harness']}\0{item['repo']}"
        counts[key] = counts.get(key, 0) + 1
    meta = {
        "roots": roots, "logical_roots": logical_roots, "sessions": by_id,
        "excluded_roots": sorted(excluded_roots), "excluded_sessions": sorted(excluded_sessions),
        "backing_shadows": sorted(backing_shadows), "projection_sources": sorted(projection_sources),
        "historical_root_backings": sorted(historical_root_backings),
        "source_backed_sessions": sorted(source_backed),
        "source_provenance": dict(sorted(source_provenance.items())),
        "excluded_synthetic_window_ids": sorted(excluded_synthetic_windows),
        "excluded_synthetic_by_kind": dict(sorted(excluded_synthetic_by_kind.items())),
        "excluded_duplicate_window_ids": sorted(copied_duplicate_windows),
        "duplicate_window_canonical_ids": copied_duplicate_windows,
        "total": total, "joined_windows": len(rows), "scanned": inspected,
        "inspected_windows": inspected,
    }
    return out, counts, meta


def _packet_window(item: dict[str, Any], max_quote: int | None) -> dict[str, Any]:
    # Keep both normalized message records and convenience fields for audits.
    result = dict(item)
    messages: list[dict[str, Any]] = []
    source_messages = item.get("message_timeline") or item["messages"]
    for message in source_messages:
        source = str(message["source_text"])
        emitted = source if max_quote is None else source[:max_quote]
        messages.append(
            dict(
                message,
                source_text=emitted,
                emitted_source_hash=_sha256(emitted),
                source_truncated=max_quote is not None and len(source) > max_quote,
            )
        )
    result["messages"] = messages
    canonical = {str(message.get("message_id") or ""): message for message in result["messages"]}
    request_id = str(item["messages"][0].get("message_id") or "")
    response_id = str(item["messages"][1].get("message_id") or "")
    result["request"] = dict(canonical.get(request_id, result["messages"][0]))
    result["response"] = dict(canonical.get(response_id, result["messages"][1]))
    result["user"] = result["request"]["source_text"]
    result["assistant"] = result["response"]["source_text"]
    result["artifact_provenance"] = dict(result.get("artifact") or {})
    result["tool_timeline"] = [
        dict(event, evidence_type="tool", fact=json.dumps(event, sort_keys=True, ensure_ascii=False))
        for event in item.get("tool_timeline", [])
    ]
    result["skill_exposures"] = [
        dict(exposure, evidence_type="skill", fact=json.dumps(exposure, sort_keys=True, ensure_ascii=False))
        for exposure in item.get("skill_exposures", [])
    ]
    return result


def _root_request_index(
    items: Iterable[Mapping[str, Any]], max_quote: int | None
) -> dict[str, list[dict[str, Any]]]:
    by_root: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        root = str(item.get("root_session_id") or "")
        if not root:
            continue
        packet_window = _packet_window(dict(item), max_quote)
        request = dict(packet_window["request"])
        by_root.setdefault(root, []).append(
            {
                "window_id": str(packet_window.get("window_id") or ""),
                "session_id": str(packet_window.get("session_id") or ""),
                "root_session_id": root,
                "timestamp": str(packet_window.get("timestamp") or request.get("timestamp") or ""),
                "request": request,
                "response_message_id": str(packet_window["response"].get("message_id") or ""),
                "response_seq": packet_window["response"].get("seq"),
                "response_model_canonical": str(packet_window["response"].get("model_canonical") or ""),
                "response_effort": str(packet_window["response"].get("effort") or ""),
            }
        )
    for entries in by_root.values():
        entries.sort(key=lambda entry: (str(entry["timestamp"]), str(entry["window_id"])))
    return dict(sorted(by_root.items()))


_PACKET_SIZING_RUN_ID = "coach_00000000T000000Z_00000000"


def _producer_contract(cfg: CoachPreprocessConfig) -> dict[str, Any]:
    expected = {
        "provider": str(cfg.producer_provider or "").strip(),
        "model": str(cfg.producer_model or "").strip(),
        "worker_id": str(cfg.producer_worker_id or "").strip(),
        "assignment_id": str(cfg.producer_assignment_id or "").strip(),
        "prompt_hash": _short_hash(COACH_PROMPT),
    }
    return {
        "required": list(expected),
        "expected": expected,
        "bound": all(expected.values()),
    }


def _packet_body(
    group: list[dict[str, Any]],
    *,
    packet_id: str,
    run_id: str,
    cfg: CoachPreprocessConfig,
    quote_limit: int | None,
    producer_contract: Mapping[str, Any],
    corpus_snapshot: Mapping[str, Any],
    packet_snapshot: Mapping[str, Any],
    root_request_index: Mapping[str, list[dict[str, Any]]],
    report: RedactionReport,
) -> dict[str, Any]:
    packet_roots = sorted({str(item["root_session_id"]) for item in group})
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "packet_id": packet_id, "run_id": run_id,
        "prompt_version": PROMPT_VERSION, "prompt_hash": _short_hash(COACH_PROMPT),
        "coach_redaction_version": COACH_REDACTION_VERSION,
        "safety_redaction_version": REDACTION_VERSION,
        "producer_contract": dict(producer_contract),
        "corpus_snapshot_hash": corpus_snapshot["snapshot_hash"], "corpus_snapshot": dict(packet_snapshot),
        "root_session_ids": packet_roots,
        "publication_mode": cfg.publication_mode,
        "windows": [_packet_window(item, quote_limit) for item in group],
        "root_request_index": {
            root: root_request_index.get(root, []) for root in packet_roots
        },
        "redaction": report.to_dict(),
        "provenance": {"selection": "score_then_temporal_strata", "source": "sqlite", "parser": "stored artifact parser_version"},
    }
    body["packet_hash"] = _short_hash(json.dumps(body, sort_keys=True, ensure_ascii=False))
    return body


def _serialized_packet_size(body: Mapping[str, Any]) -> int:
    return len((json.dumps(body, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _packet_size_callback(
    cfg: CoachPreprocessConfig,
    quote_limit: int | None,
    root_request_index: Mapping[str, list[dict[str, Any]]],
    producer_contract: Mapping[str, Any],
    corpus_snapshot: Mapping[str, Any],
    report: RedactionReport,
    *,
    run_id: str = _PACKET_SIZING_RUN_ID,
) -> Callable[[list[dict[str, Any]], int], int]:
    packet_snapshot = {
        "snapshot_hash": corpus_snapshot["snapshot_hash"],
        "counts": corpus_snapshot["counts"],
        "high_water": corpus_snapshot["high_water"],
    }

    def packet_size(group: list[dict[str, Any]], packet_index: int) -> int:
        body = _packet_body(
            group,
            packet_id=f"cpkt_{packet_index:04d}",
            run_id=run_id,
            cfg=cfg,
            quote_limit=quote_limit,
            producer_contract=producer_contract,
            corpus_snapshot=corpus_snapshot,
            packet_snapshot=packet_snapshot,
            root_request_index=root_request_index,
            report=report,
        )
        return _serialized_packet_size(body)

    return packet_size


def _selection_packet_size(
    conn: sqlite3.Connection,
    cfg: CoachPreprocessConfig,
    eligible: list[dict[str, Any]],
    quote_limit: int | None,
    report: RedactionReport,
) -> Callable[[list[dict[str, Any]], int], int] | None:
    if cfg.max_packet_chars is None:
        return None
    return _packet_size_callback(
        cfg,
        quote_limit,
        _root_request_index(eligible, quote_limit),
        _producer_contract(cfg),
        _corpus_snapshot(conn),
        report,
    )


def load_coach_prompt() -> str:
    return COACH_PROMPT


def _split_root_packet_groups(
    root: str,
    items: list[dict[str, Any]],
    cfg: CoachPreprocessConfig,
    *,
    packet_index: int,
    packet_size: Callable[[list[dict[str, Any]], int], int] | None,
) -> list[list[dict[str, Any]]]:
    if cfg.max_packet_chars is None:
        return [
            items[index:index + cfg.max_windows_per_packet]
            for index in range(0, len(items), cfg.max_windows_per_packet)
        ]
    if packet_size is None:
        raise ValueError("max_packet_chars requires a packet size callback")
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for item in items:
        if len(current) >= cfg.max_windows_per_packet:
            groups.append(current)
            current = []
        candidate = [*current, item]
        candidate_size = packet_size(candidate, packet_index + len(groups))
        if candidate_size <= cfg.max_packet_chars:
            current = candidate
            continue
        if current:
            groups.append(current)
            current = [item]
            candidate_size = packet_size(current, packet_index + len(groups))
            if candidate_size <= cfg.max_packet_chars:
                continue
        raise ValueError(
            "coach_packet_byte_budget_exceeded: "
            f"root={root} window={item['window_id']} serialized_bytes={candidate_size} "
            f"max_packet_chars={cfg.max_packet_chars}"
        )
    if current:
        groups.append(current)
    return groups


def _selection_state(
    eligible: list[dict[str, Any]],
    cfg: CoachPreprocessConfig,
    *,
    packet_size: Callable[[list[dict[str, Any]], int], int] | None = None,
) -> tuple[
    list[dict[str, Any]],
    list[list[dict[str, Any]]],
    list[str],
    dict[str, int],
    dict[str, int],
]:
    window_limit, _ = _publication_limits(cfg)
    by_root: dict[str, list[dict[str, Any]]] = {}
    for item in eligible:
        by_root.setdefault(str(item["root_session_id"]), []).append(item)
    selected: list[dict[str, Any]] = []
    selected_by_root: dict[str, int] = {}
    for root in sorted(by_root):
        picked = _select_per_root(by_root[root], window_limit)
        selected.extend(picked)
        selected_by_root[root] = len(picked)
    selected.sort(key=lambda x: (str(x["root_session_id"]), str(x["timestamp"] or ""), str(x["window_id"])))
    root_items = {root: [x for x in selected if str(x["root_session_id"]) == root] for root in by_root}
    root_buckets: dict[tuple[str, str], list[str]] = {}
    for root, items in root_items.items():
        first = min(items, key=lambda x: (str(x["timestamp"] or ""), str(x["window_id"])))
        root_buckets.setdefault((str(first["harness"] or ""), str(first["repo"] or "")), []).append(root)
    for bucket in root_buckets:
        root_buckets[bucket].sort(
            key=lambda root: (
                -max(int(item["signal_score"]) for item in root_items[root]),
                min(str(item["timestamp"] or "") for item in root_items[root]),
                root,
            )
        )
    ordered_roots: list[str] = []
    buckets = sorted(root_buckets)
    while buckets:
        next_buckets: list[tuple[str, str]] = []
        for bucket in buckets:
            roots = root_buckets[bucket]
            if roots:
                ordered_roots.append(roots.pop(0))
            if roots:
                next_buckets.append(bucket)
        buckets = next_buckets
    groups: list[list[dict[str, Any]]] = []
    for root in ordered_roots:
        root_selected = root_items[root]
        groups.extend(
            _split_root_packet_groups(
                root,
                root_selected,
                cfg,
                packet_index=len(groups) + 1,
                packet_size=packet_size,
            )
        )
    if cfg.max_packets is not None:
        groups = groups[:max(0, cfg.max_packets)]
    packetized_ids = {str(item["window_id"]) for group in groups for item in group}
    emitted_selected = [item for item in selected if str(item["window_id"]) in packetized_ids]
    emitted_by_root: dict[str, int] = {}
    for item in emitted_selected:
        root = str(item["root_session_id"])
        emitted_by_root[root] = emitted_by_root.get(root, 0) + 1
    return selected, groups, ordered_roots, selected_by_root, emitted_by_root


def _scope_component(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return "" if normalized in {"", "unknown"} else normalized


def _scope_denominators(items: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    windows: dict[str, int] = {}
    roots: dict[str, set[str]] = {}
    for item in items:
        root = str(item.get("root_session_id") or "")
        scopes = {"global"}
        for prefix, value in (
            ("harness", item.get("harness")),
            ("repo", item.get("repo")),
        ):
            component = _scope_component(value)
            if component:
                scopes.add(f"{prefix}_{component}")
        messages = item.get("message_timeline") or item.get("messages") or []
        for message in messages:
            if not isinstance(message, Mapping) or str(message.get("role") or "") != "assistant":
                continue
            component = _scope_component(message.get("model_canonical"))
            if component:
                scopes.add(f"model_{component}")
        for scope in scopes:
            windows[scope] = windows.get(scope, 0) + 1
            if root:
                roots.setdefault(scope, set()).add(root)
    return {
        scope: {
            "eligible_roots": len(roots.get(scope, set())),
            "eligible_windows": windows[scope],
        }
        for scope in sorted(windows)
    }


def _eligibility_commitment(
    eligible: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    groups: list[list[dict[str, Any]]],
    meta: Mapping[str, Any],
    ordered_roots: list[str],
    selected_by_root: Mapping[str, int],
    emitted_by_root: Mapping[str, int],
    quote_limit: int | None,
) -> dict[str, Any]:
    emitted = [item for group in groups for item in group]
    request_index = _root_request_index(eligible, quote_limit)
    def counts(items: list[dict[str, Any]], key: str) -> dict[str, int]:
        values: dict[str, int] = {}
        for item in items:
            value = str(item.get(key) or "(unknown)")
            values[value] = values.get(value, 0) + 1
        return dict(sorted(values.items()))
    return {
        "eligible_root_ids": sorted({str(item["root_session_id"]) for item in eligible}),
        "selected_root_ids": sorted({str(item["root_session_id"]) for item in selected}),
        "packetized_root_ids": sorted({str(item["root_session_id"]) for item in emitted}),
        "ordered_root_ids": list(ordered_roots),
        "excluded_root_ids": sorted(str(value) for value in meta.get("excluded_roots", []) if value),
        "excluded_synthetic_window_ids": sorted(
            str(value) for value in meta.get("excluded_synthetic_window_ids", []) if value
        ),
        "excluded_duplicate_window_ids": sorted(
            str(value) for value in meta.get("excluded_duplicate_window_ids", []) if value
        ),
        "duplicate_window_canonical_ids": {
            str(key): str(value)
            for key, value in sorted(dict(meta.get("duplicate_window_canonical_ids", {})).items())
        },
        "eligible_window_ids": sorted(str(item["window_id"]) for item in eligible),
        "selected_window_ids": sorted(str(item["window_id"]) for item in selected),
        "packetized_window_ids": sorted(str(item["window_id"]) for item in emitted),
        "eligible_per_harness": counts(eligible, "harness"),
        "eligible_per_repo": counts(eligible, "repo"),
        "packetized_per_harness": counts(emitted, "harness"),
        "packetized_per_repo": counts(emitted, "repo"),
        "selected_per_root": {key: int(selected_by_root[key]) for key in sorted(selected_by_root)},
        "packetized_per_root": {key: int(emitted_by_root[key]) for key in sorted(emitted_by_root)},
        "scope_denominators": _scope_denominators(eligible),
        "root_request_index_hashes": {
            root: _short_hash(entries) for root, entries in request_index.items()
        },
        "proof_capability": _proof_capability_census(
            eligible,
            {str(item["root_session_id"]) for item in emitted},
        ),
        "packet_groups": [
            {
                "root_session_ids": sorted({str(item["root_session_id"]) for item in group}),
                "window_ids": [str(item["window_id"]) for item in group],
            }
            for group in groups
        ],
    }


def build_eligibility_commitment(
    conn: sqlite3.Connection,
    *,
    config: CoachPreprocessConfig | None = None,
) -> dict[str, Any]:
    cfg = config or CoachPreprocessConfig()
    _, quote_limit = _publication_limits(cfg)
    report = RedactionReport()
    eligible, _, meta = _window_rows(
        conn, report, source_transcript_reader=cfg.source_transcript_reader
    )
    report.version = COACH_REDACTION_VERSION
    selected, groups, ordered_roots, selected_by_root, emitted_by_root = _selection_state(
        eligible,
        cfg,
        packet_size=_selection_packet_size(conn, cfg, eligible, quote_limit, report),
    )
    commitment = _eligibility_commitment(
        eligible, selected, groups, meta, ordered_roots, selected_by_root, emitted_by_root,
        quote_limit,
    )
    commitment["hash"] = _short_hash(json.dumps(commitment, sort_keys=True, ensure_ascii=False))
    return commitment


def build_packetized_window_index(
    conn: sqlite3.Connection,
    *,
    config: CoachPreprocessConfig | None = None,
) -> dict[str, dict[str, Any]]:
    cfg = config or CoachPreprocessConfig()
    _, quote_limit = _publication_limits(cfg)
    report = RedactionReport()
    eligible, _, _ = _window_rows(
        conn, report, source_transcript_reader=cfg.source_transcript_reader
    )
    report.version = COACH_REDACTION_VERSION
    _, groups, _, _, _ = _selection_state(
        eligible,
        cfg,
        packet_size=_selection_packet_size(conn, cfg, eligible, quote_limit, report),
    )
    return {
        str(item["window_id"]): _packet_window(item, quote_limit)
        for group in groups
        for item in group
    }


def build_root_request_index(
    conn: sqlite3.Connection,
    *,
    config: CoachPreprocessConfig | None = None,
) -> dict[str, list[dict[str, Any]]]:
    cfg = config or CoachPreprocessConfig()
    _, quote_limit = _publication_limits(cfg)
    eligible, _, _ = _window_rows(
        conn, RedactionReport(), source_transcript_reader=cfg.source_transcript_reader
    )
    return _root_request_index(eligible, quote_limit)


def build_preprocess_coverage(
    conn: sqlite3.Connection,
    *,
    config: CoachPreprocessConfig | None = None,
) -> dict[str, Any]:
    cfg = config or CoachPreprocessConfig()
    _, quote_limit = _publication_limits(cfg)
    report = RedactionReport()
    eligible, _, meta = _window_rows(
        conn, report, source_transcript_reader=cfg.source_transcript_reader
    )
    report.version = COACH_REDACTION_VERSION
    selected, groups, _, selected_by_root, emitted_by_root = _selection_state(
        eligible,
        cfg,
        packet_size=_selection_packet_size(conn, cfg, eligible, quote_limit, report),
    )
    emitted = [item for group in groups for item in group]
    by_root = {str(item["root_session_id"]): [] for item in eligible}
    source_truncated_messages = sum(
        1
        for item in emitted
        for message in _packet_window(item, quote_limit)["messages"]
        if bool(message.get("source_truncated"))
    )
    proof_capability = _proof_capability_census(
        eligible, {str(item["root_session_id"]) for item in emitted}
    )
    scope_denominators = _scope_denominators(eligible)
    return {
        "total": meta["total"], "eligible": len(eligible), "scanned": meta["scanned"],
        "joined_windows": meta["joined_windows"], "inspected_windows": meta["inspected_windows"],
        "selected": len(selected), "processed": 0, "packetized": len(emitted), "total_windows": meta["total"],
        "eligible_windows": len(eligible), "scanned_windows": meta["scanned"],
        "selected_windows": len(selected), "processed_windows": 0, "packetized_windows": len(emitted),
        "total_roots": len({meta["logical_roots"].get(str(row["id"]), str(row["id"])) for row in meta["sessions"].values()}),
        "physical_root_count": len({meta["roots"].get(str(row["id"]), str(row["id"])) for row in meta["sessions"].values()}),
        "eligible_roots": len(by_root), "selected_roots": len(selected_by_root),
        "packetized_roots": len(emitted_by_root),
        "publication_mode": str(cfg.publication_mode),
        "publication_complete": len(emitted) == len(eligible) and len(emitted_by_root) == len(by_root) and source_truncated_messages == 0,
        "source_truncated_messages": source_truncated_messages,
        "excluded_synthetic_windows": len(meta.get("excluded_synthetic_window_ids", [])),
        "excluded_synthetic_by_kind": dict(meta.get("excluded_synthetic_by_kind", {})),
        "excluded_duplicate_windows": len(meta.get("excluded_duplicate_window_ids", [])),
        "duplicate_window_canonical_ids": dict(meta.get("duplicate_window_canonical_ids", {})),
        "proof_capability_by_harness": proof_capability["by_harness"],
        "scope_denominators": scope_denominators,
    }


def _proof_capability_census(
    eligible: Iterable[Mapping[str, Any]],
    packetized_roots: set[str],
) -> dict[str, Any]:
    by_root: dict[str, list[Mapping[str, Any]]] = {}
    for item in eligible:
        root = str(item.get("root_session_id") or "")
        if root:
            by_root.setdefault(root, []).append(item)
    root_levels: dict[str, str] = {}
    root_harnesses: dict[str, str] = {}
    for root, windows in by_root.items():
        root_harnesses[root] = sorted(str(item.get("harness") or "(unknown)") for item in windows)[0]
        tools = [tool for item in windows for tool in item.get("tool_timeline", []) if isinstance(tool, Mapping)]
        deterministic = any(
            str(tool.get("operation_kind") or "unknown") in {"verification", "artifact_write"}
            and str(tool.get("action") or "").lower() in {"result", "end", "write", "apply", "patch", "commit"}
            and tool.get("success") in (True, False, 0, 1)
            for tool in tools
        )
        if deterministic:
            root_levels[root] = "deterministic_terminal"
        elif len(windows) > 1:
            root_levels[root] = "owner_message_only"
        else:
            root_levels[root] = "unknown"
    by_harness: dict[str, dict[str, Any]] = {}
    for root, level in root_levels.items():
        harness = root_harnesses[root]
        entry = by_harness.setdefault(
            harness,
            {"eligible_roots": 0, "packetized_roots": 0, "levels": {"deterministic_terminal": 0, "owner_message_only": 0, "unknown": 0}},
        )
        entry["eligible_roots"] += 1
        entry["levels"][level] += 1
        if root in packetized_roots:
            entry["packetized_roots"] += 1
    for harness, entry in by_harness.items():
        deterministic = int(entry["levels"]["deterministic_terminal"])
        eligible_roots = int(entry["eligible_roots"])
        entry["adapter_capability"] = harness_supports(str(harness), "tool_events")
        entry["observed_proof_coverage"] = (
            "complete" if deterministic == eligible_roots and eligible_roots else
            "partial" if deterministic else
            "absent" if entry["levels"]["owner_message_only"] else "unknown"
        )
        entry["capability"] = entry["adapter_capability"]
    return {
        "by_harness": dict(sorted(by_harness.items())),
        "root_levels": dict(sorted(root_levels.items())),
    }


def emit_coach_packets(
    conn: sqlite3.Connection,
    run_dir: Path,
    *,
    config: CoachPreprocessConfig | None = None,
    max_windows_per_root: int | None = None,
    max_windows_per_packet: int | None = None,
    max_packet_chars: int | None = None,
) -> dict[str, Any]:
    """Emit deterministic, redacted coach packets and a run-local manifest."""
    cfg = config or CoachPreprocessConfig()
    if (
        max_windows_per_root is not None
        or max_windows_per_packet is not None
        or max_packet_chars is not None
    ):
        cfg = replace(
            cfg,
            max_windows_per_root=max_windows_per_root if max_windows_per_root is not None else cfg.max_windows_per_root,
            max_windows_per_packet=max_windows_per_packet if max_windows_per_packet is not None else cfg.max_windows_per_packet,
            max_packet_chars=max_packet_chars if max_packet_chars is not None else cfg.max_packet_chars,
        )
    _, quote_limit = _publication_limits(cfg)
    target = assert_writable(run_dir, purpose="coach preprocess run")
    target.mkdir(parents=True, exist_ok=True)
    packet_dir = target / "packets"
    packet_dir.mkdir(parents=True, exist_ok=True)
    producer_contract = _producer_contract(cfg)
    report = RedactionReport()
    corpus_snapshot = _corpus_snapshot(conn)
    eligible, pair_counts, meta = _window_rows(
        conn, report, source_transcript_reader=cfg.source_transcript_reader
    )
    report.version = COACH_REDACTION_VERSION
    root_request_index = _root_request_index(eligible, quote_limit)
    selected, groups, ordered_roots, selected_by_root, emitted_by_root = _selection_state(
        eligible,
        cfg,
        packet_size=_packet_size_callback(
            cfg,
            quote_limit,
            root_request_index,
            producer_contract,
            corpus_snapshot,
            report,
        ) if cfg.max_packet_chars is not None else None,
    )
    by_root = {str(item["root_session_id"]): [] for item in eligible}
    run_id = f"coach_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{_short_hash(str(len(eligible)) + str(len(selected)))[:8]}"
    packets: list[dict[str, Any]] = []
    packet_snapshot = {
        "snapshot_hash": corpus_snapshot["snapshot_hash"],
        "counts": corpus_snapshot["counts"],
        "high_water": corpus_snapshot["high_water"],
    }
    packetized_ids = {str(item["window_id"]) for group in groups for item in group}
    emitted_selected = [item for item in selected if str(item["window_id"]) in packetized_ids]
    proof_capability = _proof_capability_census(
        eligible,
        {str(item["root_session_id"]) for item in emitted_selected},
    )
    scope_denominators = _scope_denominators(eligible)
    rendered_packets: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
    for packet_index, group in enumerate(groups, start=1):
        packet_id = f"cpkt_{packet_index:04d}"
        body = _packet_body(
            group,
            packet_id=packet_id,
            run_id=run_id,
            cfg=cfg,
            quote_limit=quote_limit,
            producer_contract=producer_contract,
            corpus_snapshot=corpus_snapshot,
            packet_snapshot=packet_snapshot,
            root_request_index=root_request_index,
            report=report,
        )
        serialized_bytes = _serialized_packet_size(body)
        if cfg.max_packet_chars is not None and serialized_bytes > cfg.max_packet_chars:
            raise ValueError(
                "coach_packet_byte_budget_exceeded: "
                f"packet={packet_id} serialized_bytes={serialized_bytes} "
                f"max_packet_chars={cfg.max_packet_chars}"
            )
        rendered_packets.append((packet_id, group, body))
    for packet_id, group, body in rendered_packets:
        path = packet_dir / f"{packet_id}.json"
        assert_writable(path, purpose="coach evidence packet")
        write_text(path, json.dumps(body, indent=2, ensure_ascii=False) + "\n")
        packets.append({"packet_id": packet_id, "path": str(path.relative_to(target)), "packet_hash": body["packet_hash"], "serialized_bytes": _serialized_packet_size(body), "window_ids": [x["window_id"] for x in group], "root_session_ids": body["root_session_ids"], "root_request_index_hash": _short_hash(body["root_request_index"]), "status": "pending"})
    harness_counts: dict[str, int] = {}
    repo_counts: dict[str, int] = {}
    for item in emitted_selected:
        harness_counts[item["harness"]] = harness_counts.get(item["harness"], 0) + 1
        repo = item["repo"] or "(unknown)"
        repo_counts[repo] = repo_counts.get(repo, 0) + 1
    selected_roots = len(selected_by_root)
    source_truncated_messages = sum(
        1
        for group in groups
        for item in group
        for message in _packet_window(item, quote_limit)["messages"]
        if bool(message.get("source_truncated"))
    )
    publication_complete = (
        len(emitted_selected) == len(eligible)
        and len(emitted_by_root) == len(by_root)
        and source_truncated_messages == 0
    )
    coverage = {
        "total": meta["total"], "eligible": len(eligible), "scanned": meta["scanned"],
        "joined_windows": meta["joined_windows"], "inspected_windows": meta["inspected_windows"],
        "selected": len(selected), "processed": 0, "packetized": len(emitted_selected), "total_windows": meta["total"],
        "eligible_windows": len(eligible), "scanned_windows": meta["scanned"],
        "selected_windows": len(selected), "processed_windows": 0, "packetized_windows": len(emitted_selected),
        "total_roots": len({meta["logical_roots"].get(str(r["id"]), str(r["id"])) for r in meta["sessions"].values()}),
        "physical_root_count": len({meta["roots"].get(str(r["id"]), str(r["id"])) for r in meta["sessions"].values()}),
        "eligible_roots": len(by_root), "selected_roots": selected_roots,
        "packetized_roots": len(emitted_by_root),
        "publication_mode": str(cfg.publication_mode),
        "publication_complete": publication_complete,
        "source_truncated_messages": source_truncated_messages,
        "excluded_synthetic_windows": len(meta.get("excluded_synthetic_window_ids", [])),
        "excluded_synthetic_by_kind": dict(meta.get("excluded_synthetic_by_kind", {})),
        "excluded_duplicate_windows": len(meta.get("excluded_duplicate_window_ids", [])),
        "duplicate_window_canonical_ids": dict(meta.get("duplicate_window_canonical_ids", {})),
        "proof_capability_by_harness": proof_capability["by_harness"],
        "scope_denominators": scope_denominators,
    }
    selection_config = {
        "publication_mode": str(cfg.publication_mode),
        "max_windows_per_root": cfg.max_windows_per_root,
        "max_windows_per_packet": cfg.max_windows_per_packet,
        "max_packet_chars": cfg.max_packet_chars,
        "max_packets": cfg.max_packets,
        "max_quote_chars": cfg.max_quote_chars,
    }
    eligibility_commitment = _eligibility_commitment(
        eligible, selected, groups, meta, ordered_roots, selected_by_root, emitted_by_root,
        quote_limit,
    )
    eligibility_commitment["hash"] = _short_hash(
        json.dumps(eligibility_commitment, sort_keys=True, ensure_ascii=False)
    )
    manifest = {
        "schema_version": SCHEMA_VERSION, "run_id": run_id, "created_at": _now(),
        "coach_redaction_version": COACH_REDACTION_VERSION,
        "safety_redaction_version": REDACTION_VERSION,
        "producer_contract": producer_contract,
        "corpus_snapshot_hash": corpus_snapshot["snapshot_hash"], "corpus_snapshot": corpus_snapshot,
        "coverage": coverage, "packets": packets,
        "proof_capability_by_harness": proof_capability["by_harness"],
        "selection_config": selection_config,
        "eligibility_commitment": eligibility_commitment,
        "per_harness": dict(sorted(harness_counts.items())), "per_repo": dict(sorted(repo_counts.items())),
        "harness_counts": dict(sorted(harness_counts.items())), "repo_counts": dict(sorted(repo_counts.items())),
        "counts": {"by_harness": dict(sorted(harness_counts.items())), "by_repo": dict(sorted(repo_counts.items()))},
        "eligible_per_harness_repo": {k.replace("\0", "/"): v for k, v in sorted(pair_counts.items())},
        "per_harness_repo": {k.replace("\0", "/"): sum(1 for x in emitted_selected if f"{x['harness']}\0{x['repo']}" == k) for k in sorted({f"{x['harness']}\0{x['repo']}" for x in emitted_selected})},
        "selected_per_root": selected_by_root, "packetized_per_root": emitted_by_root,
        "ordered_roots": ordered_roots, "excluded_roots": meta["excluded_roots"],
        "redaction": report.to_dict(), "prompt_hash": _short_hash(COACH_PROMPT),
    }
    assert_writable(target / "manifest.json", purpose="coach preprocess manifest")
    write_text(target / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


emit_preprocess_run = emit_coach_packets
emit_coach_preprocess = emit_coach_packets
build_coach_packets = emit_coach_packets


def _packet_windows(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(w.get("window_id")): w for w in packet.get("windows", []) if isinstance(w, dict) and w.get("window_id")}


def _clean_window_dispositions(
    raw: Mapping[str, Any],
    packet: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[ValidationFailure]]:
    failures: list[ValidationFailure] = []
    local_windows = _packet_windows(packet)
    dispositions = raw.get("window_dispositions")
    if not isinstance(dispositions, list):
        return [], [ValidationFailure("window_dispositions_not_list")]
    by_window: dict[str, dict[str, Any]] = {}
    for item in dispositions:
        if not isinstance(item, Mapping):
            failures.append(ValidationFailure("window_disposition_not_object")); continue
        window_id = str(item.get("window_id") or "")
        if not window_id or window_id not in local_windows:
            failures.append(ValidationFailure("window_disposition_unknown_window")); continue
        if window_id in by_window:
            failures.append(ValidationFailure("window_disposition_duplicate_window")); continue
        observation_ids = item.get("observation_ids")
        if not isinstance(observation_ids, list):
            failures.append(ValidationFailure("window_disposition_observation_ids_not_list")); continue
        cleaned_ids = [str(value) for value in observation_ids]
        if (
            any(not value for value in cleaned_ids)
            or len(set(cleaned_ids)) != len(cleaned_ids)
        ):
            failures.append(ValidationFailure("window_disposition_invalid_observation_ids")); continue
        no_supported = item.get("no_supported_observation")
        if not isinstance(no_supported, bool):
            failures.append(ValidationFailure("window_disposition_no_supported_not_boolean")); continue
        if no_supported != (not cleaned_ids):
            failures.append(ValidationFailure("window_disposition_no_supported_mismatch")); continue
        by_window[window_id] = {
            "window_id": window_id,
            "observation_ids": cleaned_ids,
            "no_supported_observation": no_supported,
        }
    missing = set(local_windows) - set(by_window)
    if missing:
        failures.append(ValidationFailure("window_disposition_missing_local_window"))
    return [by_window[window_id] for window_id in local_windows if window_id in by_window], failures


def _evidence_index(packet: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for wid, window in _packet_windows(packet).items():
        for msg in window.get("messages", []):
            if isinstance(msg, dict):
                out[(wid, str(msg.get("message_id") or ""))] = {"window": window, "message": msg}
        for event in window.get("tool_timeline", []):
            if isinstance(event, dict) and event.get("tool_event_id"):
                out[(wid, "tool:" + str(event["tool_event_id"]))] = {"window": window, "fact": event, "evidence_type": "tool"}
        for exposure in window.get("skill_exposures", []):
            if isinstance(exposure, dict) and exposure.get("skill_exposure_id"):
                out[(wid, "skill:" + str(exposure["skill_exposure_id"]))] = {"window": window, "fact": exposure, "evidence_type": "skill"}
    root_index = packet.get("root_request_index")
    if isinstance(root_index, Mapping):
        for root, entries in root_index.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping) or str(entry.get("root_session_id") or "") != str(root):
                    continue
                request = entry.get("request")
                window_id = str(entry.get("window_id") or "")
                message_id = str(request.get("message_id") or "") if isinstance(request, Mapping) else ""
                if not window_id or not message_id:
                    continue
                window = {
                    "window_id": window_id,
                    "session_id": str(entry.get("session_id") or ""),
                    "root_session_id": str(root),
                    "timestamp": str(entry.get("timestamp") or ""),
                    "messages": [dict(request)],
                    "artifact": {},
                    "context_only": True,
                    "response_model_canonical": str(entry.get("response_model_canonical") or ""),
                    "response_effort": str(entry.get("response_effort") or ""),
                }
                out.setdefault((window_id, message_id), {"window": window, "message": dict(request)})
    return out


def _packet_window_order(packet: Mapping[str, Any]) -> dict[str, int]:
    entries: dict[str, str] = {
        window_id: str(window.get("timestamp") or "")
        for window_id, window in _packet_windows(dict(packet)).items()
    }
    root_index = packet.get("root_request_index")
    if isinstance(root_index, Mapping):
        for requests in root_index.values():
            if not isinstance(requests, list):
                continue
            for entry in requests:
                if isinstance(entry, Mapping) and str(entry.get("window_id") or ""):
                    entries.setdefault(
                        str(entry["window_id"]), str(entry.get("timestamp") or "")
                    )
    ordered = sorted(entries.items(), key=lambda item: (_timestamp_sort_key(item[1]), item[0]))
    ranks: dict[tuple[int, str], int] = {}
    return {
        window_id: ranks.setdefault(_timestamp_sort_key(timestamp), len(ranks))
        for window_id, timestamp in ordered
    }


def validate_coach_result(raw: Any, packet: dict[str, Any]) -> tuple[dict[str, Any] | None, list[ValidationFailure]]:
    """Hard-validate model JSON against immutable packet membership."""
    failures: list[ValidationFailure] = []
    if not isinstance(raw, dict):
        return None, [ValidationFailure("result_not_object")]
    packet_id = str(packet.get("packet_id") or "")
    if not packet_id or str(raw.get("packet_id") or "") != packet_id:
        failures.append(ValidationFailure("packet_id_mismatch"))
    result_id = str(raw.get("result_id") or "")
    if not result_id:
        failures.append(ValidationFailure("missing_result_id"))
    producer = raw.get("producer")
    contract = packet.get("producer_contract") or {}
    if not isinstance(producer, dict):
        failures.append(ValidationFailure("missing_producer_metadata"))
        producer = {}
    for field in contract.get("required", ("provider", "model", "prompt_hash")):
        if not str(producer.get(field) or "").strip():
            failures.append(ValidationFailure("missing_producer_" + str(field)))
    expected = contract.get("expected") if isinstance(contract.get("expected"), dict) else {}
    if not contract.get("bound"):
        failures.append(ValidationFailure("producer_contract_unbound"))
    for field, expected_value in expected.items():
        if not str(expected_value or "") or str(producer.get(field) or "") != str(expected_value):
            failures.append(ValidationFailure("producer_assignment_mismatch:" + str(field)))
    window_dispositions, disposition_failures = _clean_window_dispositions(raw, packet)
    failures.extend(disposition_failures)
    if raw.get("abstain") is True:
        if not str(raw.get("abstain_reason") or "").strip():
            failures.append(ValidationFailure("missing_abstain_reason"))
        if raw.get("observations") not in (None, [], ()):
            failures.append(ValidationFailure("abstention_has_observations"))
        if any(not disposition["no_supported_observation"] for disposition in window_dispositions):
            failures.append(ValidationFailure("abstention_disposition_has_observation"))
        return (None, failures) if failures else ({"packet_id": packet_id, "result_id": result_id, "abstain": True, "abstain_reason": str(raw["abstain_reason"]), "producer": dict(producer), "window_dispositions": window_dispositions}, [])
    if raw.get("abstain") is not False:
        failures.append(ValidationFailure("abstain_not_boolean"))
    observations = raw.get("observations")
    if not isinstance(observations, list):
        failures.append(ValidationFailure("observations_not_list"))
        return None, failures
    idx = _evidence_index(packet)
    window_order = _packet_window_order(packet)
    root_ids = {str(x) for x in packet.get("root_session_ids", [])}
    cleaned: list[dict[str, Any]] = []
    observation_ids: set[str] = set()
    for oi, obs in enumerate(observations):
        if not isinstance(obs, dict):
            failures.append(ValidationFailure("observation_not_object", oi)); continue
        observation_id = str(obs.get("observation_id") or "").strip()
        if not observation_id:
            failures.append(ValidationFailure("missing_observation_id", oi)); continue
        if observation_id in observation_ids:
            failures.append(ValidationFailure("duplicate_observation_id", oi)); continue
        observation_ids.add(observation_id)
        kind = str(obs.get("kind") or "")
        if kind not in COACH_KINDS:
            failures.append(ValidationFailure("unknown_or_sentiment_kind", oi)); continue
        if any(term in kind.lower() or term in str(obs.get("assertion_key") or "").lower() for term in _SENTIMENT_MARKERS):
            failures.append(ValidationFailure("sentiment_kind_forbidden", oi))
        assertion = str(obs.get("assertion_key") or "").strip()
        limitation = str(obs.get("does_not_prove") or "").strip()
        confidence = obs.get("confidence")
        if not assertion: failures.append(ValidationFailure("missing_assertion_key", oi))
        if not limitation: failures.append(ValidationFailure("missing_does_not_prove", oi))
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1: failures.append(ValidationFailure("bad_confidence", oi))
        evidence = obs.get("evidence")
        if not isinstance(evidence, list) or not evidence: failures.append(ValidationFailure("missing_evidence", oi)); evidence = []
        clean_evidence: list[dict[str, Any]] = []
        refs: set[str] = set()
        for ev in evidence:
            if not isinstance(ev, dict): failures.append(ValidationFailure("evidence_not_object", oi)); continue
            wid = str(ev.get("window_id") or "")
            if ev.get("message_id"):
                mid, evidence_key = str(ev["message_id"]), str(ev["message_id"])
            elif ev.get("tool_event_id"):
                mid, evidence_key = "", "tool:" + str(ev["tool_event_id"])
            elif ev.get("skill_exposure_id"):
                mid, evidence_key = "", "skill:" + str(ev["skill_exposure_id"])
            else:
                mid, evidence_key = "", ""
            hit = idx.get((wid, evidence_key))
            if hit is None:
                failures.append(ValidationFailure("unknown_evidence_member", oi)); continue
            window = hit["window"]
            if root_ids and str(window.get("root_session_id") or "") not in root_ids:
                failures.append(ValidationFailure("cross_root_evidence", oi)); continue
            ref = str(ev.get("ref") or f"{wid}:{mid}")
            if not mid:
                ref = str(ev.get("ref") or f"{wid}:{evidence_key}")
            if ref in refs:
                failures.append(ValidationFailure("duplicate_evidence_ref", oi)); continue
            refs.add(ref)
            artifact = dict(window.get("artifact") or {})
            if mid:
                msg = hit["message"]
                if str(ev.get("role") or "") != str(msg.get("role") or "") or ev.get("seq") != msg.get("seq"):
                    failures.append(ValidationFailure("evidence_role_or_order_mismatch", oi)); continue
                quote = str(ev.get("quote") or "")
                source_text = str(msg.get("source_text") or "")
                if msg.get("emitted_source_hash") and str(msg["emitted_source_hash"]) != _sha256(source_text):
                    failures.append(ValidationFailure("source_hash_mismatch", oi)); continue
                if not quote or quote not in source_text:
                    failures.append(ValidationFailure("quote_not_substring", oi)); continue
                start = source_text.find(quote)
                if ev.get("quote_start") is not None and ev.get("quote_start") != start:
                    failures.append(ValidationFailure("quote_start_mismatch", oi)); continue
                if ev.get("quote_end") is not None and ev.get("quote_end") != start + len(quote):
                    failures.append(ValidationFailure("quote_end_mismatch", oi)); continue
                metadata_mismatch = False
                for field in ("source_hash", "content_hash", "emitted_source_hash"):
                    if ev.get(field) is not None and str(ev[field]) != str(msg.get(field) or ""):
                        failures.append(ValidationFailure(f"{field}_mismatch", oi)); metadata_mismatch = True
                if metadata_mismatch:
                    continue
                clean_evidence.append(
                    {
                        "ref": ref, "evidence_type": "message", "window_id": wid,
                        "session_id": window.get("session_id"), "root_session_id": window.get("root_session_id"),
                        "context_only": bool(window.get("context_only")),
                        "message_id": mid, "role": msg["role"], "seq": msg["seq"], "quote": quote,
                        "timestamp": msg.get("timestamp"),
                        "quote_start": start, "quote_end": start + len(quote),
                        "source_hash": msg.get("source_hash"), "content_hash": msg.get("content_hash"),
                        "emitted_source_hash": msg.get("emitted_source_hash"),
                        "artifact_id": artifact.get("artifact_id"), "artifact_hash": artifact.get("artifact_hash"),
                        "parser_version": artifact.get("parser_version"),
                    }
                )
            else:
                fact = hit["fact"]
                expected_fact = str(fact.get("fact") or "")
                if ev.get("fact") is not None and str(ev["fact"]) != expected_fact:
                    failures.append(ValidationFailure("fact_mismatch", oi)); continue
                clean_evidence.append(
                    {
                        "ref": ref, "evidence_type": hit["evidence_type"], "window_id": wid,
                        "session_id": window.get("session_id"), "root_session_id": window.get("root_session_id"),
                        "context_only": bool(window.get("context_only")),
                        "tool_event_id": fact.get("tool_event_id"), "skill_exposure_id": fact.get("skill_exposure_id"),
                        "message_id": fact.get("message_id"),
                        "timestamp": fact.get("message_timestamp"),
                        "fact": expected_fact, "artifact_id": artifact.get("artifact_id"),
                        "artifact_hash": artifact.get("artifact_hash"), "parser_version": artifact.get("parser_version"),
                    }
                )
        for wid in {e["window_id"] for e in clean_evidence}:
            same = [e for e in clean_evidence if e["window_id"] == wid and e.get("evidence_type") == "message"]
            users = [e["seq"] for e in same if e["role"] == "user"]
            assistants = [e["seq"] for e in same if e["role"] == "assistant"]
            if users and assistants and min(users) >= max(assistants):
                failures.append(ValidationFailure("evidence_role_order_invalid", oi))
        arcs = obs.get("proof_arcs")
        if not isinstance(arcs, list) or not arcs: failures.append(ValidationFailure("missing_proof_arcs", oi)); arcs = []
        arc_labels: set[str] = set()
        clean_arcs: list[dict[str, Any]] = []
        for arc in arcs:
            if not isinstance(arc, dict): failures.append(ValidationFailure("proof_arc_not_object", oi)); continue
            label = str(arc.get("arc") or arc.get("label") or "")
            arc_refs = arc.get("evidence_refs")
            if not label or not isinstance(arc_refs, list) or len(arc_refs) < 1:
                failures.append(ValidationFailure("malformed_proof_arc", oi)); continue
            if any(str(r) not in refs for r in arc_refs): failures.append(ValidationFailure("proof_arc_unknown_ref", oi)); continue
            if label not in _ARC_EVIDENCE_RULES:
                failures.append(ValidationFailure("unknown_proof_arc_label", oi)); continue
            selected = [entry for entry in clean_evidence if entry["ref"] in {str(r) for r in arc_refs}]
            allowed = _ARC_EVIDENCE_RULES[label]
            if any((entry.get("evidence_type"), entry.get("role", "")) not in allowed and (entry.get("evidence_type"), "") not in allowed for entry in selected):
                failures.append(ValidationFailure("proof_arc_evidence_type_mismatch", oi)); continue
            if label in {"skill_evidence", "artifact"} and not any(entry.get("evidence_type") in {"skill", "tool"} for entry in selected):
                failures.append(ValidationFailure("proof_arc_requires_deterministic_fact", oi)); continue
            arc_labels.add(label); clean_arcs.append({"arc": label, "evidence_refs": [str(r) for r in arc_refs]})
        missing = _REQUIRED_ARCS[kind] - arc_labels
        if missing: failures.append(ValidationFailure("missing_required_proof_arc:" + ",".join(sorted(missing)), oi))
        arc_by_label = {arc["arc"]: arc for arc in clean_arcs}
        def arc_entries(label: str) -> list[dict[str, Any]]:
            wanted = set(arc_by_label.get(label, {}).get("evidence_refs", []))
            return [entry for entry in clean_evidence if entry["ref"] in wanted]

        if clean_evidence and not any(not bool(entry.get("context_only")) for entry in clean_evidence):
            failures.append(ValidationFailure("observation_requires_local_packet_evidence", oi))

        if kind in {"instruction_follow", "delivery_gap", "verification"}:
            request_label = {"instruction_follow": "request", "delivery_gap": "expectation", "verification": "verification_request"}[kind]
            result_label = {"instruction_follow": "outcome", "delivery_gap": "delivery", "verification": "verification_result"}[kind]
            request_windows = {entry["window_id"] for entry in arc_entries(request_label)}
            for entry in arc_entries(result_label):
                if entry.get("evidence_type") == "message" and entry.get("role") == "user":
                    linked_requests = [
                        request for request in arc_entries(request_label)
                        if request.get("window_id") != entry.get("window_id")
                    ]
                    if linked_requests and (
                        _aware_timestamp(entry.get("timestamp")) is None
                        or any(_aware_timestamp(request.get("timestamp")) is None for request in linked_requests)
                    ):
                        failures.append(ValidationFailure("owner_evidence_timestamp_unusable", oi))
                    if not any(
                        is_causally_later(entry, request, window_order)
                        for request in linked_requests
                    ):
                        failures.append(ValidationFailure("owner_evidence_must_be_later", oi))
            result_supported = (
                supports_bounded_gap(
                    arc_entries(result_label), request_window_ids=request_windows, window_order=window_order,
                    request_evidence=arc_entries(request_label),
                )
                if kind == "delivery_gap"
                else supports_verification_result(
                    arc_entries(result_label), request_window_ids=request_windows, window_order=window_order,
                    request_evidence=arc_entries(request_label),
                )
                if kind == "verification"
                else supports_successful_result(
                    arc_entries(result_label), request_window_ids=request_windows, window_order=window_order,
                    request_evidence=arc_entries(request_label),
                )
            )
            if not result_supported:
                failures.append(ValidationFailure("requires_result_or_later_owner_evidence", oi))
        if kind == "instruction_miss":
            request_windows = {entry["window_id"] for entry in arc_entries("request")}
            if not supports_bounded_gap(
                arc_entries("gap"), request_window_ids=request_windows, window_order=window_order,
                request_evidence=arc_entries("request"),
            ):
                failures.append(ValidationFailure("miss_requires_failed_result_or_owner_correction", oi))
        if kind in {"instruction_follow", "instruction_miss", "delivery_gap", "verification"}:
            request_label = {"instruction_follow": "request", "instruction_miss": "request", "delivery_gap": "expectation", "verification": "verification_request"}[kind]
            response_label = {"instruction_follow": "response", "instruction_miss": "response", "delivery_gap": "delivery", "verification": "verification_result"}[kind]
            request_entries = [entry for entry in clean_evidence if entry["ref"] in set(arc_by_label.get(request_label, {}).get("evidence_refs", []))]
            response_entries = [entry for entry in clean_evidence if entry["ref"] in set(arc_by_label.get(response_label, {}).get("evidence_refs", []))]
            if not any(
                (
                    req["window_id"] == resp["window_id"]
                    and (
                        resp.get("evidence_type") == "tool"
                        or (resp.get("evidence_type") == "message" and req.get("seq", 0) < resp.get("seq", 0))
                    )
                )
                or (
                    resp.get("evidence_type") == "message"
                    and resp.get("role") == "user"
                    and is_causally_later(resp, req, window_order)
                )
                for req in request_entries
                if req.get("evidence_type") == "message"
                and req.get("role") == "user"
                for resp in response_entries
            ):
                failures.append(ValidationFailure("request_without_ordered_response", oi))
        if kind == "repeated_ask":
            first = [entry for entry in clean_evidence if entry["ref"] in set(arc_by_label.get("request_1", {}).get("evidence_refs", []))]
            second = [entry for entry in clean_evidence if entry["ref"] in set(arc_by_label.get("request_2", {}).get("evidence_refs", []))]
            if not first or not second or not all(entry.get("evidence_type") == "message" and entry.get("role") == "user" for entry in first + second):
                failures.append(ValidationFailure("repeated_ask_requires_user_requests", oi))
            elif not any(not bool(entry.get("context_only")) for entry in second):
                failures.append(ValidationFailure("repeated_ask_requires_local_second_request", oi))
            elif {(entry["window_id"], entry["message_id"]) for entry in first} & {(entry["window_id"], entry["message_id"]) for entry in second}:
                failures.append(ValidationFailure("repeated_ask_requires_distinct_requests", oi))
            elif not any(
                str(first_entry.get("session_id") or "")
                and str(first_entry.get("session_id") or "") == str(second_entry.get("session_id") or "")
                and _repeated_request_theme_matches(first_entry, second_entry)
                and (
                    (
                        first_entry["window_id"] == second_entry["window_id"]
                        and first_entry.get("seq", 0) < second_entry.get("seq", 0)
                    )
                    or is_causally_later(second_entry, first_entry, window_order)
                )
                for first_entry in first
                for second_entry in second
            ):
                failures.append(ValidationFailure("repeated_ask_order_or_theme_invalid", oi))
        if kind == "skill_use":
            skill_entries = arc_entries("skill_evidence")
            if not any(entry.get("evidence_type") == EVIDENCE_SKILL for entry in skill_entries):
                failures.append(ValidationFailure("skill_use_requires_skill_exposure", oi))
            action_entries = arc_entries("skill_action")
            if not supports_skill_action(
                action_entries,
                skill_evidence=skill_entries,
                request_evidence=arc_entries("skill_request"),
            ):
                failures.append(ValidationFailure("skill_use_requires_attributable_action", oi))
        if kind == "process_fact":
            action_entries = arc_entries("action")
            artifact_entries = arc_entries("artifact")
            action_ids = {
                str(entry.get("tool_event_id") or "")
                for entry in action_entries
                if str(entry.get("tool_event_id") or "")
            }
            artifact_ids = {
                str(entry.get("tool_event_id") or "")
                for entry in artifact_entries
                if str(entry.get("tool_event_id") or "")
            }
            if (
                not action_entries
                or not artifact_entries
                or not all(is_successful_artifact_result(entry) for entry in action_entries + artifact_entries)
                or not action_ids & artifact_ids
            ):
                failures.append(ValidationFailure("process_fact_requires_shared_successful_artifact", oi))
        cleaned.append({"observation_id": observation_id, "kind": kind, "assertion_key": assertion, "confidence": confidence, "does_not_prove": limitation, "evidence": clean_evidence, "proof_arcs": clean_arcs})
    cleaned_by_id = {str(observation["observation_id"]): observation for observation in cleaned}
    declared_by_window = {
        str(disposition["window_id"]): set(disposition["observation_ids"])
        for disposition in window_dispositions
    }
    for window_id, declared_ids in declared_by_window.items():
        if not declared_ids <= set(cleaned_by_id):
            failures.append(ValidationFailure("window_disposition_unknown_observation_id"))
            continue
        actual_ids = {
            observation_id
            for observation_id, observation in cleaned_by_id.items()
            if any(
                str(evidence.get("window_id") or "") == window_id
                and not bool(evidence.get("context_only"))
                for evidence in observation["evidence"]
            )
        }
        if declared_ids != actual_ids:
            failures.append(ValidationFailure("window_disposition_observation_mismatch"))
    if failures:
        return None, failures
    return {"packet_id": packet_id, "result_id": result_id, "abstain": False, "producer": dict(producer), "observations": cleaned, "window_dispositions": window_dispositions}, []


validate_result = validate_coach_result
validate_observation_result = validate_coach_result
