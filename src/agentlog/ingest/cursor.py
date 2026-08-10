from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from agentlog.config import CURSOR_PROJECTS_DIR, CURSOR_STATE_VSCDB
from agentlog.ingest.base import (
    TranscriptAdapter,
    content_hash_text,
    content_is_tool_plumbing,
    extract_text,
    flag_parent_authored_prompt,
    iter_jsonl_bytes,
    parse_ts,
)
from agentlog.normalize.effort import normalize_effort
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
    SkillExposure,
    ToolEvent,
)
from agentlog.normalize.synthetic import (
    flag_synthetic_user_messages,
    is_cursor_subagent_followup,
    synthetic_skill_exposures,
)
from agentlog.normalize.tool_ops import classify_operation

log = logging.getLogger("agentlog.ingest.cursor")

_USER_QUERY_RE = re.compile(
    r"<timestamp>(.*?)</timestamp>\s*<user_query>\s*(.*?)\s*</user_query>",
    re.S,
)
_TIMESTAMP_ONLY_RE = re.compile(r"<timestamp>(.*?)</timestamp>", re.S)
_MANUALLY_ATTACHED_SKILLS_RE = re.compile(
    r"<manually_attached_skills\b.*?</manually_attached_skills\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SKILL_NAME_RE = re.compile(
    r"^\s*Skill\s+Name:\s*(?P<name>[^\r\n]+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


_UNKNOWN_REPOS = frozenset({"", "unknown", "empty-window"})


def _strip_cursor_skill_injection(text: str) -> tuple[str, list[str]]:
    """Remove Cursor's inlined skill bodies and return their declared names."""
    matches = list(_MANUALLY_ATTACHED_SKILLS_RE.finditer(text or ""))
    if not matches:
        return text, []
    names: list[str] = []
    seen: set[str] = set()
    for match in matches:
        for name_match in _SKILL_NAME_RE.finditer(match.group(0)):
            name = name_match.group("name").strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    cleaned = _MANUALLY_ATTACHED_SKILLS_RE.sub("", text)
    return cleaned, names


def _cursor_owner_text(text: str) -> str:
    match = _USER_QUERY_RE.search(text or "")
    if match:
        return match.group(2).strip()
    text = re.sub(r"<timestamp>.*?</timestamp>\s*", "", text or "", flags=re.DOTALL)
    text = re.sub(r"</?user_query>\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


def _tool_detail(block: dict[str, Any]) -> str | None:
    raw_input = block.get("input")
    if not isinstance(raw_input, dict):
        return None
    for key in ("command", "cmd"):
        value = raw_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def canonical_external_id(external_id: str) -> str:
    """Strip path / subagent prefixes; composer UUID is the stable identity."""
    local = external_id.split("/", 1)[-1]
    if local.startswith("subagent:"):
        return local.split(":", 1)[1]
    return local


def _external_id(path: Path) -> str:
    # Workspace path is metadata (repo/cwd), not part of identity. Cursor may
    # copy the same composer UUID under empty-window/ and a real project path,
    # and sometimes also under another chat's subagents/ folder.
    return path.stem


def external_id_from_path(path: Path) -> str:
    """Public path → external_id helper (used by live presence)."""
    return _external_id(path)


def prefer_repo(current: str | None, incoming: str | None) -> str | None:
    """Prefer a real project slug over empty-window / unknown.

    When both are equally real, keep ``current`` (winner's attribution).
    """

    def score(repo: str | None) -> int:
        if repo is None or repo in _UNKNOWN_REPOS:
            return 0
        return 1

    if score(incoming) > score(current):
        return incoming
    if score(current) > 0:
        return current
    return incoming if score(incoming) > 0 else current


def _repo_for_path(path: Path) -> str | None:
    slug = _project_slug(path)
    if slug is None or slug in _UNKNOWN_REPOS:
        return None
    return slug


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
    if slug.startswith("Users-"):
        return "/" + slug.replace("-", "/")
    return slug


def _composer_id(path: Path) -> str | None:
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


def _normalize_user_key(text: str) -> str:
    return _cursor_owner_text(text)[:120]


@dataclass(frozen=True)
class _UserTurn:
    key: str
    model: str | None
    created_at: Any  # datetime | None — avoid circular import typing noise


@dataclass
class ComposerMeta:
    model: str | None = None
    effort: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    started_at: Any = None
    ended_at: Any = None
    still_open: bool = False
    user_turns: list[_UserTurn] = field(default_factory=list)


def _branch_from_git_repos(tracked: Any) -> str | None:
    if not isinstance(tracked, list) or not tracked:
        return None
    best_name: str | None = None
    best_ts = -1
    for repo in tracked:
        if not isinstance(repo, dict):
            continue
        for br in repo.get("branches") or []:
            if not isinstance(br, dict):
                continue
            name = br.get("branchName")
            if not name:
                continue
            ts = br.get("lastInteractionAt") or 0
            try:
                ts_n = int(ts)
            except (TypeError, ValueError):
                ts_n = 0
            if ts_n >= best_ts:
                best_ts = ts_n
                best_name = str(name)
    return best_name


def _load_bubbles(
    conn: sqlite3.Connection, composer_id: str
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
        (f"bubbleId:{composer_id}:%",),
    )
    for key, value in rows:
        try:
            raw = value if isinstance(value, str) else value.decode("utf-8")
            obj = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            continue
        if not isinstance(obj, dict):
            continue
        bid = obj.get("bubbleId") or str(key).rsplit(":", 1)[-1]
        out[str(bid)] = obj
    return out


def _meta_from_composer_obj(
    obj: dict[str, Any], bubbles: dict[str, dict[str, Any]]
) -> ComposerMeta:
    model_config = obj.get("modelConfig")
    model: str | None = None
    effort: str | None = None
    if isinstance(model_config, dict):
        model_name = model_config.get("modelName")
        if isinstance(model_name, str) and model_name and model_name != "default":
            model = model_name
        effort = _effort_from_model_config(model_config)

    branch = _branch_from_git_repos(obj.get("trackedGitRepos"))
    started_at = parse_ts(obj.get("createdAt"))
    last_updated = parse_ts(obj.get("lastUpdatedAt"))
    status = obj.get("status")
    # Observed: completed | aborted | none. Only treat unknown active-like
    # statuses as still open; none means no explicit end marker (use last ts).
    still_open = status not in (None, "none", "completed", "aborted")

    headers = obj.get("fullConversationHeadersOnly") or []
    user_turns: list[_UserTurn] = []
    max_bubble_ts = None
    for header in headers:
        if not isinstance(header, dict):
            continue
        bid = header.get("bubbleId")
        bubble = bubbles.get(str(bid)) if bid is not None else None
        ts = parse_ts((bubble or {}).get("createdAt") or header.get("createdAt"))
        if ts and (max_bubble_ts is None or ts > max_bubble_ts):
            max_bubble_ts = ts
        if not bubble or bubble.get("type") != 1:
            continue
        text = str(bubble.get("text") or "")
        key = _normalize_user_key(text)
        if not key:
            continue
        mi = bubble.get("modelInfo") if isinstance(bubble.get("modelInfo"), dict) else {}
        turn_model = mi.get("modelName")
        if not isinstance(turn_model, str) or not turn_model or turn_model == "default":
            turn_model = None
        user_turns.append(_UserTurn(key=key, model=turn_model, created_at=ts))

    ended_at = None
    if not still_open:
        ended_at = last_updated or max_bubble_ts

    return ComposerMeta(
        model=model,
        effort=effort,
        branch=branch,
        commit_sha=None,
        started_at=started_at,
        ended_at=ended_at,
        still_open=still_open,
        user_turns=user_turns,
    )


def _state_db_revision(path: Path) -> tuple[tuple[int, int, int] | None, ...]:
    revision: list[tuple[int, int, int] | None] = []
    for candidate in (path, path.with_name(f"{path.name}-wal")):
        try:
            stat = candidate.stat()
        except OSError:
            revision.append(None)
        else:
            revision.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return tuple(revision)


@lru_cache(maxsize=512)
def _composer_meta_cached(
    db_path: str,
    composer_id: str,
    _revision: tuple[tuple[int, int, int] | None, ...],
) -> ComposerMeta:
    path = Path(db_path)
    if not path.is_file() or not composer_id:
        return ComposerMeta()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        log.warning("cannot open Cursor state.vscdb read-only: %s", exc)
        return ComposerMeta()
    try:
        row = conn.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (f"composerData:{composer_id}",),
        ).fetchone()
        if not row:
            return ComposerMeta()
        try:
            raw = row[0] if isinstance(row[0], str) else row[0].decode("utf-8")
            obj = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            return ComposerMeta()
        if not isinstance(obj, dict):
            return ComposerMeta()
        bubbles = _load_bubbles(conn, composer_id)
        return _meta_from_composer_obj(obj, bubbles)
    except sqlite3.Error as exc:
        log.warning("failed reading Cursor composerData: %s", exc)
        return ComposerMeta()
    finally:
        conn.close()


def lookup_composer_meta(
    composer_id: str | None,
    *,
    state_db: Path | None = None,
) -> ComposerMeta:
    if not composer_id:
        return ComposerMeta()
    db = state_db if state_db is not None else CURSOR_STATE_VSCDB
    return _composer_meta_cached(str(db), composer_id, _state_db_revision(db))


def lookup_composer_model_effort(
    composer_id: str | None,
    *,
    state_db: Path | None = None,
) -> tuple[str | None, str | None]:
    meta = lookup_composer_meta(composer_id, state_db=state_db)
    return meta.model, meta.effort


def _composer_model_effort_map(
    db_path: str,
) -> dict[str, tuple[str | None, str | None]]:
    """Test helper: scan all composerData rows for (model, effort)."""
    path = Path(db_path)
    if not path.is_file():
        return {}
    out: dict[str, tuple[str | None, str | None]] = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        for key, value in conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
        ):
            composer_id = str(key).split(":", 1)[-1]
            try:
                raw = value if isinstance(value, str) else value.decode("utf-8")
                obj = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                continue
            if not isinstance(obj, dict):
                continue
            meta = _meta_from_composer_obj(obj, {})
            if meta.model is not None or meta.effort is not None:
                out[composer_id] = (meta.model, meta.effort)
    finally:
        conn.close()
    return out


