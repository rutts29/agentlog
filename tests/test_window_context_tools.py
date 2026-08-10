from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.extractors.window_context import load_window_contexts
from agentlog.db.schema import connect, init_db


def _seed_session(conn: sqlite3.Connection, session_id: str, harness: str) -> None:
    conn.execute(
        """
        INSERT INTO sessions (id, harness, external_id, model)
        VALUES (?, ?, ?, 'gpt-5')
        """,
        (session_id, harness, session_id.split(":", 1)[-1]),
    )


def _msg(
    conn: sqlite3.Connection,
    session_id: str,
    seq: int,
    role: str,
    text: str,
    *,
    plumbing: int = 0,
) -> str:
    mid = f"{session_id}#m{seq}"
    conn.execute(
        """
        INSERT INTO messages (id, session_id, seq, role, text, is_tool_plumbing)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (mid, session_id, seq, role, text, plumbing),
    )
    return mid


def _tool(
    conn: sqlite3.Connection,
    session_id: str,
    seq: int,
    name: str,
    *,
    message_id: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO tool_events (id, session_id, message_id, seq, tool_name, action)
        VALUES (?, ?, ?, ?, ?, 'call')
        """,
        (f"{session_id}#t{seq}", session_id, message_id, seq, name),
    )


def _window(
    conn: sqlite3.Connection, session_id: str, wid: str, req: str, resp: str
) -> None:
    conn.execute(
        """
        INSERT INTO exchange_windows (
            id, session_id, request_message_id, response_message_id,
            input_hash, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (wid, session_id, req, resp, wid, wid),
    )


class WindowToolLinkageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "a.db")
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_tool_seq_below_message_seq_is_still_included(self) -> None:
        """Codex links tools to high message seqs while tool seq restarts at 1."""
        sid = "codex:s1"
        _seed_session(self.conn, sid, "codex")
        _msg(self.conn, sid, 10, "user", "do the thing")
        resp = _msg(self.conn, sid, 11, "assistant", "done")
        _tool(self.conn, sid, 1, "shell", message_id=resp)
        _tool(self.conn, sid, 2, "read_file", message_id=resp)
        _window(self.conn, sid, "w1", f"{sid}#m10", resp)
        self.conn.commit()

        ctx = load_window_contexts(self.conn)[0]
        self.assertEqual(ctx.tool_count, 2)
        self.assertEqual(ctx.tool_timeline, ["shell|call|?", "read_file|call|?"])

    def test_tools_of_later_window_are_excluded(self) -> None:
        sid = "codex:s2"
        _seed_session(self.conn, sid, "codex")
        req1 = _msg(self.conn, sid, 1, "user", "first ask")
        a1 = _msg(self.conn, sid, 2, "assistant", "first answer")
        req2 = _msg(self.conn, sid, 3, "user", "second ask")
        a2 = _msg(self.conn, sid, 4, "assistant", "second answer")
        _tool(self.conn, sid, 1, "shell", message_id=a1)
        _tool(self.conn, sid, 2, "grep", message_id=a2)
        _window(self.conn, sid, "w1", req1, a1)
        _window(self.conn, sid, "w2", req2, a2)
        self.conn.commit()

        by_id = {c.window_id: c for c in load_window_contexts(self.conn)}
        self.assertEqual(by_id["w1"].tool_timeline, ["shell|call|?"])
        self.assertEqual(by_id["w2"].tool_timeline, ["grep|call|?"])

    def test_orphan_tools_bounded_by_linked_neighbours(self) -> None:
        sid = "codex:s3"
        _seed_session(self.conn, sid, "codex")
        req1 = _msg(self.conn, sid, 1, "user", "first ask")
        a1 = _msg(self.conn, sid, 2, "assistant", "first answer")
        req2 = _msg(self.conn, sid, 3, "user", "second ask")
        a2 = _msg(self.conn, sid, 4, "assistant", "second answer")
        _tool(self.conn, sid, 1, "linked_early", message_id=a1)
        _tool(self.conn, sid, 2, "orphan_mid", message_id=None)
        _tool(self.conn, sid, 3, "linked_late", message_id=a2)
        _tool(self.conn, sid, 4, "orphan_late", message_id=None)
        _window(self.conn, sid, "w1", req1, a1)
        _window(self.conn, sid, "w2", req2, a2)
        self.conn.commit()

        by_id = {c.window_id: c for c in load_window_contexts(self.conn)}
        self.assertEqual(
            [t.split("|")[0] for t in by_id["w1"].tool_timeline],
            ["linked_early", "orphan_mid"],
        )
        self.assertEqual(
            [t.split("|")[0] for t in by_id["w2"].tool_timeline],
            ["linked_late", "orphan_late"],
        )

    def test_fully_unlinked_session_falls_back_to_seq_window(self) -> None:
        sid = "claude:s4"
        _seed_session(self.conn, sid, "claude")
        req = _msg(self.conn, sid, 1, "user", "ask")
        resp = _msg(self.conn, sid, 2, "assistant", "answer")
        _msg(self.conn, sid, 4, "user", "next ask")
        _tool(self.conn, sid, 2, "inside", message_id=None)
        _tool(self.conn, sid, 5, "outside", message_id=None)
        _window(self.conn, sid, "w1", req, resp)
        self.conn.commit()

        ctx = load_window_contexts(self.conn)[0]
        self.assertEqual(
            [t.split("|")[0] for t in ctx.tool_timeline], ["inside"]
        )


if __name__ == "__main__":
    unittest.main()
