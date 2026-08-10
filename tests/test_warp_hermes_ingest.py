from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentlog.ingest.hermes import HermesAdapter
from agentlog.ingest.warp import WarpAdapter


def _write_warp_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE ai_queries (
          id INTEGER PRIMARY KEY NOT NULL,
          exchange_id TEXT NOT NULL,
          conversation_id TEXT NOT NULL,
          start_ts DATETIME NOT NULL,
          input TEXT NOT NULL,
          working_directory TEXT,
          output_status TEXT NOT NULL,
          model_id TEXT NOT NULL DEFAULT '',
          planning_model_id TEXT NOT NULL DEFAULT '',
          coding_model_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE ai_blocks (
          id INTEGER PRIMARY KEY NOT NULL,
          exchange_id TEXT NOT NULL,
          pane_leaf_uuid BLOB NOT NULL,
          output TEXT NOT NULL
        );
        """
    )
    q1 = json.dumps(
        [
            {
                "Query": {
                    "text": "install conda please",
                    "context": [
                        {"Directory": {"pwd": "/tmp/proj", "home_dir": "/tmp"}}
                    ],
                }
            }
        ]
    )
    q2 = json.dumps(
        [
            {
                "ActionResult": {
                    "id": "a1",
                    "result": {"RequestCommandOutput": {"ok": True}},
                }
            }
        ]
    )
    q3 = json.dumps([{"Query": {"text": "list packages"}}])
    conn.execute(
        """
        INSERT INTO ai_queries
        (exchange_id, conversation_id, start_ts, input, working_directory,
         output_status, model_id)
        VALUES
        ('e1', 'conv-1', '2025-06-23 21:00:00', ?, '/tmp/proj', 'Completed', 'auto'),
        ('e2', 'conv-1', '2025-06-23 21:01:00', ?, '/tmp/proj', 'Completed', 'auto'),
        ('e3', 'conv-2', '2025-06-24 10:00:00', ?, '/home/u', 'Completed', 'gpt-4.1')
        """,
        (q1, q2, q3),
    )
    conn.commit()
    conn.close()


def _write_hermes_state(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
          id TEXT PRIMARY KEY,
          source TEXT NOT NULL,
          parent_session_id TEXT,
          started_at REAL NOT NULL,
          ended_at REAL,
          model TEXT,
          model_config TEXT,
          cwd TEXT,
          git_branch TEXT,
          git_repo_root TEXT,
          title TEXT
        );
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL,
          role TEXT NOT NULL,
          content TEXT,
          tool_call_id TEXT,
          tool_calls TEXT,
          tool_name TEXT,
          timestamp REAL NOT NULL,
          token_count INTEGER,
          reasoning TEXT,
          reasoning_content TEXT,
          api_content TEXT,
          active INTEGER NOT NULL DEFAULT 1,
          compacted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE session_model_usage (
          session_id TEXT NOT NULL,
          model TEXT NOT NULL,
          billing_provider TEXT NOT NULL DEFAULT '',
          billing_base_url TEXT NOT NULL DEFAULT '',
          billing_mode TEXT NOT NULL DEFAULT '',
          task TEXT NOT NULL DEFAULT '',
          input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,
          cache_read_tokens INTEGER NOT NULL DEFAULT 0,
          cache_write_tokens INTEGER NOT NULL DEFAULT 0,
          reasoning_tokens INTEGER NOT NULL DEFAULT 0,
          last_seen REAL,
          PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
        );
        """
    )
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, parent_session_id, started_at, ended_at, model, model_config,
         cwd, git_branch, git_repo_root, title)
        VALUES
        ('sess-a', 'cli', NULL, 1710000000.0, 1710000100.0, 'gpt-5',
         '{"reasoning_effort":"high"}', '/repo', 'main', '/repo', 'demo'),
        ('sess-b', 'cli', 'sess-a', 1710000200.0, NULL, 'gpt-5', NULL,
         '/repo', 'feat', '/repo', 'child')
        """
    )
    tool_calls = json.dumps([{"name": "terminal", "arguments": {"cmd": "ls"}}])
    conn.execute(
        """
        INSERT INTO messages
        (session_id, role, content, tool_calls, tool_name, timestamp, token_count, active)
        VALUES
        ('sess-a', 'user', 'hello hermes', NULL, NULL, 1710000001.0, NULL, 1),
        ('sess-a', 'assistant', 'running ls', ?, NULL, 1710000002.0, 42, 1),
        ('sess-a', 'tool', 'file.txt', NULL, 'terminal', 1710000003.0, NULL, 1),
        ('sess-b', 'user', 'follow up', NULL, NULL, 1710000201.0, NULL, 1),
        ('sess-b', 'assistant', 'ok', NULL, NULL, 1710000202.0, NULL, 0)
        """,
        (tool_calls,),
    )
    conn.execute(
        """
        INSERT INTO session_model_usage
        (session_id, model, input_tokens, output_tokens, last_seen)
        VALUES ('sess-a', 'gpt-5', 100, 50, 1710000100.0)
        """
    )
    conn.commit()
    conn.close()


def _write_hermes_kanban(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          body TEXT,
          assignee TEXT,
          status TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          started_at INTEGER,
          completed_at INTEGER,
          workspace_path TEXT,
          branch_name TEXT,
          model_override TEXT,
          reasoning_effort TEXT,
          session_id TEXT,
          result TEXT
        );
        CREATE TABLE task_comments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id TEXT NOT NULL,
          author TEXT NOT NULL,
          body TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO tasks
        (id, title, body, status, created_at, started_at, completed_at,
         workspace_path, branch_name, model_override, reasoning_effort,
         session_id, result)
        VALUES
        ('task-1', 'Fix login', 'Check oauth flow', 'done',
         1710001000, 1710001001, 1710002000,
         '/app', 'fix/login', 'gpt-5', 'medium', 'sess-a', 'shipped')
        """
    )
    conn.execute(
        """
        INSERT INTO task_comments (task_id, author, body, created_at)
        VALUES
        ('task-1', 'human', 'please prioritize', 1710001100),
        ('task-1', 'hermes-worker', 'patched callback', 1710001500)
        """
    )
    conn.commit()
    conn.close()


