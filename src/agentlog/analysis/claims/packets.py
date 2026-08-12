"""Packet workflow for LLM proposal generation (Cursor subagent path).

Default path mirrors UX extraction: emit evidence packets to disk → subagents
label/write result JSON → ingest into claims/proposals. No remote API call and
no harness-file writes.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from agentlog.analysis.claims.models import (
    MIN_SESSIONS_FINDING,
    MAX_QUOTE_CHARS,
    Claim,
    ClaimEvidence,
    Proposal,
    clip_quote,
)
from agentlog.analysis.claims.proposals import (
    PROPOSAL_EXTRACTOR_VERSION,
    _append_section,
    _read_text,
    _sha1_text,
    unified_diff,
)
from agentlog.analysis.claims.scope import (
    ConfigInventory,
    discover_config_inventory,
)
from agentlog.analysis.claims.store import (
    set_proposal_status,
    upsert_claims,
    upsert_proposals,
)
from agentlog.safety.redaction import REDACTION_VERSION, RedactionReport, redact_text
from agentlog.safety.write_guard import assert_writable
from agentlog.source_reader import CachedSourceTranscriptReader
from agentlog.session_identity import (
    build_identity_context,
    explicit_worker_parent_ids,
    logical_projection,
    logical_root_session_id,
    provider_root_shadow_ids,
    resolve_implicit_parent_ids,
)

DEFAULT_MODEL = "cursor-grok-4.5-high-fast"
PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "proposal_subagent.md"
)
PROVIDER_PACKET = "cursor_subagent_packet"
PROVIDER_PROXY = "local_proxy"  # optional later adapter; not the default

PACKET_STATUS_PENDING = "pending"
PACKET_STATUS_COMPLETED = "completed"
PACKET_STATUS_REJECTED = "rejected"
PACKET_STATUS_ABSTAINED = "abstained"
PACKET_STATUS_INELIGIBLE = "ineligible"

PROPOSAL_PACKET_VALIDATOR_VERSION = "proposal_packet_v4"
ELIGIBLE_POPULATION_VERSION = "eligible_logical_root_clusters_v2"
EVIDENCE_CONTRACT_VERSION = "adjudicated_miss_pairs_v2"

MAX_WINDOWS_PER_THEME = 24
MAX_QUOTE_IN_PACKET = 400
MAX_CONFIG_CHARS = 2_500
MAX_PROPOSALS_PER_PACKET = 3
MIN_ADJUDICATED_MISS_PAIRS = 3
MIN_GLOBAL_LOGICAL_ROOTS = 15
MAX_GLOBAL_CONCENTRATION = 0.70

_PACKET_POSIX_PATH = re.compile(
    r"(?<![A-Za-z0-9_.:-])/(?!/)[^\s\"'`<>()\[\]{},;:]+"
)
_PACKET_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s\"'`<>()\[\]{},;:]+")
_LEADING_LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_INTENT_NORMALIZE = re.compile(r"[^a-z0-9]+")

# Phrase filters only — never match flag *keys* (false booleans still contain the name).
THEME_SPECS: list[tuple[str, str]] = [
    (
        "scope_narrow",
        (
            "m_user.text LIKE '%only edit%' OR "
            "m_user.text LIKE '%only touch%' OR "
            "m_user.text LIKE '%only change%' OR "
            "m_user.text LIKE '%micro-patch%' OR "
            "m_user.text LIKE '%micropatch%' OR "
            "m_user.text LIKE '%micro patch%' OR "
            "m_user.text LIKE '%stay in scope%' OR "
            "m_user.text LIKE '%stay inside%' OR "
            "m_user.text LIKE '%no drive-by%' OR "
            "m_user.text LIKE '%no drive by%' OR "
            "m_user.text LIKE '%don''t touch other%' OR "
            "m_user.text LIKE '%do not expand%' OR "
            "m_user.text LIKE '%edit only%' OR "
            "m_user.text LIKE '%over engineering%' OR "
            "m_user.text LIKE '%over-engineer%' OR "
            "m_user.text LIKE '%overengineer%' OR "
            "u.flags_json LIKE '%\"scope_narrowing\": true%'"
        ),
    ),
    (
        "verify_before_done",
        (
            "m_user.text LIKE '%run the test%' OR "
            "m_user.text LIKE '%run tests%' OR "
            "m_user.text LIKE '%run the typecheck%' OR "
            "m_user.text LIKE '%typecheck%' OR "
            "m_user.text LIKE '%verify before%' OR "
            "m_user.text LIKE '%before you finish%' OR "
            "m_user.text LIKE '%before claiming%' OR "
            "m_user.text LIKE '%make sure the test%' OR "
            "m_user.text LIKE '%regression test%' OR "
            "m_user.text LIKE '%rather than guessing%' OR "
            "m_user.text LIKE '%dont guess%' OR "
            "m_user.text LIKE '%don''t guess%' OR "
            "m_user.text LIKE '%via proxy%' OR "
            "u.flags_json LIKE '%\"verification_requested\": true%'"
        ),
    ),
    (
        "dont_act_yet_brake",
        (
            "m_user.text LIKE '%don''t act%' OR "
            "m_user.text LIKE '%do not act%' OR "
            "m_user.text LIKE '%dont act%' OR "
            "m_user.text LIKE '%just info%' OR "
            "m_user.text LIKE '%plan first%' OR "
            "m_user.text LIKE '%read first%' OR "
            "m_user.text LIKE '%don''t code yet%' OR "
            "m_user.text LIKE '%don''t start yet%' OR "
            "m_user.text LIKE '%don''t commit yet%' OR "
            "u.turn_kinds_json LIKE '%\"dont_act_yet\"%'"
        ),
    ),
    (
        "correction_redirect",
        (
            "m_user.text LIKE '%i said%' OR "
            "m_user.text LIKE '%you missed%' OR "
            "m_user.text LIKE '%instead of%' OR "
            "m_user.text LIKE '%i told you%' OR "
            "m_user.text LIKE '%wrong file%' OR "
            "m_user.text LIKE '%across everything%' OR "
            "m_user.text LIKE '%how many times%' OR "
            "m_user.text LIKE '%still missing%' OR "
            "u.flags_json LIKE '%\"instruction_violation_alleged\": true%'"
        ),
    ),
    (
        "workers_not_diy",
        (
            "m_user.text LIKE '%pawn%' OR "
            "m_user.text LIKE '%workers%' OR "
            "m_user.text LIKE '%subagent%' OR "
            "m_user.text LIKE '%don''t do it yourself%' OR "
            "m_user.text LIKE '%dont do it yourself%' OR "
            "m_user.text LIKE '%delegate%' OR "
            "m_user.text LIKE '%fan out%' OR "
            "m_user.text LIKE '%multitask%'"
        ),
    ),
    (
        "agent_teams_orchestration",
        (
            "m_user.text LIKE '%agent teams%' OR "
            "m_user.text LIKE '%agent-teams%' OR "
            "m_user.text LIKE '%--agent-teams%' OR "
            "m_user.text LIKE '%orchestration%' OR "
            "m_user.text LIKE '%Task tool%' OR "
            "m_user.text LIKE '%run_in_background%'"
        ),
    ),
    (
        "unfinished_delivery",
        (
            "m_user.text LIKE '%finish the%' OR "
            "m_user.text LIKE '%half ass%' OR "
            "m_user.text LIKE '%half-ass%' OR "
            "m_user.text LIKE '%still blocked%' OR "
            "m_user.text LIKE '%still broken%' OR "
            "m_user.text LIKE '%not done%' OR "
            "m_user.text LIKE '%incomplete%' OR "
            "m_user.text LIKE '%you didn''t%' OR "
            "m_user.text LIKE '%you didnt%'"
        ),
    ),
    (
        "privacy_security_constraint",
        (
            "m_user.text LIKE '%not public%' OR "
            "m_user.text LIKE '%dont want it public%' OR "
            "m_user.text LIKE '%don''t want it public%' OR "
            "m_user.text LIKE '%keep it private%' OR "
            "m_user.text LIKE '%no network%' OR "
            "m_user.text LIKE '%read-only%' OR "
            "m_user.text LIKE '%readonly%' OR "
            "m_user.text LIKE '%do not install%' OR "
            "m_user.text LIKE '%don''t install%' OR "
            "m_user.text LIKE '%supply chain%'"
        ),
    ),
    (
        "skill_or_harness_miss",
        (
            "m_user.text LIKE '%SKILL.md%' OR "
            "m_user.text LIKE '%use the skill%' OR "
            "m_user.text LIKE '%invoke the skill%' OR "
            "m_user.text LIKE '%AGENTS.md%' OR "
            "m_user.text LIKE '%follow the skill%' OR "
            "m_user.text LIKE '%you ignored%' OR "
            "m_user.text LIKE '%didn''t follow%' OR "
            "m_user.text LIKE '%didnt follow%'"
        ),
    ),
]


class ProposalLLMBackend(Protocol):
    """Shared interface for packet subagents and a future local-proxy client."""

    name: str

    def complete_json(self, *, system: str, user: str, model: str) -> dict[str, Any]:
        ...


class PacketProposalBackend:
    """File-based Cursor subagent handoff. Does not call a model in-process."""

    name = PROVIDER_PACKET

    def complete_json(self, *, system: str, user: str, model: str) -> dict[str, Any]:
        raise RuntimeError(
            "PacketProposalBackend does not invoke models in-process; "
            "emit packets, run Cursor subagents, then ingest results"
        )


@dataclass
class ValidationFailure:
    reason: str
    proposal_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "proposal_index": self.proposal_index}


@dataclass
class PacketIngestResult:
    packet_id: str
    status: str
    proposals: list[Proposal] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    failures: list[ValidationFailure] = field(default_factory=list)
    abstain_reason: str | None = None
    theme: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "status": self.status,
            "proposals": len(self.proposals),
            "claims": len(self.claims),
            "failures": [f.to_dict() for f in self.failures],
            "abstain_reason": self.abstain_reason,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_proposal_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def proposal_prompt_hash(text: str | None = None) -> str:
    body = text if text is not None else load_proposal_prompt()
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def _run_paths(run_dir: Path) -> dict[str, Path]:
    return {
        "manifest": run_dir / "manifest.json",
        "packets": run_dir / "packets",
        "results": run_dir / "results",
        "rejects": run_dir / "rejects",
        "prompt": run_dir / "proposal_subagent.md",
    }


def load_manifest(run_dir: Path) -> dict[str, Any]:
    path = _run_paths(run_dir)["manifest"]
    if not path.exists():
        raise FileNotFoundError(f"manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    path = assert_writable(_run_paths(run_dir)["manifest"], purpose="proposal manifest")
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def packet_run_status(run_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(run_dir)
    packets = manifest.get("packets") or {}
    counts: dict[str, int] = {}
    for meta in packets.values():
        st = str(meta.get("status") or PACKET_STATUS_PENDING)
        counts[st] = counts.get(st, 0) + 1
    return {
        "run_id": manifest.get("run_id"),
        "provider": manifest.get("provider"),
        "prompt_hash": manifest.get("prompt_hash"),
        "model": manifest.get("model"),
        "packet_count": len(packets),
        "window_count": manifest.get("window_count"),
        "status_counts": counts,
        "packets": packets,
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _redact(text: str, report: RedactionReport | None = None) -> str:
    """Redact secrets and local paths from text handed to proposal authors."""
    out = redact_text(text, report)

    def _mask_path(match: re.Match[str]) -> str:
        if report is not None:
            report.note("absolute_path")
        return "[REDACTED:absolute_path]"

    out = _PACKET_POSIX_PATH.sub(_mask_path, out)
    return _PACKET_WINDOWS_PATH.sub(_mask_path, out)


def _allowed_targets(
    inventory: ConfigInventory,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    candidates: list[Any] = []
    for f in inventory.files:
        if f.kind not in {"agents_md", "claude_md"}:
            continue
        if not f.exists:
            continue
        candidates.append(f)
    # Prefer globals + a few repo agents; keep packet small.
    globals_ = [f for f in candidates if f.scope_type == "global"]
    repos = [f for f in candidates if f.scope_type == "repo"][:8]
    out: list[dict[str, Any]] = []
    target_paths: dict[str, str] = {}
    for index, f in enumerate(globals_ + repos, start=1):
        target_id = f"target_{index:03d}"
        out.append(
            {
                "target_path": target_id,
                "label": f"{f.scope_type} {f.kind}",
                "kind": f.kind,
                "scope_type": f.scope_type,
            }
        )
        target_paths[target_id] = str(f.path)
    return out, target_paths


def _config_snippets(
    inventory: ConfigInventory,
    target_paths: dict[str, str],
    report: RedactionReport,
) -> list[dict[str, Any]]:
    target_ids_by_path = {path: target_id for target_id, path in target_paths.items()}
    snippets: list[dict[str, Any]] = []
    for f in inventory.files:
        path = str(f.path)
        target_id = target_ids_by_path.get(path)
        if target_id is None:
            continue
        text = _read_text(f.path)
        if not text:
            continue
        snippets.append(
            {
                "target_path": target_id,
                "kind": f.kind,
                "content_hash": _sha1_text(text),
                "text": _redact(text, report)[:MAX_CONFIG_CHARS],
                "truncated": len(text) > MAX_CONFIG_CHARS,
            }
        )
    return snippets


def _signal_summaries(
    conn: sqlite3.Connection, report: RedactionReport
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "claims"):
        return []
    rows = conn.execute(
        """
        SELECT id, kind, subject, support_status, sample_size, denominator,
               value_json, does_not_prove
        FROM claims
        WHERE kind IN (
            'recurring_instruction', 'correction_theme',
            'skill_exposure', 'harness_model_usage'
        )
        ORDER BY kind, sample_size DESC
        LIMIT 40
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        value = json.loads(r["value_json"] or "{}")
        summary = {
            "claim_id": r["id"],
            "kind": r["kind"],
            "subject": r["subject"],
            "support_status": r["support_status"],
            "sample_size": r["sample_size"],
            "denominator": r["denominator"],
            "theme": value.get("theme"),
            "phrasing": value.get("phrasing"),
            "does_not_prove": r["does_not_prove"],
        }
        out.append(
            {
                key: _redact(item, report) if isinstance(item, str) else item
                for key, item in summary.items()
            }
        )
    return out


