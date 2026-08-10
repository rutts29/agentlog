"""Evidence-backed claims and reviewable config proposals.

Nothing in this package writes agent configuration files. Proposals carry a
unified diff and the full proposed content for the owner to apply by hand;
agentlog only records the owner's decision.
"""

from __future__ import annotations

from agentlog.analysis.claims.extract import derive_claims
from agentlog.analysis.claims.models import Claim, ClaimEvidence, Proposal
from agentlog.analysis.claims.packets import (
    emit_proposal_packet_run,
    ingest_proposal_packet_results,
    publish_llm_proposals_from_run,
)
from agentlog.analysis.claims.proposals import generate_proposals, refresh_learnings
from agentlog.analysis.claims.store import (
    get_claim,
    get_proposal,
    list_claims,
    list_proposals,
    set_proposal_status,
    target_state,
)

__all__ = [
    "Claim",
    "ClaimEvidence",
    "Proposal",
    "derive_claims",
    "emit_proposal_packet_run",
    "generate_proposals",
    "get_claim",
    "get_proposal",
    "ingest_proposal_packet_results",
    "list_claims",
    "list_proposals",
    "publish_llm_proposals_from_run",
    "refresh_learnings",
    "set_proposal_status",
    "target_state",
]
