from __future__ import annotations

import re
from dataclasses import dataclass

from agentlog.analysis.extractors.taxonomy import (
    SKILL_BODY_CHAR_THRESHOLD,
    TurnKind,
)

_AUTO_REVIEW_RE = re.compile(
    r"(codex agent history added since your last approval assessment|"
    r"approval assessment|codex-auto-review|"
    r"the following is the (codex )?agent history)",
    re.IGNORECASE,
)
_TASK_NOTIF_RE = re.compile(
    r"^\s*<task-notification\b|"
    r"perform any necessary follow-up actions in response to the subagent completion",
    re.IGNORECASE,
)
_REALTIME_DELEGATION_RE = re.compile(
    r"realtime[_ ]?delegation|"
    r"computer[_ ]?use|"
    r"\[realtime",
    re.IGNORECASE,
)
_CONTINUE_STUB_RE = re.compile(
    r"^\s*continue from where you left off\.?\s*$",
    re.IGNORECASE,
)
_SLASH_CMD_RE = re.compile(r"^\s*/[a-zA-Z0-9_:\-]+")
_IMAGE_ONLY_RE = re.compile(r"^\s*\[Image:[^\]]*\]\s*$", re.IGNORECASE)
_CURSOR_WRAPPER_RE = re.compile(
    r"<user_query>|<timestamp>|</user_query>",
    re.IGNORECASE,
)
_WORKER_BRIEF_RE = re.compile(
    r"(owned files\s*:|finish with status|"
    r"you are (phase\d+\s+)?\w[\w\s]{0,40}worker\b|"
    r"\bSTATUS\b.*owned|"
    r"owned files)",
    re.IGNORECASE,
)
_INTER_AGENT_RE = re.compile(
    r"\[(CODEX|CLAUDE|CURSOR)\s*->\s*(CODEX|CLAUDE|CURSOR)\]|"
    r"wrong agent context|"
    r"status nudge from lead",
    re.IGNORECASE,
)
_COORDINATOR_RE = re.compile(
    r"wrong agent context|do not act\. this message was intended|"
    r"status nudge from lead",
    re.IGNORECASE,
)
_SKILL_BODY_RE = re.compile(
    r"(#\s*update config skill|"
    r"<manually_attached_skills>|"
    r"skill-injections|"
    r"^#\s+.+\n.*##\s+)",
    re.IGNORECASE | re.DOTALL,
)
_SKILL_INVOCATION_RE = re.compile(
    r"^\s*/[a-zA-Z0-9_\-]+(?:/[a-zA-Z0-9_\-]+)?\s*$|"
    r"skill\s*:\s*\S+",
    re.IGNORECASE,
)
_API_ERROR_RE = re.compile(r"^\s*API Error\s*:", re.IGNORECASE)
_USAGE_LIMIT_RE = re.compile(
    r"you'?re out of (extra )?usage|usage limit|rate limit|"
    r"resets?\s+\d",
    re.IGNORECASE,
)
_CROSS_HARNESS_RE = re.compile(
    r"\b(claude(?:\s*code)?|codex|cursor|grok)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RequestKindHit:
    kind: str
    turn_kinds: tuple[str, ...]


def unwrap_cursor_user_text(text: str) -> str:
    """Pull inner <user_query> body when present; else return text unchanged."""
    m = re.search(
        r"<user_query>\s*(.*?)\s*</user_query>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        return m.group(1).strip()
    return text


def classify_request_text(text: str) -> RequestKindHit:
    raw = text or ""
    stripped = raw.strip()
    if not stripped:
        return RequestKindHit(
            "empty",
            (TurnKind.EMPTY_OR_UNPARSEABLE.value,),
        )
    if _AUTO_REVIEW_RE.search(stripped):
        return RequestKindHit(
            "auto_review",
            (TurnKind.AUTO_REVIEW.value, TurnKind.HARNESS_SYNTHETIC.value),
        )
    if _TASK_NOTIF_RE.search(stripped):
        return RequestKindHit(
            "task_notification",
            (TurnKind.HARNESS_SYNTHETIC.value,),
        )
    if _CONTINUE_STUB_RE.match(stripped):
        return RequestKindHit(
            "continue_stub",
            (TurnKind.HARNESS_SYNTHETIC.value,),
        )
    if _REALTIME_DELEGATION_RE.search(stripped) and len(stripped) < 2000:
        return RequestKindHit(
            "realtime_delegation",
            (TurnKind.HARNESS_SYNTHETIC.value,),
        )
    if _IMAGE_ONLY_RE.match(stripped):
        return RequestKindHit(
            "image_only",
            (TurnKind.IMAGE_ONLY.value,),
        )
    if _SLASH_CMD_RE.match(stripped) and len(stripped) < 200:
        kinds = [TurnKind.SLASH_COMMAND.value]
        if _SKILL_INVOCATION_RE.match(stripped):
            kinds.append(TurnKind.SKILL_INVOCATION.value)
        return RequestKindHit("slash_command", tuple(kinds))
    if len(stripped) >= SKILL_BODY_CHAR_THRESHOLD or (
        _SKILL_BODY_RE.search(stripped) and len(stripped) >= 8_000
    ):
        return RequestKindHit(
            "skill_body",
            (TurnKind.SKILL_INVOCATION.value, TurnKind.HARNESS_SYNTHETIC.value),
        )
    if _WORKER_BRIEF_RE.search(stripped):
        return RequestKindHit(
            "worker_brief",
            (TurnKind.WORKER_BRIEF.value,),
        )
    if _INTER_AGENT_RE.search(stripped):
        kinds = [TurnKind.INTER_AGENT_HANDOFF.value]
        if _COORDINATOR_RE.search(stripped):
            kinds.append(TurnKind.COORDINATOR_NUDGE.value)
        return RequestKindHit("inter_agent_handoff", tuple(kinds))
    if _CURSOR_WRAPPER_RE.search(raw):
        return RequestKindHit(
            "cursor_wrapped",
            (TurnKind.HUMAN_TASK.value,),
        )
    return RequestKindHit(
        "substantive",
        (TurnKind.HUMAN_TASK.value,),
    )


def assistant_has_api_error(text: str) -> bool:
    return bool(_API_ERROR_RE.search(text or ""))


def assistant_has_usage_limit(text: str) -> bool:
    return bool(_USAGE_LIMIT_RE.search(text or ""))


def mentions_cross_harness(text: str) -> bool:
    return bool(_CROSS_HARNESS_RE.search(text or ""))


def is_wait_loop_shape(assistant_msg_count: int, tool_count: int) -> bool:
    return assistant_msg_count >= 15 and tool_count <= assistant_msg_count