class WarpAdapterTests(unittest.TestCase):
    def test_discovers_and_parses_conversations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "warp.sqlite"
            _write_warp_db(db)
            with mock.patch("agentlog.ingest.warp.WARP_SQLITE", db):
                adapter = WarpAdapter()
                found = adapter.discover()
                self.assertEqual(found, [db])
                results = adapter.parse_path(db, b"", start_offset=0)

        self.assertEqual(len(results), 2)
        by_id = {r.session.external_id: r for r in results}
        self.assertIn("conv-1", by_id)
        self.assertIn("conv-2", by_id)
        c1 = by_id["conv-1"]
        self.assertEqual(c1.session.harness.value, "warp")
        self.assertEqual(c1.session.cwd, "/tmp/proj")
        self.assertEqual([m.role for m in c1.messages], ["user"])
        self.assertEqual(c1.messages[0].text, "install conda please")
        self.assertEqual(len(c1.tool_events), 1)
        self.assertEqual(c1.tool_events[0].tool_name, "RequestCommandOutput")
        self.assertTrue(
            any("ai_blocks empty" in w for w in results[0].warnings)
        )
        c2 = by_id["conv-2"]
        self.assertEqual(c2.session.model, "gpt-4.1")
        self.assertEqual(c2.messages[0].text, "list packages")


class HermesAdapterTests(unittest.TestCase):
    def test_state_db_sessions_messages_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            home.mkdir()
            state = home / "state.db"
            _write_hermes_state(state)
            with mock.patch.multiple(
                "agentlog.ingest.hermes",
                HERMES_HOME=home,
                HERMES_STATE_DB=state,
                HERMES_KANBAN_DB=home / "kanban.db",
            ):
                adapter = HermesAdapter()
                self.assertEqual(adapter.discover(), [state])
                results = adapter.parse_path(state, b"", start_offset=0)

        self.assertEqual(len(results), 2)
        by_id = {r.session.external_id: r for r in results}
        a = by_id["sess-a"]
        self.assertEqual(a.session.branch, "main")
        self.assertEqual(a.session.effort, "high")
        self.assertEqual([m.role for m in a.messages], ["user", "assistant", "tool"])
        self.assertEqual(a.messages[1].model, "gpt-5")
        self.assertTrue(a.messages[2].is_tool_plumbing)
        self.assertEqual(
            [t.action for t in a.tool_events], ["call", "result"]
        )
        self.assertTrue(a.token_usages)
        sources = {u.usage_source for u in a.token_usages}
        self.assertIn("hermes_message_token_count", sources)
        self.assertIn("hermes_session_model_usage", sources)

        b = by_id["sess-b"]
        self.assertEqual(b.session.parent_session_id, "sess-a")
        # inactive assistant row skipped; only user remains
        self.assertEqual([m.role for m in b.messages], ["user"])

    def test_kanban_db_tasks_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            home.mkdir()
            kanban = home / "kanban.db"
            _write_hermes_kanban(kanban)
            with mock.patch.multiple(
                "agentlog.ingest.hermes",
                HERMES_HOME=home,
                HERMES_STATE_DB=home / "state.db",
                HERMES_KANBAN_DB=kanban,
            ):
                adapter = HermesAdapter()
                self.assertEqual(adapter.discover(), [kanban])
                results = adapter.parse_path(kanban, b"", start_offset=0)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r.session.external_id, "kanban:default:task-1")
        self.assertEqual(r.session.parent_session_id, "sess-a")
        self.assertEqual(r.session.model, "gpt-5")
        self.assertEqual(r.session.effort, "medium")
        texts = [m.text for m in r.messages]
        self.assertTrue(texts[0].startswith("Fix login"))
        self.assertIn("please prioritize", texts)
        self.assertIn("patched callback", texts)
        self.assertEqual(texts[-1], "shipped")
        roles = [m.role for m in r.messages]
        self.assertEqual(roles[0], "user")
        self.assertIn("assistant", roles)


if __name__ == "__main__":
    unittest.main()
