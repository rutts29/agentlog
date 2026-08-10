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
    MIN_SESSIONS_FLOOR,
    MAX_QUOTE_CHARS,
    Claim,
    ClaimEvidence,
    Proposal,
    clip_quote,
)
from agentlog.analysis.claims.proposals import (
    PROPOSAL_EXTRACTOR_VERSION,
    _append_section,
    _proposal_id,
    _read_text,
    _sha1_text,
    unified_diff,
)
from agentlog.analysis.claims.scope import (
    ConfigInventory,
    discover_config_inventory,
)
from agentlog.analysis.claims.store import upsert_claims, upsert_proposals
from agentlog.safety.redaction import REDACTION_VERSION, RedactionReport, redact_text
from agentlog.safety.write_guard import assert_writable

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

MAX_WINDOWS_PER_THEME = 24
MAX_QUOTE_IN_PACKET = 400
MAX_CONFIG_CHARS = 2_500
MAX_PROPOSALS_PER_PACKET = 3

_PACKET_POSIX_PATH = re.compile(
    r"(?<![A-Za-z0-9_.:-])/(?!/)[^\s\"'`<>()\[\]{},;:]+"
)
_PACKET_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s\"'`<>()\[\]{},;:]+")

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


def _fetch_theme_windows(
    conn: sqlite3.Connection,
    *,
    where_sql: str,
    limit: int,
    report: RedactionReport,
) -> list[dict[str, Any]]:
    # One window per root session (newest first) so packets clear n≥10 gates.
    sql = f"""
        WITH matched AS (
            SELECT
                w.id AS window_id,
                w.session_id,
                d.request_kind,
                s.harness,
                s.repo,
                s.cwd,
                s.started_at,
                m_user.text AS user_text,
                m_asst.text AS assistant_text,
                m_user.timestamp AS user_ts,
                u.turn_kinds_json,
                u.user_stance,
                u.agent_stance,
                u.flags_json,
                u.spans_json,
                ROW_NUMBER() OVER (
                    PARTITION BY w.session_id
                    ORDER BY COALESCE(m_user.timestamp, s.started_at) DESC
                ) AS rn
            FROM window_det_classifications d
            JOIN exchange_windows w ON w.id = d.window_id
            JOIN sessions s ON s.id = w.session_id
            JOIN messages m_user ON m_user.id = w.request_message_id
            LEFT JOIN messages m_asst ON m_asst.id = w.response_message_id
            JOIN ux_observations u ON u.window_id = w.id
            WHERE d.request_kind = 'substantive'
              AND s.parent_session_id IS NULL
              AND COALESCE(s.external_id, '') NOT LIKE 'skills:%'
              AND COALESCE(u.link_status, 'linked') = 'linked'
              AND ({where_sql})
        )
        SELECT * FROM matched
        WHERE rn = 1
        ORDER BY COALESCE(user_ts, started_at) DESC
        LIMIT ?
    """
    rows = conn.execute(sql, (limit,)).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        sid = str(r["session_id"])
        user = _redact(str(r["user_text"] or ""), report)[:MAX_QUOTE_IN_PACKET]
        asst = _redact(str(r["assistant_text"] or ""), report)[:MAX_QUOTE_IN_PACKET]
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
    return out


def _packet_hash(body: dict[str, Any]) -> str:
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:24]


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
    for i, (theme, where_sql) in enumerate(THEME_SPECS, start=1):
        report = RedactionReport()
        snippets = _config_snippets(inventory, target_paths, report)
        signals = _signal_summaries(conn, report)
        windows = _fetch_theme_windows(
            conn, where_sql=where_sql, limit=windows_per_theme, report=report
        )
        window_count += len(windows)
        packet_id = f"ppkt_{i:04d}_{theme}"
        body = {
            "packet_id": packet_id,
            "run_id": run_id,
            "theme": theme,
            "prompt_hash": phash,
            "prompt_file": "proposal_subagent.md",
            "model_hint": model,
            "provider": PROVIDER_PACKET,
            "denominator_note": (
                "Only substantive root-session windows are included. "
                "auto_review/worker_brief are excluded as habit evidence."
            ),
            "gates": {
                "min_sessions_ok": MIN_SESSIONS_FINDING,
                "min_sessions_floor": MIN_SESSIONS_FLOOR,
                "max_proposals": MAX_PROPOSALS_PER_PACKET,
            },
            "allowed_targets": allowed,
            "config_snippets": snippets,
            "signals": [
                s for s in signals if s.get("theme") == theme or s["kind"] != "recurring_instruction"
            ][:12],
            "windows": windows,
            "redaction": report.to_dict(),
        }
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
            "evidence_pack_hash": body["evidence_pack_hash"],
            "packet_path": str(packet_path.relative_to(run_dir)),
            "target_paths": target_paths,
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


