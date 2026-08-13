from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

OWNER_NOTE_PROMPT_VERSION = "owner_notes_v1"
OWNER_NOTE_PROMPT = """You extract notes for the human who ran these sessions — not for the agent, not for a pipeline.

An Insight is a note they will use next time: how they briefed, scoped, or
took over, and what to do differently. Prefer a few deep cards over many
small ones. A good card has a thesis title, two short paragraphs of logic,
a verbatim quote that exists in the packet, and a concrete limitation.

Reject: taxonomy labels, proof arcs, n-counts, unused-skill receipts,
sentiment, and anything that only restates that a tool ran.

Return JSON: {"items":[{"session_id","message_seq","kind","title","body","quote","does_not_prove"}]}.
Abstain with {"items":[]} when nothing would change how they work.
"""


def owner_prompt_hash() -> str:
    return hashlib.sha256(OWNER_NOTE_PROMPT.encode("utf-8")).hexdigest()[:24]


def collect_owner_turns(packet: dict[str, Any]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for window in packet.get("windows") or []:
        if not isinstance(window, dict):
            continue
        request = window.get("request") if isinstance(window.get("request"), dict) else {}
        text = str(window.get("user") or request.get("source_text") or "").strip()
        if not text:
            continue
        turns.append(
            {
                "packet_id": packet.get("packet_id"),
                "session_id": window.get("session_id") or "",
                "seq": request.get("seq"),
                "harness": window.get("harness"),
                "user": text,
            }
        )
    return turns


def collect_packet_dir_turns(packets_dir: Path) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for path in sorted(packets_dir.glob("*.json")):
        try:
            packet = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid coach packet {path.name}: {exc}") from exc
        if not isinstance(packet, dict):
            raise ValueError(f"coach packet {path.name} must be an object")
        turns.extend(collect_owner_turns(packet))
    return turns


def validate_owner_items(items: Iterable[Any]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise ValueError(f"owner note {idx} must be an object")
        item = {
            "session_id": _need(raw, "session_id", idx),
            "kind": _need(raw, "kind", idx),
            "title": _need(raw, "title", idx),
            "body": _need(raw, "body", idx),
            "quote": _need(raw, "quote", idx),
            "does_not_prove": _need(raw, "does_not_prove", idx),
        }
        seq = raw.get("message_seq")
        if seq is not None and str(seq).strip() != "":
            item["message_seq"] = int(seq)
        cleaned.append(item)
    return cleaned


def write_owner_fact_packet(
    path: Path,
    *,
    run_id: str,
    items: Iterable[Any],
    source: str = "owner_notes",
) -> dict[str, Any]:
    payload = {
        "run_id": run_id,
        "source": source,
        "prompt_hash": owner_prompt_hash(),
        "items": validate_owner_items(items),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return payload


def compact_turns(turns: list[dict[str, Any]], *, max_chars: int = 80_000) -> str:
    lines: list[str] = []
    used = 0
    for turn in turns:
        text = " ".join(str(turn.get("user") or "").split())
        if len(text) > 900:
            text = text[:900]
        line = (
            f"{turn.get('session_id')} seq={turn.get('seq')} "
            f"({turn.get('packet_id')} {turn.get('harness')}): {text}"
        )
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _need(raw: dict[str, Any], key: str, idx: int) -> str:
    value = str(raw.get(key) or "").strip()
    if not value:
        raise ValueError(f"owner note {idx} is missing {key}")
    return value
