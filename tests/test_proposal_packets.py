"""LLM proposal packet emit/validate/ingest (Cursor subagent path)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.claims.packets import (
    DEFAULT_MODEL,
    emit_proposal_packet_run,
    ingest_proposal_packet_results,
    validate_proposal_result,
)
from agentlog.analysis.claims.proposals import (
    EMIT_UNUSED_SKILL_ARCHIVE_PROPOSALS,
    EMIT_USAGE_PROFILE_PROPOSALS,
    generate_proposals,
    refresh_learnings,
)
from agentlog.analysis.claims.store import list_proposals
from agentlog.db.schema import connect, init_db


def _seed_run(conn, run_id: str = "run1") -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO derivation_runs (
            id, kind, extractor_name, extractor_version, model, prompt_hash,
            started_at, completed_at, status, meta_json
        ) VALUES (?, 'test', 'test', '1', 'm', 'p', 't', 't', 'ok', '{}')
        """,
        (run_id,),
    )


def _seed_session(conn, *, sid: str, cwd: str = "/tmp/demo") -> None:
    conn.execute(
        """
        INSERT INTO sessions (
            id, harness, external_id, parent_session_id, repo, cwd,
            model, model_canonical, started_at, ended_at
        ) VALUES (?, 'codex', ?, NULL, 'demo', ?, 'gpt-5.5', 'gpt-5.5',
                  '2026-08-01T10:00:00+00:00', '2026-08-01T11:00:00+00:00')
        """,
        (sid, sid.split(":", 1)[-1], cwd),
    )


