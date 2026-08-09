from __future__ import annotations

import json
import logging
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from agentlog.config import CURSOR_PROJECTS_DIR, CURSOR_STATE_VSCDB
from agentlog.ingest.base import (
    TranscriptAdapter,
    content_hash_text,
    extract_text,
    iter_jsonl_bytes,
    parse_ts,
)
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
    ToolEvent,
)

log = logging.getLogger("agentlog.ingest.cursor")


def _external_id(path: Path) -> str:
    slug = _project_slug(path) or "unknown"
    # Prefer UUID directory name for main transcripts; file stem otherwise
    if path.parent.name != "subagents" and path.stem == path.parent.name:
        local = path.stem
    elif path.parent.name == "subagents":
        local = f"subagent:{path.stem}"
    else:
        local = path.stem
    return f"{slug}/{local}"


def _project_slug(path: Path) -> str | None:
    try:
        rel = path.relative_to(CURSOR_PROJECTS_DIR)
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


def _infer_cwd(path: Path) -> str | None:
    slug = _project_slug(path)
    if not slug:
        return None
    # Cursor slug is like Users-ruttanshbhatelia-side-projects-Plugin
    if slug.startswith("Users-"):
        return "/" + slug.replace("-", "/")
    return slug


def _composer_id(path: Path) -> str | None:
    """Transcript UUID equals Cursor composerId for agent-transcripts."""
    if path.parent.name == "subagents":
        return path.stem
    if path.stem == path.parent.name:
        return path.stem
    return path.stem


def _effort_from_model_config(model_config: dict[str, Any]) -> str | None:
    for selected in model_config.get("selectedModels") or []:
        if not isinstance(selected, dict):
            continue
        for param in selected.get("parameters") or []:
            if not isinstance(param, dict):
                continue
            if param.get("id") == "effort" and param.get("value") not in (None, ""):
                return str(param["value"])
    return None


@lru_cache(maxsize=1)
def _composer_model_effort_map(
    db_path: str,
) -> dict[str, tuple[str | None, str | None]]:
    """Read composerId -> (model, effort) from state.vscdb (read-only)."""
    path = Path(db_path)
    if not path.is_file():
        return {}
    out: dict[str, tuple[str | None, str | None]] = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        log.warning("cannot open Cursor state.vscdb read-only: %s", exc)
        return {}
    try:
        rows = conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
        )
        for key, value in rows:
            composer_id = str(key).split(":", 1)[-1]
            try:
                raw = value if isinstance(value, str) else value.decode("utf-8")
                obj = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                continue
            if not isinstance(obj, dict):
                continue
            model_config = obj.get("modelConfig")
            if not isinstance(model_config, dict):
                continue
            model_name = model_config.get("modelName")
            model: str | None = None
            if isinstance(model_name, str) and model_name and model_name != "default":
                model = model_name
            effort = _effort_from_model_config(model_config)
            if model is not None or effort is not None:
                out[composer_id] = (model, effort)
    except sqlite3.Error as exc:
        log.warning("failed reading Cursor composerData: %s", exc)
        return {}
    finally:
        conn.close()
    return out


def lookup_composer_model_effort(
    composer_id: str | None,
    *,
    state_db: Path | None = None,
) -> tuple[str | None, str | None]:
    if not composer_id:
        return None, None
    db = state_db if state_db is not None else CURSOR_STATE_VSCDB
    meta = _composer_model_effort_map(str(db))
    return meta.get(composer_id, (None, None))


class CursorAdapter(TranscriptAdapter):
    harness = Harness.CURSOR

    def discover(self) -> list[Path]:
        root = CURSOR_PROJECTS_DIR
        if not root.is_dir():
            return []
        out: list[Path] = []
        for transcript_root in root.glob("*/agent-transcripts"):
            if not transcript_root.is_dir():
                continue
            out.extend(p for p in transcript_root.rglob("*.jsonl") if p.is_file())
        return sorted(out)

    def parse_chunk(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> ParseResult:
        warnings: list[str] = []
        messages: list[NormalizedMessage] = []
        tools: list[ToolEvent] = []
        external_id = _external_id(path)
        parent = None
        if path.parent.name == "subagents":
            # .../agent-transcripts/<uuid>/subagents/<id>.jsonl
            slug = _project_slug(path) or "unknown"
            parent = f"{slug}/{path.parent.parent.name}"
        cwd = _infer_cwd(path)
        model, effort = lookup_composer_model_effort(_composer_id(path))
        started_at = None
        ended_at = None
        msg_seq = 0
        tool_seq = 0
        bytes_consumed = 0

        for _s, end, obj, err in iter_jsonl_bytes(data, source=str(path)):
            bytes_consumed = end
            if err:
                warnings.append(err)
                continue
            assert obj is not None

            if obj.get("type") == "turn_ended":
                continue

            role = obj.get("role")
            if role not in ("user", "assistant", "system"):
                continue

            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            content = msg.get("content")
            text = extract_text(content)
            # Pull timestamp from user_query wrapper when present
            ts = parse_ts(obj.get("timestamp"))
            if ts is None and isinstance(text, str) and text.startswith("<timestamp>"):
                end_tag = text.find("</timestamp>")
                if end_tag != -1:
                    ts = parse_ts(text[len("<timestamp>") : end_tag].strip())
            if started_at is None and ts:
                started_at = ts
            if ts:
                ended_at = ts

            msg_seq += 1
            messages.append(
                NormalizedMessage(
                    seq=msg_seq,
                    role=str(role),
                    timestamp=ts,
                    model=model if role == "assistant" else None,
                    effort=effort if role == "assistant" else None,
                    text=text,
                    content_hash=content_hash_text(text),
                )
            )

            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_seq += 1
                        tools.append(
                            ToolEvent(
                                seq=tool_seq,
                                message_seq=msg_seq,
                                tool_name=str(block.get("name") or "tool"),
                                action="call",
                            )
                        )
                    elif block.get("type") == "tool_result":
                        tool_seq += 1
                        tools.append(
                            ToolEvent(
                                seq=tool_seq,
                                message_seq=msg_seq,
                                tool_name=str(
                                    block.get("name")
                                    or block.get("tool_use_id")
                                    or "tool"
                                ),
                                action="result",
                                success=(
                                    not bool(block.get("is_error"))
                                    if "is_error" in block
                                    else None
                                ),
                            )
                        )

        session = NormalizedSession(
            harness=Harness.CURSOR,
            external_id=external_id,
            parent_session_id=parent,
            started_at=started_at,
            ended_at=ended_at,
            cwd=cwd,
            repo=_project_slug(path),
            model=model,
            effort=effort,
        )
        return ParseResult(
            session=session,
            messages=messages,
            tool_events=tools,
            warnings=warnings,
            bytes_consumed=start_offset + bytes_consumed,
        )
