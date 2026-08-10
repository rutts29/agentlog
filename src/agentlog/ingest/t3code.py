"""t3 code ingest — reads the event-sourced state.sqlite read-only.

t3 code is an orchestrator: a thread is driven by a provider instance
(cursor / codex / claudeAgent / grok / opencode), so the provider instance is
recorded as ``agent_profile`` and the underlying vendor as ``provider`` while
``model`` keeps only the model slug.

Threads carry the conversation, activities carry tool traffic, and the
append-only ``orchestration_events`` log carries authorship (``actor_kind``)
and per-turn model selection.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from agentlog import config
from agentlog.ingest.base import (
    TranscriptAdapter,
    content_hash_text,
    parse_ts,
)
from agentlog.ingest.sqlite_ro import open_sqlite_readonly, table_exists
from agentlog.normalize.effort import normalize_effort
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
    ToolEvent,
)

log = logging.getLogger("agentlog.ingest.t3code")

KNOWN_ROLES = frozenset({"user", "assistant", "system"})

# t3 provider instance id -> upstream vendor. Instance ids are user-editable,
# so an unknown id falls back to the instance id itself as the profile.
PROVIDER_INSTANCE_VENDORS: dict[str, str] = {
    "cursor": "cursor",
    "codex": "openai",
    "claudeagent": "anthropic",
    "claude": "anthropic",
    "grok": "xai",
    "opencode": "opencode",
}

_TOOL_TONES = frozenset({"tool", "approval"})

_CALL_KINDS = frozenset(
    {"tool", "tool-call", "tool-calls", "command", "file-change", "diff"}
)
_RESULT_KINDS = frozenset({"tool-result", "tool-results"})
_APPROVAL_KINDS = frozenset(
    {"tool-approval-request", "tool-approval-response", "approval"}
)

_TOOL_NAME_KEYS = (
    "toolName",
    "tool_name",
    "tool",
    "name",
    "commandName",
    "command",
)


def _json_obj(raw: object) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _row_get(row: sqlite3.Row, key: str) -> Any:
    return row[key] if key in row.keys() else None


def discover_t3code_dbs() -> list[Path]:
    """Candidate state DBs across possible install roots; missing is fine."""
    out: list[Path] = []
    for root in config.T3CODE_HOME_CANDIDATES:
        if not root.is_dir():
            continue
        for rel in config.T3CODE_STATE_DB_GLOBS:
            for path in sorted(root.glob(rel)):
                if path.is_file() and path not in out:
                    out.append(path)
    return out


class ModelSelection:
    """Decoded t3 ``modelSelection``: instance id, model slug, options."""

    __slots__ = ("instance_id", "model", "effort", "effort_source")

    def __init__(self, raw: object) -> None:
        data = _json_obj(raw) or {}
        instance = data.get("instanceId")
        if not isinstance(instance, str) or not instance.strip():
            provider = data.get("provider")
            instance = provider if isinstance(provider, str) else None
        self.instance_id = instance.strip() if isinstance(instance, str) else None

        model = data.get("model")
        self.model = model.strip() if isinstance(model, str) and model.strip() else None

        options = data.get("options")
        raw_effort = None
        if isinstance(options, dict):
            value = options.get("effort")
            if isinstance(value, str) and value.strip():
                raw_effort = value.strip()
        self.effort, self.effort_source = normalize_effort(raw_effort)

    @property
    def agent_profile(self) -> str | None:
        return self.instance_id

    @property
    def provider(self) -> str | None:
        if not self.instance_id:
            return None
        return PROVIDER_INSTANCE_VENDORS.get(self.instance_id.lower())

    def is_empty(self) -> bool:
        return self.instance_id is None and self.model is None


def _tool_name_from(payload: dict[str, Any] | None, kind: str, summary: str) -> str:
    if payload:
        for key in _TOOL_NAME_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().split()[0]
            if isinstance(value, dict):
                nested = value.get("name")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    if kind.strip():
        return kind.strip()
    return summary.strip().split()[0] if summary.strip() else "tool"


def _action_for(kind: str, tone: str) -> str:
    key = kind.strip().lower()
    if key in _RESULT_KINDS:
        return "result"
    if key in _APPROVAL_KINDS:
        return "approval"
    if key in _CALL_KINDS:
        return "call"
    if tone == "approval":
        return "approval"
    return key or "call"


class T3CodeAdapter(TranscriptAdapter):
    harness = Harness.T3CODE
    supports_byte_append = False

    def discover(self) -> list[Path]:
        return discover_t3code_dbs()

    def parse_chunk(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> ParseResult:
        results = self.parse_path(path, data, start_offset=start_offset)
        if results:
            return results[0]
        return ParseResult(
            session=NormalizedSession(
                harness=Harness.T3CODE,
                external_id="empty",
            ),
            bytes_consumed=path.stat().st_size if path.is_file() else 0,
        )

    def parse_path(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> list[ParseResult]:
        del data, start_offset
        size = path.stat().st_size if path.is_file() else 0
        with open_sqlite_readonly(path) as conn:
            if not table_exists(conn, "projection_threads"):
                log.info("t3code: %s has no projection_threads; skipping", path)
                return []
            return self._parse_state_db(conn, size)

    def _parse_state_db(
        self, conn: sqlite3.Connection, size: int
    ) -> list[ParseResult]:
        projects = {
            str(row["project_id"]): row
            for row in conn.execute(
                "SELECT project_id, title, workspace_root, "
                "default_model_selection_json FROM projection_projects"
            )
        }
        threads = conn.execute(
            """
            SELECT thread_id, project_id, title, branch, worktree_path,
                   created_at, updated_at, deleted_at, model_selection_json,
                   runtime_mode, interaction_mode
            FROM projection_threads
            ORDER BY created_at ASC, thread_id ASC
            """
        ).fetchall()
        if not threads:
            return []

        sessions_by_thread = self._provider_sessions(conn)
        plan_parents = self._plan_parents(conn)

        results: list[ParseResult] = []
        for thread in threads:
            result = self._parse_thread(
                conn,
                thread,
                projects=projects,
                provider_sessions=sessions_by_thread,
                plan_parents=plan_parents,
                size=size,
            )
            if result is not None:
                results.append(result)
        return results

    def _provider_sessions(
        self, conn: sqlite3.Connection
    ) -> dict[str, sqlite3.Row]:
        if not table_exists(conn, "projection_thread_sessions"):
            return {}
        return {
            str(row["thread_id"]): row
            for row in conn.execute(
                "SELECT thread_id, provider_name, provider_instance_id, "
                "provider_session_id, provider_thread_id, status "
                "FROM projection_thread_sessions"
            )
        }

    def _plan_parents(self, conn: sqlite3.Connection) -> dict[str, str]:
        """implementation thread -> originating plan thread."""
        out: dict[str, str] = {}
        if table_exists(conn, "projection_thread_proposed_plans"):
            for row in conn.execute(
                "SELECT thread_id, implementation_thread_id "
                "FROM projection_thread_proposed_plans "
                "WHERE implementation_thread_id IS NOT NULL"
            ):
                child = str(row["implementation_thread_id"])
                parent = str(row["thread_id"])
                if child and parent and child != parent:
                    out[child] = parent
        if table_exists(conn, "projection_turns"):
            for row in conn.execute(
                "SELECT thread_id, source_proposed_plan_thread_id "
                "FROM projection_turns "
                "WHERE source_proposed_plan_thread_id IS NOT NULL"
            ):
                child = str(row["thread_id"])
                parent = str(row["source_proposed_plan_thread_id"])
                if child and parent and child != parent:
                    out.setdefault(child, parent)
        return out

    def _turn_message_ids(
        self, conn: sqlite3.Connection, thread_id: str
    ) -> dict[str, str]:
        """turn_id -> the message that turn produced (assistant, else pending)."""
        out: dict[str, str] = {}
        if not table_exists(conn, "projection_turns"):
            return out
        for row in conn.execute(
            """
            SELECT turn_id, assistant_message_id, pending_message_id
            FROM projection_turns
            WHERE thread_id = ? AND turn_id IS NOT NULL
            """,
            (thread_id,),
        ):
            turn_id = str(row["turn_id"])
            target = row["assistant_message_id"] or row["pending_message_id"]
            if target:
                out[turn_id] = str(target)
        return out

    def _event_facts(
        self, conn: sqlite3.Connection, thread_id: str
    ) -> tuple[
        dict[str, str],
        dict[str, ModelSelection],
        list[tuple[Any, ModelSelection]],
        list[str],
    ]:
        """Authorship and model selection recovered from the event log.

        Returns (message_id -> actor_kind, message_id -> selection,
        timeline of (occurred_at, selection), warnings).
        """
        actors: dict[str, str] = {}
        per_message: dict[str, ModelSelection] = {}
        timeline: list[tuple[Any, ModelSelection]] = []
        warnings: list[str] = []
        if not table_exists(conn, "orchestration_events"):
            return actors, per_message, timeline, warnings

        for row in conn.execute(
            """
            SELECT event_type, occurred_at, actor_kind, payload_json
            FROM orchestration_events
            WHERE aggregate_kind = 'thread' AND stream_id = ?
            ORDER BY sequence ASC
            """,
            (thread_id,),
        ):
            event_type = str(row["event_type"] or "")
            payload = _json_obj(row["payload_json"])
            if payload is None:
                warnings.append(
                    f"t3code: unparseable payload on {event_type} "
                    f"for thread {thread_id}"
                )
                continue
            actor = str(row["actor_kind"] or "").strip().lower()
            when = parse_ts(row["occurred_at"])

            message_id = payload.get("messageId")
            if event_type == "thread.message-sent" and isinstance(message_id, str):
                actors[message_id] = actor

            selection_raw = payload.get("modelSelection")
            if selection_raw is not None:
                selection = ModelSelection(selection_raw)
                if not selection.is_empty():
                    timeline.append((when, selection))
                    if event_type == "thread.turn-start-requested" and isinstance(
                        message_id, str
                    ):
                        per_message[message_id] = selection
        return actors, per_message, timeline, warnings

    @staticmethod
    def _selection_at(
        timeline: list[tuple[Any, ModelSelection]], when: Any
    ) -> ModelSelection | None:
        chosen: ModelSelection | None = None
        for occurred_at, selection in timeline:
            if when is not None and occurred_at is not None and occurred_at > when:
                break
            chosen = selection
        return chosen

    def _parse_thread(
        self,
        conn: sqlite3.Connection,
        thread: sqlite3.Row,
        *,
        projects: dict[str, sqlite3.Row],
        provider_sessions: dict[str, sqlite3.Row],
        plan_parents: dict[str, str],
        size: int,
    ) -> ParseResult | None:
        thread_id = str(thread["thread_id"])
        warnings: list[str] = []

        project = projects.get(str(thread["project_id"]))
        if project is None and thread["project_id"]:
            warnings.append(
                f"t3code: thread {thread_id} references unknown project "
                f"{thread['project_id']}"
            )

        thread_selection = ModelSelection(thread["model_selection_json"])
        if thread_selection.is_empty() and project is not None:
            thread_selection = ModelSelection(
                _row_get(project, "default_model_selection_json")
            )

        actors, per_message_sel, timeline, event_warnings = self._event_facts(
            conn, thread_id
        )
        warnings.extend(event_warnings)
        turn_messages = self._turn_message_ids(conn, thread_id)

        rows = conn.execute(
            """
            SELECT message_id, turn_id, role, text, is_streaming, created_at
            FROM projection_thread_messages
            WHERE thread_id = ?
            ORDER BY created_at ASC, message_id ASC
            """,
            (thread_id,),
        ).fetchall()

        messages: list[NormalizedMessage] = []
        seq_by_message_id: dict[str, int] = {}
        seq = 0
        for row in rows:
            role = str(row["role"] or "").strip().lower()
            if role not in KNOWN_ROLES:
                warnings.append(
                    f"t3code: unrecognized message role {role!r} on "
                    f"thread {thread_id}"
                )
                if not role:
                    continue
            message_id = str(row["message_id"])
            text = str(row["text"] or "")
            when = parse_ts(row["created_at"])

            selection = per_message_sel.get(message_id)
            if selection is None:
                selection = self._selection_at(timeline, when) or thread_selection

            # System rows are harness scaffolding and empty rows are streaming
            # placeholders; neither is a human turn.
            is_plumbing = role == "system" or not text.strip()

            seq += 1
            seq_by_message_id[message_id] = seq
            is_assistant = role == "assistant"
            messages.append(
                NormalizedMessage(
                    seq=seq,
                    role=role,
                    timestamp=when,
                    model=selection.model if is_assistant else None,
                    provider=selection.provider if is_assistant else None,
                    agent_profile=(
                        selection.agent_profile if is_assistant else None
                    ),
                    effort=selection.effort if is_assistant else None,
                    effort_source=(
                        selection.effort_source if is_assistant else None
                    ),
                    text=text,
                    content_hash=content_hash_text(text),
                    is_tool_plumbing=is_plumbing,
                    authored_by_agent=(
                        role == "user"
                        and not is_plumbing
                        and actors.get(message_id, "client") != "client"
                    ),
                )
            )

        tools, tool_warnings = self._tool_events(
            conn,
            thread_id,
            turn_messages=turn_messages,
            seq_by_message_id=seq_by_message_id,
            messages=messages,
            rows=rows,
        )
        warnings.extend(tool_warnings)

        if not messages and not tools:
            return None

        parent_thread = plan_parents.get(thread_id)
        if parent_thread:
            self._flag_plan_brief(messages)

        provider_row = provider_sessions.get(thread_id)
        provider_name = None
        agent_profile = thread_selection.agent_profile
        if provider_row is not None:
            instance = _row_get(provider_row, "provider_instance_id")
            if isinstance(instance, str) and instance.strip():
                agent_profile = instance.strip()
            raw_provider = _row_get(provider_row, "provider_name")
            if isinstance(raw_provider, str) and raw_provider.strip():
                provider_name = PROVIDER_INSTANCE_VENDORS.get(
                    raw_provider.strip().lower()
                )
        provider = provider_name or thread_selection.provider

        ended_at = parse_ts(thread["updated_at"])
        last_ts = next(
            (m.timestamp for m in reversed(messages) if m.timestamp is not None),
            None,
        )
        if ended_at is None:
            ended_at = last_ts

        workspace_root = (
            str(project["workspace_root"])
            if project is not None and project["workspace_root"]
            else None
        )
        worktree = thread["worktree_path"]

        return ParseResult(
            session=NormalizedSession(
                harness=Harness.T3CODE,
                external_id=thread_id,
                parent_session_id=parent_thread,
                started_at=parse_ts(thread["created_at"]),
                ended_at=ended_at,
                repo=workspace_root,
                cwd=str(worktree) if worktree else workspace_root,
                branch=str(thread["branch"]) if thread["branch"] else None,
                model=thread_selection.model,
                provider=provider,
                agent_profile=agent_profile,
                effort=thread_selection.effort,
                effort_source=thread_selection.effort_source,
            ),
            messages=messages,
            tool_events=tools,
            warnings=warnings,
            bytes_consumed=size,
            extras={
                "title": thread["title"],
                "runtime_mode": _row_get(thread, "runtime_mode"),
                "interaction_mode": _row_get(thread, "interaction_mode"),
                "project_id": thread["project_id"],
            },
        )

    @staticmethod
    def _flag_plan_brief(messages: list[NormalizedMessage]) -> None:
        """A plan-implementation thread is seeded by the orchestrator, not a human."""
        for msg in messages:
            if msg.role == "assistant":
                return
            if msg.role == "user" and not msg.is_tool_plumbing:
                msg.authored_by_agent = True
                return

    def _tool_events(
        self,
        conn: sqlite3.Connection,
        thread_id: str,
        *,
        turn_messages: dict[str, str],
        seq_by_message_id: dict[str, int],
        messages: list[NormalizedMessage],
        rows: list[sqlite3.Row],
    ) -> tuple[list[ToolEvent], list[str]]:
        if not table_exists(conn, "projection_thread_activities"):
            return [], []

        warnings: list[str] = []
        # Fallback linkage: attach an activity to the message it followed.
        timeline: list[tuple[Any, int]] = []
        for row in rows:
            message_id = str(row["message_id"])
            when = parse_ts(row["created_at"])
            seq = seq_by_message_id.get(message_id)
            if seq is not None and when is not None:
                timeline.append((when, seq))

        tools: list[ToolEvent] = []
        tool_seq = 0
        for row in conn.execute(
            """
            SELECT activity_id, turn_id, tone, kind, summary, payload_json,
                   created_at, sequence
            FROM projection_thread_activities
            WHERE thread_id = ?
            ORDER BY COALESCE(sequence, 0) ASC, created_at ASC, activity_id ASC
            """,
            (thread_id,),
        ):
            tone = str(row["tone"] or "").strip().lower()
            kind = str(row["kind"] or "").strip()
            if tone and tone not in {"info", "tool", "approval", "error"}:
                warnings.append(
                    f"t3code: unrecognized activity tone {tone!r} on "
                    f"thread {thread_id}"
                )
            if tone not in _TOOL_TONES and tone != "error":
                continue

            payload = _json_obj(row["payload_json"])
            if payload is None and row["payload_json"]:
                warnings.append(
                    f"t3code: unparseable activity payload on thread {thread_id} "
                    f"(kind {kind!r})"
                )

            message_seq = None
            turn_id = row["turn_id"]
            if turn_id:
                target = turn_messages.get(str(turn_id))
                if target:
                    message_seq = seq_by_message_id.get(target)
            if message_seq is None:
                message_seq = self._nearest_message_seq(
                    timeline, parse_ts(row["created_at"])
                )
            if message_seq is None and messages:
                message_seq = messages[-1].seq

            tool_seq += 1
            tools.append(
                ToolEvent(
                    seq=tool_seq,
                    message_seq=message_seq,
                    tool_name=_tool_name_from(
                        payload, kind, str(row["summary"] or "")
                    ),
                    action=_action_for(kind, tone),
                    success=False if tone == "error" else None,
                )
            )
        return tools, warnings

    @staticmethod
    def _nearest_message_seq(
        timeline: list[tuple[Any, int]], when: Any
    ) -> int | None:
        if not timeline:
            return None
        if when is None:
            return timeline[-1][1]
        chosen = None
        for occurred_at, seq in timeline:
            if occurred_at > when:
                break
            chosen = seq
        return chosen if chosen is not None else timeline[0][1]