def _seed_substantive_window(
    conn,
    *,
    sid: str,
    wid: str,
    text: str,
    turn_kinds: list[str],
    flags: dict | None = None,
    run_id: str = "run1",
) -> None:
    mid_req = f"{wid}:req"
    mid_resp = f"{wid}:resp"
    conn.execute(
        """
        INSERT INTO messages (id, session_id, seq, role, text, content_hash, authored_by_agent)
        VALUES (?, ?, 1, 'user', ?, 'h1', 0)
        """,
        (mid_req, sid, text),
    )
    conn.execute(
        """
        INSERT INTO messages (id, session_id, seq, role, text, content_hash)
        VALUES (?, ?, 2, 'assistant', 'ok doing that', 'h2')
        """,
        (mid_resp, sid),
    )
    conn.execute(
        """
        INSERT INTO exchange_windows
            (id, session_id, request_message_id, response_message_id,
             input_hash, content_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (wid, sid, mid_req, mid_resp, wid, wid),
    )
    conn.execute(
        """
        INSERT INTO window_det_classifications (
            id, window_id, run_id, turn_kinds_json, request_kind, route,
            drop_rules_json, features_json, extractor_name, extractor_version,
            created_at
        ) VALUES (?, ?, ?, '[]', 'substantive', 'ux', '[]', '{}', 'det', '1', 't')
        """,
        (f"d:{wid}", wid, run_id),
    )
    conn.execute(
        """
        INSERT INTO ux_observations (
            id, window_id, run_id, turn_kinds_json, user_stance, agent_stance,
            prior_outcome, flags_json, spans_json, confidence_json,
            abstain_reasons_json, novel_observations_json, extractor_name,
            extractor_version, model, prompt_hash, batch_size, raw_json,
            created_at, content_hash, link_status
        ) VALUES (
            ?, ?, ?, ?, 'correcting', 'executing', 'abstain', ?, ?,
            '{}', '[]', '[]', 'ux', '1', 'm', 'p', 1, '{}', 't', ?, 'linked'
        )
        """,
        (
            f"ux:{wid}",
            wid,
            run_id,
            json.dumps(turn_kinds),
            json.dumps(flags or {"scope_narrowing": True}),
            json.dumps([{"role": "user", "quote": text[:40]}]),
            wid,
        ),
    )


class StaticBoardDisabledTests(unittest.TestCase):
    def test_archive_skill_and_usage_not_published(self) -> None:
        self.assertFalse(EMIT_UNUSED_SKILL_ARCHIVE_PROPOSALS)
        self.assertFalse(EMIT_USAGE_PROFILE_PROPOSALS)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "AGENTS.md").write_text("# G\n", encoding="utf-8")
            db = home / "t.db"
            conn = connect(db)
            init_db(conn)
            skill_path = home / ".agents" / "skills" / "dusty" / "SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text(
                "---\nname: dusty\ndescription: unused\n---\n# dusty\n",
                encoding="utf-8",
            )
            for i in range(12):
                _seed_session(conn, sid=f"codex:s{i}", cwd=str(home / "demo"))
            conn.execute(
                """
                INSERT INTO skills (
                    id, name, source, source_path, description, current_content_hash,
                    first_seen_at, last_seen_at, last_indexed_at
                ) VALUES ('skd', 'dusty', 'agents', ?, 'unused', 'h', 't', 't', 't')
                """,
                (str(skill_path),),
            )
            conn.commit()
            stats = refresh_learnings(conn, home=home)
            conn.commit()
            props = list_proposals(conn, status="pending")
            self.assertEqual(stats["proposals_total"], 0)
            self.assertFalse(any(p.action == "archive_skill" for p in props))
            self.assertFalse(
                any("usage profile" in p.title.lower() for p in props)
            )
            self.assertEqual(generate_proposals(conn, home=home), [])


class ProposalSchemaValidationTests(unittest.TestCase):
    def _packet(self, *, target: str, quote: str, n: int = 11) -> dict:
        return {
            "packet_id": "ppkt_0001_scope_narrow",
            "run_id": "proposals_test",
            "theme": "scope_narrow",
            "prompt_hash": "abc",
            "evidence_pack_hash": "pack1",
            "model_hint": DEFAULT_MODEL,
            "allowed_targets": [
                {
                    "path": target,
                    "kind": "agents_md",
                    "scope_type": "global",
                    "scope_id": "global",
                }
            ],
            "windows": [
                {
                    "window_id": f"w{i}",
                    "session_id": f"codex:s{i}",
                    "user": quote,
                    "assistant": "ok",
                    "timestamp": "2026-08-01T10:00:00+00:00",
                    "spans": [],
                }
                for i in range(n)
            ],
        }

    def test_valid_llm_proposal_schema(self) -> None:
        target = "/tmp/AGENTS.md"
        quote = "stay in scope — only edit the named file"
        packet = self._packet(target=target, quote=quote)
        raw = {
            "packet_id": packet["packet_id"],
            "model": DEFAULT_MODEL,
            "abstain": False,
            "proposals": [
                {
                    "title": "Add stay-in-scope instruction",
                    "action": "add",
                    "target_path": target,
                    "heading": "Stay in named scope",
                    "instruction_rewrite": "Stay inside the files the user named.",
                    "rationale": "Recurring scope corrections across sessions.",
                    "does_not_prove": "Does not prove every task needs a scope lock.",
                    "support_tier": "ok",
                    "sample_size": 11,
                    "evidence": [
                        {
                            "session_id": f"codex:s{i}",
                            "window_id": f"w{i}",
                            "quote": quote,
                        }
                        for i in range(11)
                    ],
                }
            ],
        }
        validated, failures = validate_proposal_result(raw, packet=packet)
        self.assertEqual(failures, [])
        assert validated is not None
        self.assertFalse(validated["abstain"])
        self.assertEqual(len(validated["proposals"]), 1)

    def test_abstain_path(self) -> None:
        packet = self._packet(target="/tmp/AGENTS.md", quote="x")
        raw = {
            "packet_id": packet["packet_id"],
            "model": DEFAULT_MODEL,
            "abstain": True,
            "abstain_reason": "n too small",
            "proposals": [],
        }
        validated, failures = validate_proposal_result(raw, packet=packet)
        self.assertEqual(failures, [])
        assert validated is not None
        self.assertTrue(validated["abstain"])

    def test_rejects_invented_path(self) -> None:
        packet = self._packet(target="/tmp/AGENTS.md", quote="stay in scope")
        raw = {
            "packet_id": packet["packet_id"],
            "model": DEFAULT_MODEL,
            "abstain": False,
            "proposals": [
                {
                    "title": "Bad path",
                    "action": "add",
                    "target_path": "/invented/AGENTS.md",
                    "heading": "X",
                    "instruction_rewrite": "do x",
                    "rationale": "r",
                    "does_not_prove": "d",
                    "support_tier": "ok",
                    "sample_size": 11,
                    "evidence": [
                        {
                            "session_id": "codex:s0",
                            "window_id": "w0",
                            "quote": "stay in scope",
                        }
                    ],
                }
            ],
        }
        validated, failures = validate_proposal_result(raw, packet=packet)
        self.assertIsNone(validated)
        self.assertTrue(
            any(f.reason == "target_path_not_allowed" for f in failures)
        )

    def test_thin_n_does_not_publish(self) -> None:
        target = "/tmp/AGENTS.md"
        quote = "wait don't act yet"
        packet = self._packet(target=target, quote=quote, n=3)
        raw = {
            "packet_id": packet["packet_id"],
            "model": DEFAULT_MODEL,
            "abstain": False,
            "proposals": [
                {
                    "title": "Thin",
                    "action": "add",
                    "target_path": target,
                    "heading": "Wait",
                    "instruction_rewrite": "wait",
                    "rationale": "r",
                    "does_not_prove": "d",
                    "support_tier": "ok",
                    "sample_size": 3,
                    "evidence": [
                        {
                            "session_id": f"codex:s{i}",
                            "window_id": f"w{i}",
                            "quote": quote,
                        }
                        for i in range(3)
                    ],
                }
            ],
        }
        validated, failures = validate_proposal_result(raw, packet=packet)
        self.assertEqual(failures, [])
        assert validated is not None
        self.assertTrue(validated["abstain"])

    def test_evidence_sessions_determine_support_and_sample_size(self) -> None:
        target = "/tmp/AGENTS.md"
        quote = "stay in scope"
        packet = self._packet(target=target, quote=quote, n=11)
        raw = {
            "packet_id": packet["packet_id"],
            "model": DEFAULT_MODEL,
            "abstain": False,
            "proposals": [
                {
                    "title": "Evidence-derived support",
                    "action": "add",
                    "target_path": target,
                    "heading": "Scope",
                    "instruction_rewrite": "Stay in scope.",
                    "rationale": "Repeated instruction.",
                    "does_not_prove": "Does not prove intent.",
                    "support_tier": "ok",
                    "sample_size": 999,
                    "evidence": [
                        {
                            "session_id": f"codex:s{i}",
                            "window_id": f"w{i}",
                            "quote": quote,
                        }
                        for i in range(5)
                    ],
                }
            ],
        }
        validated, failures = validate_proposal_result(raw, packet=packet)
        self.assertEqual(failures, [])
        assert validated is not None
        proposal = validated["proposals"][0]
        self.assertEqual(proposal["sample_size"], 5)
        self.assertEqual(proposal["packet_population"], 11)
        self.assertEqual(proposal["support_tier"], "insufficient")

    def test_rejects_update_action(self) -> None:
        target = "/tmp/AGENTS.md"
        quote = "stay in scope"
        packet = self._packet(target=target, quote=quote)
        raw = {
            "packet_id": packet["packet_id"],
            "abstain": False,
            "proposals": [
                {
                    "title": "Unsafe update",
                    "action": "update",
                    "target_path": target,
                    "heading": "Scope",
                    "instruction_rewrite": "Stay in scope.",
                    "rationale": "Repeated instruction.",
                    "does_not_prove": "Does not prove intent.",
                    "support_tier": "ok",
                    "sample_size": 11,
                    "evidence": [
                        {
                            "session_id": f"codex:s{i}",
                            "window_id": f"w{i}",
                            "quote": quote,
                        }
                        for i in range(11)
                    ],
                }
            ],
        }
        validated, failures = validate_proposal_result(raw, packet=packet)
        self.assertIsNone(validated)
        self.assertTrue(any(f.reason == "update_not_supported" for f in failures))


class PacketIngestMockTests(unittest.TestCase):
    def test_ingest_mock_result_publishes_llm_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            agents = home / "AGENTS.md"
            secret = "sk-proj-0123456789abcdefghijklmnop"
            agents.write_text(
                f"# Global\n\napi_key = {secret}\n", encoding="utf-8"
            )
            db = root / "t.db"
            conn = connect(db)
            init_db(conn)
            _seed_run(conn)
            quote = f"only edit {home}/named files — no drive-by refactors"
            for i in range(11):
                sid = f"codex:s{i}"
                _seed_session(conn, sid=sid, cwd=str(root / "demo"))
                _seed_substantive_window(
                    conn,
                    sid=sid,
                    wid=f"w{i}",
                    text=quote,
                    turn_kinds=["correction"],
                    flags={"scope_narrowing": True},
                )
            conn.commit()

            run_dir = root / "proposals-run"
            from agentlog.analysis.claims import packets as packets_mod

            real_discover = packets_mod.discover_config_inventory

            def _discover(home_arg=None):
                return real_discover(home)

            packets_mod.discover_config_inventory = _discover  # type: ignore[assignment]
            try:
                manifest = emit_proposal_packet_run(
                    conn, run_dir, home=home, resume=False, windows_per_theme=11
                )
                self.assertGreaterEqual(manifest["packet_count"], 1)
                # Prefer a packet that actually got windows.
                packet_id = None
                for pid, meta in manifest["packets"].items():
                    if meta.get("session_count", 0) >= 10:
                        packet_id = pid
                        break
                self.assertIsNotNone(packet_id)
                assert packet_id is not None
                packet_path = run_dir / manifest["packets"][packet_id]["packet_path"]
                packet = json.loads(packet_path.read_text(encoding="utf-8"))
                target = packet["allowed_targets"][0]["target_path"]
                packet_text = packet_path.read_text(encoding="utf-8")
                self.assertNotIn(str(home), packet_text)
                self.assertNotIn(secret, packet_text)
                self.assertNotIn("api_key =", packet_text)
                self.assertGreater(packet["redaction"]["redaction_total"], 0)
                self.assertEqual(
                    packet["redaction"]["redaction_version"], manifest["redaction_version"]
                )
                windows = packet["windows"]
                evidence = []
                for w in windows:
                    q = quote if quote in (w.get("user") or "") else (w.get("user") or "")[:40]
                    if q and q in (w.get("user") or ""):
                        evidence.append(
                            {
                                "session_id": w["session_id"],
                                "window_id": w["window_id"],
                                "quote": q,
                            }
                        )
                self.assertGreaterEqual(len(evidence), 10)
                result = {
                    "packet_id": packet_id,
                    "model": DEFAULT_MODEL,
                    "abstain": False,
                    "proposals": [
                        {
                            "title": "Add stay-in-scope instruction",
                            "action": "add",
                            "target_path": target,
                            "heading": "Stay in named scope",
                            "instruction_rewrite": (
                                "Stay inside the files and scope the user named."
                            ),
                            "rationale": "LLM-authored from substantive corrections.",
                            "does_not_prove": "Not causal proof of scope failures.",
                            "support_tier": "ok",
                            "sample_size": len(evidence),
                            "evidence": evidence,
                        }
                    ],
                }
                (run_dir / "results" / f"{packet_id}.json").write_text(
                    json.dumps(result, indent=2) + "\n", encoding="utf-8"
                )
                results = ingest_proposal_packet_results(conn, run_dir, home=home)
            finally:
                packets_mod.discover_config_inventory = real_discover  # type: ignore[assignment]

            completed = [r for r in results if r.status == "completed"]
            props = list_proposals(conn, status="pending")
            llm_props = [
                p for p in props if (p.derivation_summary or "").startswith("LLM")
            ]
            if not completed:
                self.fail(f"no completed packets: {[r.to_dict() for r in results]}")
            self.assertTrue(llm_props)
            self.assertEqual(llm_props[0].model, DEFAULT_MODEL)
            self.assertIsNotNone(llm_props[0].run_id)
            self.assertEqual(llm_props[0].target_path, str(agents))
            self.assertEqual(llm_props[0].sample_size, len(evidence))
            self.assertEqual(
                agents.read_text(encoding="utf-8"), f"# Global\n\napi_key = {secret}\n"
            )


if __name__ == "__main__":
    unittest.main()
