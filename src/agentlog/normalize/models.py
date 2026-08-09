from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Harness(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"
    CURSOR = "cursor"


class NormalizedMessage(BaseModel):
    seq: int
    role: str
    timestamp: datetime | None = None
    model: str | None = None
    effort: str | None = None
    text: str = ""
    content_hash: str = ""
    is_tool_plumbing: bool = False


class ToolEvent(BaseModel):
    seq: int
    message_seq: int | None = None
    tool_name: str
    action: str
    success: bool | None = None
    duration_ms: int | None = None


class SkillExposure(BaseModel):
    message_seq: int | None = None
    skill_name: str
    exposure_type: str


class NormalizedSession(BaseModel):
    harness: Harness
    external_id: str
    parent_session_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    repo: str | None = None
    cwd: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    model: str | None = None
    effort: str | None = None


class ParseResult(BaseModel):
    session: NormalizedSession
    messages: list[NormalizedMessage] = Field(default_factory=list)
    tool_events: list[ToolEvent] = Field(default_factory=list)
    skill_exposures: list[SkillExposure] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    bytes_consumed: int = 0
    extras: dict[str, Any] = Field(default_factory=dict)
