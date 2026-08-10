from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EvidenceSpan(BaseModel):
    role: str
    quote: str
    supports: list[str] = Field(default_factory=list)


class ProcessFlags(BaseModel):
    premature_action_called_out: bool = False
    scope_expansion: bool = False
    scope_narrowing: bool = False
    multi_agent_reference: bool = False
    instruction_violation_alleged: bool = False
    verification_requested: bool = False
    usage_or_api_limit: bool = False


class ExtractorMeta(BaseModel):
    name: str
    version: str
    model: str | None = None
    prompt_hash: str | None = None
    packet_id: str | None = None
    provider: str | None = None
    redaction_version: str | None = None


class UxObservation(BaseModel):
    window_id: str
    extractor: ExtractorMeta
    turn_kind: list[str] = Field(default_factory=list)
    user_stance: str | None = None
    agent_stance: str | None = None
    prior_outcome: str | None = None
    flags: ProcessFlags = Field(default_factory=ProcessFlags)
    spans: list[EvidenceSpan] = Field(default_factory=list)
    confidence: dict[str, float] = Field(default_factory=dict)
    abstain_reasons: list[str] = Field(default_factory=list)
    novel_observations: list[str] = Field(default_factory=list)
    batch_size: int = 1

    def to_storage(self) -> dict[str, Any]:
        return self.model_dump()


class DetClassification(BaseModel):
    window_id: str
    turn_kinds: list[str]
    request_kind: str
    route: str
    drop_rules: list[str] = Field(default_factory=list)
    features: dict[str, Any] = Field(default_factory=dict)
    extractor: ExtractorMeta


class WindowContext(BaseModel):
    window_id: str
    session_id: str
    harness: str
    model: str | None = None
    request_text: str = ""
    assistant_text: str = ""
    next_user_text: str = ""
    tool_timeline: list[str] = Field(default_factory=list)
    skill_names: list[str] = Field(default_factory=list)
    skill_exposure_types: list[str] = Field(default_factory=list)
    is_tool_plumbing: bool = False
    authored_by_agent: bool = False
    assistant_msg_count: int = 0
    tool_count: int = 0
    request_message_id: str = ""
    response_message_id: str = ""
