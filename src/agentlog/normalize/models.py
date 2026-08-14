from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from agentlog.normalize.tool_ops import OperationKind


class Harness(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"
    CURSOR = "cursor"
    WARP = "warp"
    HERMES = "hermes"
    T3CODE = "t3code"
    GROK = "grok"


class NormalizedMessage(BaseModel):
    seq: int
    role: str
    timestamp: datetime | None = None
    model: str | None = None
    provider: str | None = None
    agent_profile: str | None = None
    effort: str | None = None
    effort_source: str | None = None
    text: str = ""
    content_hash: str = ""
    is_tool_plumbing: bool = False
    authored_by_agent: bool = False


class ToolEvent(BaseModel):
    seq: int
    message_seq: int | None = None
    tool_name: str
    action: str
    success: bool | None = None
    duration_ms: int | None = None
    operation_kind: OperationKind = "unknown"


class SkillExposure(BaseModel):
    message_seq: int | None = None
    skill_name: str
    exposure_type: str


class TokenUsage(BaseModel):
    """Harness-reported token counts. Null means not reported; 0 means measured none."""

    seq: int
    message_seq: int | None = None
    granularity: str  # message | turn | session_cumulative
    usage_source: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    timestamp: datetime | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


class NormalizedSession(BaseModel):
    harness: Harness
    external_id: str
    parent_session_id: str | None = None
    originator: str | None = None
    thread_source: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    repo: str | None = None
    cwd: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    model: str | None = None
    provider: str | None = None
    agent_profile: str | None = None
    effort: str | None = None
    effort_source: str | None = None


class ParseResult(BaseModel):
    session: NormalizedSession
    messages: list[NormalizedMessage] = Field(default_factory=list)
    tool_events: list[ToolEvent] = Field(default_factory=list)
    skill_exposures: list[SkillExposure] = Field(default_factory=list)
    token_usages: list[TokenUsage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    bytes_consumed: int = 0
    extras: dict[str, Any] = Field(default_factory=dict)