def _quote_in_window(quote: str, window: dict[str, Any]) -> bool:
    q = quote.strip()
    if not q:
        return False
    blob = "\n".join(
        [
            str(window.get("user") or ""),
            str(window.get("assistant") or ""),
            *(
                str(sp.get("quote") or "")
                for sp in (window.get("spans") or [])
                if isinstance(sp, dict)
            ),
        ]
    )
    return q in blob


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

    allowed_paths = {
        str(t.get("target_path") or t.get("path") or "")
        for t in (packet.get("allowed_targets") or [])
        if isinstance(t, dict)
    }
    windows = _window_index(packet)
    packet_population = len(
        {
            str(window.get("session_id") or "")
            for window in windows.values()
            if window.get("session_id")
        }
    )
    cleaned: list[dict[str, Any]] = []

    for idx, prop in enumerate(proposals):
        if not isinstance(prop, dict):
            failures.append(
                ValidationFailure(reason="proposal_not_object", proposal_index=idx)
            )
            continue
        title = str(prop.get("title") or "").strip()
        target = str(prop.get("target_path") or "").strip()
        rewrite = str(prop.get("instruction_rewrite") or "").strip()
        rationale = str(prop.get("rationale") or "").strip()
        does_not_prove = str(prop.get("does_not_prove") or "").strip()
        action = str(prop.get("action") or "add").strip()
        heading = str(prop.get("heading") or title).strip()
        evidence = prop.get("evidence")

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

        session_ids: set[str] = set()
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
            if not _quote_in_window(quote, window):
                failures.append(
                    ValidationFailure(reason="quote_not_in_window", proposal_index=idx)
                )
                continue
            session_ids.add(sid)
            clean_ev.append(
                {
                    "session_id": sid,
                    "window_id": wid,
                    "quote": clip_quote(quote) or quote,
                    "timestamp": ev.get("timestamp") or window.get("timestamp"),
                }
            )

        n_sessions = len(session_ids)
        if n_sessions < MIN_SESSIONS_FLOOR:
            # Thin evidence: drop this proposal rather than publish spam.
            continue
        support = (
            "ok" if n_sessions >= MIN_SESSIONS_FINDING else "insufficient"
        )

        # Publish ok and insufficient (5–9 sessions) — board shows the tier.
        # Abstain / below floor never reach pending.
        if support not in {"ok", "insufficient"}:
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
                "support_tier": support,
                "sample_size": n_sessions,
                "packet_population": packet_population,
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
    packet: dict[str, Any], target_paths: Any
) -> None:
    """Attach local target paths after reading the model-facing packet."""
    if not isinstance(target_paths, dict):
        return
    packet["_target_paths"] = {
        str(target_id): str(path)
        for target_id, path in target_paths.items()
        if isinstance(target_id, str) and isinstance(path, str)
    }


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
    model = str(validated.get("model") or packet.get("model_hint") or DEFAULT_MODEL)
    theme = str(packet.get("theme") or "llm_proposal")
    local_target_paths = packet.get("_target_paths") or {}

    for prop in validated.get("proposals") or []:
        target_ref = str(prop["target_path"])
        path = str(local_target_paths.get(target_ref) or target_ref)
        old = _read_text(Path(path))
        heading = str(prop["heading"])
        body = str(prop["instruction_rewrite"])
        new = _append_section(old, heading, f"- {body}")
        if new == old:
            # Already present — skip publish.
            continue
        diff = unified_diff(path=path, old=old, new=new)
        kind, scope_type, scope_id = _target_meta(inventory, path)
        evidence = [
            ClaimEvidence(
                session_id=e.get("session_id"),
                window_id=e.get("window_id"),
                quote=e.get("quote"),
                meta={"timestamp": e.get("timestamp")},
            )
            for e in prop.get("evidence") or []
        ]
        claim_id = hashlib.sha1(
            f"llm_proposal|{theme}|{path}|{heading}|{pack_hash}".encode()
        ).hexdigest()[:24]
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
            },
            scope_type=scope_type,  # type: ignore[arg-type]
            scope_id=scope_id,
            derivation="llm_derived",
            support_status=prop["support_tier"],
            sample_size=int(prop["sample_size"]),
            denominator=int(prop["packet_population"]),
            observed_at=now,
            extractor_name="proposal_packets",
            extractor_version=PROPOSAL_EXTRACTOR_VERSION,
            confidence_basis={
                "provider": PROVIDER_PACKET,
                "model": model,
                "run_id": run_id,
                "prompt_hash": prompt_hash,
                "evidence_pack_hash": pack_hash,
                "packet_id": packet.get("packet_id"),
                "evidence_sessions": int(prop["sample_size"]),
                "packet_session_count": int(prop["packet_population"]),
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
            "run_id": run_id,
            "prompt_hash": prompt_hash,
            "evidence_pack_hash": pack_hash,
            "packet_id": packet.get("packet_id"),
            "theme": theme,
            "redaction": packet.get("redaction") or {},
            "evidence_sessions": int(prop["sample_size"]),
            "packet_session_count": int(prop["packet_population"]),
        }
        proposals.append(
            Proposal(
                id=_proposal_id(
                    "llm_proposal",
                    theme,
                    path,
                    pack_hash,
                    PROPOSAL_EXTRACTOR_VERSION,
                ),
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
                    f"{prop['packet_population']} packet sessions"
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


def ingest_proposal_packet_results(
    conn: sqlite3.Connection,
    run_dir: Path,
    *,
    home: Path | None = None,
    dry_run: bool = False,
) -> list[PacketIngestResult]:
    """Validate result JSON files and persist proposals + claims."""
    paths = _run_paths(run_dir)
    manifest = load_manifest(run_dir)
    inventory = discover_config_inventory(home)
    now = _utc_now()
    results: list[PacketIngestResult] = []
    all_proposals: list[Proposal] = []
    all_claims: list[Claim] = []

    for packet_id, meta in (manifest.get("packets") or {}).items():
        packet_path = run_dir / str(meta["packet_path"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        _hydrate_packet_target_paths(packet, meta.get("target_paths"))
        result_path = paths["results"] / f"{packet_id}.json"
        if not result_path.is_file():
            results.append(
                PacketIngestResult(
                    packet_id=packet_id,
                    status=PACKET_STATUS_PENDING,
                    failures=[ValidationFailure(reason="result_missing")],
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
                )
            )
            continue

        all_proposals.extend(props)
        all_claims.extend(claims)
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
            )
        )

    if not dry_run and (all_proposals or all_claims):
        if all_claims:
            upsert_claims(conn, all_claims)
        if all_proposals:
            upsert_proposals(conn, all_proposals)
        conn.commit()

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


def publish_llm_proposals_from_run(
    conn: sqlite3.Connection,
    run_dir: Path,
    *,
    home: Path | None = None,
) -> dict[str, Any]:
    """Ingest packet results and supersede non-LLM pending board items."""
    from agentlog.analysis.claims.proposals import _prune_stale_pending

    results = ingest_proposal_packet_results(conn, run_dir, home=home)
    keep = {
        p.id
        for r in results
        for p in r.proposals
    }
    # Also keep any already-pending LLM proposals from this or prior runs.
    if _table_exists(conn, "proposals"):
        rows = conn.execute(
            """
            SELECT id FROM proposals
            WHERE status = 'pending'
              AND (
                derivation_summary LIKE 'LLM packet proposal%'
                OR COALESCE(run_id, '') != ''
              )
            """
        ).fetchall()
        keep.update(str(r["id"]) for r in rows)
    pruned = _prune_stale_pending(conn, keep, now=_utc_now())
    conn.commit()
    completed = sum(1 for r in results if r.status == PACKET_STATUS_COMPLETED)
    abstained = sum(1 for r in results if r.status == PACKET_STATUS_ABSTAINED)
    rejected = sum(1 for r in results if r.status == PACKET_STATUS_REJECTED)
    pending = sum(1 for r in results if r.status == PACKET_STATUS_PENDING)
    n_props = sum(len(r.proposals) for r in results)
    return {
        "run_dir": str(run_dir),
        "packets_completed": completed,
        "packets_abstained": abstained,
        "packets_rejected": rejected,
        "packets_pending": pending,
        "proposals_upserted": n_props,
        "proposals_pruned": pruned,
        "results": [r.to_dict() for r in results],
        "empty_board_reason": (
            None
            if n_props
            else "no proposals met evidence gates"
        ),
    }