def _opaque_project_key(repo: Any, cwd: Any) -> str | None:
    project = str(repo or "").strip() or str(cwd or "").strip()
    if not project:
        return None
    return f"project_{hashlib.sha1(project.encode('utf-8')).hexdigest()[:16]}"


def _session_logical_roots(conn: sqlite3.Connection) -> dict[str, dict[str, str | None]]:
    rows = conn.execute(
        "SELECT id, harness, external_id, parent_session_id, repo, cwd FROM sessions"
    ).fetchall()
    by_id = {str(row["id"]): row for row in rows}
    parents = resolve_implicit_parent_ids(rows)

    roots: dict[str, str] = {}
    for sid in sorted(by_id):
        cursor = sid
        seen: set[str] = set()
        while cursor in parents and cursor not in seen:
            seen.add(cursor)
            cursor = parents[cursor]
        roots[sid] = cursor if cursor in by_id else sid

    identity = build_identity_context(conn)
    out: dict[str, dict[str, str | None]] = {}
    for sid, row in by_id.items():
        physical_root = roots[sid]
        logical_root = logical_root_session_id(
            conn, physical_root, context=identity
        )
        logical_row = by_id[logical_root]
        project_key = _opaque_project_key(
            logical_row["repo"] or row["repo"],
            logical_row["cwd"] or row["cwd"],
        )
        out[sid] = {
            "logical_root_id": logical_root,
            "logical_harness": str(logical_row["harness"] or "(unknown)"),
            "project_key": project_key,
        }
    return out


def _eligible_root_session_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT id, harness, external_id, parent_session_id FROM sessions"
    ).fetchall()
    parents = resolve_implicit_parent_ids(rows)
    explicit_workers = explicit_worker_parent_ids(conn)
    identity = build_identity_context(conn)
    root_shadows = provider_root_shadow_ids(conn, context=identity)
    eligible: set[str] = set()
    for row in rows:
        session_id = str(row["id"])
        if (
            session_id in parents
            or session_id in explicit_workers
            or session_id in root_shadows
        ):
            continue
        projection = logical_projection(
            conn, session_id, str(row["harness"]), context=identity
        )
        eligible.add(str(projection["transcript_session_id"] or session_id))
    return eligible


