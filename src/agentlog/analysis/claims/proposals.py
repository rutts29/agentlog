"""Turn claims into reviewable unified-diff proposals (never auto-applied)."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentlog.analysis.claims.extract import derive_claims
from agentlog.analysis.claims.models import (
    MIN_SESSIONS_FINDING,
    Claim,
    Proposal,
)
from agentlog.analysis.claims.scope import (
    ConfigInventory,
    discover_config_inventory,
    instruction_already_present,
    preferred_target_for_theme,
)
from agentlog.analysis.claims.store import (
    list_claims,
    upsert_claims,
    upsert_proposals,
)

PROPOSAL_EXTRACTOR_VERSION = "proposals_v2"
MAX_PROPOSALS = 40
MAX_UNUSED_SKILL_PROPOSALS = 8
MAX_INSTRUCTION_PROPOSALS = 6
# Zero skill_exposures joins miss Cursor/Codex invocations that never write
# the same exposure events. Do not emit archive_skill / DEPRECATED banners
# until invocation telemetry covers those harnesses.
EMIT_UNUSED_SKILL_ARCHIVE_PROPOSALS = False
UNUSED_SKILL_ARCHIVE_GATE_REASON = "exposure coverage insufficient"
# Static AGENTS.md instruction templates are not the board source; those
# cards come from LLM packets. Usage-profile notes stay on (agentlog-owned
# context files only — consult: cut unused-skill spam, keep usage notes).
EMIT_STATIC_INSTRUCTION_PROPOSALS = False
EMIT_USAGE_PROFILE_PROPOSALS = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _proposal_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:24]


def _diff_path_label(side: str, path: str) -> str:
    """Build ``a/...`` / ``b/...`` labels without ``a//Users`` for abs paths."""
    cleaned = path.lstrip("/") if path.startswith("/") else path
    return f"{side}/{cleaned}"


def unified_diff(
    *,
    path: str,
    old: str,
    new: str,
    old_label: str | None = None,
    new_label: str | None = None,
) -> str:
    """Return a unified diff for proposal review."""
    import difflib

    lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=old_label or _diff_path_label("a", path),
            tofile=new_label or _diff_path_label("b", path),
            lineterm="",
        )
    )
    return "\n".join(lines) + ("\n" if lines else "")


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _append_section(existing: str, heading: str, body: str) -> str:
    section = f"\n## {heading}\n\n{body.rstrip()}\n"
    if existing.strip() == "":
        return f"# Agent instructions\n{section}"
    if heading.lower() in existing.lower():
        return existing
    text = existing
    if not text.endswith("\n"):
        text += "\n"
    return text + section


def _evidence_block(claim: Claim, limit: int = 5) -> str:
    lines: list[str] = []
    for ev in claim.evidence[:limit]:
        bits = []
        if ev.session_id:
            bits.append(f"session `{ev.session_id}`")
        if ev.window_id:
            bits.append(f"window `{ev.window_id}`")
        head = ", ".join(bits) if bits else "evidence"
        if ev.quote:
            lines.append(f"- {head}: \"{ev.quote}\"")
        else:
            lines.append(f"- {head}")
    return "\n".join(lines) if lines else "- (no verbatim spans stored)"


def _rationale_for_instruction(claim: Claim, *, already: list[str]) -> str:
    value = claim.value
    phrasing = value.get("phrasing") or ""
    suggested = value.get("suggested_instruction") or ""
    parts = [
        phrasing,
        "",
        f"Derivation: {claim.derivation} ({claim.extractor_name}).",
        f"Support: {claim.support_status}; sample_size={claim.sample_size}; "
        f"denominator={claim.denominator}.",
        "",
        "Suggested instruction text:",
        suggested,
        "",
        "Citations:",
        _evidence_block(claim),
        "",
        f"What this does not prove: {claim.does_not_prove}",
    ]
    if already:
        parts.extend(
            [
                "",
                "Overlap check: related wording already appears in config inventory "
                f"({', '.join(already)}). This proposal was still emitted because "
                "the recurring theme remains frequent; review before adding.",
            ]
        )
    label = claim.confidence_basis.get("label_basis") or {}
    if label:
        parts.extend(
            [
                "",
                "Label basis: "
                f"ux_observations={label.get('ux_observations')}, "
                f"adjudications={label.get('adjudications')}. "
                f"{label.get('note', '')}",
            ]
        )
    return "\n".join(parts)


