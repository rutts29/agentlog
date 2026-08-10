"""Warp terminal AI ingest — reads warp.sqlite (queries only; no assistant text)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agentlog.config import WARP_SQLITE
from agentlog.ingest.base import (
    TranscriptAdapter,
    content_hash_text,
    parse_ts,
)
from agentlog.ingest.sqlite_ro import open_sqlite_readonly, table_exists
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
    ToolEvent,
)

log = logging.getLogger("agentlog.ingest.warp")


def _model_or_none(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if not text or text.lower() == "auto":
        return None
    return text


def _extract_query_text(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return None


def _cwd_from_query_context(payload: Any, fallback: str | None) -> str | None:
    if isinstance(payload, dict):
        for item in payload.get("context") or []:
            if not isinstance(item, dict):
                continue
            directory = item.get("Directory")
            if isinstance(directory, dict):
                pwd = directory.get("pwd")
                if isinstance(pwd, str) and pwd.strip():
                    return pwd
    return fallback


def _parse_input_items(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


class WarpAdapter(TranscriptAdapter):
    harness = Harness.WARP
    supports_byte_append = False

    def discover(self) -> list[Path]:
        path = WARP_SQLITE
        return [path] if path.is_file() else []

    def parse_chunk(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> ParseResult:
        results = self.parse_path(path, data, start_offset=start_offset)
        if results:
            return results[0]
        return ParseResult(
            session=NormalizedSession(
                harness=Harness.WARP,
                external_id="empty",
            ),
            bytes_consumed=path.stat().st_size if path.is_file() else 0,
            warnings=["warp: no conversations found"],
        )

    def parse_path(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> list[ParseResult]:
        del data, start_offset
        size = path.stat().st_size if path.is_file() else 0
        results: list[ParseResult] = []
        warnings: list[str] = []

        with open_sqlite_readonly(path) as conn:
            if not table_exists(conn, "ai_queries"):
                warnings.append("warp: ai_queries table missing")
                return [
                    ParseResult(
                        session=NormalizedSession(
                            harness=Harness.WARP, external_id="empty"
                        ),
                        warnings=warnings,
                        bytes_consumed=size,
                    )
                ]

            has_blocks = table_exists(conn, "ai_blocks")
            block_count = 0
            if has_blocks:
                row = conn.execute("SELECT COUNT(*) AS c FROM ai_blocks").fetchone()
                block_count = int(row["c"]) if row else 0
            if block_count == 0:
                warnings.append(
                    "warp: ai_blocks empty — assistant replies not stored locally; "
                    "ingesting user queries and ActionResult tool events only"
                )

            conversation_ids = [
                str(r["conversation_id"])
                for r in conn.execute(
                    """
                    SELECT conversation_id
                    FROM ai_queries
                    GROUP BY conversation_id
                    ORDER BY MIN(start_ts)
                    """
                )
            ]

            for cid in conversation_ids:
                result = self._parse_conversation(conn, cid, size)
                if result.messages or result.tool_events:
                    results.append(result)

        if results and warnings:
            results[0].warnings = list(warnings) + list(results[0].warnings)
        return results

    def _parse_conversation(
        self,
        conn,
        conversation_id: str,
        size: int,
    ) -> ParseResult:
        messages: list[NormalizedMessage] = []
        tools: list[ToolEvent] = []
        warnings: list[str] = []
        msg_seq = 0
        tool_seq = 0
        started_at = None
        ended_at = None
        cwd: str | None = None
        session_model: str | None = None

        rows = conn.execute(
            """
            SELECT exchange_id, start_ts, input, working_directory,
                   output_status, model_id, planning_model_id, coding_model_id
            FROM ai_queries
            WHERE conversation_id = ?
            ORDER BY start_ts ASC, id ASC
            """,
            (conversation_id,),
        ).fetchall()

        for row in rows:
            ts = parse_ts(row["start_ts"])
            if ts is not None:
                if started_at is None or ts < started_at:
                    started_at = ts
                if ended_at is None or ts > ended_at:
                    ended_at = ts
            if cwd is None and row["working_directory"]:
                cwd = str(row["working_directory"])
            model = (
                _model_or_none(row["model_id"])
                or _model_or_none(row["coding_model_id"])
                or _model_or_none(row["planning_model_id"])
            )
            if session_model is None and model is not None:
                session_model = model

            items = _parse_input_items(str(row["input"] or ""))
            if not items and row["input"] not in (None, "", "[]"):
                warnings.append(
                    f"warp:{conversation_id}: unparseable input for "
                    f"{row['exchange_id']}"
                )

            for item in items:
                if "Query" in item:
                    q = item["Query"]
                    text = _extract_query_text(q)
                    if not text:
                        continue
                    cwd = _cwd_from_query_context(q, cwd)
                    msg_seq += 1
                    messages.append(
                        NormalizedMessage(
                            seq=msg_seq,
                            role="user",
                            timestamp=ts,
                            model=None,
                            text=text,
                            content_hash=content_hash_text(text),
                        )
                    )
                elif "ActionResult" in item:
                    ar = item["ActionResult"]
                    if not isinstance(ar, dict):
                        continue
                    result_obj = ar.get("result")
                    tool_name = "action"
                    if isinstance(result_obj, dict) and result_obj:
                        tool_name = str(next(iter(result_obj.keys())))
                    tool_seq += 1
                    tools.append(
                        ToolEvent(
                            seq=tool_seq,
                            message_seq=msg_seq if msg_seq else None,
                            tool_name=tool_name,
                            action="result",
                        )
                    )

        return ParseResult(
            session=NormalizedSession(
                harness=Harness.WARP,
                external_id=conversation_id,
                started_at=started_at,
                ended_at=ended_at,
                cwd=cwd,
                model=session_model,
            ),
            messages=messages,
            tool_events=tools,
            warnings=warnings,
            bytes_consumed=size,
            extras={"assistant_text_available": False},
        )
