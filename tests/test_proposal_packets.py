"""LLM proposal packet emit/validate/ingest (Cursor subagent path)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.claims import packets as packets_mod
from agentlog.analysis.claims.packets import (
    DEFAULT_MODEL,
    ELIGIBLE_POPULATION_VERSION,
    EVIDENCE_CONTRACT_VERSION,
    PROPOSAL_PACKET_VALIDATOR_VERSION,
    emit_proposal_packet_run,
    ingest_proposal_packet_results,
    materialize_proposals,
    publish_llm_proposals_from_run,
    validate_proposal_result,
)
from agentlog.analysis.claims.models import Proposal
from agentlog.analysis.claims.proposals import (
    EMIT_UNUSED_SKILL_ARCHIVE_PROPOSALS,
    EMIT_USAGE_PROFILE_PROPOSALS,
    generate_proposals,
    refresh_learnings,
)
from agentlog.analysis.claims.scope import ConfigFile, ConfigInventory
from agentlog.analysis.claims.store import (
    list_proposals,
    upsert_claims,
    upsert_proposals,
)
from agentlog.db.schema import connect, init_db
from agentlog.safety.redaction import REDACTION_VERSION, RedactionReport


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


def _seed_session(
    conn,
    *,
    sid: str,
    cwd: str = "/tmp/demo",
    harness: str = "codex",
    repo: str = "demo",
) -> None:
    conn.execute(
        """
        INSERT INTO sessions (
            id, harness, external_id, parent_session_id, repo, cwd,
            model, model_canonical, started_at, ended_at
        ) VALUES (?, ?, ?, NULL, ?, ?, 'gpt-5.5', 'gpt-5.5',
                  '2026-08-01T10:00:00+00:00', '2026-08-01T11:00:00+00:00')
        """,
        (sid, harness, sid.split(":", 1)[-1], repo, cwd),
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
    seq: int = 1,
) -> None:
    mid_req = f"{wid}:req"
    mid_resp = f"{wid}:resp"
    conn.execute(
        """
        INSERT INTO messages (id, session_id, seq, role, text, content_hash, authored_by_agent)
        VALUES (?, ?, ?, 'user', ?, 'h1', 0)
        """,
        (mid_req, sid, seq, text),
    )

    conn.execute(
        """
        INSERT INTO messages (id, session_id, seq, role, text, content_hash)
        VALUES (?, ?, ?, 'assistant', 'ok doing that', 'h2')
        """,
        (mid_resp, sid, seq + 1),
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


def _seed_adjudicated_miss_pair(conn, *, wid: str) -> None:
    conn.execute(
        """
        INSERT INTO adjudications (
            window_id, adjudicated_at, turn_kind, user_stance, agent_stance,
            prior_outcome, notes, source
        ) VALUES (?, 't', ?, 'correcting', 'executing', 'rejected_redo', '', 'ad_hoc')
        """,
        (wid, json.dumps(["correction"])),
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
    def _packet(
        self,
        *,
        target: str,
        quote: str,
        n: int = 11,
        population_n: int | None = None,
        scope_type: str = "repo",
        scope_id: str | None = "demo",
    ) -> dict:
        root_cluster_count = n if population_n is None else population_n
        windows = [
            {
                "window_id": f"w{i}",
                "session_id": f"codex:s{i}",
                "request_message_id": f"request:{i}",
                "response_message_id": f"response:{i}",
                "logical_root_id": f"root:{i}",
                "logical_harness": "codex" if i % 2 else "claude",
                "project_key": f"project:{i % 4}",
                "user": quote,
                "assistant": "ok",
                "timestamp": "2026-08-01T10:00:00+00:00",
                "spans": [],
            }
            for i in range(n)
        ]
        by_harness = {
            "codex": (root_cluster_count + 1) // 2,
            "claude": root_cluster_count // 2,
        }
        pairs = [
            {
                "pair_id": f"adjudicated_miss:w{i}",
                "pattern_key": "scope_narrow",
                "window_id": f"w{i}",
                "logical_root_id": f"root:{i}",
                "logical_harness": "codex" if i % 2 else "claude",
                "project_key": f"project:{i % 4}",
                "turn_kinds": ["correction"],
                "prior_outcome": "rejected_redo",
            }
            for i in range(n)
        ]
        return {
            "packet_id": "ppkt_0001_scope_narrow",
            "run_id": "proposals_test",
            "theme": "scope_narrow",
            "prompt_hash": "abc",
            "evidence_pack_hash": "pack1",
            "model_hint": DEFAULT_MODEL,
            "validator_version": PROPOSAL_PACKET_VALIDATOR_VERSION,
            "redaction": {
                "redaction_version": REDACTION_VERSION,
                "redaction_total": 0,
                "redactions": {},
            },
            "eligible_population": {
                "definition_version": ELIGIBLE_POPULATION_VERSION,
                "root_cluster_count": root_cluster_count,
                "by_harness": [
                    {
                        "harness": harness,
                        "root_cluster_count": count,
                    }
                    for harness, count in sorted(by_harness.items())
                    if count
                ],
            },
            "evidence_contract": {
                "version": EVIDENCE_CONTRACT_VERSION,
                "min_independent_logical_roots": 10,
                "min_adjudicated_miss_pairs": 3,
                "min_global_logical_roots": 15,
                "global_min_harnesses": 2,
                "global_max_concentration": 0.7,
            },
            "allowed_targets": [
                {
                    "path": target,
                    "kind": "agents_md",
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                }
            ],
            "config_snippets": [
                {
                    "target_path": target,
                    "content_hash": "target-content-hash",
                    "text": "# Existing configuration\n",
                }
            ],
            "windows": windows,
            "validated_miss_pairs": pairs,
        }

    def _proof(self, packet: dict, *, target: str, pair_count: int = 3) -> dict:
        return {
            "pattern_key": packet["theme"],
            "validated_miss_pair_ids": [
                pair["pair_id"]
                for pair in packet["validated_miss_pairs"][:pair_count]
            ],
            "config_gap": {
                "target_path": target,
                "content_hash": "target-content-hash",
                "finding": "The current rule does not state this instruction.",
            },
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
                    **self._proof(packet, target=target),
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

    def test_quote_source_rejects_user_assistant_ambiguity(self) -> None:
        self.assertEqual(
            packets_mod._quote_source_in_window(
                "same phrase",
                {
                    "user": "same phrase",
                    "assistant": "same phrase",
                    "request_message_id": "request-1",
                    "response_message_id": "response-1",
                },
            ),
            (None, None, "quote_source_ambiguous"),
        )

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
                    **self._proof(packet, target="/tmp/AGENTS.md"),
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
        packet = self._packet(target=target, quote=quote, n=7)
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
                    "sample_size": 7,
                    **self._proof(packet, target=target),
                    "evidence": [
                        {
                            "session_id": f"codex:s{i}",
                            "window_id": f"w{i}",
                            "quote": quote,
                        }
                        for i in range(7)
                    ],
                }
            ],
        }
        validated, failures = validate_proposal_result(raw, packet=packet)
        self.assertEqual(failures, [])
        assert validated is not None
        self.assertTrue(validated["abstain"])

    def test_requires_ten_independent_logical_roots(self) -> None:
        target = "/tmp/AGENTS.md"
        quote = "stay in scope"
        packet = self._packet(target=target, quote=quote)
        for window in packet["windows"]:
            window["logical_root_id"] = "root:shared"
            window["logical_harness"] = "codex"
            window["project_key"] = "project:shared"
        for pair in packet["validated_miss_pairs"]:
            pair["logical_root_id"] = "root:shared"
            pair["logical_harness"] = "codex"
            pair["project_key"] = "project:shared"
        raw = {
            "packet_id": packet["packet_id"],
            "model": DEFAULT_MODEL,
            "abstain": False,
            "proposals": [
                {
                    "title": "Scope instruction",
                    "action": "add",
                    "target_path": target,
                    "heading": "Scope",
                    "instruction_rewrite": "Stay in scope.",
                    "rationale": "Repeated instruction.",
                    "does_not_prove": "Does not prove intent.",
                    "support_tier": "ok",
                    "sample_size": 11,
                    **self._proof(packet, target=target),
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
        self.assertTrue(validated["abstain"])

    def test_global_target_rejects_fourteen_logical_roots(self) -> None:
        target = "/tmp/AGENTS.md"
        quote = "stay in scope"
        packet = self._packet(
            target=target,
            quote=quote,
            n=14,
            scope_type="global",
            scope_id="global",
        )
        raw = {
            "packet_id": packet["packet_id"],
            "model": DEFAULT_MODEL,
            "abstain": False,
            "proposals": [
                {
                    "title": "Scope instruction",
                    "action": "add",
                    "target_path": target,
                    "heading": "Scope",
                    "instruction_rewrite": "Stay in scope.",
                    "rationale": "Repeated instruction.",
                    "does_not_prove": "Does not prove intent.",
                    "support_tier": "ok",
                    "sample_size": 14,
                    **self._proof(packet, target=target),
                    "evidence": [
                        {
                            "session_id": f"codex:s{i}",
                            "window_id": f"w{i}",
                            "quote": quote,
                        }
                        for i in range(14)
                    ],
                }
            ],
        }
        validated, failures = validate_proposal_result(raw, packet=packet)
        self.assertEqual(failures, [])
        assert validated is not None
        self.assertTrue(validated["abstain"])

    def test_global_target_requires_diverse_harnesses_and_projects(self) -> None:
        target = "/tmp/AGENTS.md"
        quote = "stay in scope"
        packet = self._packet(
            target=target,
            quote=quote,
            n=15,
            scope_type="global",
            scope_id="global",
        )
        for window in packet["windows"]:
            window["logical_harness"] = "codex"
            window["project_key"] = "project:shared"
        for pair in packet["validated_miss_pairs"]:
            pair["logical_harness"] = "codex"
            pair["project_key"] = "project:shared"
        raw = {
            "packet_id": packet["packet_id"],
            "model": DEFAULT_MODEL,
            "abstain": False,
            "proposals": [
                {
                    "title": "Scope instruction",
                    "action": "add",
                    "target_path": target,
                    "heading": "Scope",
                    "instruction_rewrite": "Stay in scope.",
                    "rationale": "Repeated instruction.",
                    "does_not_prove": "Does not prove intent.",
                    "support_tier": "ok",
                    "sample_size": 15,
                    **self._proof(packet, target=target),
                    "evidence": [
                        {
                            "session_id": f"codex:s{i}",
                            "window_id": f"w{i}",
                            "quote": quote,
                        }
                        for i in range(15)
                    ],
                }
            ],
        }
        validated, failures = validate_proposal_result(raw, packet=packet)
        self.assertEqual(failures, [])
        assert validated is not None
        self.assertTrue(validated["abstain"])

    def test_requires_three_adjudicated_miss_pairs(self) -> None:
        target = "/tmp/AGENTS.md"
        quote = "stay in scope"
        packet = self._packet(target=target, quote=quote)
        raw = {
            "packet_id": packet["packet_id"],
            "model": DEFAULT_MODEL,
            "abstain": False,
            "proposals": [
                {
                    "title": "Scope instruction",
                    "action": "add",
                    "target_path": target,
                    "heading": "Scope",
                    "instruction_rewrite": "Stay in scope.",
                    "rationale": "Repeated instruction.",
                    "does_not_prove": "Does not prove intent.",
                    "support_tier": "ok",
                    "sample_size": 11,
                    **self._proof(packet, target=target, pair_count=2),
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
        self.assertTrue(validated["abstain"])

    def test_evidence_sessions_determine_support_and_sample_size(self) -> None:
        target = "/tmp/AGENTS.md"
        quote = "stay in scope"
        packet = self._packet(
            target=target, quote=quote, n=11, population_n=17
        )
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
                        for i in range(10)
                    ],
                    **self._proof(packet, target=target),
                }
            ],
        }
        validated, failures = validate_proposal_result(raw, packet=packet)
        self.assertEqual(failures, [])
        assert validated is not None
        proposal = validated["proposals"][0]
        self.assertEqual(proposal["sample_size"], 10)
        self.assertEqual(proposal["population_denominator"], 17)
        self.assertEqual(
            proposal["eligible_population"]["root_cluster_count"], 17
        )
        self.assertEqual(proposal["support_tier"], "ok")

    def test_normalizes_leading_markdown_list_markers(self) -> None:
        target = "/tmp/AGENTS.md"
        quote = "stay in scope"
        packet = self._packet(target=target, quote=quote)
        raw = {
            "packet_id": packet["packet_id"],
            "model": DEFAULT_MODEL,
            "abstain": False,
            "proposals": [
                {
                    "title": "Scope instruction",
                    "action": "add",
                    "target_path": target,
                    "heading": "Scope",
                    "instruction_rewrite": "- - Stay in scope.",
                    "rationale": "Repeated instruction.",
                    "does_not_prove": "Does not prove intent.",
                    "support_tier": "ok",
                    "sample_size": 11,
                    **self._proof(packet, target=target),
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
        self.assertEqual(
            validated["proposals"][0]["instruction_rewrite"], "Stay in scope."
        )

    def test_materialization_abstains_when_config_hash_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "AGENTS.md"
            original = "# Before\n"
            target.write_text(original, encoding="utf-8")
            quote = "stay in scope"
            packet = self._packet(target=str(target), quote=quote)
            packet["config_snippets"][0]["content_hash"] = hashlib.sha1(
                original.encode("utf-8")
            ).hexdigest()
            raw = {
                "packet_id": packet["packet_id"],
                "model": DEFAULT_MODEL,
                "abstain": False,
                "proposals": [
                    {
                        "title": "Scope instruction",
                        "action": "add",
                        "target_path": str(target),
                        "heading": "Scope",
                        "instruction_rewrite": "Stay in scope.",
                        "rationale": "Repeated instruction.",
                        "does_not_prove": "Does not prove intent.",
                        "support_tier": "ok",
                        "sample_size": 11,
                        **self._proof(packet, target=str(target)),
                        "config_gap": {
                            "target_path": str(target),
                            "content_hash": packet["config_snippets"][0]["content_hash"],
                            "finding": "The current rule does not state this instruction.",
                        },
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
            target.write_text("# Changed\n", encoding="utf-8")
            proposals, claims = materialize_proposals(
                validated=validated,
                packet=packet,
                inventory=ConfigInventory(home=Path(tmp)),
                now="2026-08-01T00:00:00+00:00",
            )
            self.assertEqual(proposals, [])
            self.assertEqual(claims, [])

    def test_rerun_preserves_semantic_proposal_and_claim_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "AGENTS.md"
            original = "# Before\n"
            target.write_text(original, encoding="utf-8")
            packet = self._packet(target=str(target), quote="stay in scope")
            for i, window in enumerate(packet["windows"]):
                window["request_message_id"] = f"request-{i}"
                window["response_message_id"] = f"response-{i}"
                window["assistant"] = "assistant-only evidence"
            packet["config_snippets"][0]["content_hash"] = hashlib.sha1(
                original.encode("utf-8")
            ).hexdigest()
            raw = {
                "packet_id": packet["packet_id"],
                "model": "untrusted-result-model",
                "abstain": False,
                "proposals": [
                    {
                        "title": "Scope instruction",
                        "action": "add",
                        "target_path": str(target),
                        "heading": "Scope",
                        "instruction_rewrite": "Stay in scope.",
                        "rationale": "Repeated instruction.",
                        "does_not_prove": "Does not prove intent.",
                        "support_tier": "ok",
                        "sample_size": 11,
                        **self._proof(packet, target=str(target)),
                        "config_gap": {
                            "target_path": str(target),
                            "content_hash": packet["config_snippets"][0]["content_hash"],
                            "finding": "The current rule does not state this instruction.",
                        },
                        "evidence": [
                            {
                                "session_id": f"codex:s{i}",
                                "window_id": f"w{i}",
                                "quote": "assistant-only evidence",
                            }
                            for i in range(11)
                        ],
                    }
                ],
            }
            validated, failures = validate_proposal_result(raw, packet=packet)
            self.assertEqual(failures, [])
            assert validated is not None
            inventory = ConfigInventory(
                home=Path(tmp),
                files=[
                    ConfigFile(
                        path=target,
                        kind="agents_md",
                        scope_type="repo",
                        scope_id="demo",
                        exists=True,
                    )
                ],
            )
            first_props, first_claims = materialize_proposals(
                validated=validated,
                packet=packet,
                inventory=inventory,
                now="2026-08-01T00:00:00+00:00",
            )
            self.assertEqual(first_props[0].model, DEFAULT_MODEL)
            self.assertEqual(
                first_props[0].provenance["reported_model_unverified"],
                "untrusted-result-model",
            )
            self.assertEqual(first_claims[0].evidence[0].message_id, "response-0")
            self.assertEqual(first_claims[0].evidence[0].meta["evidence_role"], "assistant")
            self.assertEqual(
                first_claims[0].evidence[0].meta["logical_root_id"], "root:0"
            )
            rerun_packet = json.loads(json.dumps(packet))
            rerun_packet["run_id"] = "proposals_rerun"
            rerun_packet["evidence_pack_hash"] = "pack2"
            second_props, second_claims = materialize_proposals(
                validated=validated,
                packet=rerun_packet,
                inventory=inventory,
                now="2026-08-02T00:00:00+00:00",
            )
            self.assertEqual(first_props[0].id, second_props[0].id)
            self.assertEqual(first_claims[0].id, second_claims[0].id)
            self.assertEqual(first_props[0].evidence_pack_hash, "pack1")
            self.assertEqual(second_props[0].evidence_pack_hash, "pack2")
            conn = connect(Path(tmp) / "rerun.db")
            init_db(conn)
            upsert_claims(conn, first_claims)
            upsert_proposals(conn, first_props)
            from agentlog.analysis.claims import packets as packets_mod

            packets_mod._record_superseded_evidence_versions(conn, second_props)
            self.assertEqual(
                second_props[0].provenance["superseded_evidence_versions"],
                [
                    {
                        "run_id": "proposals_test",
                        "prompt_hash": "abc",
                        "evidence_pack_hash": "pack1",
                        "model": DEFAULT_MODEL,
                    }
                ],
            )
            upsert_claims(conn, second_claims)
            upsert_proposals(conn, second_props)
            conn.commit()
            stored = next(
                prop for prop in list_proposals(conn, status="pending") if prop.id == first_props[0].id
            )
            self.assertEqual(stored.id, second_props[0].id)
            self.assertEqual(stored.evidence_pack_hash, "pack2")

    def test_rejects_duplicate_normalized_target_intent_heading(self) -> None:
        target = "/tmp/AGENTS.md"
        quote = "stay in scope"
        packet = self._packet(target=target, quote=quote)
        evidence = [
            {
                "session_id": f"codex:s{i}",
                "window_id": f"w{i}",
                "quote": quote,
            }
            for i in range(11)
        ]
        raw = {
            "packet_id": packet["packet_id"],
            "model": DEFAULT_MODEL,
            "abstain": False,
            "proposals": [
                {
                    "title": "Scope instruction",
                    "action": "add",
                    "target_path": target,
                    "heading": "Stay in scope",
                    "instruction_rewrite": "Stay in scope.",
                    "rationale": "Repeated instruction.",
                    "does_not_prove": "Does not prove intent.",
                    "support_tier": "ok",
                    "sample_size": 11,
                    **self._proof(packet, target=target),
                    "evidence": evidence,
                },
                {
                    "title": "Duplicate scope instruction",
                    "action": "add",
                    "target_path": target,
                    "heading": "stay-in-scope",
                    "instruction_rewrite": "Use the named scope.",
                    "rationale": "Repeated instruction.",
                    "does_not_prove": "Does not prove intent.",
                    "support_tier": "ok",
                    "sample_size": 11,
                    **self._proof(packet, target=target),
                    "evidence": evidence,
                },
            ],
        }
        validated, failures = validate_proposal_result(raw, packet=packet)
        self.assertIsNone(validated)
        self.assertTrue(
            any(
                f.reason == "duplicate_target_intent_within_packet"
                for f in failures
            )
        )

    def test_rejects_legacy_packet_without_current_provenance(self) -> None:
        packet = self._packet(target="/tmp/AGENTS.md", quote="x")
        packet.pop("validator_version")
        raw = {
            "packet_id": packet["packet_id"],
            "model": DEFAULT_MODEL,
            "abstain": True,
            "abstain_reason": "no proposal",
            "proposals": [],
        }
        validated, failures = validate_proposal_result(raw, packet=packet)
        self.assertIsNone(validated)
        self.assertTrue(
            any(
                f.reason == "legacy_packet_missing_current_provenance"
                for f in failures
            )
        )

    def test_rejects_packet_without_miss_pair_contract(self) -> None:
        packet = self._packet(target="/tmp/AGENTS.md", quote="x")
        packet.pop("evidence_contract")
        raw = {
            "packet_id": packet["packet_id"],
            "model": DEFAULT_MODEL,
            "abstain": True,
            "abstain_reason": "no proposal",
            "proposals": [],
        }
        validated, failures = validate_proposal_result(raw, packet=packet)
        self.assertIsNone(validated)
        self.assertTrue(
            any(
                f.reason == "legacy_packet_missing_current_provenance"
                for f in failures
            )
        )

    def test_rejects_legacy_packet_without_current_redaction(self) -> None:
        packet = self._packet(target="/tmp/AGENTS.md", quote="x")
        packet["redaction"]["redaction_version"] = "old_redaction_version"
        raw = {
            "packet_id": packet["packet_id"],
            "model": DEFAULT_MODEL,
            "abstain": True,
            "abstain_reason": "no proposal",
            "proposals": [],
        }
        validated, failures = validate_proposal_result(raw, packet=packet)
        self.assertIsNone(validated)
        self.assertTrue(
            any(
                f.reason == "legacy_packet_missing_current_provenance"
                for f in failures
            )
        )

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
                    **self._proof(packet, target=target),
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


class LogicalRootPacketTests(unittest.TestCase):
    def test_foreign_parent_references_remain_independent_eligible_roots(self) -> None:
        conn = connect(":memory:")
        init_db(conn)
        _seed_run(conn)
        for sid, harness in (
            ("codex:root", "codex"),
            ("cursor:qualified", "cursor"),
            ("claude:bare", "claude"),
        ):
            _seed_session(conn, sid=sid, harness=harness)
            _seed_substantive_window(
                conn,
                sid=sid,
                wid=f"w:{sid}",
                text=f"evidence for {sid}",
                turn_kinds=["correction"],
            )
        conn.execute(
            "UPDATE sessions SET parent_session_id = 'codex:root' "
            "WHERE id = 'cursor:qualified'"
        )
        conn.execute(
            "UPDATE sessions SET parent_session_id = 'root' "
            "WHERE id = 'claude:bare'"
        )
        conn.commit()

        logical_roots = packets_mod._session_logical_roots(conn)
        population = packets_mod._eligible_root_population(conn, logical_roots)
        self.assertEqual(
            packets_mod._eligible_root_session_ids(conn),
            {"codex:root", "cursor:qualified", "claude:bare"},
        )
        windows = packets_mod._fetch_theme_windows(
            conn,
            where_sql="1=1",
            limit=10,
            report=RedactionReport(),
            logical_roots=logical_roots,
        )
        self.assertEqual(population["root_cluster_count"], 3)
        self.assertEqual(
            {window["logical_root_id"] for window in windows},
            {"codex:root", "cursor:qualified", "claude:bare"},
        )
        conn.close()

    def test_window_selection_reaches_independent_roots_beyond_duplicate_burst(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            init_db(conn)
            _seed_run(conn)
            _seed_session(conn, sid="codex:duplicate-root")
            for i in range(81):
                _seed_substantive_window(
                    conn,
                    sid="codex:duplicate-root",
                    wid=f"duplicate:{i}",
                    text=f"duplicate evidence {i}",
                    turn_kinds=["correction"],
                    seq=i * 2 + 1,
                )
            for i in range(10):
                sid = f"codex:independent-{i}"
                _seed_session(conn, sid=sid)
                _seed_substantive_window(
                    conn,
                    sid=sid,
                    wid=f"independent:{i}",
                    text=f"independent evidence {i}",
                    turn_kinds=["correction"],
                )
            logical_roots = packets_mod._session_logical_roots(conn)
            windows = packets_mod._fetch_theme_windows(
                conn,
                where_sql="1=1",
                limit=10,
                report=RedactionReport(),
                logical_roots=logical_roots,
            )
            self.assertEqual(len(windows), 10)
            self.assertEqual(
                len({window["logical_root_id"] for window in windows}), 10
            )

    def test_t3_orchestrator_and_codex_backing_share_one_logical_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.db")
            init_db(conn)
            _seed_run(conn)
            _seed_session(conn, sid="t3code:root", harness="t3code")
            _seed_session(conn, sid="codex:backing", harness="codex")
            t3_artifact = conn.execute(
                """
                INSERT INTO artifacts
                  (harness, path, size, mtime_ns, content_hash, parsed_offset,
                   parser_version, transcript_storage)
                VALUES ('t3code', '/tmp/t3-promoted.jsonl', 1, 1, 't3', 1,
                        'test', 'source_backed')
                """
            ).lastrowid
            backing_artifact = conn.execute(
                """
                INSERT INTO artifacts
                  (harness, path, size, mtime_ns, content_hash, parsed_offset,
                   parser_version, transcript_storage)
                VALUES ('codex', '/tmp/codex-promoted.jsonl', 1, 1, 'codex', 1,
                        'test', 'source_backed')
                """
            ).lastrowid
            conn.execute(
                """
                UPDATE sessions
                SET artifact_id = ?, transcript_storage = 'source_backed', provider = 'openai'
                WHERE id = 't3code:root'
                """,
                (t3_artifact,),
            )
            conn.execute(
                """
                UPDATE sessions
                SET artifact_id = ?, transcript_storage = 'source_backed', provider = 'openai'
                WHERE id = 'codex:backing'
                """,
                (backing_artifact,),
            )
            _seed_substantive_window(
                conn,
                sid="t3code:root",
                wid="t3-window",
                text="stay in scope",
                turn_kinds=["correction"],
            )
            _seed_substantive_window(
                conn,
                sid="codex:backing",
                wid="codex-window",
                text="stay in scope",
                turn_kinds=["correction"],
            )
            conn.execute(
                """
                INSERT INTO session_links (
                    source_session_id, target_session_id, link_type,
                    target_harness, target_external_id
                ) VALUES ('t3code:root', 'codex:backing', 'provider_backing', 'codex', 'backing')
                """
            )
            from agentlog.analysis.claims import packets as packets_mod

            logical_roots = packets_mod._session_logical_roots(conn)
            population = packets_mod._eligible_root_population(conn, logical_roots)
            self.assertEqual(
                logical_roots["codex:backing"]["logical_root_id"], "t3code:root"
            )
            self.assertEqual(population["root_cluster_count"], 1)
            self.assertEqual(
                packets_mod._eligible_root_session_ids(conn), {"t3code:root"}
            )
            self.assertEqual(
                population["by_harness"],
                [{"harness": "t3code", "root_cluster_count": 1}],
            )


class PacketIntegrityTests(unittest.TestCase):
    def _emit_run(self, root: Path) -> tuple[object, Path, dict, str, Path]:
        home = root / "home"
        home.mkdir()
        agents = home / "AGENTS.md"
        agents.write_text("# Global\n", encoding="utf-8")
        conn = connect(root / "t.db")
        init_db(conn)
        _seed_run(conn)
        for i in range(15):
            sid = f"codex:s{i}"
            _seed_session(conn, sid=sid, cwd=str(root / f"demo-{i % 3}"))
            _seed_substantive_window(
                conn,
                sid=sid,
                wid=f"w{i}",
                text="only edit the named files",
                turn_kinds=["correction"],
                flags={"scope_narrowing": True},
            )
            _seed_adjudicated_miss_pair(conn, wid=f"w{i}")
        conn.commit()
        run_dir = root / "proposals-run"
        manifest = emit_proposal_packet_run(
            conn, run_dir, home=home, resume=False, windows_per_theme=15
        )
        packet_id = next(
            packet_id
            for packet_id, meta in manifest["packets"].items()
            if meta["session_count"] >= 15
        )
        return conn, run_dir, manifest, packet_id, agents

    def test_ingest_rejects_tampered_evidence_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn, run_dir, manifest, packet_id, _ = self._emit_run(root)
            packet_path = run_dir / manifest["packets"][packet_id]["packet_path"]
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            packet["theme"] = "tampered_theme"
            packet_path.write_text(json.dumps(packet), encoding="utf-8")
            results = ingest_proposal_packet_results(conn, run_dir, home=root / "home")
            result = next(item for item in results if item.packet_id == packet_id)
            self.assertEqual(result.status, "ineligible")
            self.assertEqual(result.failures[0].reason, "packet_evidence_hash_mismatch")

    def test_ingest_rejects_tampered_target_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn, run_dir, manifest, packet_id, _ = self._emit_run(root)
            meta = manifest["packets"][packet_id]
            target_ref = next(iter(meta["target_paths"]))
            meta["target_paths"][target_ref] = "/tmp/not-the-emitted-target"
            (run_dir / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            results = ingest_proposal_packet_results(conn, run_dir, home=root / "home")
            result = next(item for item in results if item.packet_id == packet_id)
            self.assertEqual(result.status, "ineligible")
            self.assertEqual(result.failures[0].reason, "packet_target_bindings_mismatch")

    def test_incomplete_run_does_not_supersede_pending_llm_cards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn, run_dir, _, _, agents = self._emit_run(root)
            old = Proposal(
                id="old-llm-proposal",
                title="Old LLM proposal",
                action="add",
                status="pending",
                target_path=str(agents),
                target_kind="agents_md",
                scope_type="global",
                scope_id="global",
                base_content_hash=None,
                unified_diff="",
                proposed_content=None,
                rationale="Old packet result.",
                derivation_summary="LLM packet proposal (old run)",
                run_id="proposals_prior",
            )
            upsert_proposals(conn, [old])
            conn.commit()
            stats = publish_llm_proposals_from_run(conn, run_dir, home=root / "home")
            self.assertFalse(stats["publish_ready"])
            self.assertEqual(stats["proposals_pruned"], 0)
            self.assertTrue(
                any(prop.id == old.id for prop in list_proposals(conn, status="pending"))
            )

    def test_complete_all_abstain_requires_explicit_quarantine_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn, run_dir, manifest, _, agents = self._emit_run(root)
            theme = str(next(iter(manifest["packets"].values()))["theme"])
            matching = Proposal(
                id="old-matching-theme",
                title="Old matching proposal",
                action="add",
                status="pending",
                target_path=str(agents),
                target_kind="agents_md",
                scope_type="global",
                scope_id="global",
                base_content_hash=None,
                unified_diff="",
                proposed_content=None,
                rationale="Old packet result.",
                derivation_summary=(
                    "LLM packet proposal (cursor_subagent_packet/test); "
                    f"theme={theme}; support=observed"
                ),
                run_id="proposals_prior",
            )
            unmatched = Proposal(
                id="old-other-theme",
                title="Old other proposal",
                action="add",
                status="pending",
                target_path=str(agents),
                target_kind="agents_md",
                scope_type="global",
                scope_id="global",
                base_content_hash=None,
                unified_diff="",
                proposed_content=None,
                rationale="Old packet result.",
                derivation_summary=(
                    "LLM packet proposal (cursor_subagent_packet/test); "
                    "theme=other_theme; support=observed"
                ),
                run_id="proposals_prior",
            )
            upsert_proposals(conn, [matching, unmatched])
            for packet_id in manifest["packets"]:
                (run_dir / "results" / f"{packet_id}.json").write_text(
                    json.dumps(
                        {
                            "packet_id": packet_id,
                            "model": DEFAULT_MODEL,
                            "abstain": True,
                            "abstain_reason": "no safe proposal",
                        }
                    ),
                    encoding="utf-8",
                )
            conn.commit()

            withheld = publish_llm_proposals_from_run(
                conn, run_dir, home=root / "home"
            )
            self.assertTrue(withheld["complete_all_abstain"])
            self.assertFalse(withheld["publish_ready"])
            self.assertEqual(withheld["publication_block_reason"], "complete_all_abstain")
            self.assertEqual(withheld["proposals_pruned"], 0)
            self.assertEqual(
                {p.id for p in list_proposals(conn, status="pending")},
                {matching.id, unmatched.id},
            )

            authorized = publish_llm_proposals_from_run(
                conn,
                run_dir,
                home=root / "home",
                quarantine_on_all_abstain=True,
            )
            self.assertTrue(authorized["publish_ready"])
            self.assertTrue(authorized["all_abstain_quarantine_authorized"])
            self.assertEqual(authorized["proposals_pruned"], 1)
            self.assertEqual(
                {p.id for p in list_proposals(conn, status="pending")},
                {unmatched.id},
            )
            self.assertTrue(
                any(
                    proposal.id == matching.id
                    for proposal in list_proposals(conn, status="superseded")
                )
            )


class PacketIngestMockTests(unittest.TestCase):
    def test_partial_run_withholds_then_complete_run_publishes(self) -> None:
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
            for i in range(15):
                harness = "codex" if i < 8 else "claude"
                sid = f"{harness}:s{i}"
                _seed_session(
                    conn,
                    sid=sid,
                    harness=harness,
                    repo=f"demo-{i % 4}",
                    cwd=str(root / f"demo-{i % 4}"),
                )
                _seed_substantive_window(
                    conn,
                    sid=sid,
                    wid=f"w{i}",
                    text=quote,
                    turn_kinds=["correction"],
                    flags={"scope_narrowing": True},
                )
                _seed_adjudicated_miss_pair(conn, wid=f"w{i}")
            for i in range(15, 19):
                sid = f"codex:other-{i}"
                _seed_session(conn, sid=sid, cwd=str(root / "demo"))
                _seed_substantive_window(
                    conn,
                    sid=sid,
                    wid=f"other-{i}",
                    text="routine status update",
                    turn_kinds=["progress"],
                    flags={"scope_narrowing": False},
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
                    conn, run_dir, home=home, resume=False, windows_per_theme=15
                )
                self.assertGreaterEqual(manifest["packet_count"], 1)
                self.assertEqual(
                    manifest["eligible_population"]["root_cluster_count"], 19
                )
                self.assertEqual(
                    manifest["eligible_population"]["by_harness"],
                    [
                        {"harness": "claude", "root_cluster_count": 7},
                        {"harness": "codex", "root_cluster_count": 12},
                    ],
                )
                # Prefer a packet that actually got windows.
                packet_id = None
                for pid, meta in manifest["packets"].items():
                    if meta.get("session_count", 0) >= 15:
                        packet_id = pid
                        break
                self.assertIsNotNone(packet_id)
                assert packet_id is not None
                packet_path = run_dir / manifest["packets"][packet_id]["packet_path"]
                packet = json.loads(packet_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    packet["eligible_population"]["root_cluster_count"], 19
                )
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
                self.assertGreaterEqual(len(evidence), 15)
                duplicate_packet_id = "ppkt_duplicate_scope_narrow"
                duplicate_packet = dict(packet)
                duplicate_packet["packet_id"] = duplicate_packet_id
                duplicate_packet["evidence_pack_hash"] = packets_mod._packet_hash(
                    duplicate_packet
                )
                duplicate_packet_path = run_dir / "packets" / f"{duplicate_packet_id}.json"
                duplicate_packet_path.write_text(
                    json.dumps(duplicate_packet, indent=2) + "\n", encoding="utf-8"
                )
                duplicate_meta = dict(manifest["packets"][packet_id])
                duplicate_meta["packet_path"] = str(
                    duplicate_packet_path.relative_to(run_dir)
                )
                duplicate_meta["evidence_pack_hash"] = duplicate_packet[
                    "evidence_pack_hash"
                ]
                manifest["packets"][duplicate_packet_id] = duplicate_meta
                legacy_packet_id = next(
                    pid
                    for pid in manifest["packets"]
                    if pid not in {packet_id, duplicate_packet_id}
                )
                legacy_path = run_dir / manifest["packets"][legacy_packet_id]["packet_path"]
                legacy_packet = json.loads(legacy_path.read_text(encoding="utf-8"))
                legacy_packet.pop("validator_version")
                legacy_path.write_text(
                    json.dumps(legacy_packet, indent=2) + "\n", encoding="utf-8"
                )
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
                                "- Stay inside the files and scope the user named."
                            ),
                            "rationale": "LLM-authored from substantive corrections.",
                            "does_not_prove": "Not causal proof of scope failures.",
                            "support_tier": "ok",
                            "sample_size": len(evidence),
                            "pattern_key": packet["theme"],
                            "validated_miss_pair_ids": [
                                pair["pair_id"]
                                for pair in packet["validated_miss_pairs"][:3]
                            ],
                            "config_gap": {
                                "target_path": target,
                                "content_hash": next(
                                    snippet["content_hash"]
                                    for snippet in packet["config_snippets"]
                                    if snippet["target_path"] == target
                                ),
                                "finding": "The file lacks this standing rule.",
                            },
                            "evidence": evidence,
                        }
                    ],
                }
                (run_dir / "results" / f"{packet_id}.json").write_text(
                    json.dumps(result, indent=2) + "\n", encoding="utf-8"
                )
                duplicate_result = dict(result)
                duplicate_result["packet_id"] = duplicate_packet_id
                (run_dir / "results" / f"{duplicate_packet_id}.json").write_text(
                    json.dumps(duplicate_result, indent=2) + "\n", encoding="utf-8"
                )
                (run_dir / "manifest.json").write_text(
                    json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
                )
                old_llm = Proposal(
                    id="old-llm-proposal",
                    title="Old LLM proposal",
                    action="add",
                    status="pending",
                    target_path=str(agents),
                    target_kind="agents_md",
                    scope_type="global",
                    scope_id="global",
                    base_content_hash=None,
                    unified_diff="",
                    proposed_content=None,
                    rationale="Old packet result.",
                    derivation_summary="LLM packet proposal (old run)",
                    run_id="proposals_prior",
                )
                unrelated = Proposal(
                    id="unrelated-run-proposal",
                    title="Unrelated pending proposal",
                    action="add",
                    status="pending",
                    target_path=str(agents),
                    target_kind="agents_md",
                    scope_type="global",
                    scope_id="global",
                    base_content_hash=None,
                    unified_diff="",
                    proposed_content=None,
                    rationale="Not a packet proposal.",
                    derivation_summary="Manual review proposal",
                    run_id="unrelated_batch",
                )
                upsert_proposals(conn, [old_llm, unrelated])
                conn.commit()
                stats = publish_llm_proposals_from_run(conn, run_dir, home=home)
            finally:
                packets_mod.discover_config_inventory = real_discover  # type: ignore[assignment]

            completed = [
                result for result in stats["results"] if result["status"] == "completed"
            ]
            props = list_proposals(conn, status="pending")
            llm_props = [
                p
                for p in props
                if (p.derivation_summary or "").startswith("LLM")
                and p.id != old_llm.id
            ]
            if not completed:
                self.fail(f"no completed packets: {stats['results']}")
            self.assertFalse(llm_props)
            self.assertEqual(stats["packets_ineligible"], 1)
            self.assertEqual(stats["packets_abstained"], 1)
            self.assertFalse(stats["publish_ready"])
            self.assertEqual(stats["proposals_staged"], 1)
            self.assertEqual(stats["proposals_upserted"], 0)
            self.assertEqual(stats["proposals_pruned"], 0)
            superseded = list_proposals(conn, status="superseded")
            self.assertFalse(any(prop.id == old_llm.id for prop in superseded))
            self.assertTrue(
                any(prop.id == unrelated.id for prop in list_proposals(conn, status="pending"))
            )

            legacy_packet["validator_version"] = PROPOSAL_PACKET_VALIDATOR_VERSION
            legacy_path.write_text(
                json.dumps(legacy_packet, indent=2) + "\n", encoding="utf-8"
            )
            for pending_packet_id in manifest["packets"]:
                result_path = run_dir / "results" / f"{pending_packet_id}.json"
                if result_path.exists():
                    continue
                result_path.write_text(
                    json.dumps(
                        {
                            "packet_id": pending_packet_id,
                            "model": DEFAULT_MODEL,
                            "abstain": True,
                            "abstain_reason": "no safe proposal",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            published = publish_llm_proposals_from_run(conn, run_dir, home=home)
            published_props = [
                p
                for p in list_proposals(conn, status="pending")
                if (p.derivation_summary or "").startswith("LLM")
            ]
            self.assertTrue(published["publish_ready"])
            self.assertEqual(published["proposals_upserted"], 1)
            self.assertEqual(published["proposals_pruned"], 1)
            self.assertEqual(len(published_props), 1)
            self.assertEqual(published_props[0].model, DEFAULT_MODEL)
            self.assertEqual(published_props[0].target_path, str(agents))
            self.assertEqual(published_props[0].sample_size, len(evidence))
            self.assertEqual(published_props[0].claims[0].denominator, 19)
            self.assertEqual(published_props[0].claims[0].rate, len(evidence) / 19)
            self.assertNotIn("+- -", published_props[0].unified_diff)
            self.assertIn("+- Stay inside", published_props[0].unified_diff)
            self.assertTrue(
                any(
                    proposal.id == old_llm.id
                    for proposal in list_proposals(conn, status="superseded")
                )
            )
            self.assertEqual(
                agents.read_text(encoding="utf-8"), f"# Global\n\napi_key = {secret}\n"
            )


if __name__ == "__main__":
    unittest.main()