def _instruction_proposals(
    claims: list[Claim],
    inventory: ConfigInventory,
    now: str,
) -> list[Proposal]:
    if not EMIT_STATIC_INSTRUCTION_PROPOSALS:
        return []
    out: list[Proposal] = []
    for claim in claims:
        if claim.kind != "recurring_instruction":
            continue
        if claim.support_status != "ok":
            continue
        if claim.sample_size < MIN_SESSIONS_FINDING:
            continue
        theme = str(claim.value.get("theme") or claim.subject)
        top_projects = claim.value.get("top_projects") or []
        repo_key = None
        if top_projects and isinstance(top_projects[0], dict):
            repo_key = str(top_projects[0].get("project") or "") or None
        # Global themes go to global AGENTS unless strongly project-local.
        if theme in {"dont_act_yet_brake", "scope_narrow", "spawn_workers"}:
            windows = int(claim.value.get("windows") or 0)
            top_n = int(top_projects[0]["windows"]) if top_projects else 0
            if windows and top_n / windows >= 0.6 and repo_key:
                target = preferred_target_for_theme(
                    inventory, theme=theme, repo_key=repo_key
                )
            else:
                target = preferred_target_for_theme(
                    inventory, theme=theme, repo_key=None
                )
        else:
            target = preferred_target_for_theme(
                inventory, theme=theme, repo_key=repo_key
            )

        # Dedupe only against the chosen target (plus global AGENTS), not every
        # project file — otherwise ai_sec wording suppresses global proposals.
        from agentlog.analysis.claims.scope import ConfigInventory

        scoped_files = [
            f
            for f in inventory.files
            if f.path == target.path
            or (f.scope_type == "global" and f.kind in {"agents_md", "claude_md"})
        ]
        scoped = ConfigInventory(home=inventory.home, files=scoped_files)
        already = instruction_already_present(scoped, theme)
        # Skip verify_before_done when global AGENTS already covers it tightly.
        if theme == "verify_before_done" and already:
            continue
        if already and theme != "dont_act_yet_brake":
            continue

        old = _read_text(target.path) if target.exists else ""
        heading = {
            "dont_act_yet_brake": "Wait for explicit go-ahead",
            "verify_before_done": "Verify before claiming done",
            "scope_narrow": "Stay in named scope",
            "spawn_workers": "Prefer workers when asked",
        }.get(theme, theme.replace("_", " ").title())
        body = str(claim.value.get("suggested_instruction") or "")
        new = _append_section(old, heading, f"- {body}")
        if new == old:
            continue
        path = str(target.path)
        diff = unified_diff(path=path, old=old, new=new)
        out.append(
            Proposal(
                id=_proposal_id(
                    "add_instruction", theme, path, PROPOSAL_EXTRACTOR_VERSION
                ),
                title=f"Add instruction: {heading}",
                action="add",
                status="pending",
                target_path=path,
                target_kind=target.kind,
                scope_type=target.scope_type,  # type: ignore[arg-type]
                scope_id=target.scope_id,
                base_content_hash=_sha1_text(old) if old else None,
                unified_diff=diff,
                proposed_content=new,
                rationale=_rationale_for_instruction(claim, already=already),
                derivation_summary=(
                    f"LLM-derived theme '{theme}' from labeled windows; "
                    f"n_sessions={claim.sample_size}"
                ),
                does_not_prove=claim.does_not_prove,
                sample_size=claim.sample_size,
                claim_ids=[claim.id],
                claims=[claim],
                created_at=now,
                updated_at=now,
            )
        )
        if len(out) >= MAX_INSTRUCTION_PROPOSALS:
            break
    return out


