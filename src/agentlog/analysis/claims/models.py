from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Derivation = Literal["deterministic", "llm_derived"]
SupportStatus = Literal["ok", "insufficient", "abstain"]
ClaimStatus = Literal[
    "candidate", "approved", "rejected", "published", "superseded"
]
ProposalStatus = Literal[
    "pending", "accepted", "rejected", "deferred", "superseded"
]
# Owner decisions only. System pruning uses ``superseded`` and must not appear here.
DECIDED_STATUSES: frozenset[str] = frozenset({"accepted", "rejected", "deferred"})
SYSTEM_STATUSES: frozenset[str] = frozenset({"superseded"})
ProposalAction = Literal["add", "update", "remove", "archive_skill"]
ScopeType = Literal[
    "global",
    "repo",
    "skill",
    "harness",
    "model",
    "user_rules",
]

EXTRACTOR_VERSION = "claims_v1"
MIN_SESSIONS_FINDING = 10
MIN_SESSIONS_FLOOR = 5
MAX_EVIDENCE_PER_CLAIM = 8
MAX_QUOTE_CHARS = 280


@dataclass
class ClaimEvidence:
    session_id: str | None = None
    window_id: str | None = None
    message_id: str | None = None
    quote: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "window_id": self.window_id,
            "message_id": self.message_id,
            "quote": self.quote,
            "meta": dict(self.meta),
        }


@dataclass
class Claim:
    id: str
    kind: str
    subject: str
    predicate: str
    value: dict[str, Any]
    scope_type: ScopeType
    scope_id: str | None
    derivation: Derivation
    status: ClaimStatus = "candidate"
    support_status: SupportStatus = "ok"
    sample_size: int = 0
    denominator: int | None = None
    rate: float | None = None
    observed_at: str = ""
    extractor_name: str = "claims"
    extractor_version: str = EXTRACTOR_VERSION
    confidence_basis: dict[str, Any] = field(default_factory=dict)
    does_not_prove: str = ""
    supersedes_id: str | None = None
    evidence: list[ClaimEvidence] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": dict(self.value),
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "derivation": self.derivation,
            "status": self.status,
            "support_status": self.support_status,
            "sample_size": self.sample_size,
            "denominator": self.denominator,
            "rate": self.rate,
            "observed_at": self.observed_at,
            "extractor_name": self.extractor_name,
            "extractor_version": self.extractor_version,
            "confidence_basis": dict(self.confidence_basis),
            "does_not_prove": self.does_not_prove,
            "supersedes_id": self.supersedes_id,
            "evidence": [e.to_dict() for e in self.evidence],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Proposal:
    id: str
    title: str
    action: ProposalAction
    status: ProposalStatus
    target_path: str
    target_kind: str
    scope_type: ScopeType
    scope_id: str | None
    base_content_hash: str | None
    unified_diff: str
    proposed_content: str | None
    rationale: str
    derivation_summary: str = ""
    does_not_prove: str = ""
    sample_size: int = 0
    claim_ids: list[str] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    decided_at: str | None = None
    decision_note: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    model: str | None = None
    prompt_hash: str | None = None
    evidence_pack_hash: str | None = None

    def to_dict(self, *, include_claims: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id,
            "title": self.title,
            "action": self.action,
            "status": self.status,
            "target_path": self.target_path,
            "target_kind": self.target_kind,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "base_content_hash": self.base_content_hash,
            "unified_diff": self.unified_diff,
            "proposed_content": self.proposed_content,
            "rationale": self.rationale,
            "derivation_summary": self.derivation_summary,
            "does_not_prove": self.does_not_prove,
            "sample_size": self.sample_size,
            "claim_ids": list(self.claim_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "decided_at": self.decided_at,
            "decision_note": self.decision_note,
            "provenance": dict(self.provenance),
            "run_id": self.run_id,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "evidence_pack_hash": self.evidence_pack_hash,
        }
        if include_claims:
            data["claims"] = [c.to_dict() for c in self.claims]
        return data


def value_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def clip_quote(text: str | None, limit: int = MAX_QUOTE_CHARS) -> str | None:
    if text is None:
        return None
    cleaned = " ".join(text.split())
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "…"


def observational_rate_phrase(
    subject: str, numerator: int, denominator: int
) -> str:
    if denominator <= 0:
        return f"{subject}: no denominator available"
    rate = numerator / denominator
    return (
        f"sessions where {subject} was observed showed rate "
        f"{rate:.4f} (n={denominator}; events={numerator})"
    )
