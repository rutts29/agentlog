"""On-demand, source-backed tool call detail for the local dashboard.

The normalized ledger intentionally omits tool arguments and outputs. This
module reads the original local artifact only for an explicit drill-down and
returns bounded previews without changing parser or database contracts.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

_DETAIL_LIMIT = 12_000
_MAX_ITEMS = 200
_CODEX_CALL_TYPES = {
    "function_call",
    "custom_tool_call",
    "web_search_call",
    "tool_search_call",
}
_CODEX_RESULT_TYPES = {
    "function_call_output",
    "custom_tool_call_output",
    "tool_search_output",
}
_CODEX_END_TYPES = {
    "exec_command_end",
    "patch_apply_end",
    "web_search_end",
    "mcp_tool_call_end",
}


def _bounded(value: Any) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(str(item["text"]))
            else:
                parts.append(json.dumps(item, ensure_ascii=False, indent=2))
        text = "\n\n".join(part for part in parts if part.strip()).strip()
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    if not text:
        return None, False
    if len(text) <= _DETAIL_LIMIT:
        return text, False
    return text[:_DETAIL_LIMIT].rstrip() + "…", True


def _call_id(payload: dict[str, Any], fallback: str) -> str:
    for key in ("call_id", "callId", "tool_call_id", "toolCallId", "id"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    return fallback


def _raw_codex_tools(path: Path) -> dict[int, dict[str, Any]]:
    events: dict[int, dict[str, Any]] = {}
    call_names: dict[str, str] = {}
    result_seq_by_call: dict[str, int] = {}
    seen_calls: set[str] = set()
    seq = 0
    try:
        stream = path.open("r", encoding="utf-8")
    except OSError:
        return events
    with stream:
        for line in stream:
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(obj, dict) or obj.get("type") not in {"response_item", "event_msg"}:
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            payload_type = str(payload.get("type") or "")
            timestamp = obj.get("timestamp")
            if payload_type in _CODEX_CALL_TYPES:
                call_id = _call_id(payload, f"{payload_type}:{seq + 1}")
                if call_id in seen_calls:
                    continue
                seen_calls.add(call_id)
                name = str(payload.get("name") or payload.get("tool_name") or payload_type)
                call_names[call_id] = name
                seq += 1
                detail, truncated = _bounded(
                    payload.get("arguments")
                    if payload.get("arguments") is not None
                    else payload.get("input")
                    if payload.get("input") is not None
                    else payload.get("params")
                )
                events[seq] = {
                    "kind": "call",
                    "call_id": call_id,
                    "tool_name": name,
                    "timestamp": timestamp,
                    "detail": detail,
                    "detail_truncated": truncated,
                }
                continue
            if payload_type in _CODEX_RESULT_TYPES:
                call_id = _call_id(payload, f"{payload_type}:{seq + 1}")
                seq += 1
                detail, truncated = _bounded(
                    payload.get("output")
                    if payload.get("output") is not None
                    else payload.get("result")
                    if payload.get("result") is not None
                    else payload.get("content")
                )
                events[seq] = {
                    "kind": "result",
                    "call_id": call_id,
                    "tool_name": call_names.get(call_id),
                    "timestamp": timestamp,
                    "detail": detail,
                    "detail_truncated": truncated,
                }
                result_seq_by_call[call_id] = seq
                continue
            if payload_type in _CODEX_END_TYPES:
                call_id = _call_id(payload, f"{payload_type}:{seq + 1}")
                detail, truncated = _bounded(
                    payload.get("output")
                    if payload.get("output") is not None
                    else payload.get("result")
                )
                existing_seq = result_seq_by_call.get(call_id)
                if existing_seq is not None:
                    if detail:
                        events[existing_seq]["detail"] = detail
                        events[existing_seq]["detail_truncated"] = truncated
                    continue
                seq += 1
                events[seq] = {
                    "kind": "result",
                    "call_id": call_id,
                    "tool_name": call_names.get(call_id) or payload_type.removesuffix("_end"),
                    "timestamp": timestamp,
                    "detail": detail,
                    "detail_truncated": truncated,
                }
                result_seq_by_call[call_id] = seq
    return events


def _session_source(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT s.id, s.harness, a.path
        FROM sessions s
        LEFT JOIN artifacts a ON a.id = s.artifact_id
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchone()


def tool_trace(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    start_seq: int,
    end_seq: int,
) -> dict[str, Any] | None:
    source = _session_source(conn, session_id)
    if source is None:
        return None
    low, high = sorted((start_seq, end_seq))
    rows = conn.execute(
        """
        SELECT t.id, t.seq, t.tool_name, t.action, t.success, t.duration_ms,
               t.operation_kind, t.message_id, m.timestamp,
               m.model_canonical, m.model, m.effort
        FROM tool_events t
        LEFT JOIN messages m ON m.id = t.message_id
        WHERE t.session_id = ?
        ORDER BY t.seq
        """,
        (session_id,),
    ).fetchall()
    path = Path(str(source["path"])).expanduser() if source["path"] else None
    raw = (
        _raw_codex_tools(path)
        if source["harness"] == "codex" and path is not None and path.is_file()
        else {}
    )
    row_by_seq = {int(row["seq"]): row for row in rows}
    result_seq_by_call: dict[str, int] = {}
    for seq, event in raw.items():
        if event.get("kind") == "result" and event.get("call_id"):
            result_seq_by_call[str(event["call_id"])] = seq

    consumed: set[int] = set()
    items: list[dict[str, Any]] = []
    for row in rows:
        seq = int(row["seq"])
        if seq in consumed or not low <= seq <= high:
            continue
        raw_event = raw.get(seq, {})
        result_row: sqlite3.Row | None = None
        result_raw: dict[str, Any] = {}
        result_seq: int | None = None
        if str(row["action"]).casefold() == "call":
            call_id = raw_event.get("call_id")
            if call_id:
                result_seq = result_seq_by_call.get(str(call_id))
            if result_seq is None:
                candidate = row_by_seq.get(seq + 1)
                if candidate is not None and str(candidate["tool_name"]) == str(row["tool_name"]):
                    result_seq = seq + 1
            if result_seq is not None:
                result_row = row_by_seq.get(result_seq)
                result_raw = raw.get(result_seq, {})
                consumed.add(result_seq)

        input_text = raw_event.get("detail") if raw_event.get("kind") == "call" else None
        output_text = result_raw.get("detail") if result_raw.get("kind") == "result" else None
        if str(row["action"]).casefold() != "call" and raw_event.get("kind") == "result":
            output_text = raw_event.get("detail")
        terminal = result_row or row
        items.append(
            {
                "id": str(row["id"]),
                "call_id": raw_event.get("call_id"),
                "call_seq": seq,
                "result_seq": result_seq,
                "tool_name": str(row["tool_name"]),
                "operation_kind": str(row["operation_kind"] or "unknown"),
                "timestamp": raw_event.get("timestamp") or row["timestamp"],
                "result_timestamp": result_raw.get("timestamp"),
                "message_id": row["message_id"],
                "recorded_action": str(row["action"] or "unknown"),
                "model": row["model_canonical"] or row["model"],
                "effort": row["effort"],
                "input": input_text,
                "input_truncated": bool(raw_event.get("detail_truncated")),
                "output": output_text,
                "output_truncated": bool(result_raw.get("detail_truncated")),
                "success": terminal["success"],
                "duration_ms": terminal["duration_ms"],
            }
        )
        if len(items) >= _MAX_ITEMS:
            break

    source_detail_count = sum(
        1 for item in items if item["input"] is not None or item["output"] is not None
    )
    return {
        "session_id": session_id,
        "harness": str(source["harness"]),
        "start_seq": low,
        "end_seq": high,
        "capture": {
            "source_backed": bool(raw),
            "detailed_items": source_detail_count,
            "total_items": len(items),
            "note": (
                "Inputs and outputs were read on demand from the local source artifact."
                if raw
                else "This harness exposes normalized tool metadata only for this session."
            ),
        },
        "items": items,
    }