def _skill_removal_proposals(
    claims: list[Claim],
    now: str,
) -> list[Proposal]:
    """Archive/DEPRECATED proposals for zero-exposure skills.

    Hard-gated: ``skill_exposures`` under-counts Cursor/Codex skill use, so
    "0 exposures ⇒ prepend DEPRECATED" is misleading. Claims may still exist
    as inventory; proposals stay off until coverage is trustworthy.
    """
    if not EMIT_UNUSED_SKILL_ARCHIVE_PROPOSALS:
        return []
    out: list[Proposal] = []
    unused = [
        c
        for c in claims
        if c.kind == "skill_unused" and c.support_status in {"ok", "insufficient"}
    ]
    # Prefer agents/codex user skills; skip if sample abstained.
    unused.sort(key=lambda c: (c.support_status != "ok", c.subject))
    for claim in unused:
        if claim.support_status == "abstain":
            continue
        source_path = str(claim.value.get("source_path") or "")
        if not source_path:
            continue
        path = Path(source_path)
        old = _read_text(path)
        if not old:
            continue
        # Soft archive: prepend deprecation banner rather than deleting bytes
        # out from under the user without review. Apply still requires approval.
        banner = (
            "<!-- agentlog proposal: unused in observed sessions; "
            "review before deleting -->\n"
            "# DEPRECATED / CANDIDATE FOR REMOVAL\n\n"
            "agentlog observed 0 skill exposures for this definition across the "
            "current evidence ledger. This banner is a reviewable proposal, not "
            "an automatic deletion.\n\n"
        )
        if old.lstrip().startswith("# DEPRECATED"):
            continue
        new = banner + old
        diff = unified_diff(path=str(path), old=old, new=new)
        out.append(
            Proposal(
                id=_proposal_id(
                    "archive_skill",
                    claim.scope_id or claim.subject,
                    PROPOSAL_EXTRACTOR_VERSION,
                ),
                title=f"Mark unused skill for removal: {claim.subject}",
                action="archive_skill",
                status="pending",
                target_path=str(path),
                target_kind="skill",
                scope_type="skill",
                scope_id=claim.scope_id,
                base_content_hash=_sha1_text(old),
                unified_diff=diff,
                proposed_content=new,
                rationale="\n".join(
                    [
                        str(claim.value.get("phrasing") or ""),
                        "",
                        f"Derivation: deterministic skill exposure join.",
                        f"Support: {claim.support_status}; "
                        f"sessions_total={claim.denominator}.",
                        "",
                        f"What this does not prove: {claim.does_not_prove}",
                        "",
                        "If approved and applied, this only writes the deprecation "
                        "banner. Deleting the skill file remains a separate manual "
                        "step.",
                    ]
                ),
                derivation_summary="deterministic zero-exposure inventory join",
                does_not_prove=claim.does_not_prove,
                sample_size=claim.sample_size,
                claim_ids=[claim.id],
                claims=[claim],
                created_at=now,
                updated_at=now,
            )
        )
        if len(out) >= MAX_UNUSED_SKILL_PROPOSALS:
            break
    return out


def _usage_note_proposals(
    claims: list[Claim],
    inventory: ConfigInventory,
    now: str,
) -> list[Proposal]:
    """Project usage mix notes go to agentlog-managed context, not AGENTS.md."""
    if not EMIT_USAGE_PROFILE_PROPOSALS:
        return []
    out: list[Proposal] = []
    context_root = inventory.home / ".agentlog" / "context"
    for claim in claims:
        if claim.kind != "harness_model_usage":
            continue
        if claim.support_status != "ok":
            continue
        label = claim.subject
        path = context_root / f"{label}.usage.md"
        old = _read_text(path) if path.is_file() else ""
        models = claim.value.get("models") or []
        harnesses = claim.value.get("harnesses") or []
        lines = [
            f"# Usage profile: {label}",
            "",
            "Descriptive only. Generated by agentlog from session history.",
            "This is not a ranking and does not prescribe a model.",
            "",
            f"Root sessions observed: {claim.sample_size}",
            "",
            "## Models",
        ]
        for m in models:
            lines.append(
                f"- {m['model']}: {m['sessions']} sessions "
                f"({m['share']:.4f})"
            )
        lines.extend(["", "## Harnesses"])
        for h in harnesses:
            lines.append(
                f"- {h['harness']}: {h['sessions']} sessions "
                f"({h['share']:.4f})"
            )
        lines.extend(
            [
                "",
                "## What this does not prove",
                claim.does_not_prove,
            ]
        )
        new = "\n".join(lines) + "\n"
        if new == old:
            continue
        diff = unified_diff(path=str(path), old=old, new=new)
        out.append(
            Proposal(
                id=_proposal_id(
                    "usage_profile", label, PROPOSAL_EXTRACTOR_VERSION
                ),
                title=f"Refresh usage profile note: {label}",
                action="update" if old else "add",
                status="pending",
                target_path=str(path),
                target_kind="agentlog_context",
                scope_type="repo",
                scope_id=label,
                base_content_hash=_sha1_text(old) if old else None,
                unified_diff=diff,
                proposed_content=new,
                rationale="\n".join(
                    [
                        str(claim.value.get("phrasing") or ""),
                        "",
                        "Derivation: deterministic session counts by "
                        "harness/model_canonical.",
                        f"Support: {claim.support_status}; n={claim.sample_size}.",
                        "",
                        "Target is an agentlog-managed context file (not AGENTS.md) "
                        "to avoid prompt bloat.",
                        "",
                        f"What this does not prove: {claim.does_not_prove}",
                    ]
                ),
                derivation_summary="deterministic harness/model usage mix",
                does_not_prove=claim.does_not_prove,
                sample_size=claim.sample_size,
                claim_ids=[claim.id],
                claims=[claim],
                created_at=now,
                updated_at=now,
            )
        )
        if len(out) >= 5:
            break
    return out


