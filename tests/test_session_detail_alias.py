from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentlog.api.descriptive import session_detail_v2
from agentlog.db.schema import connect, init_db


def _seed(
    conn: sqlite3.Connection,
    session_id: str,
    harness: str,
    external_id: str,
    *,
    parent: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sessions (id, harness, external_id, parent_session_id, model)
        VALUES (?, ?, ?, ?, 'gpt-5')
        """,
        (session_id, harness, external_id, parent),
    )
    req = f"{session_id}#m1"
    resp = f"{session_id}#m2"
    conn.execute(
        "INSERT INTO messages (id, session_id, seq, role, text) "
        "VALUES (?, ?, 1, 'user', 'ask something')",
        (req, session_id),
    )
    conn.execute(
        "INSERT INTO messages (id, session_id, seq, role, text) "
        "VALUES (?, ?, 2, 'assistant', 'an answer')",
        (resp, session_id),
    )
    conn.execute(
        """
        INSERT INTO tool_events (id, session_id, message_id, seq, tool_name, action)
        VALUES (?, ?, ?, 1, 'shell', 'call')
        """,
        (f"{session_id}#t1", session_id, resp),
    )
    conn.execute(
        """
        INSERT INTO skill_exposures (id, session_id, message_id, skill_name, exposure_type)
        VALUES (?, ?, ?, 'review', 'matched')
        """,
        (f"{session_id}#k1", session_id, req),
    )
    conn.execute(
        """
        INSERT INTO exchange_windows (
            id, session_id, request_message_id, response_message_id,
            input_hash, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (f"{session_id}#w1", session_id, req, resp, "h", "h"),
    )


class SessionDetailAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "a.db")
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _assert_full(self, detail: dict | None, session_id: str) -> None:
        assert detail is not None
        self.assertEqual(detail["session"]["id"], session_id)
        self.assertEqual(len(detail["messages"]), 2)
        self.assertEqual(len(detail["tool_events"]), 1)
        self.assertEqual(len(detail["skills"]), 1)
        self.assertEqual(detail["anatomy"]["message_count"], 2)
        self.assertEqual(detail["anatomy"]["tool_count"], 1)
        self.assertEqual(detail["anatomy"]["window_count"], 1)

    def test_canonical_and_external_id_agree(self) -> None:
        _seed(self.conn, "codex:abc", "codex", "abc")
        self.conn.commit()
        canonical = session_detail_v2(self.conn, "codex:abc")
        alias = session_detail_v2(self.conn, "abc")
        self._assert_full(canonical, "codex:abc")
        self._assert_full(alias, "codex:abc")
        self.assertEqual(canonical, alias)

    def test_cursor_id_with_slashes(self) -> None:
        sid = "cursor:Users-me-proj/9f1c-2b"
        _seed(self.conn, sid, "cursor", "Users-me-proj/9f1c-2b")
        self.conn.commit()
        self._assert_full(session_detail_v2(self.conn, sid), sid)
        self._assert_full(session_detail_v2(self.conn, "9f1c-2b"), sid)
        self._assert_full(
            session_detail_v2(self.conn, "Users-me-proj/9f1c-2b"), sid
        )

    def test_children_resolve_for_alias_lookup(self) -> None:
        _seed(self.conn, "codex:root", "codex", "root")
        _seed(self.conn, "codex:kid", "codex", "kid", parent="codex:root")
        self.conn.commit()
        alias = session_detail_v2(self.conn, "root")
        assert alias is not None
        self.assertEqual(alias["anatomy"]["child_count"], 1)
        self.assertEqual(alias["children"][0]["id"], "codex:kid")

    def test_unknown_id_returns_none(self) -> None:
        self.assertIsNone(session_detail_v2(self.conn, "nope"))


if __name__ == "__main__":
    unittest.main()
