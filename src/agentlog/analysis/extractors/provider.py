from __future__ import annotations

from typing import Any, Protocol

from agentlog.analysis.extractors.llm_client import ChatClient, XAIChatClient


class ExtractionProvider(Protocol):
    """Model-invocation step for UX extraction.

    Implementations either call an API in-process (ApiExtractionProvider)
    or orchestrate a file-based subagent handoff (PacketExtractionProvider
    in packets.py — emit/ingest, no in-process model call).
    """

    name: str

    def complete_json(self, *, system: str, user: str, model: str) -> dict[str, Any]:
        ...


class ApiExtractionProvider:
    """Direct xAI/OpenAI-compatible chat completions (unattended / nightly)."""

    name = "api"

    def __init__(self, client: ChatClient | None = None) -> None:
        self.client = client or XAIChatClient()

    def complete_json(self, *, system: str, user: str, model: str) -> dict[str, Any]:
        return self.client.complete_json(system=system, user=user, model=model)
