from __future__ import annotations

import json
import re
from typing import Any

from agentlog.analysis.extractors.llm_client import ChatClient
from agentlog.analysis.extractors.models import (
    EvidenceSpan,
    ExtractorMeta,
    ProcessFlags,
    UxObservation,
    WindowContext,
)
from agentlog.analysis.extractors.prompt import load_ux_prompt, ux_prompt_hash
from agentlog.analysis.extractors.provider import ApiExtractionProvider, ExtractionProvider
from agentlog.analysis.extractors.taxonomy import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_UX_MODEL,
    EXTRACTOR_NAME_UX,
    EXTRACTOR_VERSION,
    AgentStance,
    PriorOutcome,
    UserStance,
)
from agentlog.analysis.extractors.window_context import truncate_for_ux
from agentlog.safety.redaction import REDACTION_VERSION

SYSTEM_PROMPT = load_ux_prompt()
PROMPT_HASH = ux_prompt_hash(SYSTEM_PROMPT)

_CORRECTION_CUES = re.compile(
    r"\bi said\b|\byou missed\b|\binstead of\b|\bacross everything\b|"
    r"\bnot only\b|\bi told you\b|\bthat'?s not what\b|\brevert\b",
    re.IGNORECASE,
)
_FRUSTRATION_CUES = re.compile(
    r"\bfrustrated\b|\bannoying\b|\bwaste of\b|\bsick of\b|\brage\b|"
    r"\bunacceptable\b|\bthis is ridiculous\b",
    re.IGNORECASE,
)


def prompt_hash() -> str:
    return PROMPT_HASH


def build_user_message(payloads: list[dict[str, Any]]) -> str:
    """The single place the labeler's user message is assembled."""
    return "Label each window independently. Data follows.\n" + "\n".join(
        f"<window id=\"{p['window_id']}\">\n{json.dumps(p, ensure_ascii=False)}\n</window>"
        for p in payloads
    )


def _parse_batch_response(data: dict[str, Any], expected_ids: list[str]) -> list[dict]:
    if "windows" in data and isinstance(data["windows"], list):
        rows = data["windows"]
    elif "window_id" in data:
        rows = [data]
    else:
        raise ValueError("LLM JSON missing windows/window_id")
    by_id = {str(r.get("window_id")): r for r in rows if isinstance(r, dict)}
    ordered: list[dict] = []
    for wid in expected_ids:
        if wid not in by_id:
            ordered.append(
                {
                    "window_id": wid,
                    "turn_kind": [],
                    "user_stance": UserStance.ABSTAIN.value,
                    "agent_stance": AgentStance.ABSTAIN.value,
                    "prior_outcome": PriorOutcome.ABSTAIN.value,
                    "flags": {},
                    "spans": [],
                    "confidence": {},
                    "abstain_reasons": ["missing_from_model_response"],
                    "novel_observations": [],
                }
            )
        else:
            ordered.append(by_id[wid])
    return ordered