def generate_proposals(
    conn: sqlite3.Connection,
    *,
    claims: list[Claim] | None = None,
    inventory: ConfigInventory | None = None,
    home: Path | None = None,
    now: str | None = None,
) -> list[Proposal]:
    """Deterministic proposal templates (mostly gated off).

    The live board is filled by ``claims.packets`` ingest (Cursor subagent
    LLM results). This function may still emit nothing while claims refresh.
    """
    ts = now or _utc_now()
    claim_list = claims if claims is not None else list_claims(conn, status=None)
    inv = inventory or discover_config_inventory(home)
    proposals: list[Proposal] = []
    proposals.extend(_instruction_proposals(claim_list, inv, ts))
    proposals.extend(_skill_removal_proposals(claim_list, ts))
    proposals.extend(_usage_note_proposals(claim_list, inv, ts))
    return proposals[:MAX_PROPOSALS]


def _prune_stale_pending(
    conn: sqlite3.Connection, keep_ids: set[str], *, now: str
) -> int:
    """Retire stale pending proposals as ``superseded`` (not owner-rejected)."""
    from agentlog.analysis.claims.store import set_proposal_status

    rows = conn.execute(
        "SELECT id FROM proposals WHERE status = 'pending'"
    ).fetchall()
    pruned = 0
    for r in rows:
        pid = str(r["id"])
        if pid in keep_ids:
            continue
        set_proposal_status(
            conn,
            pid,
            "superseded",
            note="system-superseded: claim no longer meets proposal gates",
            allow_system=True,
        )
        pruned += 1
    return pruned


def _prune_stale_skill_unused_claims(
    conn: sqlite3.Connection, keep_ids: set[str], *, now: str
) -> int:
    """Supersede skill_unused inventory rows no longer emitted by derive."""
    rows = conn.execute(
        """
        SELECT id FROM claims
        WHERE kind = 'skill_unused' AND status = 'candidate'
        """
    ).fetchall()
    pruned = 0
    for r in rows:
        cid = str(r["id"])
        if cid in keep_ids:
            continue
        conn.execute(
            """
            UPDATE claims
            SET status = 'superseded', updated_at = ?
            WHERE id = ? AND status = 'candidate'
            """,
            (now, cid),
        )
        pruned += 1
    return pruned


def refresh_learnings(
    conn: sqlite3.Connection,
    *,
    home: Path | None = None,
    include_llm_derived: bool = True,
    prune_non_llm_pending: bool = True,
) -> dict[str, Any]:
    """Derive claims and refresh gated deterministic proposals.

    Unused-skill archive cards stay off. Usage-profile notes may still emit.
    Instruction AGENTS.md cards come from LLM packet ingest, not templates.
    """
    now = _utc_now()
    claims = derive_claims(
        conn, now=now, include_llm_derived=include_llm_derived
    )
    n_claims = upsert_claims(conn, claims)
    unused_kept = {c.id for c in claims if c.kind == "skill_unused"}
    unused_pruned = _prune_stale_skill_unused_claims(conn, unused_kept, now=now)
    inventory = discover_config_inventory(home)
    proposals = generate_proposals(
        conn, claims=claims, inventory=inventory, home=home, now=now
    )
    n_props = upsert_proposals(conn, proposals) if proposals else 0
    keep_ids = {p.id for p in proposals}
    if prune_non_llm_pending and _proposals_table_ready(conn):
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(proposals)")}
        run_expr = "COALESCE(run_id, '')" if "run_id" in cols else "''"
        rows = conn.execute(
            f"""
            SELECT id, derivation_summary, {run_expr} AS run_id
            FROM proposals WHERE status = 'pending'
            """
        ).fetchall()
        for r in rows:
            summary = str(r["derivation_summary"] or "")
            if summary.startswith("LLM packet proposal") or str(r["run_id"]):
                keep_ids.add(str(r["id"]))
    pruned = _prune_stale_pending(conn, keep_ids, now=now)
    by_kind: dict[str, int] = {}
    by_derivation: dict[str, int] = {}
    support: dict[str, int] = {}
    for c in claims:
        by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
        by_derivation[c.derivation] = by_derivation.get(c.derivation, 0) + 1
        support[c.support_status] = support.get(c.support_status, 0) + 1
    return {
        "claims_upserted": n_claims,
        "claims_total": len(claims),
        "proposals_upserted": n_props,
        "proposals_total": len(proposals),
        "proposals_pruned": pruned,
        "skill_unused_claims_pruned": unused_pruned,
        "by_kind": by_kind,
        "by_derivation": by_derivation,
        "by_support": support,
        "inventory_files": len(inventory.files),
        "extractor_version": PROPOSAL_EXTRACTOR_VERSION,
        "board_source": "llm_packets",
        "empty_board_hint": (
            "no proposals met evidence gates — run "
            "agentlog propose packets-emit then ingest subagent results"
            if not keep_ids
            else None
        ),
    }


def _proposals_table_ready(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proposals'"
        ).fetchone()
        is not None
    )
