"""Hermes agent ingest — reads ~/.hermes/state.db and kanban.db read-only."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agentlog.config import HERMES_HOME, HERMES_KANBAN_DB, HERMES_STATE_DB
from agentlog.ingest.base import (
    TranscriptAdapter,
    content_hash_text,
    flag_parent_authored_prompt,
    parse_ts,
)
from agentlog.ingest.sqlite_ro import open_sqlite_readonly, table_exists
from agentlog.normalize.effort import normalize_effort
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
    TokenUsage,
    ToolEvent,
)

log = logging.getLogger("agentlog.ingest.hermes")


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_tool_calls(raw: object) -> list[dict[str, Any]]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    return []


def _tool_name(call: dict[str, Any]) -> str:
    for key in ("name", "tool_name", "function"):
        val = call.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict) and isinstance(val.get("name"), str):
            return val["name"]
    return "tool"


def _message_text(row) -> str:
    for key in ("content", "api_content", "reasoning", "reasoning_content"):
        val = row[key] if key in row.keys() else None
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _discover_hermes_dbs() -> list[Path]:
    out: list[Path] = []
    if HERMES_STATE_DB.is_file():
        out.append(HERMES_STATE_DB)
    if HERMES_KANBAN_DB.is_file():
        out.append(HERMES_KANBAN_DB)
    boards = HERMES_HOME / "kanban" / "boards"
    if boards.is_dir():
        for path in sorted(boards.glob("*/kanban.db")):
            if path.is_file() and path not in out:
                out.append(path)
    return out


class HermesAdapter(TranscriptAdapter):
    harness = Harness.HERMES
    supports_byte_append = False

    def discover(self) -> list[Path]:
        return _discover_hermes_dbs()

    def parse_chunk(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> ParseResult:
        results = self.parse_path(path, data, start_offset=start_offset)
        if results:
            return results[0]
        return ParseResult(
            session=NormalizedSession(
                harness=Harness.HERMES,
                external_id="empty",
            ),
            bytes_consumed=path.stat().st_size if path.is_file() else 0,
            warnings=["hermes: no sessions found"],
        )

    def parse_path(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> list[ParseResult]:
        del data, start_offset
        size = path.stat().st_size if path.is_file() else 0
        with open_sqlite_readonly(path) as conn:
            if table_exists(conn, "sessions") and table_exists(conn, "messages"):
                return self._parse_state_db(conn, size)
            if table_exists(conn, "tasks"):
                return self._parse_kanban_db(conn, path, size)
        return []

    def _parse_state_db(self, conn, size: int) -> list[ParseResult]:
        results: list[ParseResult] = []
        sessions = conn.execute(
            """
            SELECT id, parent_session_id, started_at, ended_at, model,
                   cwd, git_branch, git_repo_root, title, model_config
            FROM sessions
            ORDER BY started_at ASC
            """
        ).fetchall()
        for sess in sessions:
            sid = str(sess["id"])
            messages: list[NormalizedMessage] = []
            tools: list[ToolEvent] = []
            usages: list[TokenUsage] = []
            warnings: list[str] = []
            msg_seq = 0
            tool_seq = 0
            usage_seq = 0

            effort = None
            effort_source = None
            raw_cfg = sess["model_config"]
            if isinstance(raw_cfg, str) and raw_cfg.strip():
                try:
                    cfg = json.loads(raw_cfg)
                except json.JSONDecodeError:
                    cfg = None
                if isinstance(cfg, dict):
                    effort, effort_source = normalize_effort(
                        cfg.get("reasoning_effort") or cfg.get("effort")
                    )

            rows = conn.execute(
                """
                SELECT id, role, content, tool_call_id, tool_calls, tool_name,
                       timestamp, token_count, reasoning, reasoning_content,
                       api_content, active, compacted
                FROM messages
                WHERE session_id = ?
                ORDER BY timestamp ASC, id ASC
                """,
                (sid,),
            ).fetchall()

            for row in rows:
                if "active" in row.keys() and row["active"] is not None:
                    if int(row["active"]) == 0:
                        continue
                role = str(row["role"] or "assistant")
                text = _message_text(row)
                ts = parse_ts(row["timestamp"])
                tool_calls = _parse_tool_calls(row["tool_calls"])
                is_plumbing = role == "tool" or (
                    not text.strip() and bool(tool_calls)
                )
                if not text.strip() and not tool_calls and role != "tool":
                    continue

                msg_seq += 1
                msg_model = (
                    str(sess["model"])
                    if sess["model"] and role == "assistant"
                    else None
                )
                messages.append(
                    NormalizedMessage(
                        seq=msg_seq,
                        role=role,
                        timestamp=ts,
                        model=msg_model,
                        effort=effort if role == "assistant" else None,
                        effort_source=effort_source if role == "assistant" else None,
                        text=text,
                        content_hash=content_hash_text(text),
                        is_tool_plumbing=is_plumbing,
                    )
                )

                for call in tool_calls:
                    tool_seq += 1
                    tools.append(
                        ToolEvent(
                            seq=tool_seq,
                            message_seq=msg_seq,
                            tool_name=_tool_name(call),
                            action="call",
                        )
                    )
                if role == "tool":
                    tool_seq += 1
                    tools.append(
                        ToolEvent(
                            seq=tool_seq,
                            message_seq=msg_seq,
                            tool_name=str(row["tool_name"] or "tool"),
                            action="result",
                        )
                    )

                tok = _int_or_none(row["token_count"])
                if tok is not None and role == "assistant":
                    usage_seq += 1
                    usages.append(
                        TokenUsage(
                            seq=usage_seq,
                            message_seq=msg_seq,
                            granularity="message",
                            usage_source="hermes_message_token_count",
                            model=msg_model,
                            total_tokens=tok,
                            timestamp=ts,
                        )
                    )

            if table_exists(conn, "session_model_usage"):
                for urow in conn.execute(
                    """
                    SELECT model, input_tokens, output_tokens, cache_read_tokens,
                           cache_write_tokens, reasoning_tokens, last_seen
                    FROM session_model_usage
                    WHERE session_id = ?
                    """,
                    (sid,),
                ):
                    usage_seq += 1
                    usages.append(
                        TokenUsage(
                            seq=usage_seq,
                            message_seq=None,
                            granularity="session_cumulative",
                            usage_source="hermes_session_model_usage",
                            model=str(urow["model"]) if urow["model"] else None,
                            input_tokens=_int_or_none(urow["input_tokens"]),
                            output_tokens=_int_or_none(urow["output_tokens"]),
                            cache_read_input_tokens=_int_or_none(
                                urow["cache_read_tokens"]
                            ),
                            cache_write_input_tokens=_int_or_none(
                                urow["cache_write_tokens"]
                            ),
                            reasoning_output_tokens=_int_or_none(
                                urow["reasoning_tokens"]
                            ),
                            timestamp=parse_ts(urow["last_seen"]),
                        )
                    )

            if not messages and not tools:
                continue

            parent_ref = (
                str(sess["parent_session_id"])
                if sess["parent_session_id"]
                else None
            )
            if parent_ref:
                flag_parent_authored_prompt(messages)

            results.append(
                ParseResult(
                    session=NormalizedSession(
                        harness=Harness.HERMES,
                        external_id=sid,
                        parent_session_id=parent_ref,
                        started_at=parse_ts(sess["started_at"]),
                        ended_at=parse_ts(sess["ended_at"]),
                        cwd=str(sess["cwd"]) if sess["cwd"] else None,
                        branch=(
                            str(sess["git_branch"]) if sess["git_branch"] else None
                        ),
                        repo=(
                            str(sess["git_repo_root"])
                            if sess["git_repo_root"]
                            else None
                        ),
                        model=str(sess["model"]) if sess["model"] else None,
                        effort=effort,
                        effort_source=effort_source,
                    ),
                    messages=messages,
                    tool_events=tools,
                    token_usages=usages,
                    warnings=warnings,
                    bytes_consumed=size,
                )
            )
        return results

    def _parse_kanban_db(self, conn, path: Path, size: int) -> list[ParseResult]:
        results: list[ParseResult] = []
        board = path.parent.name if path.parent.name != "kanban" else "default"
        if path.name == "kanban.db" and path.parent.name == ".hermes":
            board = "default"
        if path == HERMES_KANBAN_DB:
            board = "default"

        tasks = conn.execute(
            """
            SELECT id, title, body, status, created_at, started_at, completed_at,
                   workspace_path, branch_name, model_override, reasoning_effort,
                   session_id, result
            FROM tasks
            ORDER BY created_at ASC
            """
        ).fetchall()
        has_comments = table_exists(conn, "task_comments")

        for task in tasks:
            tid = str(task["id"])
            messages: list[NormalizedMessage] = []
            msg_seq = 0
            started_at = parse_ts(task["started_at"] or task["created_at"])
            ended_at = parse_ts(task["completed_at"])
            effort, effort_source = normalize_effort(task["reasoning_effort"])
            model = (
                str(task["model_override"]) if task["model_override"] else None
            )

            title = str(task["title"] or "").strip()
            body = str(task["body"] or "").strip()
            head = title
            if body:
                head = f"{title}\n\n{body}" if title else body
            if head:
                msg_seq += 1
                messages.append(
                    NormalizedMessage(
                        seq=msg_seq,
                        role="user",
                        timestamp=parse_ts(task["created_at"]),
                        text=head,
                        content_hash=content_hash_text(head),
                    )
                )

            if has_comments:
                for c in conn.execute(
                    """
                    SELECT author, body, created_at
                    FROM task_comments
                    WHERE task_id = ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (tid,),
                ):
                    text = str(c["body"] or "").strip()
                    if not text:
                        continue
                    author = str(c["author"] or "").lower()
                    role = (
                        "assistant"
                        if any(
                            x in author
                            for x in ("agent", "hermes", "worker", "assistant")
                        )
                        else "user"
                    )
                    msg_seq += 1
                    messages.append(
                        NormalizedMessage(
                            seq=msg_seq,
                            role=role,
                            timestamp=parse_ts(c["created_at"]),
                            model=model if role == "assistant" else None,
                            effort=effort if role == "assistant" else None,
                            effort_source=(
                                effort_source if role == "assistant" else None
                            ),
                            text=text,
                            content_hash=content_hash_text(text),
                        )
                    )

            result_text = str(task["result"] or "").strip()
            if result_text:
                msg_seq += 1
                messages.append(
                    NormalizedMessage(
                        seq=msg_seq,
                        role="assistant",
                        timestamp=ended_at,
                        model=model,
                        effort=effort,
                        effort_source=effort_source,
                        text=result_text,
                        content_hash=content_hash_text(result_text),
                    )
                )

            if not messages:
                continue

            parent = (
                str(task["session_id"]) if task["session_id"] else None
            )
            results.append(
                ParseResult(
                    session=NormalizedSession(
                        harness=Harness.HERMES,
                        external_id=f"kanban:{board}:{tid}",
                        parent_session_id=parent,
                        started_at=started_at,
                        ended_at=ended_at,
                        cwd=(
                            str(task["workspace_path"])
                            if task["workspace_path"]
                            else None
                        ),
                        branch=(
                            str(task["branch_name"])
                            if task["branch_name"]
                            else None
                        ),
                        model=model,
                        effort=effort,
                        effort_source=effort_source,
                    ),
                    messages=messages,
                    bytes_consumed=size,
                    extras={"kanban_status": task["status"], "board": board},
                )
            )
        return results
