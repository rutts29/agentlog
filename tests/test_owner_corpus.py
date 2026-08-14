from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.owner_corpus import collect_owner_corpus
from agentlog.analysis.owner_notes import prepare_owner_insight_messages
from agentlog.db.schema import init_db
from agentlog.source_reader import CachedSourceTranscriptReader, SourceReadResult
from agentlog.session_identity import INTERNAL_APPROVAL_GUARDIAN_THREAD_SOURCE


class OwnerCorpusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.facts_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.conn.close()
        self.facts_dir.cleanup()

    def add_session(self, session_id: str, *, harness: str = "codex", parent: str | None = None, storage: str = "legacy_materialized", guardian: bool = False) -> None:
        self.conn.execute(
            "INSERT INTO sessions(id,harness,external_id,parent_session_id,repo,started_at,transcript_storage,thread_source) VALUES(?,?,?,?,?,?,?,?)",
            (session_id, harness, session_id, parent, "repo", "2026-08-14T00:00:00+00:00", storage, INTERNAL_APPROVAL_GUARDIAN_THREAD_SOURCE if guardian else None),
        )

    def add_message(self, session_id: str, seq: int, text: str, role: str = "user") -> None:
        self.conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            (f"{session_id}:m:{seq}", session_id, seq, role, text, f"{session_id}-hash-{seq}-{text}"),
        )

    def test_corpus_uses_visible_logical_sessions_and_excludes_backing_and_guardian(self) -> None:
        self.add_session("t3-root", harness="t3code")
        self.add_session("codex-backing")
        self.add_session("codex-worker", parent="codex-backing")
        self.add_session("guardian", guardian=True)
        for session_id in ("t3-root", "codex-backing", "codex-worker", "guardian"):
            self.add_message(session_id, 1, f"{session_id} text")
        self.conn.execute(
            "INSERT INTO session_links(source_session_id,target_session_id,link_type,target_harness,target_external_id,link_role) VALUES(?,?,?,?,?,?)",
            ("t3-root", "codex-backing", "provider_backing", "codex", "codex-backing", "root"),
        )
        self.conn.commit()

        corpus = collect_owner_corpus(self.conn)

        self.assertEqual(corpus.visible_sessions, 2)
        self.assertEqual(set(corpus.session_ids), {"codex-backing", "codex-worker"})
        self.assertEqual({message["session_id"] for message in corpus.messages}, {"codex-backing", "codex-worker"})
        backing = next(message for message in corpus.messages if message["session_id"] == "codex-backing")
        self.assertEqual(backing["source_snapshot"]["logical_session_id"], "t3-root")
        self.assertNotIn("guardian", corpus.session_ids)

    def test_corpus_ignores_whitespace_only_records(self) -> None:
        self.add_session("one")
        self.add_message("one", 1, "   \n")
        self.add_message("one", 2, "substantive")
        self.conn.commit()

        corpus = collect_owner_corpus(self.conn)

        self.assertEqual([message["message_id"] for message in corpus.messages], ["one:m:2"])

    def test_source_backed_text_is_hydrated_and_context_is_redacted(self) -> None:
        self.add_session("source", storage="source_backed")
        self.add_message("source", 1, "persisted text must not be exported")
        self.conn.execute(
            "INSERT INTO tool_events(id,session_id,message_id,seq,tool_name,action,success) VALUES(?,?,?,?,?,?,?)",
            ("tool", "source", "source:m:1", 1, "pytest", "call", 0),
        )
        self.conn.execute(
            "INSERT INTO skill_exposures(id,session_id,message_id,skill_name,exposure_type) VALUES(?,?,?,?,?)",
            ("skill", "source", "source:m:1", "verification", "loaded"),
        )
        self.conn.commit()

        def reader(conn, session_id):
            self.assertEqual(session_id, "source")
            return SourceReadResult("ready", [{
                "id": "source:m:1", "seq": 1, "role": "user", "text": "Use sk-abcdefghijklmnopqrstuvwxyz1234567890 safely", "content_hash": "source-hash", "is_tool_plumbing": False, "authored_by_agent": False,
            }], source_identity="source-id", source_hash="artifact-hash")

        self.conn.execute("UPDATE messages SET content_hash='source-hash' WHERE id='source:m:1'")
        corpus = collect_owner_corpus(self.conn, source_reader=reader)
        message = corpus.messages[0]
        self.assertIn("[REDACTED:openai_key]", message["text"])
        self.assertNotIn("persisted text", message["text"])
        self.assertEqual(message["context_facts"], ["Tool context: pytest call; outcome=failed.", "Skill context: verification was loaded."])
        self.assertEqual(message["source_snapshot"]["source_provenance"]["source_identity"], "source-id")

    def test_ledger_is_incremental_and_blocks_historical_rewrite(self) -> None:
        self.add_session("one")
        self.add_message("one", 1, "first")
        self.add_message("one", 2, "second", "assistant")
        self.conn.commit()
        first = collect_owner_corpus(self.conn)
        prepared = prepare_owner_insight_messages(self.conn, first.messages)
        self.conn.commit()
        self.assertEqual(prepared["new_messages"], 2)
        unchanged = prepare_owner_insight_messages(self.conn, collect_owner_corpus(self.conn).messages)
        self.assertEqual(unchanged["new_messages"], 0)

        self.add_message("one", 3, "third")
        self.conn.commit()
        appended = prepare_owner_insight_messages(self.conn, collect_owner_corpus(self.conn).messages)
        self.assertEqual(appended["new_messages"], 1)
        self.assertTrue(any(item["source_role"] == "context" for batch in appended["batches"] for item in batch.messages))

        self.conn.execute("UPDATE messages SET text='rewritten', content_hash='rewritten' WHERE id='one:m:1'")
        self.conn.commit()
        rewritten = prepare_owner_insight_messages(self.conn, collect_owner_corpus(self.conn).messages)
        self.assertIn("one", rewritten["blocked_sessions"])

    def test_source_change_during_export_fails_closed(self) -> None:
        self.add_session("source", storage="source_backed")
        self.add_message("source", 1, "metadata")
        self.conn.execute("UPDATE messages SET content_hash='hash' WHERE id='source:m:1'")
        self.conn.commit()

        class ChangingReader(CachedSourceTranscriptReader):
            def __call__(self, conn, session_id):
                return SourceReadResult("ready", [{
                    "id": "source:m:1", "seq": 1, "role": "user", "text": "fresh", "content_hash": "hash", "is_tool_plumbing": False, "authored_by_agent": False,
                }], source_identity="id", source_hash="hash")

            def verify_current(self):
                return False

        with self.assertRaisesRegex(ValueError, "changed during corpus export"):
            collect_owner_corpus(self.conn, source_reader=ChangingReader())

    def test_cross_session_insight_requires_exported_evidence_and_has_stable_scope_id(self) -> None:
        self.add_session("one")
        self.add_session("two")
        self.add_message("one", 1, "Keep a resume ledger when handing work over")
        self.add_message("two", 1, "A resume ledger makes the next handoff safer")
        self.conn.commit()
        prepared = prepare_owner_insight_messages(self.conn, collect_owner_corpus(self.conn).messages)
        from agentlog.analysis.insights import import_session_fact_packet
        from agentlog.analysis.owner_notes import write_owner_fact_packet

        facts = Path(self.facts_dir.name) / "facts.json"
        write_owner_fact_packet(
            facts,
            run_id="cross-session",
            batches=prepared["batches"],
            items=[{
                "kind": "handoff_design", "title": "Make handoffs resumable",
                "body": "The same concrete handoff artifact appears across distinct sessions.",
                "does_not_prove": "That every task needs a resume ledger.",
                "insight_key": "resume-ledger-pattern",
                "evidence": [
                    {"session_id": "one", "message_seq": 1, "quote": "resume ledger"},
                    {"session_id": "two", "message_seq": 1, "quote": "resume ledger"},
                ],
            }],
        )
        result = import_session_fact_packet(self.conn, facts, model="reviewer")
        self.assertEqual(result["claims"], 1)
        claim = self.conn.execute("SELECT id,sample_size,scope_type,scope_id FROM claims").fetchone()
        self.assertEqual(claim["sample_size"], 2)
        self.assertEqual((claim["scope_type"], claim["scope_id"]), ("repo", "repo"))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM claim_evidence WHERE claim_id=?", (claim["id"],)).fetchone()[0],
            2,
        )

    def test_import_matches_redacted_quote_to_its_exact_message(self) -> None:
        self.add_session("one")
        self.add_message("one", 1, "Use sk-abcdefghijklmnopqrstuvwxyz1234567890 safely")
        self.add_message("one", 2, "A different message")
        self.conn.commit()
        corpus = collect_owner_corpus(self.conn)
        prepared = prepare_owner_insight_messages(self.conn, corpus.messages)
        from agentlog.analysis.insights import import_session_fact_packet
        from agentlog.analysis.owner_notes import write_owner_fact_packet

        facts = Path(self.facts_dir.name) / "redacted.json"
        write_owner_fact_packet(
            facts,
            run_id="redacted",
            batches=prepared["batches"],
            items=[{
                "session_id": "one", "message_seq": 1,
                "kind": "safety_boundary", "title": "Keep credentials out of review payloads",
                "body": "The exported evidence masks the credential while retaining its context.",
                "quote": "[REDACTED:openai_key]", "does_not_prove": "That every secret shape is detected.",
                "insight_key": "redacted-review-evidence",
            }],
        )
        wrong = Path(self.facts_dir.name) / "wrong-seq.json"
        write_owner_fact_packet(
            wrong,
            run_id="wrong-seq",
            batches=prepared["batches"],
            items=[{
                "session_id": "one", "message_seq": 2,
                "kind": "safety_boundary", "title": "Keep credentials out of review payloads",
                "body": "This deliberately points at the wrong message.",
                "quote": "[REDACTED:openai_key]", "does_not_prove": "Anything about the second message.",
                "insight_key": "wrong-sequence-evidence",
            }],
        )
        with self.assertRaisesRegex(ValueError, "evidence quote not found"):
            import_session_fact_packet(self.conn, wrong, model="reviewer")
        self.assertEqual(import_session_fact_packet(self.conn, facts, model="reviewer")["claims"], 1)

    def test_prepared_source_evidence_survives_append_but_stays_message_bound(self) -> None:
        self.add_session("source", storage="source_backed")
        self.add_message("source", 1, "metadata one")
        self.conn.execute("UPDATE messages SET content_hash='one' WHERE id='source:m:1'")
        self.conn.commit()
        state = {"messages": [{"id": "source:m:1", "seq": 1, "role": "user", "timestamp": None, "text": "first source evidence", "content_hash": "one", "is_tool_plumbing": False, "authored_by_agent": False}], "hash": "before"}

        def read_source(conn, session_id):
            return SourceReadResult("ready", state["messages"], source_identity="source-id", source_hash=state["hash"])

        prepared = prepare_owner_insight_messages(
            self.conn, collect_owner_corpus(self.conn, source_reader=read_source).messages
        )
        self.conn.commit()
        self.add_message("source", 2, "metadata two", "assistant")
        self.conn.execute("UPDATE messages SET content_hash='two' WHERE id='source:m:2'")
        self.conn.commit()
        state["messages"] = state["messages"] + [{"id": "source:m:2", "seq": 2, "role": "assistant", "timestamp": None, "text": "appended source evidence", "content_hash": "two", "is_tool_plumbing": False, "authored_by_agent": False}]
        state["hash"] = "after"
        from agentlog.analysis import insights
        from agentlog.analysis.owner_notes import batch_message_evidence, write_owner_fact_packet

        stored = batch_message_evidence(self.conn, [{"id": batch.id, "content_hash": batch.content_hash} for batch in prepared["batches"]])
        self.assertNotIn("source_hash", stored[("source", "source:m:1")]["source_snapshot"]["source_provenance"])

        class Reader:
            def __call__(self, conn, session_id):
                return read_source(conn, session_id)

            def verify_current(self):
                return True

        facts = Path(self.facts_dir.name) / "source-facts.json"
        write_owner_fact_packet(
            facts, run_id="after-append", batches=prepared["batches"],
            items=[{"session_id": "source", "message_seq": 1, "kind": "handoff_design", "title": "Keep evidence exact", "body": "The first source message remains exact after a later append.", "quote": "first source evidence", "does_not_prove": "That appends are always harmless.", "insight_key": "source-append"}],
        )
        original = insights.CachedSourceTranscriptReader
        insights.CachedSourceTranscriptReader = Reader
        try:
            result = insights.import_session_fact_packet(self.conn, facts, model="reviewer")
        finally:
            insights.CachedSourceTranscriptReader = original
        self.assertEqual(result["claims"], 1)

    def test_all_corpus_detects_a_whole_reviewed_session_disappearing(self) -> None:
        self.add_session("gone")
        self.add_session("kept")
        self.add_message("gone", 1, "gone")
        self.add_message("kept", 1, "kept")
        self.conn.commit()
        prepare_owner_insight_messages(self.conn, collect_owner_corpus(self.conn).messages)
        self.conn.commit()
        self.conn.execute("DELETE FROM sessions WHERE id='gone'")
        self.conn.commit()
        next_run = prepare_owner_insight_messages(self.conn, collect_owner_corpus(self.conn).messages)
        self.assertIn("gone", next_run["blocked_sessions"])

    def test_scoped_review_does_not_treat_unselected_sessions_as_deleted(self) -> None:
        self.add_session("one")
        self.add_session("two")
        self.add_message("one", 1, "first")
        self.add_message("two", 1, "second")
        self.conn.commit()
        prepare_owner_insight_messages(self.conn, collect_owner_corpus(self.conn).messages)
        self.conn.commit()

        selected = collect_owner_corpus(self.conn, session_ids=["one"])
        scoped = prepare_owner_insight_messages(
            self.conn, selected.messages, detect_missing_sessions=False
        )

        self.assertNotIn("two", scoped["blocked_sessions"])
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM owner_insight_session_state WHERE session_id='two'"
            ).fetchone()["status"],
            "ready",
        )

    def test_range_compares_timestamp_offsets_as_instants(self) -> None:
        self.add_session("before")
        self.add_session("after")
        self.conn.execute("UPDATE sessions SET started_at='2026-08-13T23:45:00-01:00' WHERE id='before'")
        self.conn.execute("UPDATE sessions SET started_at='2026-08-14T00:15:00+00:00' WHERE id='after'")
        self.add_message("before", 1, "after cutoff in UTC")
        self.add_message("after", 1, "before cutoff in UTC")
        self.conn.commit()
        corpus = collect_owner_corpus(self.conn, since="2026-08-14T00:30:00+00:00")
        self.assertEqual(corpus.session_ids, ("before",))


if __name__ == "__main__":
    unittest.main()