def _match_user_turn(
    text: str, turns: list[_UserTurn], start_idx: int
) -> tuple[int | None, _UserTurn | None]:
    key = _normalize_user_key(text)
    if not key:
        return None, None
    for i in range(start_idx, len(turns)):
        turn = turns[i]
        if turn.key == key or turn.key[:40] == key[:40] or key[:40] == turn.key[:40]:
            return i, turn
    for i in range(0, min(start_idx, len(turns))):
        turn = turns[i]
        if turn.key == key or turn.key[:40] == key[:40]:
            return i, turn
    return None, None


def _message_timestamp(obj: dict[str, Any], text: str) -> Any:
    ts = parse_ts(obj.get("timestamp"))
    if ts is not None:
        return ts
    m = _TIMESTAMP_ONLY_RE.search(text or "")
    if m:
        return parse_ts(m.group(1).strip())
    return None


def _parent_transcript_path(path: Path) -> Path | None:
    """.../agent-transcripts/<parent-uuid>/subagents/<child>.jsonl → parent jsonl."""
    if path.parent.name != "subagents":
        return None
    parent_dir = path.parent.parent
    candidate = parent_dir / f"{parent_dir.name}.jsonl"
    return candidate if candidate.is_file() else None


@lru_cache(maxsize=256)
def _first_user_content_hash(path_str: str) -> str | None:
    path = Path(path_str)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    for _s, _e, obj, err in iter_jsonl_bytes(data, source=path_str):
        if err or obj is None:
            continue
        if obj.get("role") != "user":
            continue
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        text = extract_text(msg.get("content"))
        text, _ = _strip_cursor_skill_injection(text)
        return content_hash_text(_cursor_owner_text(text))
    return None