def _fetch_theme_windows(
    conn: sqlite3.Connection,
    *,
    where_sql: str,
    limit: int,
    report: RedactionReport,
    logical_roots: dict[str, dict[str, str | None]],
    source_reader: CachedSourceTranscriptReader | None = None,
) -> list[dict[str, Any]]:
    sql = f"""
        SELECT
            w.id AS window_id,
            w.session_id,
            w.request_message_id,
            w.response_message_id,
            d.request_kind,
            s.harness,
            s.transcript_storage,
            s.started_at,
            m_user.text AS user_text,
            m_asst.text AS assistant_text,
            m_user.timestamp AS user_ts,
            u.turn_kinds_json,
            u.user_stance,
            u.agent_stance,
            u.flags_json,
            u.spans_json
        FROM window_det_classifications d
        JOIN exchange_windows w ON w.id = d.window_id
        JOIN sessions s ON s.id = w.session_id
        JOIN messages m_user ON m_user.id = w.request_message_id
        LEFT JOIN messages m_asst ON m_asst.id = w.response_message_id
        JOIN ux_observations u ON u.window_id = w.id
        WHERE d.request_kind = 'substantive'
          AND COALESCE(s.external_id, '') NOT LIKE 'skills:%'
          AND COALESCE(u.link_status, 'linked') = 'linked'
          AND COALESCE(m_user.authored_by_agent, 0) = 0
          AND ({where_sql})
        ORDER BY COALESCE(m_user.timestamp, s.started_at) DESC
    """
    rows = conn.execute(sql).fetchall()
    source_reader = source_reader or CachedSourceTranscriptReader()
    source_text: dict[str, str] = {}
    for session_id in sorted({str(row["session_id"]) for row in rows if row["transcript_storage"] == "source_backed"}):
        source = source_reader(conn, session_id)
        if not source.ready:
            raise RuntimeError(
                f"canonical source unavailable for {session_id}: "
                f"{source.warning or source.status}"
            )
        source_text.update({str(message["id"]): str(message["text"]) for message in source.messages})
    eligible_session_ids = _eligible_root_session_ids(conn)
    out: list[dict[str, Any]] = []
    seen_sessions: set[str] = set()
    seen_roots: set[str] = set()
    for r in rows:
        sid = str(r["session_id"])
        if sid not in eligible_session_ids:
            continue
        if sid in seen_sessions:
            continue
        logical = logical_roots.get(sid)
        if logical is None:
            continue
        logical_root_id = str(logical["logical_root_id"] or "")
        if not logical_root_id or logical_root_id in seen_roots:
            continue
        seen_sessions.add(sid)
        seen_roots.add(logical_root_id)
        user = _redact(source_text.get(str(r["request_message_id"]), str(r["user_text"] or "")), report)[:MAX_QUOTE_IN_PACKET]
        asst = _redact(source_text.get(str(r["response_message_id"]), str(r["assistant_text"] or "")), report)[:MAX_QUOTE_IN_PACKET]
        spans = []
        try:
            raw_spans = json.loads(r["spans_json"] or "[]")
        except json.JSONDecodeError:
            raw_spans = []
        if isinstance(raw_spans, list):
            for sp in raw_spans[:3]:
                if not isinstance(sp, dict) or not sp.get("quote"):
                    continue
                spans.append(
                    {
                        "role": sp.get("role"),
                        "quote": _redact(str(sp["quote"]), report)[:MAX_QUOTE_CHARS],
                    }
                )
        out.append(
            {
                "window_id": str(r["window_id"]),
                "session_id": sid,
                "request_message_id": str(r["request_message_id"]),
                "response_message_id": str(r["response_message_id"] or ""),
                "logical_root_id": logical_root_id,
                "logical_harness": logical["logical_harness"],
                "project_key": logical["project_key"],
                "request_kind": r["request_kind"],
                "harness": r["harness"],
                "timestamp": r["user_ts"] or r["started_at"],
                "user": user,
                "assistant": asst,
                "turn_kinds": json.loads(r["turn_kinds_json"] or "[]"),
                "user_stance": r["user_stance"],
                "agent_stance": r["agent_stance"],
                "flags": json.loads(r["flags_json"] or "{}"),
                "spans": spans,
            }
        )
        if len(out) >= limit:
            break
    return out


