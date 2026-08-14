from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentlog.analysis.owner_notes import (
    OWNER_NOTE_CONFIRMATION,
    OWNER_NOTE_MAX_PROPOSAL_TARGET_EXPORT_BYTES,
    OWNER_NOTE_MAX_PROPOSAL_TARGETS,
    OWNER_NOTE_PROMPT,
    OwnerProposalTarget,
    collect_packet_dir_messages,
    prepare_owner_proposal_targets,
    prepare_owner_insight_batches,
    reset_owner_insight_session,
    validate_owner_items,
    write_owner_batch_export,
    write_owner_fact_packet,
)
from agentlog.db.schema import connect, init_db


class OwnerNotesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = connect(self.root / "owner.db")
        init_db(self.conn)
        self.packet_dir = self.root / "packets"
        self.packet_dir.mkdir()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _add_message(self, seq: int, text: str, role: str = "user") -> dict[str, object]:
        session_id, message_id = "codex:one", f"codex:one:m:{seq}"
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions(id,harness,external_id,repo) VALUES(?,?,?,?)",
            (session_id, "codex", "one", "demo"),
        )
        digest = f"hash-{seq}-{text}"
        self.conn.execute(
            "INSERT OR REPLACE INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            (message_id, session_id, seq, role, text, digest),
        )
        return {
            "message_id": message_id,
            "seq": seq,
            "role": role,
            "source_text": text,
            "content_hash": digest,
            "source_truncated": False,
        }

    def _write_packet(self, messages: list[dict[str, object]]) -> None:
        body = {
            "packet_id": "cpkt_0001",
            "packet_hash": "packet-hash",
            "corpus_snapshot_hash": "snapshot-hash",
            "safety_redaction_version": "r2",
            "redaction": {"redaction_version": "r2", "redactions": {}},
            "windows": [
                {
                    "session_id": "codex:one",
                    "artifact": {"artifact_hash": "artifact-hash"},
                    "messages": messages,
                }
            ],
        }
        (self.packet_dir / "cpkt_0001.json").write_text(json.dumps(body), encoding="utf-8")

    def test_batches_every_message_without_a_silent_digest_cap(self) -> None:
        messages = [self._add_message(i, "x" * 80) for i in range(1, 10)]
        self.conn.commit()
        self._write_packet(messages)

        prepared = prepare_owner_insight_batches(self.conn, self.packet_dir, max_batch_chars=700)

        self.assertEqual(prepared["messages_seen"], 9)
        self.assertEqual(prepared["new_messages"], 9)
        self.assertGreater(len(prepared["batches"]), 1)
        emitted = sum(len(batch.messages) for batch in prepared["batches"])
        self.assertEqual(emitted, 9)

    def test_oversized_message_is_kept_intact_in_its_own_batch(self) -> None:
        text = "important evidence " * 200
        self.conn.commit()
        self._write_packet([self._add_message(1, text)])

        prepared = prepare_owner_insight_batches(
            self.conn, self.packet_dir, max_batch_chars=700
        )

        self.assertEqual(len(prepared["batches"]), 1)
        self.assertEqual(prepared["batches"][0].messages[0]["text"], text)

    def test_migration_tail_records_history_shape(self) -> None:
        version = self.conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        self.assertGreaterEqual(int(version["version"]), 35)
        columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(owner_insight_seen_messages)")}
        self.assertTrue({"seq", "role"}.issubset(columns))
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_insight_targets'"
            ).fetchone()
        )
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='parser_upgrade_freezes'"
            ).fetchone()
        )

    def test_tool_and_skill_context_reaches_manual_review_without_becoming_quote_evidence(self) -> None:
        message = self._add_message(1, "Check the migration output", "assistant")
        self.conn.commit()
        self._write_packet([message])
        packet_path = self.packet_dir / "cpkt_0001.json"
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        packet["windows"][0]["tool_timeline"] = [
            {"message_id": message["message_id"], "tool_name": "pytest", "action": "call", "success": False}
        ]
        packet["windows"][0]["skill_exposures"] = [
            {"message_id": message["message_id"], "skill_name": "verification"}
        ]
        packet_path.write_text(json.dumps(packet), encoding="utf-8")
        messages = collect_packet_dir_messages(self.packet_dir)
        self.assertEqual(messages[0]["context_facts"], ["Tool context: pytest call; outcome=failed.", "Skill context: verification was exposed."])

    def test_proposal_targets_are_exported_once_outside_evidence_batches(self) -> None:
        messages = [self._add_message(i, "x" * 90) for i in range(1, 10)]
        self.conn.commit()
        self._write_packet(messages)
        prepared = prepare_owner_insight_batches(self.conn, self.packet_dir, max_batch_chars=700)
        targets = [
            OwnerProposalTarget(
                id="owner_target:one",
                path="/safe/AGENTS.md",
                target_kind="instruction_file",
                scope_type="repo",
                scope_id="safe",
                base_content_hash="hash-one",
                content="# Rules\n" + "r" * 2_000,
            )
        ]
        export = self.root / "export"
        manifest = write_owner_batch_export(export, prepared["batches"], targets=targets)
        self.assertGreater(len(prepared["batches"]), 1)
        self.assertEqual(manifest["proposal_targets_file"], "proposal_targets.json")
        self.assertEqual(manifest["proposal_target_count"], 1)
        self.assertLessEqual(manifest["proposal_target_bytes"], OWNER_NOTE_MAX_PROPOSAL_TARGET_EXPORT_BYTES)
        self.assertEqual(manifest["proposal_target_coverage"], {"exported": 1, "omitted": 0})
        target_payload = json.loads((export / "proposal_targets.json").read_text())
        self.assertEqual(target_payload["targets"][0]["content"], targets[0].content)
        self.assertEqual(target_payload["coverage"], {"exported": 1, "omitted": 0})
        for batch in prepared["batches"]:
            payload = json.loads((export / f"{batch.id.rsplit(':', 1)[-1]}.json").read_text())
            self.assertEqual(payload["proposal_target_stage"], "final_review_only")
            self.assertNotIn("proposal_targets", payload)
            self.assertNotIn(targets[0].content, json.dumps(payload))

    def test_proposal_target_export_has_a_hard_byte_limit(self) -> None:
        message = self._add_message(1, "review")
        self.conn.commit()
        self._write_packet([message])
        prepared = prepare_owner_insight_batches(self.conn, self.packet_dir)
        oversized = OwnerProposalTarget(
            id="owner_target:oversized",
            path="/safe/AGENTS.md",
            target_kind="instruction_file",
            scope_type="repo",
            scope_id="safe",
            base_content_hash="hash",
            content="x" * OWNER_NOTE_MAX_PROPOSAL_TARGET_EXPORT_BYTES,
        )
        with self.assertRaisesRegex(ValueError, "byte limit"):
            write_owner_batch_export(self.root / "too-large", prepared["batches"], targets=[oversized])

    def test_proposal_target_coverage_reports_bounded_omissions(self) -> None:
        paths = []
        for index in range(OWNER_NOTE_MAX_PROPOSAL_TARGETS + 1):
            path = self.root / f"skill-{index}.md"
            path.write_text("# Skill\n")
            paths.append(("test", path))
        coverage: dict[str, int] = {}
        with (
            mock.patch(
                "agentlog.analysis.claims.scope.discover_config_inventory",
                return_value=SimpleNamespace(files=[]),
            ),
            mock.patch("agentlog.analysis.skills.default_skill_roots", return_value=[]),
            mock.patch("agentlog.analysis.skills.discover_skill_files", return_value=paths),
        ):
            targets = prepare_owner_proposal_targets(self.conn, coverage=coverage)

        self.assertEqual(len(targets), OWNER_NOTE_MAX_PROPOSAL_TARGETS)
        self.assertEqual(coverage["discovered"], OWNER_NOTE_MAX_PROPOSAL_TARGETS + 1)
        self.assertEqual(coverage["exported"], OWNER_NOTE_MAX_PROPOSAL_TARGETS)
        self.assertEqual(coverage["omitted"], 1)
        self.assertEqual(coverage["omitted_limit"], 1)

    def test_unchanged_messages_are_not_reprocessed_and_prepared_batch_resumes(self) -> None:
        messages = [self._add_message(1, "first"), self._add_message(2, "second", "assistant")]
        self.conn.commit()
        self._write_packet(messages)
        first = prepare_owner_insight_batches(self.conn, self.packet_dir)
        self.conn.commit()
        second = prepare_owner_insight_batches(self.conn, self.packet_dir)

        self.assertEqual(second["new_messages"], 0)
        self.assertEqual(second["resumed_batches"], 1)
        self.assertEqual(second["batches"][0].id, first["batches"][0].id)

        self.conn.execute("DELETE FROM messages WHERE id='codex:one:m:1'")
        self.conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            ("codex:one:m:1", "codex:one", 1, "user", "first", "hash-1-first"),
        )
        self.conn.commit()
        exact_rewrite = prepare_owner_insight_batches(self.conn, self.packet_dir)
        self.assertEqual(exact_rewrite["resumed_batches"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM owner_insight_batch_messages").fetchone()[0],
            2,
        )

    def test_append_processes_only_new_message_with_bounded_prior_context(self) -> None:
        original = [self._add_message(1, "first"), self._add_message(2, "second", "assistant")]
        self.conn.commit()
        self._write_packet(original)
        first = prepare_owner_insight_batches(self.conn, self.packet_dir)
        payload = write_owner_fact_packet(
            self.root / "facts.json", run_id="one", items=[], batches=first["batches"]
        )
        from agentlog.analysis.insights import import_session_fact_packet

        import_session_fact_packet(self.conn, self.root / "facts.json", model="reviewer")
        self.conn.commit()
        appended = original + [self._add_message(3, "third")]
        self.conn.commit()
        self._write_packet(appended)

        prepared = prepare_owner_insight_batches(self.conn, self.packet_dir)

        self.assertEqual(prepared["new_messages"], 1)
        roles = {item["source_role"] for item in prepared["batches"][0].messages}
        self.assertEqual(roles, {"new", "context"})

    def test_owner_note_import_requires_a_prepared_batch(self) -> None:
        self._add_message(1, "Use a resume ledger")
        self.conn.commit()
        facts = self.root / "facts.json"
        write_owner_fact_packet(
            facts,
            run_id="unprepared",
            items=[{
                "session_id": "codex:one",
                "message_seq": 1,
                "kind": "handoff_design",
                "title": "Keep handoffs resumable",
                "body": "A compact ledger helps preserve state.",
                "quote": "resume ledger",
                "does_not_prove": "That every task needs one.",
                "insight_key": "resume-ledger",
            }],
        )
        from agentlog.analysis.insights import import_session_fact_packet

        with self.assertRaisesRegex(ValueError, "require prepared owner insight batches"):
            import_session_fact_packet(self.conn, facts, model="reviewer")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 0)

    def test_changed_history_blocks_instead_of_silently_forgetting_it(self) -> None:
        message = self._add_message(1, "original")
        self.conn.commit()
        self._write_packet([message])
        prepare_owner_insight_batches(self.conn, self.packet_dir)
        self.conn.commit()
        changed = dict(message, content_hash="changed", source_text="rewritten")
        self._write_packet([changed])

        prepared = prepare_owner_insight_batches(self.conn, self.packet_dir)

        self.assertIn("codex:one", prepared["blocked_sessions"])
        self.assertEqual(prepared["batches"], [])
        row = self.conn.execute(
            "SELECT status FROM owner_insight_session_state WHERE session_id='codex:one'"
        ).fetchone()
        self.assertEqual(row["status"], "blocked_rewrite")
        reset_owner_insight_session(self.conn, "codex:one")
        restarted = prepare_owner_insight_batches(self.conn, self.packet_dir)
        self.assertEqual(restarted["new_messages"], 1)

    def test_coach_taxonomy_receipt_is_rejected_but_advisory_note_survives(self) -> None:
        with self.assertRaisesRegex(ValueError, "Coach receipt"):
            validate_owner_items(
                [{"session_id": "s", "kind": "instruction_follow", "title": "receipt", "body": "tool ran", "quote": "x", "does_not_prove": "x", "insight_key": "receipt"}]
            )
        note = validate_owner_items(
            [{"session_id": "s", "kind": "handoff_design", "title": "Make handoffs executable", "body": "A concise handoff can preserve the decision boundary.", "quote": "leave a resume ledger", "does_not_prove": "That every task needs one.", "insight_key": "handoff-executable"}]
        )
        self.assertEqual(note[0]["kind"], "handoff_design")
        self.assertIn("untrusted data", OWNER_NOTE_PROMPT)
        self.assertTrue(OWNER_NOTE_CONFIRMATION.startswith("i-understand"))

    def test_import_rejects_out_of_batch_evidence_before_writing_claims(self) -> None:
        messages = [self._add_message(1, "The tool failed"), self._add_message(2, "Please fix it")]
        self._add_message(3, "Not present in the exported packet")
        self.conn.commit()
        self._write_packet(messages)
        prepared = prepare_owner_insight_batches(self.conn, self.packet_dir)
        facts = self.root / "facts.json"
        write_owner_fact_packet(
            facts,
            run_id="batch-import",
            batches=prepared["batches"],
            items=[
                {
                    "session_id": "codex:one",
                    "message_seq": 3,
                    "kind": "recovery_pattern",
                    "title": "Use corrections to revisit failures",
                    "body": "The correction supplies a useful recovery signal.",
                    "quote": "Not present in the exported packet",
                    "does_not_prove": "That every correction indicates a failure.",
                    "insight_key": "recovery-corrections",
                }
            ],
        )
        from agentlog.analysis.insights import import_session_fact_packet

        with self.assertRaisesRegex(ValueError, "not in its owner insight batch"):
            import_session_fact_packet(self.conn, facts, model="reviewer")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 0)

    def test_missing_or_reordered_history_blocks_instead_of_reprocessing(self) -> None:
        first = self._add_message(1, "first")
        second = self._add_message(2, "second", "assistant")
        self.conn.commit()
        self._write_packet([first, second])
        prepare_owner_insight_batches(self.conn, self.packet_dir)
        self.conn.commit()
        self._write_packet([second])
        missing = prepare_owner_insight_batches(self.conn, self.packet_dir)
        self.assertIn("codex:one", missing["blocked_sessions"])

        reset_owner_insight_session(self.conn, "codex:one")
        self._write_packet([first, second])
        prepare_owner_insight_batches(self.conn, self.packet_dir)
        self.conn.commit()
        reordered = dict(second, seq=1)
        self._write_packet([first, reordered])
        changed = prepare_owner_insight_batches(self.conn, self.packet_dir)
        self.assertIn("codex:one", changed["blocked_sessions"])

    def test_owner_proposal_is_evidence_bound_pending_and_idempotent(self) -> None:
        message = self._add_message(1, "Use a resume ledger after a long handoff")
        self.conn.commit()
        self._write_packet([message])
        prepared = prepare_owner_insight_batches(self.conn, self.packet_dir)
        target = self.root / "AGENTS.md"
        target.write_text("# Instructions\n", encoding="utf-8")
        from agentlog.analysis.claims.proposals import _sha1_text
        from agentlog.analysis.insights import import_session_fact_packet

        item = {
            "session_id": "codex:one",
            "message_seq": 1,
            "kind": "handoff_design",
            "insight_key": "resume-ledger",
            "title": "Keep handoffs resumable",
            "body": "A compact resume ledger makes recovery less lossy.",
            "quote": "resume ledger",
            "does_not_prove": "That every task needs a ledger.",
        }
        proposal = {
            "proposal_key": "resume-ledger-rule",
            "title": "Add a resumable handoff rule",
            "action": "update",
            "target_id": "owner_target:test",
            "target_kind": "instruction_file",
            "proposed_content": "# Instructions\n\nKeep a concise resume ledger for long handoffs.\n",
            "rationale": "The cited handoff needs a recoverable boundary.",
            "does_not_prove": "That it will improve every handoff.",
            "supporting_insight_keys": ["resume-ledger"],
            "evidence": [{"session_id": "codex:one", "message_seq": 1, "quote": "resume ledger"}],
            "human_review_required": True,
        }
        target_record = OwnerProposalTarget(
            id="owner_target:test",
            path=str(target.resolve()),
            target_kind="instruction_file",
            scope_type="repo",
            scope_id="test",
            base_content_hash=_sha1_text(target.read_text()),
            content=target.read_text(),
        )
        self.conn.execute(
            "INSERT INTO owner_insight_targets(id,path,target_kind,scope_type,scope_id,base_content_hash,exported_at) VALUES(?,?,?,?,?,?,?)",
            (target_record.id, target_record.path, target_record.target_kind, target_record.scope_type, target_record.scope_id, target_record.base_content_hash, "2026-01-01T00:00:00+00:00"),
        )
        facts = self.root / "facts.json"
        write_owner_fact_packet(facts, run_id="first-run", batches=prepared["batches"], items=[item], proposals=[proposal], targets=[target_record])
        first = import_session_fact_packet(self.conn, facts, model="reviewer")
        self.assertEqual(first["proposals"], 1)
        self.assertEqual(target.read_text(), "# Instructions\n")
        row = self.conn.execute("SELECT status FROM proposals").fetchone()
        self.assertEqual(row["status"], "pending")

        write_owner_fact_packet(facts, run_id="second-run", batches=prepared["batches"], items=[item], proposals=[proposal], targets=[target_record])
        second = import_session_fact_packet(self.conn, facts, model="reviewer")
        self.assertEqual(second["proposals"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0], 1)

    def test_proposal_target_binding_rejects_unlisted_outside_and_changed_files(self) -> None:
        from agentlog.analysis.claims.proposals import _sha1_text
        from agentlog.analysis.insights import _proposal_target

        target = self.root / "AGENTS.md"
        target.write_text("# Safe\n", encoding="utf-8")
        self.conn.execute(
            "INSERT INTO owner_insight_targets(id,path,target_kind,scope_type,scope_id,base_content_hash,exported_at) VALUES(?,?,?,?,?,?,?)",
            ("owner_target:safe", str(target.resolve()), "instruction_file", "repo", "test", _sha1_text(target.read_text()), "2026-01-01T00:00:00+00:00"),
        )
        payload = {"owner_insight_targets": [{"id": "owner_target:safe", "base_content_hash": _sha1_text(target.read_text())}]}
        with self.assertRaisesRegex(ValueError, "not exported"):
            _proposal_target(self.conn, payload, "owner_target:../../outside", "instruction_file")
        with self.assertRaisesRegex(ValueError, "not exported"):
            _proposal_target(self.conn, {"owner_insight_targets": []}, "owner_target:safe", "instruction_file")
        target.write_text("# Changed\n", encoding="utf-8")
        path, expected, _, _ = _proposal_target(self.conn, payload, "owner_target:safe", "instruction_file")
        self.assertEqual(path, target.resolve())
        self.assertNotEqual(_sha1_text(path.read_text()), expected)


if __name__ == "__main__":
    unittest.main()
