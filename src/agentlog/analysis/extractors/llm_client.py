from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Protocol


class ChatClient(Protocol):
    def complete_json(self, *, system: str, user: str, model: str) -> dict[str, Any]:
        ...


class XAIChatClient:
    """OpenAI-compatible chat completions against xAI (Grok)."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.x.ai/v1",
        timeout_s: float = 120.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("XAI_API_KEY") or ""
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def complete_json(self, *, system: str, user: str, model: str) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError(
                "XAI_API_KEY is not set; cannot call Grok for UX extraction"
            )
        payload = {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"xAI HTTP {exc.code}: {detail[:500]}") from exc
        content = body["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return json.loads(content)


class ScriptedChatClient:
    """Deterministic client for tests / offline audit dry-runs."""

    def __init__(self, responder) -> None:
        self._responder = responder

    def complete_json(self, *, system: str, user: str, model: str) -> dict[str, Any]:
        return self._responder(system=system, user=user, model=model)