def _eligible_root_population(
    conn: sqlite3.Connection,
    logical_roots: dict[str, dict[str, str | None]],
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT DISTINCT s.id
        FROM sessions s
        JOIN exchange_windows w ON w.session_id = s.id
        JOIN window_det_classifications d ON d.window_id = w.id
        JOIN ux_observations u ON u.window_id = w.id
        JOIN messages m_user ON m_user.id = w.request_message_id
        WHERE COALESCE(s.external_id, '') NOT LIKE 'skills:%'
          AND d.request_kind = 'substantive'
          AND COALESCE(u.link_status, 'linked') = 'linked'
          AND COALESCE(m_user.authored_by_agent, 0) = 0
        """
    ).fetchall()
    eligible_session_ids = _eligible_root_session_ids(conn)
    unique_roots = {
        str(logical["logical_root_id"])
        for row in rows
        if str(row["id"]) in eligible_session_ids
        if (logical := logical_roots.get(str(row["id"]))) is not None
        and logical.get("logical_root_id")
    }
    root_info = {
        root_id: next(
            logical
            for logical in logical_roots.values()
            if logical.get("logical_root_id") == root_id
        )
        for root_id in unique_roots
    }
    counts: dict[str, int] = {}
    for logical in root_info.values():
        harness = str(logical.get("logical_harness") or "(unknown)")
        counts[harness] = counts.get(harness, 0) + 1
    by_harness = [
        {"harness": harness, "root_cluster_count": count}
        for harness, count in sorted(counts.items())
    ]
    return {
        "definition_version": ELIGIBLE_POPULATION_VERSION,
        "root_cluster_count": len(unique_roots),
        "by_harness": by_harness,
    }


def _adjudicated_miss_pairs(
    conn: sqlite3.Connection,
    windows: list[dict[str, Any]],
    theme: str,
) -> list[dict[str, Any]]:
    if not windows or not _table_exists(conn, "adjudications"):
        return []
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(adjudications)")
    }
    if "window_id" not in columns or "turn_kind" not in columns:
        return []
    window_ids = [str(window["window_id"]) for window in windows]
    placeholders = ", ".join("?" for _ in window_ids)
    fields = ["window_id", "turn_kind", "user_stance", "prior_outcome"]
    if "link_status" in columns:
        fields.append("link_status")
    rows = conn.execute(
        f"SELECT {', '.join(fields)} FROM adjudications "
        f"WHERE window_id IN ({placeholders})",
        window_ids,
    ).fetchall()
    windows_by_id = {str(window["window_id"]): window for window in windows}
    pairs: list[dict[str, Any]] = []
    for row in rows:
        if "link_status" in columns and row["link_status"] != "linked":
            continue
        try:
            turn_kinds = json.loads(row["turn_kind"] or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(turn_kinds, list):
            continue
        kinds = {str(kind) for kind in turn_kinds}
        if not kinds.intersection({"correction", "redirect_or_brake"}):
            continue
        if str(row["prior_outcome"] or "") != "rejected_redo":
            continue
        window_id = str(row["window_id"])
        window = windows_by_id.get(window_id)
        if window is None:
            continue
        pairs.append(
            {
                "pair_id": f"adjudicated_miss:{window_id}",
                "pattern_key": theme,
                "window_id": window_id,
                "logical_root_id": window["logical_root_id"],
                "logical_harness": window["logical_harness"],
                "project_key": window["project_key"],
                "turn_kinds": sorted(kinds),
                "prior_outcome": "rejected_redo",
            }
        )
    return sorted(pairs, key=lambda pair: str(pair["pair_id"]))


def _packet_provenance_failure(packet: dict[str, Any]) -> str | None:
    redaction = packet.get("redaction")
    if (
        not isinstance(redaction, dict)
        or redaction.get("redaction_version") != REDACTION_VERSION
    ):
        return "legacy_packet_missing_current_provenance"
    if packet.get("validator_version") != PROPOSAL_PACKET_VALIDATOR_VERSION:
        return "legacy_packet_missing_current_provenance"
    contract = packet.get("evidence_contract")
    if (
        not isinstance(contract, dict)
        or contract.get("version") != EVIDENCE_CONTRACT_VERSION
        or contract.get("min_independent_logical_roots") != MIN_SESSIONS_FINDING
        or contract.get("min_adjudicated_miss_pairs") != MIN_ADJUDICATED_MISS_PAIRS
        or contract.get("min_global_logical_roots") != MIN_GLOBAL_LOGICAL_ROOTS
    ):
        return "legacy_packet_missing_current_provenance"
    if not isinstance(packet.get("validated_miss_pairs"), list):
        return "legacy_packet_missing_current_provenance"
    return None


def _eligible_population(
    packet: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    population = packet.get("eligible_population")
    if not isinstance(population, dict):
        return None, "packet_missing_eligible_population"
    if population.get("definition_version") != ELIGIBLE_POPULATION_VERSION:
        return None, "packet_missing_eligible_population"
    count = population.get("root_cluster_count")
    if not isinstance(count, int) or count < 0:
        return None, "packet_invalid_eligible_population"
    distribution = population.get("by_harness")
    if not isinstance(distribution, list):
        return None, "packet_invalid_eligible_population"
    counts: list[int] = []
    for item in distribution:
        if not isinstance(item, dict) or not str(item.get("harness") or ""):
            return None, "packet_invalid_eligible_population"
        item_count = item.get("root_cluster_count")
        if not isinstance(item_count, int) or item_count < 0:
            return None, "packet_invalid_eligible_population"
        counts.append(item_count)
    if sum(counts) != count:
        return None, "packet_invalid_eligible_population"
    return population, None


def _normalized_key(value: Any) -> str:
    return _INTENT_NORMALIZE.sub("-", str(value or "").lower()).strip("-")


def _validated_miss_pair_index(
    packet: dict[str, Any], windows: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], str | None]:
    pairs = packet.get("validated_miss_pairs")
    if not isinstance(pairs, list):
        return {}, "packet_missing_validated_miss_pairs"
    expected_pattern = _normalized_key(packet.get("theme"))
    index: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            return {}, "packet_invalid_validated_miss_pairs"
        pair_id = str(pair.get("pair_id") or "")
        window_id = str(pair.get("window_id") or "")
        window = windows.get(window_id)
        if not pair_id or pair_id in index or window is None:
            return {}, "packet_invalid_validated_miss_pairs"
        if _normalized_key(pair.get("pattern_key")) != expected_pattern:
            return {}, "packet_invalid_validated_miss_pairs"
        for key in ("logical_root_id", "logical_harness", "project_key"):
            if pair.get(key) != window.get(key):
                return {}, "packet_invalid_validated_miss_pairs"
        index[pair_id] = pair
    return index, None


def _config_hashes(packet: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for snippet in packet.get("config_snippets") or []:
        if not isinstance(snippet, dict):
            continue
        target = str(snippet.get("target_path") or "")
        content_hash = str(snippet.get("content_hash") or "")
        if target and content_hash:
            out[target] = content_hash
    return out


def _normalize_instruction_rewrite(rewrite: str) -> str:
    cleaned = rewrite.strip()
    while True:
        normalized = _LEADING_LIST_MARKER.sub("", cleaned, count=1).strip()
        if normalized == cleaned:
            return normalized
        cleaned = normalized


def _normalized_intent(theme: str, heading: str) -> str:
    normalized_theme = _normalized_key(theme)
    normalized_heading = _normalized_key(heading)
    return f"{normalized_theme}\x1f{normalized_heading}"


def _semantic_identity(
    *,
    scope_type: str,
    scope_id: str | None,
    target_ref: str,
    path: str,
    intent_key: str,
) -> str:
    target_identity = path or target_ref
    return "\x1f".join(
        (scope_type, scope_id or "", target_identity, intent_key)
    )


def _semantic_id(prefix: str, semantic_identity: str) -> str:
    return hashlib.sha1(
        f"{prefix}\x1f{semantic_identity}".encode("utf-8")
    ).hexdigest()[:24]


def _proposal_intent_key(proposal: Proposal) -> str:
    intent = str(proposal.provenance.get("intent_key") or "")
    if not intent:
        intent = _normalized_intent(
            str(proposal.provenance.get("theme") or ""), proposal.title
        )
    return f"{proposal.target_path}\x1f{intent}"


def _packet_hash(body: dict[str, Any]) -> str:
    canonical_body = {
        key: value for key, value in body.items() if key != "evidence_pack_hash"
    }
    blob = json.dumps(canonical_body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:24]


def _target_bindings_hash(target_paths: dict[str, str]) -> str:
    blob = json.dumps(target_paths, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:24]


def _packet_integrity_failure(packet: dict[str, Any], meta: Any) -> str | None:
    expected_hash = str(packet.get("evidence_pack_hash") or "")
    if not expected_hash or expected_hash != _packet_hash(packet):
        return "packet_evidence_hash_mismatch"
    if not isinstance(meta, dict):
        return "packet_manifest_metadata_invalid"
    if str(meta.get("evidence_pack_hash") or "") != expected_hash:
        return "packet_manifest_evidence_hash_mismatch"
    return None


def emit_proposal_packet_run(
    conn: sqlite3.Connection,
    run_dir: Path,
    *,
    model: str = DEFAULT_MODEL,
    home: Path | None = None,
    resume: bool = True,
    windows_per_theme: int = MAX_WINDOWS_PER_THEME,
) -> dict[str, Any]:
    """Emit stratified evidence packets for Cursor subagent proposal authors."""
    paths = _run_paths(run_dir)
    if resume and paths["manifest"].exists():
        return load_manifest(run_dir)

    if not _table_exists(conn, "window_det_classifications"):
        raise RuntimeError(
            "window_det_classifications missing; run agentlog classify first"
        )
    if not _table_exists(conn, "ux_observations"):
        raise RuntimeError("ux_observations missing; run UX extraction first")

    inventory = discover_config_inventory(home)
    allowed, target_paths = _allowed_targets(inventory)
    logical_roots = _session_logical_roots(conn)
    eligible_population = _eligible_root_population(conn, logical_roots)
    prompt_text = load_proposal_prompt()
    phash = proposal_prompt_hash(prompt_text)

    assert_writable(run_dir, purpose="proposal packet run dir")
    run_dir.mkdir(parents=True, exist_ok=True)
    for key in ("packets", "results", "rejects"):
        paths[key].mkdir(parents=True, exist_ok=True)
    paths["prompt"].write_text(prompt_text, encoding="utf-8")

    run_id = f"proposals_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    packet_meta: dict[str, Any] = {}
    window_count = 0
    source_reader = CachedSourceTranscriptReader()
    for i, (theme, where_sql) in enumerate(THEME_SPECS, start=1):
        report = RedactionReport()
        snippets = _config_snippets(inventory, target_paths, report)
        signals = _signal_summaries(conn, report)
        windows = _fetch_theme_windows(
            conn,
            where_sql=where_sql,
            limit=windows_per_theme,
            report=report,
            logical_roots=logical_roots,
            source_reader=source_reader,
        )
        miss_pairs = _adjudicated_miss_pairs(conn, windows, theme)
        window_count += len(windows)
        packet_id = f"ppkt_{i:04d}_{theme}"
        target_paths_hash = _target_bindings_hash(target_paths)
        body = {
            "packet_id": packet_id,
            "run_id": run_id,
            "theme": theme,
            "prompt_hash": phash,
            "prompt_file": "proposal_subagent.md",
            "model_hint": model,
            "provider": PROVIDER_PACKET,
            "validator_version": PROPOSAL_PACKET_VALIDATOR_VERSION,
            "denominator_note": (
                "Only substantive root-session windows are included. "
                "auto_review/worker_brief are excluded as habit evidence."
            ),
            "gates": {
                "min_sessions_ok": MIN_SESSIONS_FINDING,
                "min_global_logical_roots": MIN_GLOBAL_LOGICAL_ROOTS,
                "min_adjudicated_miss_pairs": MIN_ADJUDICATED_MISS_PAIRS,
                "global_min_harnesses": 2,
                "global_max_concentration": MAX_GLOBAL_CONCENTRATION,
                "max_proposals": MAX_PROPOSALS_PER_PACKET,
            },
            "evidence_contract": {
                "version": EVIDENCE_CONTRACT_VERSION,
                "min_independent_logical_roots": MIN_SESSIONS_FINDING,
                "min_adjudicated_miss_pairs": MIN_ADJUDICATED_MISS_PAIRS,
                "min_global_logical_roots": MIN_GLOBAL_LOGICAL_ROOTS,
                "global_min_harnesses": 2,
                "global_max_concentration": MAX_GLOBAL_CONCENTRATION,
            },
            "allowed_targets": allowed,
            "target_paths_hash": target_paths_hash,
            "config_snippets": snippets,
            "signals": [
                s for s in signals if s.get("theme") == theme or s["kind"] != "recurring_instruction"
            ][:12],
            "eligible_population": eligible_population,
            "windows": windows,
            "validated_miss_pairs": miss_pairs,
            "redaction": report.to_dict(),
        }
        if not source_reader.verify_current():
            raise RuntimeError("canonical source changed before proposal packet write")
        body["evidence_pack_hash"] = _packet_hash(body)
        packet_path = paths["packets"] / f"{packet_id}.json"
        packet_path.write_text(
            json.dumps(body, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        packet_meta[packet_id] = {
            "status": PACKET_STATUS_PENDING,
            "theme": theme,
            "window_ids": [w["window_id"] for w in windows],
            "session_count": len({w["session_id"] for w in windows}),
            "eligible_population": eligible_population,
            "evidence_pack_hash": body["evidence_pack_hash"],
            "packet_path": str(packet_path.relative_to(run_dir)),
            "target_paths": target_paths,
            "target_paths_hash": target_paths_hash,
            "redaction": body["redaction"],
            "result_path": None,
            "ingested_at": None,
            "reject_reasons": [],
        }

    manifest = {
        "run_id": run_id,
        "provider": PROVIDER_PACKET,
        "created_at": _utc_now(),
        "model": model,
        "prompt_hash": phash,
        "redaction_version": REDACTION_VERSION,
        "validator_version": PROPOSAL_PACKET_VALIDATOR_VERSION,
        "eligible_population": eligible_population,
        "window_count": window_count,
        "packet_count": len(packet_meta),
        "packets": packet_meta,
        "optional_backends": [PROVIDER_PROXY],
        "default_backend": PROVIDER_PACKET,
    }
    save_manifest(run_dir, manifest)
    return manifest


def _window_index(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(w["window_id"]): w for w in packet.get("windows") or [] if w.get("window_id")}


def _quote_source_in_window(
    quote: str, window: dict[str, Any]
) -> tuple[str | None, str | None, str | None]:
    q = quote.strip()
    if not q:
        return None, None, "quote_not_in_window"
    request_message_id = str(
        window.get("request_message_id") or window.get("message_id") or ""
    )
    response_message_id = str(window.get("response_message_id") or "")
    matches: set[tuple[str, str]] = set()
    if q in str(window.get("user") or ""):
        matches.add(("user", request_message_id))
    if q in str(window.get("assistant") or ""):
        matches.add(("assistant", response_message_id))
    for span in window.get("spans") or []:
        if not isinstance(span, dict) or q not in str(span.get("quote") or ""):
            continue
        role = str(span.get("role") or "")
        if role == "user":
            matches.add((role, request_message_id))
        elif role == "assistant":
            matches.add((role, response_message_id))
        else:
            return None, None, "quote_source_message_unbound"
    if not matches:
        return None, None, "quote_not_in_window"
    if len(matches) != 1:
        return None, None, "quote_source_ambiguous"
    role, message_id = next(iter(matches))
    if not message_id:
        return None, None, "quote_source_message_unbound"
    return role, message_id, None


def validate_proposal_result(
    raw: Any,
    *,
    packet: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[ValidationFailure]]:
    """Hard validation — reject rather than coerce."""
    failures: list[ValidationFailure] = []
    if not isinstance(raw, dict):
        return None, [ValidationFailure(reason="result_not_object")]

    packet_id = str(packet.get("packet_id") or "")
    if str(raw.get("packet_id") or "") != packet_id:
        failures.append(ValidationFailure(reason="packet_id_mismatch"))

    provenance_failure = _packet_provenance_failure(packet)
    if provenance_failure:
        failures.append(ValidationFailure(reason=provenance_failure))
    population, population_failure = _eligible_population(packet)
    if population_failure:
        failures.append(ValidationFailure(reason=population_failure))

    if raw.get("abstain") is True:
        if failures:
            return None, failures
        return {
            "packet_id": packet_id,
            "abstain": True,
            "abstain_reason": str(raw.get("abstain_reason") or "abstain"),
            "proposals": [],
            "model": raw.get("model"),
        }, []

    proposals = raw.get("proposals")
    if not isinstance(proposals, list):
        failures.append(ValidationFailure(reason="proposals_not_list"))
        return None, failures
    if len(proposals) > MAX_PROPOSALS_PER_PACKET:
        failures.append(ValidationFailure(reason="too_many_proposals"))

    allowed_targets = {
        str(target.get("target_path") or target.get("path") or ""): target
        for target in (packet.get("allowed_targets") or [])
        if isinstance(target, dict)
    }
    allowed_paths = set(allowed_targets)
    windows = _window_index(packet)
    miss_pairs, miss_pair_failure = _validated_miss_pair_index(packet, windows)
    if miss_pair_failure:
        return None, [ValidationFailure(reason=miss_pair_failure)]
    config_hashes = _config_hashes(packet)
    population_denominator = int((population or {}).get("root_cluster_count") or 0)
    intent_theme = str(packet.get("theme") or "")
    seen_intents: set[str] = set()
    cleaned: list[dict[str, Any]] = []

    for idx, prop in enumerate(proposals):
        if not isinstance(prop, dict):
            failures.append(
                ValidationFailure(reason="proposal_not_object", proposal_index=idx)
            )
            continue
        title = str(prop.get("title") or "").strip()
        target = str(prop.get("target_path") or "").strip()
        rewrite = _normalize_instruction_rewrite(
            str(prop.get("instruction_rewrite") or "")
        )
        rationale = str(prop.get("rationale") or "").strip()
        does_not_prove = str(prop.get("does_not_prove") or "").strip()
        action = str(prop.get("action") or "add").strip()
        heading = str(prop.get("heading") or title).strip()
        evidence = prop.get("evidence")
        pattern_key = _normalized_key(prop.get("pattern_key"))
        pair_ids = prop.get("validated_miss_pair_ids")
        config_gap = prop.get("config_gap")

        if not title:
            failures.append(
                ValidationFailure(reason="missing_title", proposal_index=idx)
            )
        if target not in allowed_paths:
            failures.append(
                ValidationFailure(reason="target_path_not_allowed", proposal_index=idx)
            )
        if not rewrite:
            failures.append(
                ValidationFailure(reason="missing_instruction_rewrite", proposal_index=idx)
            )
        if not rationale:
            failures.append(
                ValidationFailure(reason="missing_rationale", proposal_index=idx)
            )
        if not does_not_prove:
            failures.append(
                ValidationFailure(reason="missing_does_not_prove", proposal_index=idx)
            )
        if pattern_key != _normalized_key(intent_theme):
            failures.append(
                ValidationFailure(
                    reason="pattern_key_not_linked_to_packet_theme",
                    proposal_index=idx,
                )
            )
        selected_pairs: list[dict[str, Any]] = []
        if not isinstance(pair_ids, list) or not pair_ids:
            failures.append(
                ValidationFailure(
                    reason="missing_validated_miss_pair_ids",
                    proposal_index=idx,
                )
            )
        else:
            pair_id_set = {str(pair_id) for pair_id in pair_ids}
            if len(pair_id_set) != len(pair_ids) or not pair_id_set <= set(miss_pairs):
                failures.append(
                    ValidationFailure(
                        reason="invalid_validated_miss_pair_ids",
                        proposal_index=idx,
                    )
                )
            else:
                selected_pairs = [miss_pairs[pair_id] for pair_id in sorted(pair_id_set)]
        clean_gap: dict[str, str] | None = None
        if not isinstance(config_gap, dict):
            failures.append(
                ValidationFailure(reason="missing_config_gap", proposal_index=idx)
            )
        else:
            gap_target = str(config_gap.get("target_path") or "")
            gap_hash = str(config_gap.get("content_hash") or "")
            gap_finding = str(config_gap.get("finding") or "").strip()
            if (
                gap_target != target
                or config_hashes.get(target) != gap_hash
                or not gap_finding
            ):
                failures.append(
                    ValidationFailure(
                        reason="config_gap_not_linked_to_packet_target",
                        proposal_index=idx,
                    )
                )
            else:
                clean_gap = {
                    "target_path": gap_target,
                    "content_hash": gap_hash,
                    "finding": gap_finding,
                }
        intent_key = _normalized_intent(intent_theme, heading)
        proposal_key = f"{target}\x1f{intent_key}"
        if proposal_key in seen_intents:
            failures.append(
                ValidationFailure(
                    reason="duplicate_target_intent_within_packet",
                    proposal_index=idx,
                )
            )
        else:
            seen_intents.add(proposal_key)
        if action == "update":
            failures.append(
                ValidationFailure(reason="update_not_supported", proposal_index=idx)
            )
        elif action != "add":
            failures.append(
                ValidationFailure(reason="bad_action", proposal_index=idx)
            )
        if not isinstance(evidence, list) or not evidence:
            failures.append(
                ValidationFailure(reason="missing_evidence", proposal_index=idx)
            )
            evidence = []

        logical_roots: dict[str, tuple[str, str | None]] = {}
        clean_ev: list[dict[str, Any]] = []
        for ev in evidence:
            if not isinstance(ev, dict):
                failures.append(
                    ValidationFailure(reason="evidence_not_object", proposal_index=idx)
                )
                continue
            wid = str(ev.get("window_id") or "")
            quote = str(ev.get("quote") or "").strip()
            sid = str(ev.get("session_id") or "")
            window = windows.get(wid)
            if window is None:
                failures.append(
                    ValidationFailure(reason="unknown_window_id", proposal_index=idx)
                )
                continue
            if sid and sid != str(window.get("session_id") or ""):
                failures.append(
                    ValidationFailure(reason="session_window_mismatch", proposal_index=idx)
                )
                continue
            sid = str(window.get("session_id") or sid)
            evidence_role, message_id, quote_failure = _quote_source_in_window(
                quote, window
            )
            if quote_failure is not None:
                failures.append(
                    ValidationFailure(reason=quote_failure, proposal_index=idx)
                )
                continue
            logical_root_id = str(window.get("logical_root_id") or "")
            logical_harness = str(window.get("logical_harness") or "")
            project_key = window.get("project_key")
            if not logical_root_id or not logical_harness:
                failures.append(
                    ValidationFailure(
                        reason="window_missing_logical_root_metadata",
                        proposal_index=idx,
                    )
                )
                continue
            existing_root = logical_roots.get(logical_root_id)
            root_meta = (logical_harness, str(project_key) if project_key else None)
            if existing_root is not None and existing_root != root_meta:
                failures.append(
                    ValidationFailure(
                        reason="logical_root_metadata_conflict",
                        proposal_index=idx,
                    )
                )
                continue
            logical_roots[logical_root_id] = root_meta
            clean_ev.append(
                {
                    "session_id": sid,
                    "window_id": wid,
                    "message_id": message_id,
                    "evidence_role": evidence_role,
                    "logical_root_id": logical_root_id,
                    "logical_harness": logical_harness,
                    "project_key": project_key,
                    "quote": clip_quote(quote) or quote,
                    "timestamp": ev.get("timestamp") or window.get("timestamp"),
                }
            )

        n_sessions = len(logical_roots)
        target_meta = allowed_targets.get(target) or {}
        is_global_target = str(target_meta.get("scope_type") or "") == "global"
        minimum_roots = (
            MIN_GLOBAL_LOGICAL_ROOTS if is_global_target else MIN_SESSIONS_FINDING
        )
        if n_sessions > population_denominator:
            failures.append(
                ValidationFailure(
                    reason="evidence_exceeds_eligible_population",
                    proposal_index=idx,
                )
            )
        if n_sessions < minimum_roots:
            continue
        evidence_window_ids = {str(item["window_id"]) for item in clean_ev}
        pair_roots = {
            str(pair["logical_root_id"])
            for pair in selected_pairs
            if str(pair.get("window_id") or "") in evidence_window_ids
        }
        if (
            len(pair_roots) < MIN_ADJUDICATED_MISS_PAIRS
            or len(selected_pairs) < MIN_ADJUDICATED_MISS_PAIRS
        ):
            continue
        harness_counts: dict[str, int] = {}
        project_counts: dict[str, int] = {}
        for harness, project_key in logical_roots.values():
            harness_counts[harness] = harness_counts.get(harness, 0) + 1
            if project_key is not None:
                project_counts[project_key] = project_counts.get(project_key, 0) + 1
        if is_global_target:
            largest_harness = max(harness_counts.values(), default=0) / n_sessions
            largest_project = max(project_counts.values(), default=0) / n_sessions
            if (
                len(harness_counts) < 2
                or sum(project_counts.values()) != n_sessions
                or largest_harness > MAX_GLOBAL_CONCENTRATION
                or largest_project > MAX_GLOBAL_CONCENTRATION
            ):
                continue

        cleaned.append(
            {
                "title": title,
                "action": action,
                "target_path": target,
                "heading": heading,
                "instruction_rewrite": rewrite,
                "rationale": rationale,
                "does_not_prove": does_not_prove,
                "support_tier": "ok",
                "sample_size": n_sessions,
                "population_denominator": population_denominator,
                "eligible_population": population,
                "intent_key": intent_key,
                "validated_miss_pair_ids": [
                    str(pair["pair_id"]) for pair in selected_pairs
                ],
                "config_gap": clean_gap,
                "support_distribution": {
                    "by_harness": dict(sorted(harness_counts.items())),
                    "by_project": dict(sorted(project_counts.items())),
                },
                "evidence": clean_ev,
            }
        )

    if failures:
        return None, failures
    if not cleaned:
        return {
            "packet_id": packet_id,
            "abstain": True,
            "abstain_reason": str(
                raw.get("abstain_reason")
                or "no proposals met evidence gates"
            ),
            "proposals": [],
            "model": raw.get("model"),
        }, []
    return {
        "packet_id": packet_id,
        "abstain": False,
        "abstain_reason": None,
        "proposals": cleaned,
        "model": raw.get("model"),
    }, []


def _target_meta(
    inventory: ConfigInventory, path: str
) -> tuple[str, str, str | None]:
    for f in inventory.files:
        if str(f.path) == path:
            return f.kind, f.scope_type, f.scope_id
    return "agents_md", "global", "global"


def _hydrate_packet_target_paths(
    packet: dict[str, Any], meta: Any
) -> str | None:
    """Attach local target paths after reading the model-facing packet."""
    if not isinstance(meta, dict):
        return "packet_manifest_metadata_invalid"
    target_paths = meta.get("target_paths")
    if not isinstance(target_paths, dict):
        return "packet_target_bindings_missing"
    bindings = {
        str(target_id): str(path)
        for target_id, path in target_paths.items()
        if isinstance(target_id, str) and isinstance(path, str)
    }
    if len(bindings) != len(target_paths):
        return "packet_target_bindings_invalid"
    expected_hash = str(packet.get("target_paths_hash") or "")
    actual_hash = _target_bindings_hash(bindings)
    if (
        not expected_hash
        or expected_hash != actual_hash
        or str(meta.get("target_paths_hash") or "") != actual_hash
    ):
        return "packet_target_bindings_mismatch"
    allowed_refs = {
        str(target.get("target_path") or target.get("path") or "")
        for target in packet.get("allowed_targets") or []
        if isinstance(target, dict)
    }
    if not allowed_refs or set(bindings) != allowed_refs:
        return "packet_target_bindings_mismatch"
    packet["_target_paths"] = bindings
    return None


def materialize_proposals(
    *,
    validated: dict[str, Any],
    packet: dict[str, Any],
    inventory: ConfigInventory,
    now: str,
) -> tuple[list[Proposal], list[Claim]]:
    proposals: list[Proposal] = []
    claims: list[Claim] = []
    run_id = str(packet.get("run_id") or "")
    pack_hash = str(packet.get("evidence_pack_hash") or "")
    prompt_hash = str(packet.get("prompt_hash") or "")
    model = str(packet.get("model_hint") or DEFAULT_MODEL)
    reported_model = str(validated.get("model") or "").strip() or None
    theme = str(packet.get("theme") or "llm_proposal")
    local_target_paths = packet.get("_target_paths") or {}

    for prop in validated.get("proposals") or []:
        target_ref = str(prop["target_path"])
        path = str(local_target_paths.get(target_ref) or target_ref)
        old = _read_text(Path(path))
        config_gap = prop.get("config_gap") or {}
        if (
            not isinstance(config_gap, dict)
            or str(config_gap.get("content_hash") or "") != _sha1_text(old)
        ):
            continue
        heading = str(prop["heading"])
        body = _normalize_instruction_rewrite(str(prop["instruction_rewrite"]))
        if not body:
            continue
        new = _append_section(old, heading, f"- {body}")
        if new == old:
            # Already present — skip publish.
            continue
        diff = unified_diff(path=path, old=old, new=new)
        kind, scope_type, scope_id = _target_meta(inventory, path)
        semantic_identity = _semantic_identity(
            scope_type=scope_type,
            scope_id=scope_id,
            target_ref=target_ref,
            path=path,
            intent_key=str(prop["intent_key"]),
        )
        evidence = [
            ClaimEvidence(
                session_id=e.get("session_id"),
                window_id=e.get("window_id"),
                message_id=e.get("message_id"),
                quote=e.get("quote"),
                meta={
                    "timestamp": e.get("timestamp"),
                    "logical_root_id": e.get("logical_root_id"),
                    "logical_harness": e.get("logical_harness"),
                    "project_key": e.get("project_key"),
                    "evidence_role": e.get("evidence_role"),
                },
            )
            for e in prop.get("evidence") or []
        ]
        claim_id = _semantic_id("llm_proposal_claim", semantic_identity)
        claim = Claim(
            id=claim_id,
            kind="llm_instruction_proposal",
            subject=theme,
            predicate="suggested_instruction",
            value={
                "theme": theme,
                "heading": heading,
                "suggested_instruction": body,
                "title": prop["title"],
                "packet_id": packet.get("packet_id"),
                "validated_miss_pair_ids": prop["validated_miss_pair_ids"],
                "config_gap": prop["config_gap"],
                "semantic_identity": semantic_identity,
            },
            scope_type=scope_type,  # type: ignore[arg-type]
            scope_id=scope_id,
            derivation="llm_derived",
            support_status=prop["support_tier"],
            sample_size=int(prop["sample_size"]),
            denominator=int(prop["population_denominator"]),
            rate=(
                int(prop["sample_size"]) / int(prop["population_denominator"])
                if int(prop["population_denominator"])
                else None
            ),
            observed_at=now,
            extractor_name="proposal_packets",
            extractor_version=PROPOSAL_EXTRACTOR_VERSION,
            confidence_basis={
                "provider": PROVIDER_PACKET,
                "model": model,
                "reported_model_unverified": reported_model,
                "run_id": run_id,
                "prompt_hash": prompt_hash,
                "evidence_pack_hash": pack_hash,
                "packet_id": packet.get("packet_id"),
                "evidence_sessions": int(prop["sample_size"]),
                "eligible_population": prop["eligible_population"],
                "intent_key": prop["intent_key"],
                "semantic_identity": semantic_identity,
                "target_ref": target_ref,
                "validated_miss_pair_ids": prop["validated_miss_pair_ids"],
                "config_gap": prop["config_gap"],
                "support_distribution": prop["support_distribution"],
                "validator_version": packet.get("validator_version"),
            },
            does_not_prove=str(prop["does_not_prove"]),
            evidence=evidence,
            created_at=now,
            updated_at=now,
        )
        claims.append(claim)
        provenance = {
            "provider": PROVIDER_PACKET,
            "model": model,
            "reported_model_unverified": reported_model,
            "run_id": run_id,
            "prompt_hash": prompt_hash,
            "evidence_pack_hash": pack_hash,
            "packet_id": packet.get("packet_id"),
            "theme": theme,
            "redaction": packet.get("redaction") or {},
            "evidence_sessions": int(prop["sample_size"]),
            "eligible_population": prop["eligible_population"],
            "intent_key": prop["intent_key"],
            "semantic_identity": semantic_identity,
            "target_ref": target_ref,
            "validated_miss_pair_ids": prop["validated_miss_pair_ids"],
            "config_gap": prop["config_gap"],
            "support_distribution": prop["support_distribution"],
            "validator_version": packet.get("validator_version"),
        }
        proposals.append(
            Proposal(
                id=_semantic_id("llm_proposal", semantic_identity),
                title=str(prop["title"]),
                action=prop["action"],  # type: ignore[arg-type]
                status="pending",
                target_path=path,
                target_kind=kind,
                scope_type=scope_type,  # type: ignore[arg-type]
                scope_id=scope_id,
                base_content_hash=_sha1_text(old) if old else None,
                unified_diff=diff,
                proposed_content=new,
                rationale=str(prop["rationale"]),
                derivation_summary=(
                    f"LLM packet proposal ({PROVIDER_PACKET}/{model}); "
                    f"theme={theme}; support={prop['support_tier']}; "
                    f"n_sessions={prop['sample_size']}/"
                    f"{prop['population_denominator']} eligible root clusters"
                ),
                does_not_prove=str(prop["does_not_prove"]),
                sample_size=int(prop["sample_size"]),
                claim_ids=[claim.id],
                claims=[claim],
                created_at=now,
                updated_at=now,
                provenance=provenance,
                run_id=run_id,
                model=model,
                prompt_hash=prompt_hash,
                evidence_pack_hash=pack_hash,
            )
        )
    return proposals, claims


def _abstain_duplicate_intents(
    results: list[PacketIngestResult], manifest: dict[str, Any]
) -> None:
    seen: dict[str, str] = {}
    packets = manifest.get("packets") or {}
    for result in results:
        if result.status != PACKET_STATUS_COMPLETED:
            continue
        keys = {_proposal_intent_key(proposal) for proposal in result.proposals}
        duplicate_of = next((seen[key] for key in keys if key in seen), None)
        if duplicate_of is not None:
            result.status = PACKET_STATUS_ABSTAINED
            result.abstain_reason = (
                "duplicate_target_intent_within_run; "
                f"already proposed by {duplicate_of}"
            )
            result.failures.append(
                ValidationFailure(reason="duplicate_target_intent_within_run")
            )
            result.proposals = []
            result.claims = []
            meta = packets.get(result.packet_id)
            if isinstance(meta, dict):
                meta["status"] = PACKET_STATUS_ABSTAINED
                meta["reject_reasons"] = [
                    failure.to_dict() for failure in result.failures
                ]
            continue
        for key in keys:
            seen[key] = result.packet_id


def _record_superseded_evidence_versions(
    conn: sqlite3.Connection, proposals: list[Proposal]
) -> None:
    if not _table_exists(conn, "proposals"):
        return
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(proposals)")
    }
    required = {
        "run_id",
        "prompt_hash",
        "evidence_pack_hash",
        "model",
        "provenance_json",
    }
    if not required <= columns:
        return
    for proposal in proposals:
        row = conn.execute(
            """
            SELECT run_id, prompt_hash, evidence_pack_hash, model, provenance_json
            FROM proposals WHERE id = ?
            """,
            (proposal.id,),
        ).fetchone()
        if row is None:
            continue
        previous = {
            "run_id": row["run_id"],
            "prompt_hash": row["prompt_hash"],
            "evidence_pack_hash": row["evidence_pack_hash"],
            "model": row["model"],
        }
        current = {
            "run_id": proposal.run_id,
            "prompt_hash": proposal.prompt_hash,
            "evidence_pack_hash": proposal.evidence_pack_hash,
            "model": proposal.model,
        }
        if previous == current:
            continue
        try:
            prior_provenance = json.loads(row["provenance_json"] or "{}")
        except json.JSONDecodeError:
            prior_provenance = {}
        history = prior_provenance.get("superseded_evidence_versions", [])
        if not isinstance(history, list):
            history = []
        if previous not in history:
            history.append(previous)
        proposal.provenance["superseded_evidence_versions"] = history
        for claim in proposal.claims:
            claim.confidence_basis["superseded_evidence_versions"] = history


def ingest_proposal_packet_results(
    conn: sqlite3.Connection,
    run_dir: Path,
    *,
    home: Path | None = None,
    dry_run: bool = False,
) -> list[PacketIngestResult]:
    """Validate and materialize packet results without publishing them."""
    paths = _run_paths(run_dir)
    manifest = load_manifest(run_dir)
    inventory = discover_config_inventory(home)
    now = _utc_now()
    results: list[PacketIngestResult] = []

    for packet_id, meta in (manifest.get("packets") or {}).items():
        packet_path = run_dir / str(meta["packet_path"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        result_path = paths["results"] / f"{packet_id}.json"
        integrity_failure = _packet_integrity_failure(packet, meta)
        binding_failure = (
            _hydrate_packet_target_paths(packet, meta)
            if integrity_failure is None
            else None
        )
        provenance_failure = (
            integrity_failure or binding_failure or _packet_provenance_failure(packet)
        )
        if provenance_failure:
            meta["status"] = PACKET_STATUS_INELIGIBLE
            meta["result_path"] = (
                str(result_path.relative_to(run_dir)) if result_path.is_file() else None
            )
            meta["ingested_at"] = now
            meta["reject_reasons"] = [
                ValidationFailure(reason=provenance_failure).to_dict()
            ]
            results.append(
                PacketIngestResult(
                    packet_id=packet_id,
                    status=PACKET_STATUS_INELIGIBLE,
                    failures=[ValidationFailure(reason=provenance_failure)],
                )
            )
            continue
        packet_theme = str(packet.get("theme") or "")
        if not result_path.is_file():
            results.append(
                PacketIngestResult(
                    packet_id=packet_id,
                    status=PACKET_STATUS_PENDING,
                    failures=[ValidationFailure(reason="result_missing")],
                    theme=packet_theme,
                )
            )
            continue
        try:
            raw = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            fail = ValidationFailure(reason=f"invalid_json:{exc}")
            _write_reject(paths["rejects"], packet_id, raw=None, failures=[fail])
            meta["status"] = PACKET_STATUS_REJECTED
            meta["reject_reasons"] = [fail.to_dict()]
            results.append(
                PacketIngestResult(
                    packet_id=packet_id,
                    status=PACKET_STATUS_REJECTED,
                    failures=[fail],
                    theme=packet_theme,
                )
            )
            continue

        validated, failures = validate_proposal_result(raw, packet=packet)
        if failures or validated is None:
            _write_reject(paths["rejects"], packet_id, raw=raw, failures=failures)
            meta["status"] = PACKET_STATUS_REJECTED
            meta["reject_reasons"] = [f.to_dict() for f in failures]
            meta["result_path"] = str(result_path.relative_to(run_dir))
            results.append(
                PacketIngestResult(
                    packet_id=packet_id,
                    status=PACKET_STATUS_REJECTED,
                    failures=failures,
                    theme=packet_theme,
                )
            )
            continue

        if validated.get("abstain"):
            meta["status"] = PACKET_STATUS_ABSTAINED
            meta["result_path"] = str(result_path.relative_to(run_dir))
            meta["ingested_at"] = now
            meta["reject_reasons"] = []
            results.append(
                PacketIngestResult(
                    packet_id=packet_id,
                    status=PACKET_STATUS_ABSTAINED,
                    abstain_reason=str(validated.get("abstain_reason")),
                    theme=packet_theme,
                )
            )
            continue

        props, claims = materialize_proposals(
            validated=validated,
            packet=packet,
            inventory=inventory,
            now=now,
        )
        if not props:
            meta["status"] = PACKET_STATUS_ABSTAINED
            meta["result_path"] = str(result_path.relative_to(run_dir))
            meta["ingested_at"] = now
            results.append(
                PacketIngestResult(
                    packet_id=packet_id,
                    status=PACKET_STATUS_ABSTAINED,
                    abstain_reason="materialize_empty_or_already_present",
                    theme=packet_theme,
                )
            )
            continue

        meta["status"] = PACKET_STATUS_COMPLETED
        meta["result_path"] = str(result_path.relative_to(run_dir))
        meta["ingested_at"] = now
        meta["reject_reasons"] = []
        results.append(
            PacketIngestResult(
                packet_id=packet_id,
                status=PACKET_STATUS_COMPLETED,
                proposals=props,
                claims=claims,
                theme=packet_theme,
            )
        )

    _abstain_duplicate_intents(results, manifest)
    save_manifest(run_dir, manifest)
    return results


def _write_reject(
    rejects_dir: Path,
    packet_id: str,
    *,
    raw: Any,
    failures: list[ValidationFailure],
) -> None:
    path = assert_writable(
        rejects_dir / f"{packet_id}.json", purpose="proposal reject"
    )
    path.write_text(
        json.dumps(
            {
                "packet_id": packet_id,
                "failures": [f.to_dict() for f in failures],
                "raw": raw,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _supersede_prior_pending_llm_proposals(
    conn: sqlite3.Connection, keep_ids: set[str]
) -> int:
    rows = conn.execute(
        """
        SELECT id FROM proposals
        WHERE status = 'pending'
          AND derivation_summary LIKE 'LLM packet proposal (%'
        """
    ).fetchall()
    superseded = 0
    for row in rows:
        proposal_id = str(row["id"])
        if proposal_id in keep_ids:
            continue
        set_proposal_status(
            conn,
            proposal_id,
            "superseded",
            note="system-superseded: replaced by latest LLM proposal run",
            allow_system=True,
        )
        superseded += 1
    return superseded


def _quarantine_prior_pending_llm_proposals(
    conn: sqlite3.Connection, themes: set[str]
) -> int:
    """Quarantine only pending LLM proposals whose packet theme is explicit."""
    if not themes:
        return 0
    clauses = " OR ".join("derivation_summary LIKE ?" for _ in themes)
    rows = conn.execute(
        f"""
        SELECT id FROM proposals
        WHERE status = 'pending'
          AND derivation_summary LIKE 'LLM packet proposal (%'
          AND ({clauses})
        """,
        [f"%theme={theme};%" for theme in sorted(themes)],
    ).fetchall()
    for row in rows:
        set_proposal_status(
            conn,
            str(row["id"]),
            "superseded",
            note="system-superseded: authorized complete all-abstain packet run",
            allow_system=True,
        )
    return len(rows)


def publish_llm_proposals_from_run(
    conn: sqlite3.Connection,
    run_dir: Path,
    *,
    home: Path | None = None,
    quarantine_on_all_abstain: bool = False,
) -> dict[str, Any]:
    """Publish a complete valid packet run as one board-visible operation."""
    results = ingest_proposal_packet_results(conn, run_dir, home=home)
    completed = sum(1 for r in results if r.status == PACKET_STATUS_COMPLETED)
    abstained = sum(1 for r in results if r.status == PACKET_STATUS_ABSTAINED)
    rejected = sum(1 for r in results if r.status == PACKET_STATUS_REJECTED)
    ineligible = sum(1 for r in results if r.status == PACKET_STATUS_INELIGIBLE)
    pending = sum(1 for r in results if r.status == PACKET_STATUS_PENDING)
    n_props = sum(len(r.proposals) for r in results)
    all_terminal_valid = bool(results) and not (pending or rejected or ineligible)
    complete_all_abstain = all_terminal_valid and abstained == len(results)
    has_publishable_proposals = bool(completed and n_props)
    publish_ready = all_terminal_valid and (
        has_publishable_proposals
        or (complete_all_abstain and quarantine_on_all_abstain)
    )
    keep = {
        p.id
        for r in results
        if r.status == PACKET_STATUS_COMPLETED
        for p in r.proposals
    }
    pruned = 0
    proposals_upserted = 0
    if publish_ready:
        if has_publishable_proposals:
            all_proposals = [
                proposal
                for result in results
                if result.status == PACKET_STATUS_COMPLETED
                for proposal in result.proposals
            ]
            all_claims = [
                claim
                for result in results
                if result.status == PACKET_STATUS_COMPLETED
                for claim in result.claims
            ]
            _record_superseded_evidence_versions(conn, all_proposals)
            upsert_claims(conn, all_claims)
            proposals_upserted = upsert_proposals(conn, all_proposals)
            pruned = _supersede_prior_pending_llm_proposals(conn, keep)
        else:
            themes = {
                str(result.theme)
                for result in results
                if result.status == PACKET_STATUS_ABSTAINED and result.theme
            }
            pruned = _quarantine_prior_pending_llm_proposals(conn, themes)
        conn.commit()
    if pending or rejected or ineligible:
        publication_block_reason = "run_incomplete_or_invalid"
    elif complete_all_abstain:
        publication_block_reason = (
            None if quarantine_on_all_abstain else "complete_all_abstain"
        )
    elif not has_publishable_proposals:
        publication_block_reason = "run_without_valid_proposals"
    else:
        publication_block_reason = None
    return {
        "run_dir": str(run_dir),
        "packets_completed": completed,
        "packets_abstained": abstained,
        "packets_rejected": rejected,
        "packets_ineligible": ineligible,
        "packets_pending": pending,
        "proposals_staged": n_props,
        "proposals_upserted": proposals_upserted,
        "proposals_pruned": pruned,
        "publish_ready": publish_ready,
        "publication_block_reason": publication_block_reason,
        "complete_all_abstain": complete_all_abstain,
        "all_abstain_quarantine_authorized": (
            complete_all_abstain and quarantine_on_all_abstain
        ),
        "results": [r.to_dict() for r in results],
        "empty_board_reason": (
            None
            if n_props
            else "no proposals met evidence gates"
        ),
    }
