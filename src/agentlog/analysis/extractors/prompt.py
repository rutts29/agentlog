from __future__ import annotations

import hashlib
from pathlib import Path

PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "ux_extraction_subagent.md"
)


def load_ux_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def ux_prompt_hash(text: str | None = None) -> str:
    body = text if text is not None else load_ux_prompt()
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]