def _clear_flag_if_copied_parent_history(
    path: Path, messages: list[NormalizedMessage]
) -> None:
    """Side chats under subagents/ copy parent history; those first turns are human."""
    if not messages or not messages[0].authored_by_agent:
        return
    parent_path = _parent_transcript_path(path)
    if parent_path is None:
        return
    parent_hash = _first_user_content_hash(str(parent_path))
    if parent_hash and parent_hash == messages[0].content_hash:
        messages[0].authored_by_agent = False


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
        skills: list[SkillExposure] = []
        tool_operations: dict[str, str] = {}
        tool_names: dict[str, str] = {}
        external_id = _external_id(path)
        parent = None
        if path.parent.name == "subagents":
            parent = path.parent.parent.name
        repo = _repo_for_path(path)
        cwd = _infer_cwd(path) if repo is not None else None
        meta = lookup_composer_meta(_composer_id(path))
        session_model = meta.model
        effort, effort_source = normalize_effort(meta.effort)
        started_at = meta.started_at
        last_msg_ts = None
        msg_seq = 0
        tool_seq = 0
        bytes_consumed = 0
        # Per-generation model from user-bubble modelInfo only — never broadcast
        # the session modelConfig onto every assistant message.
        current_gen_model: str | None = None
        turn_scan_idx = 0

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
            skill_names: list[str] = []
            if role == "user":
                text, skill_names = _strip_cursor_skill_injection(text)
            ts = _message_timestamp(obj, text)
            if role == "user":
                idx, turn = _match_user_turn(text, meta.user_turns, turn_scan_idx)
                if turn is not None:
                    current_gen_model = turn.model
                    if idx is not None:
                        turn_scan_idx = idx + 1
                    if ts is None:
                        ts = turn.created_at
                else:
                    current_gen_model = None

            if started_at is None and ts:
                started_at = ts
            if ts is not None and (last_msg_ts is None or ts > last_msg_ts):
                last_msg_ts = ts

            msg_model = current_gen_model if role == "assistant" else None
            msg_effort = effort if role == "assistant" else None
            msg_effort_source = effort_source if role == "assistant" else None
            stored_text = _cursor_owner_text(text) if role == "user" else text

            msg_seq += 1
            messages.append(
                NormalizedMessage(
                    seq=msg_seq,
                    role=str(role),
                    timestamp=ts,
                    model=msg_model,
                    effort=msg_effort,
                    effort_source=msg_effort_source,
                    text=stored_text,
                    content_hash=content_hash_text(stored_text),
                    is_tool_plumbing=content_is_tool_plumbing(content),
                    authored_by_agent=is_cursor_subagent_followup(text),
                )
            )

            for skill_name in skill_names:
                skills.append(
                    SkillExposure(
                        message_seq=msg_seq,
                        skill_name=skill_name,
                        exposure_type="attached",
                    )
                )

            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_seq += 1
                        name = str(block.get("name") or "tool")
                        tool_id = str(block.get("id") or block.get("tool_use_id") or "")
                        operation_kind = str(
                            classify_operation(name, _tool_detail(block))
                        )
                        if tool_id:
                            tool_operations[tool_id] = operation_kind
                            tool_names[tool_id] = name
                        tools.append(
                            ToolEvent(
                                seq=tool_seq,
                                message_seq=msg_seq,
                                tool_name=name,
                                action="call",
                                operation_kind=operation_kind,
                            )
                        )
                    elif block.get("type") == "tool_result":
                        tool_seq += 1
                        tool_id = str(block.get("tool_use_id") or block.get("id") or "")
                        result_name = block.get("name")
                        name = str(
                            result_name
                            or tool_names.get(tool_id)
                            or tool_id
                            or "tool"
                        )
                        tools.append(
                            ToolEvent(
                                seq=tool_seq,
                                message_seq=msg_seq,
                                tool_name=name,
                                action="result",
                                success=(
                                    not bool(block.get("is_error"))
                                    if "is_error" in block
                                    else None
                                ),
                                operation_kind=tool_operations.get(
                                    tool_id, str(classify_operation(name))
                                ),
                            )
                        )

        if meta.still_open:
            ended_at = None
        else:
            # Prefer composer lastUpdatedAt / max bubble ts; else last message ts.
            ended_at = meta.ended_at or last_msg_ts

        # Child transcripts under subagents/: first user turn is the parent's Task prompt.
        if parent is not None and start_offset == 0:
            flag_parent_authored_prompt(messages)
            _clear_flag_if_copied_parent_history(path, messages)
        flag_synthetic_user_messages(messages)
        skills.extend(synthetic_skill_exposures(messages))

        session = NormalizedSession(
            harness=Harness.CURSOR,
            external_id=external_id,
            parent_session_id=parent,
            started_at=started_at,
            ended_at=ended_at,
            cwd=cwd,
            repo=repo,
            branch=meta.branch,
            commit_sha=meta.commit_sha,
            model=session_model,
            effort=effort,
            effort_source=effort_source,
        )
        return ParseResult(
            session=session,
            messages=messages,
            tool_events=tools,
            skill_exposures=skills,
            warnings=warnings,
            bytes_consumed=start_offset + bytes_consumed,
        )
