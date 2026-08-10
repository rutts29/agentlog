"""Show the owner the exact bytes remote extraction would transmit.

Nothing here opens a socket. The request body is produced by the same
``XAIChatClient.build_request_body`` and ``build_user_message`` the live send
uses, so what the owner reviews is what would go out — not a reconstruction.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from agentlog.analysis.extractors.llm_client import XAIChatClient
from agentlog.analysis.extractors.taxonomy import DEFAULT_BATCH_SIZE, DEFAULT_UX_MODEL, Route
from agentlog.analysis.extractors.ux_extractor import (
    SYSTEM_PROMPT,
    build_user_message,
    prompt_hash,
)
from agentlog.analysis.extractors.window_context import load_window_contexts, truncate_for_ux
from agentlog.safety.egress import EGRESS_DISCLOSURE, remote_extraction_enabled
from agentlog.safety.redaction import REDACTION_VERSION, RedactionReport, redact_text
from agentlog.safety.write_guard import assert_writable


def build_egress_preview(
    conn: sqlite3.Connection,
    *,
    window_ids: list[str] | None = None,
    limit: int = 5,
    model: str = DEFAULT_UX_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    base_url: str = "https://api.x.ai/v1",
    ux_only: bool = True,
) -> dict[str, Any]:
    contexts = load_window_contexts(conn, window_ids=window_ids)
    if ux_only:
        from agentlog.analysis.extractors.triage import triage_window

        contexts = [c for c in contexts if triage_window(c).route == Route.UX]
    eligible_total = len(contexts)
    if limit > 0:
        contexts = contexts[:limit]

    report = RedactionReport()
    payloads = [truncate_for_ux(c, report=report) for c in contexts]

    client = XAIChatClient(base_url=base_url)
    bs = max(1, batch_size)
    requests: list[dict[str, Any]] = []
    for i in range(0, len(payloads), bs):
        chunk = payloads[i : i + bs]
        body = client.build_request_body(
            system=SYSTEM_PROMPT, user=build_user_message(chunk), model=model
        )
        requests.append(
            {
                "method": "POST",
                "url": client.endpoint,
                "headers": {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer <XAI_API_KEY>",
                },
                "window_ids": [p["window_id"] for p in chunk],
                "body_bytes": len(json.dumps(body).encode("utf-8")),
                "body": body,
            }
        )

    return {
        "sent_anything": False,
        "remote_extraction_enabled": remote_extraction_enabled(),
        "api_key_configured": bool(client.api_key),
        "endpoint": client.endpoint,
        "model": model,
        "prompt_hash": prompt_hash(),
        "batch_size": bs,
        "eligible_window_count": eligible_total,
        "previewed_window_count": len(payloads),
        "disclosure": EGRESS_DISCLOSURE,
        **report.to_dict(),
        "requests": requests,
    }


def verify_preview_clean(preview: dict[str, Any], needles: list[str]) -> list[str]:
    """Return any ``needles`` that survive into the serialized request bodies."""
    blob = json.dumps(preview.get("requests") or [], ensure_ascii=False)
    return [n for n in needles if n and n in blob]


def write_egress_preview(preview: dict[str, Any], path: Path) -> Path:
    target = assert_writable(path, purpose="egress preview")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(preview, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return target


def preview_summary(preview: dict[str, Any]) -> dict[str, Any]:
    """Console-sized view: counts and one redacted sample, never the full corpus."""
    requests = preview.get("requests") or []
    sample = ""
    if requests:
        messages = requests[0]["body"]["messages"]
        sample = redact_text(str(messages[-1]["content"])[:1200])
    return {
        "endpoint": preview["endpoint"],
        "model": preview["model"],
        "remote_extraction_enabled": preview["remote_extraction_enabled"],
        "api_key_configured": preview["api_key_configured"],
        "eligible_window_count": preview["eligible_window_count"],
        "previewed_window_count": preview["previewed_window_count"],
        "request_count": len(requests),
        "total_body_bytes": sum(int(r["body_bytes"]) for r in requests),
        "redaction_version": preview.get("redaction_version", REDACTION_VERSION),
        "redactions": preview.get("redactions", {}),
        "sample_user_message_head": sample,
    }
