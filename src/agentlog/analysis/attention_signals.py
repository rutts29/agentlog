"""Compact, transcript-free signals used by the Attention Inbox."""

from __future__ import annotations

import re
from collections.abc import Iterable

from agentlog.normalize.models import NormalizedMessage


_INCOMPLETE_TODO_RE = re.compile(
    r"(?:"
    r"^\s*[-*]\s+\[\s\]\s+\S"
    r'|"status"\s*:\s*"(?:pending|in_progress)"'
    r"|'status'\s*:\s*'(?:pending|in_progress)'"
    r")",
    re.MULTILINE | re.IGNORECASE,
)


def final_paragraph(text: str) -> str:
    parts = [part.strip() for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]
    return parts[-1] if parts else ""


def assistant_asks_question(text: str) -> bool:
    return "?" in final_paragraph(text)


def incomplete_todo_in_text(text: str) -> bool:
    return bool(_INCOMPLETE_TODO_RE.search(text or ""))


def last_attention_signal(messages: Iterable[NormalizedMessage]) -> str:
    """Return a small semantic marker for the latest non-plumbing turn."""
    last = next(
        (message for message in reversed(list(messages)) if not message.is_tool_plumbing),
        None,
    )
    if last is None or last.role != "assistant":
        return "none"
    if assistant_asks_question(last.text):
        return "question"
    if incomplete_todo_in_text(last.text):
        return "incomplete_todo"
    return "none"
