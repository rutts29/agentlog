from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.claims.models import Claim, ClaimEvidence, Proposal
from agentlog.analysis.claims.store import upsert_claims, upsert_proposals
from agentlog.analysis.insights import import_session_fact_packet
from agentlog.api.queries import _insight_provenance, insights_feed
from agentlog.api.ranges import parse_range
from agentlog.db.schema import connect, init_db
from agentlog.ingest.base import content_hash_text, hash_prefix


def _codex_message(role: str, text: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [
                        {
                            "type": (
                                "input_text" if role == "user" else "output_text"
                            ),
                            "text": text,
                        }
                    ],
                },
            }
        )
        + "\n"
    ).encode()


def _claim(**kwargs) -> Claim:
    base = dict(
        id="c1",
        kind="recurring_instruction",
        subject="scope_narrow",
        predicate="observed_in_labeled_windows",
        value={
            "theme": "scope_narrow",
            "phrasing": "theme scope_narrow matched 11/374 sessions",
            "suggested_instruction": "- Prefer the smallest change.",
        },
        scope_type="global",
        scope_id="global",
        derivation="llm_derived",
        status="approved",
        support_status="ok",
        sample_size=11,
        observed_at="2026-08-09T12:00:00+00:00",
        does_not_prove="Does not prove the instruction is missing.",
        created_at="2026-08-09T12:00:00+00:00",
        updated_at="2026-08-09T12:00:00+00:00",
    )
    base.update(kwargs)
    return Claim(**base)


def _coach_provenance(
    *,
    processed: int = 374,
    eligible: int = 374,
    calibrated_sampling_gate: dict | None = None,
    proof_capability_by_harness: dict | None = None,
    include_proof_capability: bool = True,
) -> dict:
    provenance = {
        "provider": "coach_pipeline",
        "processed_roots": processed,
        "eligible_roots": eligible,
        "full_eligible_root_denominator": eligible,
        "coverage_state": "complete" if processed == eligible else "partial",
        "selection_method": "score_then_temporal_strata",
        "selection_caveat": (
            "Only a sampled subset was processed; the support count is not corpus "
            "prevalence or recurrence."
            if processed < eligible
            else "Selection is descriptive and does not establish causality."
        ),
        "coverage": {
            "spkt-1": {
                "processed_roots": processed,
                "eligible_roots": eligible,
                "selected_roots": processed,
            }
        },
    }
    if calibrated_sampling_gate is not None:
        provenance["calibrated_sampling_gate"] = calibrated_sampling_gate
    if include_proof_capability:
        provenance["proof_capability_by_harness"] = (
            proof_capability_by_harness
            if proof_capability_by_harness is not None
            else {
                "codex": {
                    "processed_roots": processed,
                    "eligible_roots": eligible,
                    "proof_capable_roots": eligible,
                    "levels": {
                        "deterministic_terminal": eligible,
                        "owner_message_only": 0,
                        "unknown": 0,
                    },
                    "capability": "supported",
                    "capability_complete": processed == eligible,
                }
            }
        )
    return provenance


def _proposal(**kwargs) -> Proposal:
    base = dict(
        id="p1",
        title="Add standing minimal-scope rule",
        action="add",
        status="pending",
        target_path="~/AGENTS.md",
        target_kind="agents_md",
        scope_type="global",
        scope_id="global",
        base_content_hash=None,
        unified_diff="--- a\n+++ b\n+bullet\n",
        proposed_content="- Prefer the smallest change.",
        rationale="Among labeled sessions, scope_narrow matched 11/374.",
        derivation_summary="llm instruction proposal",
        does_not_prove="Does not prove the edit would help.",
        sample_size=11,
        claim_ids=[],
        created_at="2026-08-09T13:00:00+00:00",
        updated_at="2026-08-09T13:00:00+00:00",
        model="gpt-5.6-terra",
        run_id="proposals_test",
        provenance=_coach_provenance(),
    )
    base.update(kwargs)
    return Proposal(**base)


class InsightsFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "insights.db"
        self.conn = connect(self.path)
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_insight_provenance_uses_bound_terra_replay_lineage(self) -> None:
        provenance = _insight_provenance(
            derivation="llm_derived",
            extractor="coach_materializer",
            extractor_version="coach-materialize.v2",
            basis={
                "catalog_id": "catalog_123",
                "materializer_version": "coach-materialize.v2",
                "run_replay": {
                    "terra_synthesis_producer": {
                        "provider": "openai",
                        "model": "gpt-terra",
                        "worker_id": "terra-synthesis",
                    },
                    "terra_review_producer": {
                        "provider": "openai",
                        "model": "gpt-review",
                        "worker_id": "terra-review",
                    },
                    "terra_review_id": "review_123",
                    "terra_synthesis_results": [
                        {"packet_id": "spkt_1", "result_id": "terra_1"}
                    ],
                },
            },
        )
        self.assertEqual(provenance["source"], "terra_synthesis")
        self.assertEqual(provenance["model"], "gpt-terra")
        self.assertEqual(provenance["synthesis_model"], "gpt-terra")
        self.assertEqual(provenance["review_model"], "gpt-review")
        self.assertEqual(provenance["catalog_id"], "catalog_123")
        self.assertEqual(provenance["review_id"], "review_123")
        self.assertEqual(provenance["source_packet_ids"], ["spkt_1"])
        self.assertEqual(provenance["source_result_ids"], ["terra_1"])
        self.assertEqual(provenance["review_state"], "Terra synthesis and second review bound")

    def test_empty_feed_keeps_empty_payload(self) -> None:
        tr = parse_range("all")
        body = insights_feed(self.conn, tr)
        self.assertEqual(body["items"], [])
        self.assertIn("empty", body)
        self.assertTrue(body["empty"]["title"])

        bounded = insights_feed(
            self.conn,
            parse_range(
                "custom",
                custom_start="2026-08-01T00:00:00+00:00",
                custom_end="2026-08-02T00:00:00+00:00",
            ),
        )
        self.assertEqual(bounded["items"], [])
        self.assertEqual(
            bounded["empty"]["missing"],
            ["observed instances", "corpus patterns", "coach proposals"],
        )

    def test_feed_shows_pending_proposals_and_only_reviewed_ok_claims(self) -> None:
        upsert_claims(
            self.conn,
            [
                _claim(
                    id="c_ok",
                    subject="scope_narrow",
                    sample_size=11,
                    denominator=374,
                    support_status="ok",
                ),
                _claim(
                    id="c_insuf",
                    kind="harness_model_usage",
                    subject="demo",
                    predicate="usage_mix",
                    value={"phrasing": "project demo had 7 sessions"},
                    support_status="insufficient",
                    sample_size=7,
                    derivation="deterministic",
                ),
                _claim(
                    id="c_abstain",
                    subject="spawn_workers",
                    support_status="abstain",
                    sample_size=1,
                ),
                _claim(
                    id="c_candidate",
                    subject="unreviewed",
                    support_status="ok",
                    status="candidate",
                    sample_size=20,
                ),
                _claim(
                    id="c_unused",
                    kind="skill_unused",
                    subject="rare-skill",
                    predicate="zero_exposures",
                    value={"phrasing": "unused"},
                    support_status="ok",
                    sample_size=80,
                    derivation="deterministic",
                ),
                _claim(
                    id="c_corr",
                    kind="correction_theme",
                    subject="correction",
                    predicate="rate",
                    value={
                        "phrasing": "correction label rate 0.12 (n=60)",
                    },
                    support_status="ok",
                    sample_size=60,
                    derivation="llm_derived",
                    does_not_prove="Not a quality score.",
                ),
            ],
        )
        upsert_proposals(self.conn, [_proposal()])
        self.conn.commit()

        body = insights_feed(self.conn, parse_range("all"))
        kinds = [i["kind"] for i in body["items"]]
        sources = [i["source"] for i in body["items"]]
        titles = [i["title"] for i in body["items"]]

        self.assertEqual(sources[0], "proposal")
        self.assertEqual(kinds[0], "coach")
        self.assertIn("Add standing minimal-scope rule", titles[0])

        # Unreviewed, insufficient, abstained, and unsupported kinds stay hidden.
        claim_ids = {
            i["source_id"] for i in body["items"] if i["source"] == "claim"
        }
        self.assertIn("c_ok", claim_ids)
        self.assertIn("c_corr", claim_ids)
        self.assertNotIn("c_insuf", claim_ids)
        self.assertNotIn("c_abstain", claim_ids)
        self.assertNotIn("c_candidate", claim_ids)
        self.assertNotIn("c_unused", claim_ids)

        fact_cards = [i for i in body["items"] if i["source"] == "claim"]
        self.assertTrue(all(card["confidence"] == "ok" for card in fact_cards))
        self.assertTrue(
            all(card["review_state"] in {"approved", "published"} for card in fact_cards)
        )
        coach = body["items"][0]
        self.assertEqual(coach["href"], "/proposals")
        self.assertTrue(coach["does_not_prove"])
        self.assertEqual(coach["insight_type"], "coach_proposal")
        self.assertEqual(coach["review_state"], "pending")
        self.assertIn("provenance", coach)

        corpus = next(item for item in body["items"] if item["source_id"] == "c_ok")
        self.assertEqual(corpus["insight_type"], "corpus_pattern")
        self.assertEqual(corpus["review_state"], "approved")
        self.assertEqual(corpus["denominator"], 374)
        self.assertEqual(corpus["coverage"], "support n=11 of denominator=374")

    def test_pending_coach_cards_coalesce_exact_semantic_duplicates(self) -> None:
        first = _proposal(
            id="proposal-old",
            created_at="2026-08-09T10:00:00+00:00",
            updated_at="2026-08-09T10:00:00+00:00",
            provenance={**_coach_provenance(), "semantic_identity": "global:scope"},
        )
        second = _proposal(
            id="proposal-new",
            created_at="2026-08-09T11:00:00+00:00",
            updated_at="2026-08-09T11:00:00+00:00",
            provenance={**_coach_provenance(), "semantic_identity": "global:scope"},
        )
        upsert_proposals(self.conn, [first, second])
        self.conn.commit()

        body = insights_feed(self.conn, parse_range("all"))
        proposal_cards = [item for item in body["items"] if item["source"] == "proposal"]
        self.assertEqual([item["source_id"] for item in proposal_cards], ["proposal-new"])

    def test_partial_run_qualification_and_proposal_coverage_gate(self) -> None:
        coach_value = {
            "title": "Verification misses recurred",
            "summary": "Verification misses recurred across the corpus.",
            "canonical": {
                "scope": "harness_codex",
                "subject": "verification",
                "predicate": "instruction_miss",
                "polarity": "negative",
            },
        }
        partial_provenance = _coach_provenance(processed=8, eligible=20)
        upsert_claims(
            self.conn,
            [
                _claim(
                    id="partial-instance",
                    kind="coach_observed_instance",
                    subject="verification",
                    predicate="instruction_miss",
                    value=coach_value,
                    sample_size=1,
                    denominator=20,
                    confidence_basis=partial_provenance,
                    evidence=[
                        ClaimEvidence(
                            session_id="physical-session-0",
                            message_id="message-0",
                            quote="The verification result was missing.",
                            meta={"logical_root_session_id": "logical-root-0"},
                        )
                    ],
                ),
                _claim(
                    id="partial-pattern",
                    kind="coach_corpus_pattern",
                    subject="verification",
                    predicate="instruction_miss",
                    value=coach_value,
                    sample_size=5,
                    denominator=20,
                    confidence_basis=partial_provenance,
                ),
                _claim(
                    id="complete-pattern",
                    kind="coach_corpus_pattern",
                    subject="verification",
                    predicate="instruction_miss",
                    value=coach_value,
                    sample_size=5,
                    denominator=20,
                    confidence_basis=_coach_provenance(processed=20, eligible=20),
                ),
                _claim(
                    id="unknown-coverage",
                    kind="coach_corpus_pattern",
                    subject="verification",
                    predicate="instruction_miss",
                    value=coach_value,
                    sample_size=5,
                    denominator=20,
                    confidence_basis={"provider": "coach_pipeline"},
                ),
                _claim(
                    id="unknown-proof-capability",
                    kind="coach_corpus_pattern",
                    subject="verification",
                    predicate="instruction_miss",
                    value=coach_value,
                    sample_size=5,
                    denominator=20,
                    confidence_basis=_coach_provenance(
                        processed=20,
                        eligible=20,
                        proof_capability_by_harness={
                            "codex": {
                                "processed_roots": 15,
                                "eligible_roots": 15,
                                "proof_capable_roots": 15,
                                "levels": {
                                    "deterministic_terminal": 15,
                                    "owner_message_only": 0,
                                    "unknown": 0,
                                },
                                "capability": "supported",
                                "capability_complete": True,
                            },
                            "warp": {
                                "processed_roots": 5,
                                "eligible_roots": 5,
                                "proof_capable_roots": 0,
                                "levels": {
                                    "deterministic_terminal": 0,
                                    "owner_message_only": 5,
                                    "unknown": 0,
                                },
                                "capability": "absent",
                                "capability_complete": False,
                            },
                            "mystery": {},
                        },
                    ),
                ),
            ],
        )
        calibrated_gate = {
            "passed": True,
            "method": "stratified_reweighting",
            "calibration_id": "calibration-1",
            "validator_version": "sampling-gate.v1",
        }
        upsert_proposals(
            self.conn,
            [
                _proposal(
                    id="complete-proposal",
                    provenance=_coach_provenance(processed=20, eligible=20),
                    sample_size=10,
                ),
                _proposal(
                    id="partial-proposal",
                    provenance=partial_provenance,
                    sample_size=5,
                ),
                _proposal(
                    id="calibrated-proposal",
                    provenance=_coach_provenance(
                        processed=8,
                        eligible=20,
                        calibrated_sampling_gate=calibrated_gate,
                    ),
                    sample_size=5,
                ),
                _proposal(id="unknown-proposal", provenance={}, sample_size=10),
                _proposal(
                    id="missing-capability-proposal",
                    provenance=_coach_provenance(
                        processed=20,
                        eligible=20,
                        include_proof_capability=False,
                    ),
                    sample_size=10,
                ),
                _proposal(
                    id="unknown-capability-proposal",
                    provenance=_coach_provenance(
                        processed=20,
                        eligible=20,
                        proof_capability_by_harness={"warp": "unknown"},
                    ),
                    sample_size=10,
                ),
                _proposal(
                    id="calibrated-unknown-capability-proposal",
                    provenance=_coach_provenance(
                        processed=8,
                        eligible=20,
                        calibrated_sampling_gate=calibrated_gate,
                        proof_capability_by_harness={"warp": "unknown"},
                    ),
                    sample_size=5,
                ),
            ],
        )
        self.conn.commit()

        items = insights_feed(self.conn, parse_range("all"))["items"]
        by_id = {item["source_id"]: item for item in items}
        self.assertNotIn("unknown-coverage", by_id)
        self.assertNotIn("partial-proposal", by_id)
        self.assertNotIn("unknown-proposal", by_id)
        self.assertNotIn("missing-capability-proposal", by_id)
        self.assertNotIn("unknown-capability-proposal", by_id)
        self.assertNotIn("calibrated-unknown-capability-proposal", by_id)
        self.assertIn("complete-proposal", by_id)
        self.assertIn("calibrated-proposal", by_id)

        partial = by_id["partial-instance"]
        self.assertEqual(partial["supporting_roots"], 1)
        self.assertEqual(partial["processed_roots"], 8)
        self.assertEqual(partial["eligible_roots"], 20)
        self.assertEqual(partial["coverage_state"], "partial")
        self.assertEqual(partial["processing_coverage_state"], "partial")
        self.assertTrue(partial["title"].startswith("Sampled run ·"))
        self.assertTrue(partial["body"].startswith("Sampled-run finding"))
        self.assertIn("not corpus prevalence or recurrence", partial["selection_caveat"])
        self.assertEqual(
            partial["href"], "/sessions/physical-session-0?msg=message-0"
        )

        sampled_pattern = by_id["partial-pattern"]
        self.assertEqual(sampled_pattern["coverage_state"], "partial")
        self.assertTrue(sampled_pattern["title"].startswith("Sampled run ·"))
        self.assertTrue(sampled_pattern["body"].startswith("Sampled-run finding"))
        self.assertEqual(by_id["complete-pattern"]["coverage_state"], "complete")
        self.assertEqual(
            by_id["complete-pattern"]["processing_coverage_state"], "complete"
        )
        unknown_capability = by_id["unknown-proof-capability"]
        self.assertEqual(unknown_capability["coverage_state"], "partial")
        self.assertEqual(
            unknown_capability["processing_coverage_state"], "complete"
        )
        self.assertTrue(unknown_capability["title"].startswith("Evidence-limited ·"))
        self.assertTrue(
            unknown_capability["body"].startswith("Evidence-limited finding")
        )
        self.assertEqual(
            unknown_capability["proof_capability_by_harness"]["warp"]["level"],
            "absent",
        )
        self.assertEqual(
            unknown_capability["proof_capability_by_harness"]["mystery"]["level"],
            "unknown",
        )
        self.assertEqual(
            unknown_capability["proof_capability_by_harness"]["codex"][
                "proof_capable_roots"
            ],
            15,
        )
        self.assertIn(
            "remain in the eligible denominator",
            unknown_capability["proof_capability_caveat"],
        )
        self.assertTrue(
            by_id["calibrated-proposal"]["title"].startswith("Calibrated sample ·")
        )
        self.assertIn("stratified_reweighting", by_id["calibrated-proposal"]["sampling_gate"])

    def test_demo_run_is_hidden_but_legitimate_session_fact_remains(self) -> None:
        facts = []
        for claim_id, run_id in (
            ("demo", "insights-session-demo"),
            ("legitimate", "coach-observations-001"),
        ):
            facts.append(
                _claim(
                    id=claim_id,
                    kind="session_fact",
                    subject="follow",
                    predicate="observed_in_session",
                    value={
                        "title": f"{claim_id} observation",
                        "phrasing": "A bounded transcript observation.",
                        "theme": "follow",
                    },
                    sample_size=1,
                    denominator=1,
                    confidence_basis={
                        "run_id": run_id,
                        "source": "session_llm_facts",
                    },
                )
            )
        upsert_claims(self.conn, facts)
        self.conn.commit()

        items = insights_feed(self.conn, parse_range("all"))["items"]
        self.assertEqual([item["source_id"] for item in items], ["legitimate"])
        self.assertEqual(items[0]["insight_type"], "observed_instance")
        self.assertEqual(
            items[0]["coverage"],
            "one transcript instance; no corpus-pattern inference",
        )

    def test_bounded_range_does_not_fall_back_to_old_claims_or_proposals(self) -> None:
        upsert_claims(
            self.conn,
            [
                _claim(id="old", observed_at="2026-07-01T12:00:00+00:00"),
                _claim(id="current", observed_at="2026-08-09T12:00:00+00:00"),
            ],
        )
        upsert_proposals(
            self.conn,
            [
                _proposal(
                    id="old-proposal", created_at="2026-07-01T12:00:00+00:00"
                ),
                _proposal(
                    id="current-proposal", created_at="2026-08-09T13:00:00+00:00"
                ),
            ],
        )
        self.conn.commit()

        tr = parse_range(
            "custom",
            custom_start="2026-08-08T00:00:00+00:00",
            custom_end="2026-08-10T00:00:00+00:00",
        )
        items = insights_feed(self.conn, tr)["items"]
        self.assertEqual(
            {item["source_id"] for item in items},
            {"current", "current-proposal"},
        )

        stale_only = parse_range(
            "custom",
            custom_start="2026-06-01T00:00:00+00:00",
            custom_end="2026-06-02T00:00:00+00:00",
        )
        self.assertEqual(insights_feed(self.conn, stale_only)["items"], [])

    def test_imported_session_fact_requires_verbatim_database_evidence(self) -> None:
        session_id = "cursor:project/session"
        self.conn.execute(
            """
            INSERT INTO sessions (id, harness, external_id, started_at, repo)
            VALUES (?, 'cursor', 'project/session', '2026-08-09T12:00:00+00:00', 'demo')
            """,
            (session_id,),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES ('m1', ?, 1, 'assistant', '2026-08-09T12:01:00+00:00', ?)
            """,
            (session_id, "Ran the requested verification before reporting done."),
        )
        packet = Path(self._tmp.name) / "facts.json"
        packet.write_text(
            json.dumps(
                {
                    "run_id": "facts-001",
                    "source": "session_llm_facts",
                    "items": [
                        {
                            "session_id": session_id,
                            "message_seq": 1,
                            "kind": "follow",
                            "title": "Verified before reporting completion",
                            "body": "The agent ran the requested verification first.",
                            "quote": "requested verification before reporting done",
                            "does_not_prove": "That every task was verified.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        stats = import_session_fact_packet(
            self.conn, packet, model="cursor-grok-4.5-high-fast"
        )
        self.conn.commit()

        self.assertEqual(stats["claims"], 1)
        self.assertEqual(insights_feed(self.conn, parse_range("all"))["items"], [])
        self.conn.execute(
            "UPDATE claims SET status = 'approved' WHERE extractor_name = ?",
            ("session_fact_packet",),
        )
        self.conn.commit()

        body = insights_feed(self.conn, parse_range("all"))
        card = body["items"][0]
        self.assertEqual(card["source"], "claim")
        self.assertEqual(card["origin"], "session")
        self.assertEqual(card["insight_type"], "observed_instance")
        self.assertEqual(card["review_state"], "approved")
        self.assertEqual(card["sample_size"], 1)
        self.assertEqual(card["denominator"], 1)
        self.assertEqual(card["evidence_count"], 1)
        self.assertEqual(card["title"], "Verified before reporting completion")
        self.assertEqual(
            card["href"], "/sessions/cursor%3Aproject%2Fsession?msg=m1"
        )
        self.assertEqual(card["provenance"]["run_id"], "facts-001")
        self.assertEqual(card["provenance"]["source"], "session_llm_facts")

        packet_payload = json.loads(packet.read_text(encoding="utf-8"))
        packet_payload["items"][0]["quote"] = "not present"
        packet.write_text(json.dumps(packet_payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "evidence quote not found"):
            import_session_fact_packet(
                self.conn, packet, model="cursor-grok-4.5-high-fast"
            )

    def test_imported_session_fact_hydrates_exact_source_backed_message(self) -> None:
        external_id = "019fbdec-7065-7470-bb1e-dfa6c0d38237"
        session_id = f"codex:{external_id}"
        source = Path(self._tmp.name) / (
            f"rollout-2026-08-11T10-00-00-{external_id}.jsonl"
        )
        source.write_bytes(
            _codex_message("user", "check the source")
            + _codex_message(
                "assistant", "Canonical evidence survived promotion exactly."
            )
        )
        size = source.stat().st_size
        self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES ('codex', ?, ?, ?, ?, ?, 'test', 'legacy_materialized')
            """,
            (
                str(source),
                size,
                source.stat().st_mtime_ns,
                hash_prefix(source, size),
                size,
            ),
        )
        artifact_id = self.conn.execute(
            "SELECT id FROM artifacts WHERE path = ?", (str(source),)
        ).fetchone()["id"]
        self.conn.execute(
            """
            INSERT INTO sessions
              (id, harness, external_id, artifact_id, started_at, repo,
               transcript_storage)
            VALUES (?, 'codex', ?, ?, '2026-08-11T10:00:00+00:00', 'demo',
                    'source_backed')
            """,
            (session_id, external_id, artifact_id),
        )
        for seq, role, text in (
            (1, "user", "check the source"),
            (2, "assistant", "Canonical evidence survived promotion exactly."),
        ):
            self.conn.execute(
                "INSERT INTO messages "
                "(id,session_id,seq,role,text,content_hash) "
                "VALUES (?,?,?,?, '', ?)",
                (
                    f"{session_id}:m:{seq}",
                    session_id,
                    seq,
                    role,
                    content_hash_text(text),
                ),
            )
        self.conn.commit()
        packet = Path(self._tmp.name) / "source-backed-facts.json"
        packet.write_text(
            json.dumps(
                {
                    "run_id": "facts-source-backed",
                    "items": [
                        {
                            "session_id": session_id,
                            "message_seq": 2,
                            "kind": "verification",
                            "title": "Canonical evidence remained available",
                            "body": "The fact is grounded in its source transcript.",
                            "quote": "evidence survived promotion exactly",
                            "does_not_prove": "That unrelated sessions are valid.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        stats = import_session_fact_packet(
            self.conn, packet, model="gpt-5.6-sol"
        )

        self.assertEqual(stats["claims"], 1)
        evidence = self.conn.execute(
            "SELECT message_id, quote FROM claim_evidence"
        ).fetchone()
        self.assertEqual(evidence["message_id"], f"{session_id}:m:2")
        self.assertEqual(
            evidence["quote"], "evidence survived promotion exactly"
        )
        self.conn.execute(
            "UPDATE messages SET content_hash = 'mismatch' "
            "WHERE id = ?",
            (f"{session_id}:m:2",),
        )
        with self.assertRaisesRegex(ValueError, "canonical evidence unavailable"):
            import_session_fact_packet(
                self.conn, packet, model="gpt-5.6-sol"
            )


if __name__ == "__main__":
    unittest.main()
