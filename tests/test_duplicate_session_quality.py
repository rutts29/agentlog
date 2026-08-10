from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.ingest.base import content_hash_text
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
    SkillExposure,
    TokenUsage,
    ToolEvent,
)

TS = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
TURNS = [("user", "port the parser"), ("assistant", "ported it")]


def _messages(*, rich: bool) -> list[NormalizedMessage]:
    out: list[NormalizedMessage] = []
    for i, (role, text) in enumerate(TURNS, start=1):
        out.append(
            NormalizedMessage(
                seq=i,
                role=role,
                text=text,
                content_hash=content_hash_text(text),
                timestamp=TS if rich else None,
                model="gpt-5-codex" if (rich and role == "assistant") else None,
                effort="high" if (rich and role == "assistant") else None,
            )
        )
    return out


def _result(external_id: str, *, rich: bool) -> ParseResult:
    session = NormalizedSession(
        harness=Harness.CURSOR,
        external_id=external_id,
        started_at=TS if rich else None,
        ended_at=TS if rich else None,
        repo="github.com/me/proj" if rich else None,
        cwd="/Users/me/proj" if rich else None,
        branch="main" if rich else None,
        model="gpt-5-codex" if rich else None,
        parent_session_id="cursor:parent" if rich else None,
    )
    return ParseResult(
        session=session,
        messages=_messages(rich=rich),
        tool_events=(
            [
                ToolEvent(seq=1, message_seq=2, tool_name="shell", action="call"),
                ToolEvent(
                    seq=2, message_seq=2, tool_name="shell", action="result",
                    success=True,
                ),
            ]
            if rich
            else []
        ),
        skill_exposures=(
            [SkillExposure(message_seq=1, skill_name="review", exposure_type="matched")]
            if rich
            else []
        ),
        token_usages=(
            [
                TokenUsage(
                    seq=1,
                    granularity="turn",
                    usage_source="test",
                    input_tokens=10,
                    output_tokens=5,
                )
            ]
            if rich
            else []
        ),
        bytes_consumed=1,
    )


class EqualLengthDuplicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "a.db")
        init_db(self.conn)
        self.repo = Repository(self.conn)
        self.artifact = self.repo.upsert_artifact(
            harness="cursor",
            path="/tmp/a.jsonl",
            size=1,
            mtime_ns=1,
            content_hash="h",
            parsed_offset=1,
            parser_version="v1",
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _save(self, result: ParseResult) -> str:
        return self.repo.save_parse_result(
            artifact_id=self.artifact, result=result, append=False
        )

    def _snapshot(self, session_id: str) -> dict[str, object]:
        c = self.conn
        row = c.execute(
            "SELECT repo, cwd, branch, model, parent_session_id, started_at "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return {
            "messages": c.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"],
            "tools": c.execute(
                "SELECT COUNT(*) AS c FROM tool_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"],
            "usage": c.execute(
                "SELECT COUNT(*) AS c FROM token_usage WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"],
            "skills": c.execute(
                "SELECT COUNT(*) AS c FROM skill_exposures WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"],
            "msg_models": c.execute(
                "SELECT COUNT(*) AS c FROM messages "
                "WHERE session_id = ? AND COALESCE(model, '') != ''",
                (session_id,),
            ).fetchone()["c"],
            "msg_timestamps": c.execute(
                "SELECT COUNT(*) AS c FROM messages "
                "WHERE session_id = ? AND COALESCE(timestamp, '') != ''",
                (session_id,),
            ).fetchone()["c"],
            "session": dict(row) if row is not None else {},
        }

    def test_poorer_equal_length_copy_never_replaces_richer(self) -> None:
        sid = self._save(_result("dup", rich=True))
        before = self._snapshot(sid)
        self._save(_result("dup", rich=False))
        after = self._snapshot(sid)
        self.assertEqual(before, after)
        self.assertEqual(after["tools"], 2)
        self.assertEqual(after["msg_models"], 1)

    def test_ingest_order_does_not_change_stored_data(self) -> None:
        sid = self._save(_result("dup", rich=True))
        self._save(_result("dup", rich=False))
        rich_first = self._snapshot(sid)

        self.conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        self.conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        self.conn.execute("DELETE FROM tool_events WHERE session_id = ?", (sid,))
        self.conn.execute("DELETE FROM token_usage WHERE session_id = ?", (sid,))
        self.conn.execute("DELETE FROM skill_exposures WHERE session_id = ?", (sid,))

        self._save(_result("dup", rich=False))
        self._save(_result("dup", rich=True))
        poor_first = self._snapshot(sid)
        self.assertEqual(rich_first, poor_first)

    def test_richer_equal_length_copy_still_replaces(self) -> None:
        sid = self._save(_result("dup", rich=False))
        self.assertEqual(self._snapshot(sid)["tools"], 0)
        self._save(_result("dup", rich=True))
        after = self._snapshot(sid)
        self.assertEqual(after["tools"], 2)
        self.assertEqual(after["usage"], 1)
        self.assertEqual(after["skills"], 1)
        self.assertEqual(after["session"]["repo"], "github.com/me/proj")

    def test_changed_turns_still_replace(self) -> None:
        sid = self._save(_result("dup", rich=True))
        edited = _result("dup", rich=False)
        edited.messages[1].text = "ported it, with fixes"
        edited.messages[1].content_hash = content_hash_text("ported it, with fixes")
        self._save(edited)
        text = self.conn.execute(
            "SELECT text FROM messages WHERE session_id = ? AND seq = 2",
            (sid,),
        ).fetchone()["text"]
        self.assertEqual(text, "ported it, with fixes")


if __name__ == "__main__":
    unittest.main()
