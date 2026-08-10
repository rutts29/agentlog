from __future__ import annotations

import re
from dataclasses import dataclass

from agentlog.normalize.models import NormalizedMessage, SkillExposure


_CURSOR_OWNER_QUERY_RE = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>",
    re.IGNORECASE | re.DOTALL,
)
_CURSOR_SUBAGENT_FOLLOWUP_RE = re.compile(
    r"perform\s+any\s+necessary\s+follow[- ]?up\s+actions\s+"
    r"in\s+response\s+to\s+the\s+subagent\s+completion(?:\s+above)?\."
    r"(?:\s*if\s+no\s+follow[- ]?up\s+work\s+is\s+needed,\s+"
    r"no\s+further\s+action\s+is\s+required\.(?:\s+if\s+you\s+mention\s+"
    r"an\s+agent\s+or\s+subagent\b.*)?)?",
    re.IGNORECASE | re.DOTALL,
)

_CODEX_GOAL_CONTEXT_RE = re.compile(
    r"^\s*<codex_internal_context\b[^>]*\bsource\s*=\s*(['\"]?)goal\1"
    r"(?![\w-])[^>]*>.*?</codex_internal_context>\s*$",
    re.IGNORECASE | re.DOTALL,
)


def is_cursor_subagent_followup(text: str) -> bool:
    """Recognize Cursor's synthetic callback prompt, not a human request."""
    owner = _CURSOR_OWNER_QUERY_RE.search(text or "")
    candidate = owner.group(1) if owner else text or ""
    candidate = re.sub(
        r"^\s*<timestamp>.*?</timestamp>\s*",
        "",
        candidate,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    return bool(_CURSOR_SUBAGENT_FOLLOWUP_RE.fullmatch(candidate))


def is_codex_internal_context_goal(text: str) -> bool:
    """Recognize Codex's goal continuation prompt, not an owner request."""
    return bool(_CODEX_GOAL_CONTEXT_RE.search(text or ""))


_ENVELOPE_RE = re.compile(
    r"^\s*<(?P<tag>[a-z][\w-]*)\b(?P<attrs>[^>]*)>.*?</(?P=tag)>\s*$",
    re.IGNORECASE | re.DOTALL,
)
_AGENT_ENVELOPES = frozenset(
    {
        "subagent_notification",
        "task-notification",
        "task_notification",
        "teammate-message",
    }
)
_PLUMBING_ENVELOPES = frozenset({"system-reminder", "local-command-stdout", "skill"})
_IMAGE_PLACEHOLDERS = frozenset(
    {
        "<image>",
        "<image-placeholder>",
        "[image]",
        "[image placeholder]",
        "image omitted",
    }
)
_CONTINUE_STUB_RE = re.compile(r"^<continue\s*/?>$", re.IGNORECASE)
_RECOMMENDED_PLUGINS_RE = re.compile(
    r"^\s*<recommended_plugins>.*</environment_context>\s*$",
    re.IGNORECASE | re.DOTALL,
)
_IN_APP_BROWSER_REQUEST_RE = re.compile(
    r"^\s*<in-app-browser-context\b[^>]*>.*?</in-app-browser-context>\s*"
    r"##\s*my request for codex:\s*(?P<request>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_PAIRED_PREFIX_RE = re.compile(
    r"^\s*<image\b[^>]*>.*?</image>\s*",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_SELF_CLOSING_PREFIX_RE = re.compile(
    r"^\s*<image\b[^>]*/>\s*",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_OPEN_PREFIX_RE = re.compile(
    r"^\s*<image(?:[-_ ]placeholder)?\b[^>]*>\s*",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_CHROME_LINE_RE = re.compile(
    r"^\s*(?:"
    r"</image>"
    r"|<<\s*image(?:displayed)?\s*>>"
    r"|\[\s*image(?:\s+placeholder|\s*#?\d+)?\s*\]"
    r"|(?:/private)?/tmp/[^\r\n]+\.(?:png|jpe?g|gif|webp|heic)"
    r")\s*(?:\r?\n|$)",
    re.IGNORECASE,
)
_SKILL_NAME_ATTR_RE = re.compile(r"\bname\s*=\s*(['\"])([^'\"]+)\1", re.IGNORECASE)
_SKILL_NAME_ELEMENT_RE = re.compile(
    r"<name>\s*([^<>\r\n]+?)\s*</name>", re.IGNORECASE
)
_SKILL_HEADING_RE = re.compile(r"^\s*#{1,3}\s+([A-Za-z0-9][A-Za-z0-9_.-]*)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class SyntheticFlags:
    authored_by_agent: bool = False
    is_tool_plumbing: bool = False


@dataclass(frozen=True)
class SyntheticTextNormalization:
    text: str
    flags: SyntheticFlags = SyntheticFlags()


def normalize_synthetic_user_text(text: str) -> SyntheticTextNormalization:
    raw = text or ""
    browser = _IN_APP_BROWSER_REQUEST_RE.fullmatch(raw)
    if browser is not None:
        request = browser.group("request").strip()
        if request:
            return SyntheticTextNormalization(request)
    remaining = raw
    saw_image = False
    while True:
        self_closing = _IMAGE_SELF_CLOSING_PREFIX_RE.match(remaining)
        if self_closing is not None:
            remaining = remaining[self_closing.end():]
            saw_image = True
            continue
        paired = _IMAGE_PAIRED_PREFIX_RE.match(remaining)
        if paired is not None:
            remaining = remaining[paired.end():]
            saw_image = True
            continue
        opening = _IMAGE_OPEN_PREFIX_RE.match(remaining)
        if opening is not None:
            remaining = remaining[opening.end():]
            saw_image = True
        break
    if saw_image:
        while True:
            chrome = _IMAGE_CHROME_LINE_RE.match(remaining)
            if chrome is None:
                break
            remaining = remaining[chrome.end():]
        remaining = remaining.strip()
        if remaining:
            return SyntheticTextNormalization(remaining)
        return SyntheticTextNormalization(
            "<image>", SyntheticFlags(is_tool_plumbing=True)
        )
    return SyntheticTextNormalization(raw, classify_synthetic_user_text(raw))


def classify_synthetic_user_text(text: str) -> SyntheticFlags:
    raw = text or ""
    stripped = raw.strip()
    if not stripped:
        return SyntheticFlags()
    if is_cursor_subagent_followup(raw) or is_codex_internal_context_goal(raw):
        return SyntheticFlags(authored_by_agent=True)
    if _CONTINUE_STUB_RE.fullmatch(stripped):
        return SyntheticFlags(authored_by_agent=True)
    if stripped.casefold() in _IMAGE_PLACEHOLDERS:
        return SyntheticFlags(is_tool_plumbing=True)
    if "<user_query" in stripped.casefold():
        return SyntheticFlags()
    if stripped.casefold().startswith("<in-app-browser-context>") and "## my request for codex:" in stripped.casefold():
        return SyntheticFlags()
    if stripped.casefold().startswith("<open_subagent_context>") and "<timestamp" in stripped.casefold():
        return SyntheticFlags()
    if (
        _RECOMMENDED_PLUGINS_RE.fullmatch(stripped)
        and "## my request" not in stripped.casefold()
    ):
        return SyntheticFlags(authored_by_agent=True)
    match = _ENVELOPE_RE.fullmatch(stripped)
    if match is None:
        return SyntheticFlags()
    tag = match.group("tag").casefold()
    if tag in _PLUMBING_ENVELOPES:
        return SyntheticFlags(is_tool_plumbing=True)
    if tag in _AGENT_ENVELOPES:
        return SyntheticFlags(authored_by_agent=True)
    return SyntheticFlags()


def skill_exposure_from_synthetic_message(
    message: NormalizedMessage,
) -> SkillExposure | None:
    if message.role != "user":
        return None
    match = _ENVELOPE_RE.fullmatch((message.text or "").strip())
    if match is None or match.group("tag").casefold() != "skill":
        return None
    name = None
    attr = _SKILL_NAME_ATTR_RE.search(match.group("attrs") or "")
    if attr is not None:
        name = attr.group(2).strip()
    if not name:
        element = _SKILL_NAME_ELEMENT_RE.search(message.text)
        if element is not None:
            name = element.group(1).strip()
    if not name:
        heading = _SKILL_HEADING_RE.search(message.text)
        if heading is not None:
            name = heading.group(1).strip()
    if not name:
        return None
    return SkillExposure(
        message_seq=message.seq,
        skill_name=name,
        exposure_type="injected",
    )


def synthetic_skill_exposures(
    messages: list[NormalizedMessage],
) -> list[SkillExposure]:
    return [
        exposure
        for message in messages
        for exposure in [skill_exposure_from_synthetic_message(message)]
        if exposure is not None
    ]


def flag_synthetic_user_messages(messages: list[NormalizedMessage]) -> None:
    for message in messages:
        if message.role != "user":
            continue
        flags = classify_synthetic_user_text(message.text)
        message.authored_by_agent = message.authored_by_agent or flags.authored_by_agent
        message.is_tool_plumbing = message.is_tool_plumbing or flags.is_tool_plumbing