def enforce_reliability_tiers(
    raw: dict[str, Any],
    *,
    payload: dict[str, Any],
) -> UxObservation:
    """Post-validate model output against reliability tiers (API path; soft coerce)."""
    window_id = str(raw.get("window_id") or payload["window_id"])
    turn_kind = [str(x) for x in (raw.get("turn_kind") or []) if x]
    banned = {
        "harness_synthetic",
        "auto_review",
        "empty_or_unparseable",
        "tool_plumbing",
    }
    turn_kind = [k for k in turn_kind if k not in banned]

    abstain_reasons = [str(x) for x in (raw.get("abstain_reasons") or [])]
    user_blob = " ".join(
        [
            str(payload.get("user") or ""),
            str(payload.get("next_user") or ""),
        ]
    )
    assistant_blob = str(payload.get("assistant") or "")

    spans_in = raw.get("spans") or []
    spans: list[EvidenceSpan] = []
    for s in spans_in:
        if not isinstance(s, dict):
            continue
        quote = str(s.get("quote") or "").strip()
        if not quote:
            continue
        role = str(s.get("role") or "user")
        source = {
            "user": payload.get("user") or "",
            "assistant": payload.get("assistant") or "",
            "next_user": payload.get("next_user") or "",
        }.get(role, "")
        if quote not in source:
            if quote.lower() not in source.lower():
                abstain_reasons.append(f"span_not_in_{role}")
                continue
        spans.append(
            EvidenceSpan(
                role=role,
                quote=quote[:500],
                supports=[str(x) for x in (s.get("supports") or [])],
            )
        )

    if "correction" in turn_kind and not _CORRECTION_CUES.search(user_blob):
        turn_kind = [k for k in turn_kind if k != "correction"]
        abstain_reasons.append("correction_evidence_bar_not_met")
    if "frustrated" in turn_kind and not _FRUSTRATION_CUES.search(user_blob):
        turn_kind = [k for k in turn_kind if k != "frustrated"]
        abstain_reasons.append("frustrated_evidence_bar_not_met")

    user_stance = raw.get("user_stance")
    if user_stance == UserStance.CORRECTING.value and "correction" not in turn_kind:
        user_stance = UserStance.ABSTAIN.value
        abstain_reasons.append("correcting_stance_without_correction_label")
    if user_stance == UserStance.FRUSTRATED.value and not _FRUSTRATION_CUES.search(
        user_blob
    ):
        user_stance = UserStance.ABSTAIN.value
        abstain_reasons.append("frustrated_stance_default_abstain")

    agent_stance = raw.get("agent_stance")
    if agent_stance == AgentStance.PUSHING_BACK.value:
        has_quote = any(
            "pushing_back" in sp.supports or "pushback" in sp.supports for sp in spans
        ) or any(
            sp.role == "assistant" and sp.quote and sp.quote in assistant_blob
            for sp in spans
        )
        if not has_quote:
            agent_stance = AgentStance.ABSTAIN.value
            abstain_reasons.append("pushing_back_requires_quote")

    novel = []
    for item in raw.get("novel_observations") or []:
        text = str(item).strip()
        if not text:
            continue
        low = text.lower()
        if "caused by skill" in low or "skill caused" in low or "because the skill" in low:
            abstain_reasons.append("skill_causation_stripped")
            continue
        novel.append(text[:240])

    flags_raw = raw.get("flags") or {}
    if not isinstance(flags_raw, dict):
        flags_raw = {}
    flags = ProcessFlags(
        premature_action_called_out=bool(flags_raw.get("premature_action_called_out")),
        scope_expansion=bool(flags_raw.get("scope_expansion")),
        scope_narrowing=bool(flags_raw.get("scope_narrowing")),
        multi_agent_reference=bool(flags_raw.get("multi_agent_reference")),
        instruction_violation_alleged=bool(
            flags_raw.get("instruction_violation_alleged")
        ),
        verification_requested=bool(flags_raw.get("verification_requested")),
        usage_or_api_limit=bool(flags_raw.get("usage_or_api_limit")),
    )

    conf_raw = raw.get("confidence") or {}
    confidence = {
        str(k): float(v)
        for k, v in conf_raw.items()
        if isinstance(v, (int, float))
    }

    return UxObservation(
        window_id=window_id,
        extractor=ExtractorMeta(
            name=EXTRACTOR_NAME_UX,
            version=EXTRACTOR_VERSION,
            model=DEFAULT_UX_MODEL,
            prompt_hash=PROMPT_HASH,
            provider=ApiExtractionProvider.name,
            redaction_version=str(
                payload.get("redaction_version") or REDACTION_VERSION
            ),
        ),
        turn_kind=turn_kind,
        user_stance=str(user_stance) if user_stance is not None else None,
        agent_stance=str(agent_stance) if agent_stance is not None else None,
        prior_outcome=str(raw.get("prior_outcome"))
        if raw.get("prior_outcome") is not None
        else None,
        flags=flags,
        spans=spans,
        confidence=confidence,
        abstain_reasons=sorted(set(abstain_reasons)),
        novel_observations=novel[:12],
    )


class UxExtractor:
    def __init__(
        self,
        client: ChatClient | None = None,
        *,
        provider: ExtractionProvider | None = None,
        model: str = DEFAULT_UX_MODEL,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if provider is not None:
            self.provider = provider
        else:
            self.provider = ApiExtractionProvider(client=client)
        self.client = client
        self.model = model
        self.batch_size = max(1, batch_size)

    def extract_one(self, ctx: WindowContext) -> UxObservation:
        return self.extract_many([ctx], batch_size=1)[0]

    def extract_many(
        self,
        contexts: list[WindowContext],
        *,
        batch_size: int | None = None,
    ) -> list[UxObservation]:
        bs = max(1, batch_size if batch_size is not None else self.batch_size)
        out: list[UxObservation] = []
        for i in range(0, len(contexts), bs):
            chunk = contexts[i : i + bs]
            out.extend(self._extract_batch(chunk))
        for obs in out:
            obs.batch_size = bs
            obs.extractor.model = self.model
            obs.extractor.provider = getattr(self.provider, "name", None)
        return out

    def _extract_batch(self, contexts: list[WindowContext]) -> list[UxObservation]:
        payloads = [truncate_for_ux(c) for c in contexts]
        for p in payloads:
            assert len(p["user"]) <= 4000 + 1
        user_msg = build_user_message(payloads)
        raw = self.provider.complete_json(
            system=SYSTEM_PROMPT, user=user_msg, model=self.model
        )
        rows = _parse_batch_response(raw, [p["window_id"] for p in payloads])
        payload_by_id = {p["window_id"]: p for p in payloads}
        return [
            enforce_reliability_tiers(row, payload=payload_by_id[row["window_id"]])
            if row.get("window_id") in payload_by_id
            else enforce_reliability_tiers(
                row,
                payload={
                    "window_id": row.get("window_id", "?"),
                    "user": "",
                    "assistant": "",
                    "next_user": "",
                },
            )
            for row in rows
        ]
