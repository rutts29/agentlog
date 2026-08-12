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

import hashlib
import json
import logging
import re
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
from agentlog.normalize.synthetic import (
    flag_synthetic_user_messages,
    synthetic_skill_exposures,
)
from agentlog.normalize.tool_ops import classify_operation

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

_PROVIDER_FAMILIES = {
    "codex": "codex",
    "openai": "codex",
    "cursor": "cursor",
    "claude": "anthropic",
    "claudeagent": "anthropic",
    "anthropic": "anthropic",
    "grok": "xai",
    "xai": "xai",
    "google": "google",
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

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
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


def _provider_family(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip().lower()
    return _PROVIDER_FAMILIES.get(value, value)


def _provider_from_mapping(data: dict[str, Any]) -> str | None:
    for key in (
        "provider",
        "providerName",
        "provider_name",
        "providerInstanceId",
        "provider_instance_id",
        "modelProvider",
        "model_provider",
        "instanceId",
        "instance_id",
    ):
        family = _provider_family(data.get(key))
        if family is not None:
            return family
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
            values = (options.get("effort"), options.get("reasoningEffort"))
            for value in values:
                if isinstance(value, str) and value.strip():
                    raw_effort = value.strip()
                    break
        elif isinstance(options, list):
            for option in options:
                if not isinstance(option, dict):
                    continue
                option_id = str(option.get("id") or "").strip().lower()
                if option_id not in {"effort", "reasoningeffort"}:
                    continue
                value = option.get("value")
                if isinstance(value, str) and value.strip():
                    raw_effort = value.strip()
                    break
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

    def parse_session(
        self, path: Path, external_id: str
    ) -> ParseResult | None:
        """Read one thread from a coherent read transaction.

        ``parse_path`` is intentionally still the full-artifact ingest path.
        Detail views only need one thread, so avoid traversing every thread in
        the shared T3 projection while another thread is being written.
        """
        result, _source_hash = self.parse_session_with_hash(path, external_id)
        return result

    def parse_session_with_hash(
        self, path: Path, external_id: str
    ) -> tuple[ParseResult | None, str]:
        """Read one thread and fingerprint only its source-backed content."""
        size = path.stat().st_size if path.is_file() else 0
        with open_sqlite_readonly(path) as conn:
            conn.execute("BEGIN")
            try:
                result = self.parse_session_connection(
                    conn, external_id, size=size
                )
                if result is None:
                    return None, "missing"
                payload = result.model_dump(
                    mode="json", exclude={"bytes_consumed"}
                )
                encoded = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                return result, hashlib.sha256(encoded).hexdigest()
            finally:
                conn.rollback()

    def parse_session_connection(
        self,
        conn: sqlite3.Connection,
        external_id: str,
        *,
        size: int = 0,
    ) -> ParseResult | None:
        """Parse one thread using the caller-owned coherent SQLite snapshot."""
        if not table_exists(conn, "projection_threads"):
            return None
        thread = conn.execute(
            """
            SELECT thread_id, project_id, title, branch, worktree_path,
                   created_at, updated_at, deleted_at, model_selection_json,
                   runtime_mode, interaction_mode
            FROM projection_threads
            WHERE thread_id = ?
            """,
            (external_id,),
        ).fetchone()
        if thread is None:
            return None

        project = None
        if table_exists(conn, "projection_projects"):
            project = conn.execute(
                """
                SELECT project_id, title, workspace_root,
                       default_model_selection_json
                FROM projection_projects
                WHERE project_id = ?
                """,
                (thread["project_id"],),
            ).fetchone()
        projects = {str(project["project_id"]): project} if project else {}

        provider_sessions: dict[str, sqlite3.Row] = {}
        if table_exists(conn, "projection_thread_sessions"):
            provider = conn.execute(
                """
                SELECT thread_id, provider_name, provider_instance_id,
                       provider_session_id, provider_thread_id, status
                FROM projection_thread_sessions
                WHERE thread_id = ?
                """,
                (external_id,),
            ).fetchone()
            if provider is not None:
                provider_sessions[external_id] = provider

        plan_parents: dict[str, str] = {}
        if table_exists(conn, "projection_thread_proposed_plans"):
            plan = conn.execute(
                """
                SELECT thread_id, implementation_thread_id
                FROM projection_thread_proposed_plans
                WHERE implementation_thread_id = ?
                LIMIT 1
                """,
                (external_id,),
            ).fetchone()
            if plan is not None and plan["thread_id"]:
                plan_parents[external_id] = str(plan["thread_id"])
        if table_exists(conn, "projection_turns"):
            turn = conn.execute(
                """
                SELECT thread_id, source_proposed_plan_thread_id
                FROM projection_turns
                WHERE thread_id = ?
                  AND source_proposed_plan_thread_id IS NOT NULL
                LIMIT 1
                """,
                (external_id,),
            ).fetchone()
            if turn is not None and turn["source_proposed_plan_thread_id"]:
                plan_parents.setdefault(
                    external_id, str(turn["source_proposed_plan_thread_id"])
                )

        return self._parse_thread(
            conn,
            thread,
            projects=projects,
            provider_sessions=provider_sessions,
            plan_parents=plan_parents,
            size=size,
        )

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
        provider_backings = self._provider_backings(conn, thread_id)

        if not messages and not tools:
            return None

        parent_thread = plan_parents.get(thread_id)
        if parent_thread:
            self._flag_plan_brief(messages)
        flag_synthetic_user_messages(messages)
        skills = synthetic_skill_exposures(messages)

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
            skill_exposures=skills,
            warnings=warnings,
            bytes_consumed=size,
            extras={
                "title": thread["title"],
                "runtime_mode": _row_get(thread, "runtime_mode"),
                "interaction_mode": _row_get(thread, "interaction_mode"),
                "project_id": thread["project_id"],
                "session_links": provider_backings,
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
                    operation_kind=classify_operation(
                        _tool_name_from(payload, kind, str(row["summary"] or ""))
                    ),
                )
            )
        return tools, warnings

    def _provider_backings(
        self, conn: sqlite3.Connection, thread_id: str
    ) -> list[dict[str, Any]]:
        """Recover Codex session ids emitted by T3 task orchestration events."""
        if not table_exists(conn, "projection_thread_activities"):
            return []

        def established_provider(table: str) -> str | bool | None:
            if not table_exists(conn, table):
                return None
            columns = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})")
            }
            selected = [
                column
                for column in ("provider_name", "provider_instance_id")
                if column in columns
            ]
            if not selected:
                return None
            row = conn.execute(
                f"SELECT {', '.join(selected)} FROM {table} WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                return None
            values = {
                family
                for column in selected
                for family in [_provider_family(row[column])]
                if family is not None
            }
            if not values:
                return None
            return next(iter(values)) if len(values) == 1 else False

        # Runtime state is authoritative when no historical activity evidence
        # exists. Activity timestamps below can still recover an earlier Codex
        # provider after the thread has fallen back to another provider.
        current_provider: str | None = None
        for table in ("provider_session_runtime", "projection_thread_sessions"):
            provider_state = established_provider(table)
            if provider_state is not None:
                current_provider = provider_state if isinstance(provider_state, str) else None
                break
        else:
            thread_row = conn.execute(
                "SELECT model_selection_json FROM projection_threads "
                "WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            selection = ModelSelection(
                thread_row["model_selection_json"] if thread_row is not None else None
            )
            current_provider = _provider_family(selection.instance_id)

        provider_history: list[tuple[Any, str]] = []
        if table_exists(conn, "orchestration_events"):
            for event in conn.execute(
                "SELECT occurred_at, payload_json "
                "FROM orchestration_events WHERE aggregate_kind = 'thread' "
                "AND stream_id = ? ORDER BY occurred_at, stream_version",
                (thread_id,),
            ):
                payload = _json_obj(event["payload_json"])
                if payload is None:
                    continue
                selection = payload.get("modelSelection")
                if not isinstance(selection, dict):
                    selection = payload.get("model_selection")
                if isinstance(selection, dict):
                    provider = _provider_from_mapping(selection)
                else:
                    provider = _provider_from_mapping(payload)
                occurred_at = parse_ts(event["occurred_at"])
                if provider is not None and occurred_at is not None:
                    provider_history.append((occurred_at, provider))

        def activity_provider(
            payload: dict[str, Any], created_at: object
        ) -> str | None:
            explicit = _provider_from_mapping(payload)
            if explicit is None:
                data = payload.get("data")
                if isinstance(data, dict):
                    explicit = _provider_from_mapping(data)
            if explicit is not None:
                return explicit
            when = parse_ts(created_at)
            historical = None
            if when is not None:
                for occurred_at, provider in provider_history:
                    if occurred_at > when:
                        break
                    historical = provider
            if historical is None or current_provider is None:
                return historical or current_provider
            if historical == current_provider:
                return historical
            if current_provider == "codex" and historical not in {"codex", "xai"}:
                return current_provider
            latest_event = provider_history[-1] if provider_history else None
            if latest_event is not None and when is not None:
                latest_at, latest_provider = latest_event
                if latest_provider != current_provider and latest_at <= when:
                    return current_provider
            return historical
        known_thread_ids = {
            str(row[0])
            for row in conn.execute("SELECT thread_id FROM projection_threads")
        }
        evidence_by_id: dict[str, list[tuple[str, dict[str, Any]]]] = {}

        def add(
            raw: object,
            activity_id: object,
            field: str,
            link_role: str,
        ) -> None:
            if not isinstance(raw, str):
                return
            value = raw.strip()
            if (
                not _UUID_RE.fullmatch(value)
                or value == thread_id
                or value in known_thread_ids
            ):
                return
            evidence_by_id.setdefault(value, []).append(
                (
                    link_role,
                    {
                        "source": "t3code.projection_thread_activities",
                        "activity_id": str(activity_id),
                        "field": field,
                    },
                )
            )

        for row in conn.execute(
            "SELECT activity_id, payload_json, created_at "
            "FROM projection_thread_activities WHERE thread_id = ? "
            "ORDER BY COALESCE(sequence, 0), created_at, activity_id",
            (thread_id,),
        ):
            payload = _json_obj(row["payload_json"])
            if payload is None:
                continue
            if activity_provider(payload, row["created_at"]) != "codex":
                continue
            # T3 task ids are the provider CLI session ids in Codex's JSONL
            # session_meta; senderThreadId is the equivalent collab bridge id.
            add(
                payload.get("taskId"),
                row["activity_id"],
                "taskId",
                "worker",
            )
            add(
                payload.get("senderThreadId"),
                row["activity_id"],
                "senderThreadId",
                "root",
            )
            data = payload.get("data")
            if isinstance(data, dict):
                add(
                    data.get("threadId"),
                    row["activity_id"],
                    "data.threadId",
                    "root",
                )
            receiver_ids = payload.get("receiverThreadIds")
            if isinstance(receiver_ids, list):
                for idx, value in enumerate(receiver_ids):
                    add(
                        value,
                        row["activity_id"],
                        f"receiverThreadIds[{idx}]",
                        "worker",
                    )
        links: list[dict[str, Any]] = []
        for value, evidence in evidence_by_id.items():
            workers = [item for item in evidence if item[0] == "worker"]
            role, selected_evidence = (workers or evidence)[0]
            links.append(
                {
                    "link_type": "provider_backing",
                    "target_harness": "codex",
                    "target_external_id": value,
                    "link_role": role,
                    "evidence": selected_evidence,
                }
            )
        return links

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
