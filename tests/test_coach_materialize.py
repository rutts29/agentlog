import copy
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentlog.analysis.claims.models import Claim, Proposal
from agentlog.analysis.claims.store import upsert_claims, upsert_proposals
from agentlog.analysis.coach.materialize import (
    LegacyQuarantinePlan,
    MaterializationError,
    _instruction_semantically_present,
    _validate_atomic_instruction,
    _validate_complete_processing,
    _validate_global_gate,
    _validate_miss_arcs,
    _validate_observation_proof,
    _validate_pattern_authorization,
    _validate_proof_capability,
    _validate_proposal_destination,
    _validate_sampling_gate,
    _validate_theme_binding,
    _verify_message_evidence,
    _verify_tool_evidence,
    MATERIALIZER_VERSION,
    apply_materialization_plan,
    plan_legacy_quarantine,
    plan_materialization,
    quarantine_legacy_records,
    verify_coach_run,
)
from agentlog.analysis.coach.preprocess import CoachPreprocessConfig, emit_coach_packets
from agentlog.analysis.coach.synthesis import (
    build_candidate_catalog,
    run_synthesis_pipeline,
    validate_terra_result,
)
from agentlog.api.queries import insights_feed
from agentlog.api.ranges import parse_range
from agentlog.db.schema import init_db
from agentlog.source_reader import SourceReadResult


def _sha256(value):
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _short_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:24]


class CoachMaterializeTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.addCleanup(self.conn.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def _seed_roots(
        self,
        count,
        *,
        action="result",
        success=0,
        operation_kind="verification",
        skill=False,
        linked_skill=True,
        response_model=None,
        tool_name="pytest",
    ):
        for number in range(count):
            session_id = f"root-{number}"
            timestamp = f"2026-08-{number + 1:02d}T00:00:00+00:00"
            request_id = f"request-{number}"
            response_id = f"response-{number}"
            window_id = f"window-{number}"
            tool_id = f"tool-{number}"
            request = (
                "Please use the verification skill."
                if skill
                else "Please verify the requested work."
            )
            response = f"I attempted the requested work for root {number}."
            self.conn.execute(
                "INSERT INTO sessions "
                "(id,harness,external_id,started_at,repo,agent_profile) "
                "VALUES(?,?,?,?,?,?)",
                (session_id, "codex", session_id, timestamp, "demo", "codex"),
            )
            for message_id, seq, role, text in (
                (request_id, 1, "user", request),
                (response_id, 2, "assistant", response),
            ):
                self.conn.execute(
                    "INSERT INTO messages "
                    "(id,session_id,seq,role,timestamp,text,content_hash,model_canonical) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        message_id,
                        session_id,
                        seq,
                        role,
                        timestamp,
                        text,
                        _sha256(text),
                        response_model if role == "assistant" else None,
                    ),
                )
            self.conn.execute(
                "INSERT INTO exchange_windows "
                "(id,session_id,request_message_id,response_message_id,input_hash,content_hash) "
                "VALUES(?,?,?,?,?,?)",
                (
                    window_id,
                    session_id,
                    request_id,
                    response_id,
                    f"input-{number}",
                    f"window-hash-{number}",
                ),
            )
            self.conn.execute(
                "INSERT INTO tool_events "
                "(id,session_id,message_id,seq,tool_name,action,success,operation_kind) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    tool_id,
                    session_id,
                    response_id,
                    3,
                    tool_name,
                    action,
                    success,
                    operation_kind,
                ),
            )

            if skill:
                self.conn.execute(
                    "INSERT INTO skill_exposures "
                    "(id,session_id,message_id,skill_name,exposure_type) VALUES(?,?,?,?,?)",
                    (
                        f"skill-{number}",
                        session_id,
                        response_id if linked_skill else None,
                        "verification",
                        "loaded",
                    ),
                )
        self.conn.commit()

    def test_source_backed_message_evidence_replays_transient_source_text(self):
        source_text = "Please verify the transient source-backed result."
        response_text = "The source-backed result passed."
        self.conn.execute(
            "INSERT INTO sessions(id,harness,external_id,repo,transcript_storage) VALUES(?,?,?,?,?)",
            ("source", "codex", "source", "demo", "source_backed"),
        )
        self.conn.executemany(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            [
                ("source:m:1", "source", 1, "user", "", _sha256(source_text)),
                ("source:m:2", "source", 2, "assistant", "", _sha256(response_text)),
            ],
        )
        self.conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
            ("source-window", "source", "source:m:1", "source:m:2", _sha256(source_text), "source-window-hash"),
        )
        self.conn.commit()
        window = self.conn.execute(
            "SELECT id, session_id, request_message_id, response_message_id FROM exchange_windows WHERE id = 'source-window'"
        ).fetchone()
        result = SourceReadResult(
            "ready",
            [
                {"id": "source:m:1", "seq": 1, "role": "user", "timestamp": None, "model": None, "model_canonical": None, "effort": None, "text": source_text, "content_hash": _sha256(source_text), "is_tool_plumbing": False, "authored_by_agent": False},
                {"id": "source:m:2", "seq": 2, "role": "assistant", "timestamp": None, "model": None, "model_canonical": None, "effort": None, "text": response_text, "content_hash": _sha256(response_text), "is_tool_plumbing": False, "authored_by_agent": False},
            ],
            source_identity="source-identity", source_hash="source-hash",
        )
        evidence = {
            "message_id": "source:m:1", "role": "user", "seq": 1, "timestamp": "",
            "source_hash": _sha256(source_text), "content_hash": _sha256(source_text),
            "emitted_source_hash": _sha256(source_text), "quote": "Please verify",
            "quote_start": 0, "quote_end": len("Please verify"),
        }
        with patch(
            "agentlog.source_reader.CachedSourceTranscriptReader.__call__",
            return_value=result,
        ) as reader:
            self.assertEqual(_verify_message_evidence(self.conn, evidence, "source", window, {}), "")
        reader.assert_called_once_with(self.conn, "source")

    def _write_luna_results(
        self, run_dir, manifest, kind="instruction_miss", abstain_roots=()
    ):
        results = run_dir / "results"
        results.mkdir(exist_ok=True)
        for entry in manifest["packets"]:
            packet = json.loads((run_dir / entry["path"]).read_text())
            window = packet["windows"][0]
            observation_id = f"observation-{packet['packet_id']}"
            request, response = window["messages"]
            tool = window["tool_timeline"][0]
            if window["root_session_id"] in abstain_roots:
                raw = {
                    "packet_id": packet["packet_id"],
                    "result_id": f"luna-{packet['packet_id']}",
                    "abstain": True,
                    "producer": packet["producer_contract"]["expected"],
                    "abstain_reason": "No deterministic proof for this bounded root.",
                    "window_dispositions": [
                        {
                            "window_id": item["window_id"],
                            "observation_ids": [],
                            "no_supported_observation": True,
                        }
                        for item in packet["windows"]
                    ],
                }
                (results / f"{packet['packet_id']}.json").write_text(json.dumps(raw))
                continue
            if kind == "skill_use":
                skill = window["skill_exposures"][0]
                evidence = [
                    {
                        "window_id": window["window_id"],
                        "message_id": request["message_id"],
                        "role": "user",
                        "seq": request["seq"],
                        "quote": request["source_text"],
                    },
                    {
                        "window_id": window["window_id"],
                        "skill_exposure_id": skill["skill_exposure_id"],
                        "fact": skill["fact"],
                    },
                    {
                        "window_id": window["window_id"],
                        "tool_event_id": tool["tool_event_id"],
                        "fact": tool["fact"],
                    },
                ]
                arcs = [
                    {
                        "arc": "skill_request",
                        "evidence_refs": [
                            f"{window['window_id']}:{request['message_id']}"
                        ],
                    },
                    {
                        "arc": "skill_evidence",
                        "evidence_refs": [
                            f"{window['window_id']}:skill:{skill['skill_exposure_id']}"
                        ],
                    },
                    {
                        "arc": "skill_action",
                        "evidence_refs": [
                            f"{window['window_id']}:tool:{tool['tool_event_id']}"
                        ],
                    },
                ]
                assertion_key = "verification_skill_use"
                limitation = (
                    "This bounded skill exposure does not prove behavior outside this session."
                )
            elif kind == "process_fact":
                evidence = [
                    {
                        "window_id": window["window_id"],
                        "tool_event_id": tool["tool_event_id"],
                        "fact": tool["fact"],
                    }
                ]
                tool_ref = f"{window['window_id']}:tool:{tool['tool_event_id']}"
                arcs = [
                    {"arc": "action", "evidence_refs": [tool_ref]},
                    {"arc": "artifact", "evidence_refs": [tool_ref]},
                ]
                assertion_key = "configuration_patch_applied"
                limitation = (
                    "This terminal artifact result does not establish later review or retention."
                )
            else:
                evidence = [
                    {
                        "window_id": window["window_id"],
                        "message_id": request["message_id"],
                        "role": "user",
                        "seq": request["seq"],
                        "quote": request["source_text"],
                    },
                    {
                        "window_id": window["window_id"],
                        "message_id": response["message_id"],
                        "role": "assistant",
                        "seq": response["seq"],
                        "quote": response["source_text"],
                    },
                    {
                        "window_id": window["window_id"],
                        "tool_event_id": tool["tool_event_id"],
                        "fact": tool["fact"],
                    },
                ]
                arcs = [
                    {
                        "arc": "request",
                        "evidence_refs": [
                            f"{window['window_id']}:{request['message_id']}"
                        ],
                    },
                    {
                        "arc": "response",
                        "evidence_refs": [
                            f"{window['window_id']}:{response['message_id']}"
                        ],
                    },
                    {
                        "arc": "gap",
                        "evidence_refs": [
                            f"{window['window_id']}:tool:{tool['tool_event_id']}"
                        ],
                    },
                ]
                assertion_key = "verification_gap"
                limitation = (
                    "This single bounded exchange does not establish the cause of the "
                    "verification failure."
                )
            raw = {
                "packet_id": packet["packet_id"],
                "result_id": f"luna-{packet['packet_id']}",
                "abstain": False,
                "producer": packet["producer_contract"]["expected"],
                "window_dispositions": [
                    {
                        "window_id": item["window_id"],
                        "observation_ids": [observation_id]
                        if item["window_id"] == window["window_id"]
                        else [],
                        "no_supported_observation": item["window_id"]
                        != window["window_id"],
                    }
                    for item in packet["windows"]
                ],
                "observations": [
                    {
                        "observation_id": observation_id,
                        "kind": kind,
                        "assertion_key": assertion_key,
                        "confidence": 0.9,
                        "does_not_prove": limitation,
                        "evidence": evidence,
                        "proof_arcs": arcs,
                    }
                ],
            }
            (results / f"{packet['packet_id']}.json").write_text(json.dumps(raw))

    def _candidate(self, packet, kind, support_ids):
        source_kind = packet["supporting_observations"][0]["kind"]
        polarity = (
            "positive" if source_kind in {"skill_use", "process_fact"} else "negative"
        )
        predicate = (
            "artifact_write"
            if source_kind == "process_fact"
            else source_kind
            if source_kind == "skill_use"
            else "instruction_miss"
        )
        n = len(
            {
                observation["root_session_id"]
                for observation in packet["supporting_observations"]
                if observation["observation_id"] in support_ids
            }
        )
        denominator = packet["coverage"]["full_eligible_root_denominator"]
        subject = (
            "configuration_artifact"
            if source_kind == "process_fact"
            else "verification"
        )
        canonical = {
            "scope": packet["group"]["scope"],
            "subject": subject,
            "predicate": predicate,
            "polarity": polarity,
        }
        if source_kind == "skill_use":
            title = "Verification skill use had attributable action proof"
            summary = (
                f"In {n}/{denominator} root session, the verification skill was applied "
                "after an explicit request and the attributable verification action "
                "succeeded with recorded evidence."
            )
        elif source_kind == "process_fact":
            title = "A configuration patch had a terminal artifact result"
            summary = (
                f"In {n}/{denominator} root session, a configuration patch wrote an "
                "artifact and returned a successful terminal result during the requested change."
            )
        else:
            title = "Verification requests repeatedly lacked terminal proof"
            processed = packet["coverage"]["processed_roots"]
            summary = (
                f"In {n} of {processed} processed roots from a partial sample of "
                f"{processed} of {denominator} eligible roots, requested verification "
                "was attempted after explicit owner instructions and failed with a "
                "recorded terminal result during the reviewed work."
                if processed < denominator
                else f"In {n}/{denominator} root sessions, requested verification was "
                "attempted after explicit owner instructions and failed with a recorded "
                "terminal result during the reviewed work."
            )
        candidate = {
            "kind": kind,
            "canonical": canonical,
            "title": title,
            "summary": summary,
            "does_not_prove": (
                "This bounded reviewed corpus does not establish causes or behavior "
                "outside the cited sessions."
            ),
            "supporting_observation_ids": support_ids,
            "counterevidence_observation_ids": [],
            "n": n,
            "denominator": denominator,
            "processed_roots": packet["coverage"]["processed_roots"],
            "eligible_roots": packet["coverage"]["eligible_roots"],
        }
        if kind != "observed_instance":
            full_population = packet["full_population"]
            candidate.update(
                {
                    "population_hash": full_population["hash"],
                    "cited_supporting_roots": n,
                    "counterevidence_roots": full_population["counterexamples"][
                        "root_count"
                    ],
                    "counterevidence_observations": full_population[
                        "counterexamples"
                    ]["observation_count"],
                    "n": full_population["supporting"]["root_count"],
                }
            )
        if kind == "coach_proposal":
            target = packet["config_overlap"]["searched"][0]
            candidate.update(
                {
                    "title": "Require explicit verification results before completion",
                    "instruction_text": (
                        "Require an explicit successful verification result before marking "
                        "requested work complete."
                    ),
                    "pattern_canonical_key": (
                        f"{packet['group']['scope']}:verification:instruction_miss:negative"
                    ),
                    "target_ref": target["target_ref"],
                    "target_kind": target["target_kind"],
                    "action": "add",
                    "base_content_hash": target["fingerprint"],
                    "config_gap": {},
                    "miss_proof_arcs": [
                        {"observation_id": observation_id, "arc": "gap"}
                        for observation_id in support_ids[:3]
                    ],
                }
            )
        return candidate

    def _finalize_run(
        self,
        run_dir,
        config_inventory,
        *,
        include_proposal=True,
        include_observed=False,
        source_kind="instruction_miss",
        reject_kinds=(),
        accepted_scope="harness_codex",
    ):
        first = run_synthesis_pipeline(run_dir, config_inventory=config_inventory)
        terra_results = []
        cleaned_results = []
        target_map = json.loads((run_dir / "synthesis_config_targets.json").read_text())
        for entry in first["synthesis_manifest"]["packets"]:
            packet = json.loads((run_dir / entry["path"]).read_text())
            if packet["group"]["scope"] != accepted_scope:
                raw = {
                    "packet_id": packet["packet_id"],
                    "result_id": f"terra-{packet['packet_id']}",
                    "abstain": True,
                    "abstain_reason": "This fixture materializes the harness-scoped packet only.",
                    "producer": packet["synthesis_assignment"],
                }
                cleaned, failures = validate_terra_result(
                    raw, packet, config_targets=target_map
                )
                self.assertEqual(failures, [])
                self.assertIsNotNone(cleaned)
                terra_results.append(raw)
                cleaned_results.append(cleaned)
                continue
            all_support = [
                observation["observation_id"]
                for observation in packet["supporting_observations"]
            ]
            candidates = []
            if source_kind in {"skill_use", "process_fact"}:
                candidates.append(self._candidate(packet, "observed_instance", all_support[:1]))
            else:
                if include_observed:
                    candidates.append(
                        self._candidate(packet, "observed_instance", all_support[:1])
                    )
                if len(all_support) >= 5:
                    candidates.append(self._candidate(packet, "corpus_pattern", all_support))
                if include_proposal and len(all_support) >= 10:
                    candidates.append(self._candidate(packet, "coach_proposal", all_support))
            raw = {
                "packet_id": packet["packet_id"],
                "result_id": f"terra-{packet['packet_id']}",
                "abstain": False,
                "producer": packet["synthesis_assignment"],
                "candidates": candidates,
            }
            cleaned, failures = validate_terra_result(
                raw, packet, config_targets=target_map
            )
            self.assertEqual(failures, [])
            self.assertIsNotNone(cleaned)
            terra_results.append(raw)
            cleaned_results.append(cleaned)
        catalog, failures = build_candidate_catalog(
            first["synthesis_manifest"], cleaned_results
        )
        self.assertEqual(failures, [])
        self.assertIsNotNone(catalog)
        review = {
            "catalog_id": catalog["catalog_id"],
            "review_id": "review-one",
            "producer": catalog["review_assignment"],
            "decisions": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "canonical_key": candidate["canonical_key"],
                    "decision": (
                        "reject" if candidate["kind"] in reject_kinds else "accept"
                    ),
                    "observation_ids": sorted(
                        set(candidate["supporting_observation_ids"])
                        | set(candidate["counterevidence_observation_ids"])
                    ),
                }
                for candidate in catalog["candidates"]
            ],
        }
        final = run_synthesis_pipeline(
            run_dir,
            config_inventory=config_inventory,
            terra_results=terra_results,
            second_review=review,
        )
        self.assertEqual(final["validation_failures"], [])
        self.assertIsNotNone(final["second_review"])
        return final

    def _build_run(
        self,
        *,
        count=10,
        include_proposal=True,
        include_observed=False,
        source_kind="instruction_miss",
        reject_kinds=(),
        target_kind="instruction_file",
        target_name="AGENTS.md",
        abstain_roots=(),
        accepted_scope="harness_codex",
    ):
        run_dir = self.base / f"run-{len(list(self.base.glob('run-*')))}"
        target = run_dir / target_name
        run_dir.mkdir()
        target.write_text("# Existing rules\n")
        fingerprint = hashlib.sha256(target.read_bytes()).hexdigest()
        inventory = [
            {
                "path": str(target),
                "content": target.read_text(),
                "fingerprint": fingerprint,
                "target_kind": target_kind,
            }
        ]
        manifest = emit_coach_packets(self.conn, run_dir)
        self._write_luna_results(
            run_dir, manifest, source_kind, abstain_roots=abstain_roots
        )
        final = self._finalize_run(
            run_dir,
            inventory,
            include_proposal=include_proposal,
            include_observed=include_observed,
            source_kind=source_kind,
            reject_kinds=reject_kinds,
            accepted_scope=accepted_scope,
        )
        return run_dir, target, manifest, inventory, final

    def test_secure_run_materializes_pattern_and_pending_proposal(self):
        self._seed_roots(10)
        run_dir, target, _, _, _ = self._build_run()
        original = target.read_text()
        verified = verify_coach_run(self.conn, run_dir)
        self.assertTrue(verified.bundle_hash)
        plan = plan_materialization(
            self.conn, run_dir, now="2026-08-20T00:00:00+00:00"
        )
        self.assertEqual(len(plan.claims), 1)
        self.assertEqual(len(plan.proposals), 1)
        self.assertEqual(plan.claims[0].kind, "coach_corpus_pattern")
        self.assertEqual(plan.proposals[0].status, "pending")
        self.assertEqual(plan.proposals[0].claim_ids, [plan.claims[0].id])
        proposal_provenance = plan.proposals[0].provenance
        self.assertEqual(plan.proposals[0].model, "gpt-5.6-terra")
        self.assertEqual(
            {item["model"] for item in proposal_provenance["luna_producers"]},
            {"gpt-5.6-luna"},
        )
        self.assertTrue(proposal_provenance["luna_result_ids"])
        self.assertEqual(
            proposal_provenance["terra_synthesis_producer"]["worker_id"],
            "terra-synthesis",
        )
        self.assertTrue(proposal_provenance["terra_synthesis_result_ids"])
        self.assertEqual(
            proposal_provenance["terra_review_producer"]["worker_id"],
            "terra-second-review",
        )
        self.assertEqual(proposal_provenance["terra_review_id"], "review-one")
        self.assertTrue(proposal_provenance["packet_id"].startswith("spkt_"))
        self.assertEqual(
            proposal_provenance["validator_version"], MATERIALIZER_VERSION
        )
        self.assertEqual(
            proposal_provenance["support_distribution"],
            plan.claims[0].value["distribution"],
        )
        self.assertEqual(target.read_text(), original)
        dry = apply_materialization_plan(self.conn, plan, dry_run=True)
        self.assertEqual(dry["claims_written"], 0)
        written = apply_materialization_plan(self.conn, plan, dry_run=False)
        self.assertEqual(written["claims_written"], 1)
        self.assertEqual(written["proposals_written"], 1)
        self.assertEqual(
            self.conn.execute("SELECT status FROM proposals").fetchone()["status"],
            "pending",
        )
        self.assertEqual(target.read_text(), original)
        repeated = plan_materialization(self.conn, run_dir)
        self.assertEqual(repeated.claims, [])
        self.assertEqual(repeated.proposals, [])
        self.assertEqual(repeated.unchanged_claim_ids, [plan.claims[0].id])
        self.assertEqual(repeated.unchanged_proposal_ids, [plan.proposals[0].id])

    def test_duplicate_identical_inventory_entries_keep_bundle_verifiable(self):
        self._seed_roots(10)
        run_dir = self.base / "duplicate-identical-inventory"
        run_dir.mkdir()
        target = run_dir / "AGENTS.md"
        target.write_text("# Existing rules\n")
        entry = {
            "path": str(target),
            "content": target.read_text(),
            "fingerprint": hashlib.sha256(target.read_bytes()).hexdigest(),
            "target_kind": "instruction_file",
        }
        manifest = emit_coach_packets(self.conn, run_dir)
        self._write_luna_results(run_dir, manifest, "instruction_miss")
        self._finalize_run(run_dir, [entry, dict(entry)])

        target_map = json.loads((run_dir / "synthesis_config_targets.json").read_text())
        self.assertEqual(len(target_map["targets"]), 1)
        verified = verify_coach_run(self.conn, run_dir)
        self.assertTrue(verified.bundle_hash)
        plan = plan_materialization(self.conn, run_dir)
        self.assertEqual(len(plan.claims), 1)
        self.assertEqual(len(plan.proposals), 1)

    def test_verify_accepts_scoped_luna_results_and_ignores_terra_artifacts(self):
        self._seed_roots(10)
        run_dir, _, manifest, _, _ = self._build_run()
        luna_dir = run_dir / "results" / "luna"
        luna_dir.mkdir()
        for entry in manifest["packets"]:
            source = run_dir / "results" / f"{entry['packet_id']}.json"
            shutil.move(source, luna_dir / source.name)
        terra_dir = run_dir / "results" / "terra"
        terra_dir.mkdir()
        (terra_dir / "unrelated.json").write_text(json.dumps({
            "packet_id": "spkt_unrelated",
            "result_id": "terra-placeholder",
        }))

        verified = verify_coach_run(self.conn, run_dir)

        result_hashes = verified.replay_provenance["preprocess_result_hashes"]
        self.assertEqual(len(result_hashes), len(manifest["packets"]))
        self.assertTrue(all(path.startswith("results/luna/") for path in result_hashes))
        self.assertNotIn("results/terra/unrelated.json", result_hashes)

    def test_conflicting_config_target_refs_remain_rejected(self):
        self._seed_roots(10)
        run_dir = self.base / "conflicting-target-kind"
        run_dir.mkdir()
        target = run_dir / "AGENTS.md"
        target.write_text("# Existing rules\n")
        entry = {
            "path": str(target),
            "content": target.read_text(),
            "fingerprint": hashlib.sha256(target.read_bytes()).hexdigest(),
            "target_kind": "instruction_file",
        }
        manifest = emit_coach_packets(self.conn, run_dir)
        self._write_luna_results(run_dir, manifest, "instruction_miss")
        self._finalize_run(
            run_dir,
            [entry, {**entry, "target_kind": "harness_rule"}],
            include_proposal=False,
        )

        with self.assertRaisesRegex(
            MaterializationError, "private config target entry is not self-consistent"
        ):
            verify_coach_run(self.conn, run_dir)

    def test_observed_instance_keeps_physical_navigation_and_source_time(self):
        self._seed_roots(1)
        self.conn.execute(
            "INSERT INTO sessions "
            "(id,harness,external_id,started_at,repo,agent_profile) "
            "VALUES(?,?,?,?,?,?)",
            (
                "logical-parent",
                "codex",
                "logical-parent",
                "2026-08-01T00:00:00+00:00",
                "demo",
                "codex",
            ),
        )
        self.conn.execute(
            "UPDATE sessions SET parent_session_id = ? WHERE id = ?",
            ("logical-parent", "root-0"),
        )
        self.conn.commit()
        run_dir, _, _, _, _ = self._build_run(
            count=1, include_proposal=False, include_observed=True
        )
        plan = plan_materialization(self.conn, run_dir)
        instance = next(claim for claim in plan.claims if claim.kind == "coach_observed_instance")
        self.assertEqual(instance.sample_size, 1)
        self.assertEqual(instance.scope_type, "repo")
        self.assertTrue(instance.observed_at.startswith("2026-08-"))
        self.assertTrue(
            all(evidence.session_id == "root-0" for evidence in instance.evidence)
        )
        self.assertTrue(
            all(
                evidence.meta["logical_root_session_id"] == "logical-parent"
                for evidence in instance.evidence
            )
        )
        apply_materialization_plan(self.conn, plan, dry_run=False)
        card = next(
            item
            for item in insights_feed(self.conn, parse_range("all"))["items"]
            if item["source_id"] == instance.id
        )
        first_evidence = instance.evidence[0]
        self.assertEqual(
            card["href"],
            f"/sessions/{first_evidence.session_id}?msg={first_evidence.message_id}",
        )
        self.assertEqual(card["supporting_roots"], 1)
        self.assertEqual(card["processed_roots"], 1)
        self.assertEqual(card["eligible_roots"], 1)
        self.assertEqual(card["coverage_state"], "complete")
        self.assertEqual(
            card["proof_capability_by_harness"]["codex"]["proof_capable_roots"],
            1,
        )

    def test_model_scope_uses_full_terminal_population_and_denominator(self):
        self._seed_roots(10, response_model="gpt-5.6-codex")
        run_dir, _, _, _, _ = self._build_run(
            accepted_scope="model_gpt_5_6_codex"
        )
        plan = plan_materialization(self.conn, run_dir)
        self.assertEqual(len(plan.claims), 1)
        self.assertEqual(len(plan.proposals), 1)
        self.assertEqual(
            (plan.claims[0].scope_type, plan.claims[0].scope_id),
            ("model", "gpt_5_6_codex"),
        )
        self.assertEqual(
            (plan.proposals[0].scope_type, plan.proposals[0].scope_id),
            ("model", "gpt_5_6_codex"),
        )
        self.assertEqual(plan.claims[0].sample_size, 10)
        self.assertEqual(plan.claims[0].denominator, 10)

    def test_linked_skill_observation_validates_through_materialization(self):
        self._seed_roots(1, success=1, skill=True)
        run_dir, _, _, _, _ = self._build_run(
            count=1,
            include_proposal=False,
            source_kind="skill_use",
        )
        plan = plan_materialization(self.conn, run_dir)
        self.assertEqual(len(plan.claims), 1)
        self.assertEqual(plan.claims[0].kind, "coach_observed_instance")
        self.assertEqual(
            {evidence.meta["evidence_type"] for evidence in plan.claims[0].evidence},
            {"message", "skill", "tool"},
        )

    def test_process_fact_validates_through_catalog_review_and_materialization(self):
        self._seed_roots(
            1,
            action="write",
            success=1,
            operation_kind="artifact_write",
            tool_name="apply_patch",
        )
        run_dir, _, _, _, _ = self._build_run(
            count=1,
            include_proposal=False,
            source_kind="process_fact",
        )
        plan = plan_materialization(self.conn, run_dir)
        self.assertEqual(len(plan.claims), 1)
        self.assertEqual(plan.claims[0].kind, "coach_observed_instance")
        self.assertEqual(
            {evidence.meta["evidence_type"] for evidence in plan.claims[0].evidence},
            {"tool"},
        )

    def test_self_rehashed_preprocess_packet_is_rejected(self):
        self._seed_roots(10)
        run_dir, _, manifest, _, _ = self._build_run()
        packet_path = run_dir / manifest["packets"][0]["path"]
        packet = json.loads(packet_path.read_text())
        packet["windows"][0]["signal_score"] += 1
        body = dict(packet)
        body.pop("packet_hash")
        packet["packet_hash"] = _short_hash(body)
        packet_path.write_text(json.dumps(packet))
        with self.assertRaisesRegex(
            MaterializationError,
            "packet content differs|packet windows are malformed|packet byte budget is invalid|preprocess replay failed",
        ):
            verify_coach_run(self.conn, run_dir)

    def test_preprocess_packet_path_traversal_is_rejected_before_read(self):
        self._seed_roots(10)
        run_dir, _, _, _, _ = self._build_run()
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["packets"][0]["packet_id"] = "../../outside/evil"
        manifest["packets"][0]["path"] = "packets/../../outside/evil.json"
        manifest_path.write_text(json.dumps(manifest))
        bundle_path = run_dir / "synthesis_run_bundle.json"
        bundle = json.loads(bundle_path.read_text())
        bundle["source_preprocess_manifest"]["hash"] = _sha256(manifest)
        body = dict(bundle)
        body.pop("bundle_hash")
        body.pop("bundle_id")
        bundle["bundle_id"] = f"bundle_{_sha256(body)[:24]}"
        body = dict(bundle)
        body.pop("bundle_hash")
        bundle["bundle_hash"] = _sha256(body)
        bundle_path.write_text(json.dumps(bundle))
        with self.assertRaisesRegex(MaterializationError, "packet index is invalid"):
            verify_coach_run(self.conn, run_dir)

    def test_preprocess_packet_byte_budget_is_replay_bound(self):
        self._seed_roots(10)
        run_dir, _, _, _, _ = self._build_run()
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["packets"][0]["serialized_bytes"] += 1
        manifest_path.write_text(json.dumps(manifest))
        bundle_path = run_dir / "synthesis_run_bundle.json"
        bundle = json.loads(bundle_path.read_text())
        bundle["source_preprocess_manifest"]["hash"] = _sha256(manifest)
        body = dict(bundle)
        body.pop("bundle_hash")
        body.pop("bundle_id")
        bundle["bundle_id"] = f"bundle_{_sha256(body)[:24]}"
        body = dict(bundle)
        body.pop("bundle_hash")
        bundle["bundle_hash"] = _sha256(body)
        bundle_path.write_text(json.dumps(bundle))
        with self.assertRaisesRegex(MaterializationError, "byte budget is invalid"):
            verify_coach_run(self.conn, run_dir)

    def test_preprocess_packet_groups_remain_bound_to_canonical_order(self):
        self._seed_roots(10)
        run_dir, _, _, _, _ = self._build_run()
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        first, second = manifest["packets"][:2]
        first["window_ids"], second["window_ids"] = (
            second["window_ids"],
            first["window_ids"],
        )
        first["root_session_ids"], second["root_session_ids"] = (
            second["root_session_ids"],
            first["root_session_ids"],
        )
        manifest_path.write_text(json.dumps(manifest))
        bundle_path = run_dir / "synthesis_run_bundle.json"
        bundle = json.loads(bundle_path.read_text())
        bundle["source_preprocess_manifest"]["hash"] = _sha256(manifest)
        body = dict(bundle)
        body.pop("bundle_hash")
        body.pop("bundle_id")
        bundle["bundle_id"] = f"bundle_{_sha256(body)[:24]}"
        body = dict(bundle)
        body.pop("bundle_hash")
        bundle["bundle_hash"] = _sha256(body)
        bundle_path.write_text(json.dumps(bundle))
        with self.assertRaisesRegex(MaterializationError, "packet groups differ"):
            verify_coach_run(self.conn, run_dir)

    def test_cyclic_session_lineage_cannot_inflate_root_support(self):
        self._seed_roots(10)
        for number in range(10):
            self.conn.execute(
                "UPDATE sessions SET parent_session_id = ? WHERE id = ?",
                (f"root-{(number + 1) % 10}", f"root-{number}"),
            )
        self.conn.commit()
        run_dir, _, _, _, _ = self._build_run()
        with self.assertRaisesRegex(MaterializationError, "lineage contains a cycle"):
            plan_materialization(self.conn, run_dir)

    def test_shared_unresolved_parent_is_not_lineage(self):
        self._seed_roots(10)
        self.conn.execute(
            "UPDATE sessions SET parent_session_id = 'shared-missing-parent'"
        )
        self.conn.commit()
        run_dir, _, _, _, _ = self._build_run()
        plan_materialization(self.conn, run_dir)

    def test_uncited_packetized_window_change_invalidates_the_run(self):
        self._seed_roots(10)
        timestamp = "2026-08-01T00:01:00+00:00"
        request = "Please verify the additional work for root zero."
        response = "I attempted the additional work for root zero."
        for message_id, seq, role, text in (
            ("request-extra", 4, "user", request),
            ("response-extra", 5, "assistant", response),
        ):
            self.conn.execute(
                "INSERT INTO messages "
                "(id,session_id,seq,role,timestamp,text,content_hash) "
                "VALUES(?,?,?,?,?,?,?)",
                (message_id, "root-0", seq, role, timestamp, text, _sha256(text)),
            )
        self.conn.execute(
            "INSERT INTO exchange_windows "
            "(id,session_id,request_message_id,response_message_id,input_hash,content_hash) "
            "VALUES(?,?,?,?,?,?)",
            (
                "window-extra",
                "root-0",
                "request-extra",
                "response-extra",
                "input-extra",
                "window-extra-hash",
            ),
        )
        self.conn.execute(
            "INSERT INTO tool_events "
            "(id,session_id,message_id,seq,tool_name,action,success,operation_kind) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "tool-extra",
                "root-0",
                "response-extra",
                6,
                "pytest",
                "result",
                0,
                "verification",
            ),
        )
        self.conn.commit()
        run_dir, _, _, _, _ = self._build_run()
        plan = plan_materialization(self.conn, run_dir)
        changed = "The uncited packetized response changed after review."
        self.conn.execute(
            "UPDATE messages SET text = ?, content_hash = ? WHERE id = ?",
            (changed, _sha256(changed), "response-extra"),
        )
        self.conn.commit()
        with self.assertRaisesRegex(MaterializationError, "packet content differs"):
            verify_coach_run(self.conn, run_dir)
        with self.assertRaisesRegex(MaterializationError, "packet content differs"):
            apply_materialization_plan(self.conn, plan, dry_run=False)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0], 0)

    def test_missing_luna_result_is_rejected(self):
        self._seed_roots(10)
        run_dir, _, manifest, _, _ = self._build_run()
        (run_dir / "results" / f"{manifest['packets'][0]['packet_id']}.json").unlink()
        with self.assertRaisesRegex(MaterializationError, "missing a Luna result"):
            verify_coach_run(self.conn, run_dir)

    def test_luna_result_requires_a_disposition_for_every_packet_window(self):
        self._seed_roots(10)
        run_dir, _, manifest, _, _ = self._build_run()
        result_path = (
            run_dir / "results" / f"{manifest['packets'][0]['packet_id']}.json"
        )
        result = json.loads(result_path.read_text())
        result["window_dispositions"] = []
        result_path.write_text(json.dumps(result))
        with self.assertRaisesRegex(
            MaterializationError, "window_disposition_missing_local_window"
        ):
            verify_coach_run(self.conn, run_dir)

    def test_missing_terra_result_is_rejected(self):
        self._seed_roots(10)
        run_dir, _, _, _, _ = self._build_run()
        results_path = run_dir / "synthesis_results" / "validated_results.json"
        results = json.loads(results_path.read_text())
        results["results"] = []
        body = dict(results)
        body.pop("results_hash")
        results["results_hash"] = _sha256(body)
        results_path.write_text(json.dumps(results))
        bundle_path = run_dir / "synthesis_run_bundle.json"
        bundle = json.loads(bundle_path.read_text())
        bundle["validated_results"]["hash"] = results["results_hash"]
        bundle_body = dict(bundle)
        bundle_body.pop("bundle_hash")
        bundle_body.pop("bundle_id")
        bundle["bundle_id"] = f"bundle_{_sha256(bundle_body)[:24]}"
        bundle_body = dict(bundle)
        bundle_body.pop("bundle_hash")
        bundle["bundle_hash"] = _sha256(bundle_body)
        bundle_path.write_text(json.dumps(bundle))
        with self.assertRaisesRegex(MaterializationError, "missing a Terra result"):
            verify_coach_run(self.conn, run_dir)

    def test_full_chain_rebuilt_with_forged_denominator_is_rejected(self):
        self._seed_roots(10)
        original, _, _, inventory, _ = self._build_run(include_proposal=False)
        forged = self.base / "forged-chain"
        shutil.copytree(original, forged)
        manifest_path = forged / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        removed = manifest["packets"].pop()
        removed_root = removed["root_session_ids"][0]
        removed_window = removed["window_ids"][0]
        commitment = dict(manifest["eligibility_commitment"])
        commitment.pop("hash")
        for key in (
            "eligible_root_ids",
            "selected_root_ids",
            "packetized_root_ids",
            "ordered_root_ids",
        ):
            commitment[key] = [
                value for value in commitment[key] if value != removed_root
            ]
        for key in (
            "eligible_window_ids",
            "selected_window_ids",
            "packetized_window_ids",
        ):
            commitment[key] = [
                value for value in commitment[key] if value != removed_window
            ]
        commitment["packet_groups"] = [
            group
            for group in commitment["packet_groups"]
            if removed_window not in group["window_ids"]
        ]
        commitment["selected_per_root"].pop(removed_root)
        commitment["packetized_per_root"].pop(removed_root)
        for key in (
            "eligible_per_harness",
            "eligible_per_repo",
            "packetized_per_harness",
            "packetized_per_repo",
        ):
            only_key = next(iter(commitment[key]))
            commitment[key][only_key] -= 1
        commitment["hash"] = _short_hash(commitment)
        manifest["eligibility_commitment"] = commitment
        for key in (
            "eligible",
            "eligible_windows",
            "selected",
            "selected_windows",
            "packetized",
            "packetized_windows",
            "eligible_roots",
            "selected_roots",
            "packetized_roots",
        ):
            manifest["coverage"][key] -= 1
        for key in ("per_harness", "harness_counts", "per_repo", "repo_counts"):
            only_key = next(iter(manifest[key]))
            manifest[key][only_key] -= 1
        for key in ("by_harness", "by_repo"):
            only_key = next(iter(manifest["counts"][key]))
            manifest["counts"][key][only_key] -= 1
        manifest["selected_per_root"].pop(removed_root)
        manifest["packetized_per_root"].pop(removed_root)
        manifest["ordered_roots"] = [
            value for value in manifest["ordered_roots"] if value != removed_root
        ]
        for key in ("eligible_per_harness_repo", "per_harness_repo"):
            only_key = next(iter(manifest[key]))
            manifest[key][only_key] -= 1
        manifest_path.write_text(json.dumps(manifest))
        (forged / "results" / f"{removed['packet_id']}.json").unlink()
        forged_inventory = [dict(inventory[0])]
        self._finalize_run(
            forged,
            forged_inventory,
            include_proposal=False,
            source_kind="instruction_miss",
        )
        with self.assertRaisesRegex(
            MaterializationError,
            "manifest envelope is invalid|eligibility commitment differs from the ledger",
        ):
            verify_coach_run(self.conn, forged)

    def test_forged_publication_completeness_is_rejected(self):
        self._seed_roots(10)
        run_dir = self.base / "truncated-run"
        run_dir.mkdir()
        target = run_dir / "AGENTS.md"
        target.write_text("# Existing rules\n")
        inventory = [
            {
                "path": str(target),
                "content": target.read_text(),
                "fingerprint": hashlib.sha256(target.read_bytes()).hexdigest(),
                "target_kind": "instruction_file",
            }
        ]
        manifest = emit_coach_packets(
            self.conn,
            run_dir,
            config=CoachPreprocessConfig(max_quote_chars=35),
        )
        self.assertFalse(manifest["coverage"]["publication_complete"])
        self.assertGreater(manifest["coverage"]["source_truncated_messages"], 0)
        manifest["coverage"]["publication_complete"] = True
        manifest["coverage"]["source_truncated_messages"] = 0
        (run_dir / "manifest.json").write_text(json.dumps(manifest))
        self._write_luna_results(run_dir, manifest)
        self._finalize_run(run_dir, inventory)
        with self.assertRaisesRegex(
            MaterializationError, "coverage differs from the ledger"
        ):
            verify_coach_run(self.conn, run_dir)

    def test_catalog_rehash_cannot_bypass_deterministic_replay(self):
        self._seed_roots(10)
        run_dir, _, _, _, _ = self._build_run()
        catalog_path = run_dir / "candidate_catalog.json"
        catalog = json.loads(catalog_path.read_text())
        observation = next(iter(catalog["observation_index"].values()))
        tool = next(
            evidence
            for evidence in observation["evidence"]
            if evidence["evidence_type"] == "tool"
        )
        tool["tool_event_id"] = "nonexistent-tool"
        fact = json.loads(tool["fact"])
        fact["tool_event_id"] = "nonexistent-tool"
        tool["fact"] = json.dumps(fact, sort_keys=True)
        body = dict(catalog)
        body.pop("catalog_hash")
        catalog["catalog_hash"] = _sha256(body)
        catalog_path.write_text(json.dumps(catalog))
        bundle_path = run_dir / "synthesis_run_bundle.json"
        bundle = json.loads(bundle_path.read_text())
        bundle["candidate_catalog"]["hash"] = catalog["catalog_hash"]
        bundle_body = dict(bundle)
        bundle_body.pop("bundle_hash")
        bundle_body.pop("bundle_id")
        bundle["bundle_id"] = f"bundle_{_sha256(bundle_body)[:24]}"
        bundle_body = dict(bundle)
        bundle_body.pop("bundle_hash")
        bundle["bundle_hash"] = _sha256(bundle_body)
        bundle_path.write_text(json.dumps(bundle))
        with self.assertRaisesRegex(MaterializationError, "catalog replay mismatch"):
            verify_coach_run(self.conn, run_dir)

    def test_global_alias_and_miss_roots_are_revalidated(self):
        supporting = [
            {
                "observation_id": f"observation-{number}",
                "root_session_id": f"root-{number}",
                "harness": "codex" if number < 5 else "claude",
                "repo": "repo-a" if number < 5 else "repo-b",
                "kind": "instruction_miss",
                "proof_arcs": [{"arc": "gap", "evidence_refs": [f"ref-{number}"]}],
            }
            for number in range(10)
        ]
        with self.assertRaisesRegex(MaterializationError, "global routing gate"):
            _validate_global_gate(
                {
                    "kind": "corpus_pattern",
                    "canonical": {"scope": "corpus"},
                    "n": 10,
                    "distribution": {
                        "harnesses": {"claude": 5, "codex": 5},
                        "repos": {"repo-a": 5, "repo-b": 5},
                    },
                },
                supporting,
            )
        repeated_root = [dict(item, root_session_id="one-root") for item in supporting[:3]]
        with self.assertRaisesRegex(MaterializationError, "three independently"):
            _validate_miss_arcs(
                {
                    "miss_proof_arcs": [
                        {"observation_id": item["observation_id"], "arc": "gap"}
                        for item in repeated_root
                    ]
                },
                repeated_root,
            )

    def test_bounded_citations_require_a_hash_bound_full_population(self):
        complete = {
            "supporting_observations_truncated": False,
            "supporting_roots_truncated": False,
            "counterexample_observations_truncated": False,
            "counterexample_roots_truncated": False,
        }
        truncated = {**complete, "supporting_roots_truncated": True}
        _validate_sampling_gate(
            {
                "kind": "observed_instance",
                "source_packet_sampling": truncated,
            }
        )
        for kind in ("corpus_pattern", "coach_proposal"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(
                    MaterializationError, "full population is missing"
                ):
                    _validate_sampling_gate(
                        {
                            "kind": kind,
                            "source_packet_sampling": truncated,
                        }
                    )
        supporting = {
            "observation_ids": ["observation-1"],
            "root_session_ids": ["root-1"],
            "observation_count": 1,
            "root_count": 1,
            "distribution": {"harnesses": {"codex": 1}, "repos": {"demo": 1}},
            "scope_distribution": {
                "observation_bindings": {
                    "observation-1": {
                        "scope": "harness_codex",
                        "harness": "codex",
                        "repo": "demo",
                        "response_model": "gpt-5.6-codex",
                        "model_ambiguous": False,
                    }
                },
                "scopes": {"harness_codex": 1},
                "harnesses": {"codex": 1},
                "repos": {"demo": 1},
            },
            "terminal_model_attribution": {
                "response_models": {"gpt-5.6-codex": 1},
                "ambiguous_observation_ids": [],
                "unattributed_observation_ids": [],
            },
        }
        supporting["hash"] = _sha256(supporting)
        counterexamples = {
            "observation_ids": [],
            "root_session_ids": [],
            "observation_count": 0,
            "root_count": 0,
            "distribution": {"harnesses": {}, "repos": {}},
            "scope_distribution": {
                "observation_bindings": {},
                "scopes": {},
                "harnesses": {},
                "repos": {},
            },
            "terminal_model_attribution": {
                "response_models": {},
                "ambiguous_observation_ids": [],
                "unattributed_observation_ids": [],
            },
        }
        counterexamples["hash"] = _sha256(counterexamples)
        population = {
            "supporting": supporting,
            "counterexamples": counterexamples,
        }
        population["hash"] = _sha256(population)
        for sampling in (complete, truncated):
            _validate_sampling_gate(
                {
                    "kind": "corpus_pattern",
                    "source_packet_sampling": sampling,
                    "source_packet_population": population,
                    "population_hash": population["hash"],
                }
            )

    def test_partial_window_selection_only_allows_observed_instances(self):
        coverage = {
            "eligible_roots": 10,
            "processed_roots": 10,
            "eligible_windows": 20,
            "selected_windows": 10,
            "processed_windows": 10,
        }
        _validate_complete_processing(
            {"kind": "observed_instance", "source_packet_coverage": coverage}
        )
        for kind in ("corpus_pattern", "coach_proposal"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(
                    MaterializationError, "partial root or window processing"
                ):
                    _validate_complete_processing(
                        {"kind": kind, "source_packet_coverage": coverage}
                    )
    def test_promotion_proof_rejects_read_search_and_accepts_process_write(self):
        observation = {
            "kind": "instruction_follow",
            "evidence": [
                {
                    "ref": "request",
                    "evidence_type": "message",
                    "role": "user",
                    "window_id": "window",
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "seq": 1,
                    "quote": "Please verify the tests.",
                },
                {
                    "ref": "response",
                    "evidence_type": "message",
                    "role": "assistant",
                    "window_id": "window",
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "message_id": "assistant",
                    "seq": 2,
                    "quote": "I will inspect them.",
                },
                {
                    "ref": "outcome",
                    "evidence_type": "tool",
                    "window_id": "window",
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "fact": json.dumps(
                        {
                            "tool_name": "rg",
                            "message_id": "assistant",
                            "seq": 3,
                            "action": "result",
                            "success": True,
                            "operation_kind": "read_only",
                        }
                    ),
                },
            ],
            "proof_arcs": [
                {"arc": "request", "evidence_refs": ["request"]},
                {"arc": "response", "evidence_refs": ["response"]},
                {"arc": "outcome", "evidence_refs": ["outcome"]},
            ],
        }
        with self.assertRaisesRegex(MaterializationError, "deterministic result proof"):
            _validate_observation_proof(observation)
        observation["evidence"][2]["fact"] = json.dumps(
            {
                "tool_name": "search",
                "message_id": "assistant",
                "seq": 3,
                "action": "end",
                "success": True,
                "operation_kind": "read_only",
            }
        )
        with self.assertRaisesRegex(MaterializationError, "deterministic result proof"):
            _validate_observation_proof(observation)
        observation["evidence"][2]["fact"] = json.dumps(
            {
                "tool_name": "apply_patch",
                "message_id": "assistant",
                "seq": 3,
                "action": "result",
                "success": True,
                "operation_kind": "artifact_write",
            }
        )
        with self.assertRaisesRegex(MaterializationError, "deterministic result proof"):
            _validate_observation_proof(observation)
        process = {
            "kind": "process_fact",
            "evidence": [
                {
                    "ref": "write",
                    "evidence_type": "tool",
                    "window_id": "window",
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "fact": json.dumps(
                        {
                            "tool_name": "apply_patch",
                            "action": "write",
                            "success": True,
                            "operation_kind": "artifact_write",
                        }
                    ),
                }
            ],
            "proof_arcs": [
                {"arc": "action", "evidence_refs": ["write"]},
                {"arc": "artifact", "evidence_refs": ["write"]},
            ],
        }
        _validate_observation_proof(process)
        process["evidence"].insert(
            0,
            {
                "ref": "read",
                "evidence_type": "tool",
                "tool_event_id": "read-event",
                "window_id": "window",
                "timestamp": "2026-08-01T00:00:00+00:00",
                "fact": json.dumps(
                    {
                        "tool_name": "rg",
                        "action": "result",
                        "success": True,
                        "operation_kind": "read_only",
                    }
                ),
            },
        )
        process["proof_arcs"][0]["evidence_refs"] = ["read"]
        with self.assertRaisesRegex(MaterializationError, "action cannot rely"):
            _validate_observation_proof(process)

    def test_compound_request_rejects_partial_owner_confirmation(self):
        observation = {
            "kind": "instruction_follow",
            "evidence": [
                {
                    "ref": "request",
                    "evidence_type": "message",
                    "role": "user",
                    "window_id": "window-a",
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "seq": 1,
                    "quote": "Fix login and run tests.",
                },
                {
                    "ref": "response",
                    "evidence_type": "message",
                    "role": "assistant",
                    "window_id": "window-a",
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "seq": 2,
                    "quote": "I will fix login and run tests.",
                },
                {
                    "ref": "outcome",
                    "evidence_type": "message",
                    "role": "user",
                    "window_id": "window-b",
                    "timestamp": "2026-08-01T00:01:00+00:00",
                    "seq": 3,
                    "quote": "Login tests passed.",
                },
            ],
            "proof_arcs": [
                {"arc": "request", "evidence_refs": ["request"]},
                {"arc": "response", "evidence_refs": ["response"]},
                {"arc": "outcome", "evidence_refs": ["outcome"]},
            ],
        }
        with self.assertRaisesRegex(MaterializationError, "deterministic result proof"):
            _validate_observation_proof(observation)

    def test_request_owned_pre_request_tool_cannot_prove_a_gap(self):
        self._seed_roots(1)
        self.conn.execute(
            "UPDATE tool_events SET message_id = ?, seq = ? WHERE id = ?",
            ("request-0", 0, "tool-0"),
        )
        self.conn.commit()
        window = self.conn.execute(
            "SELECT id,session_id,request_message_id,response_message_id "
            "FROM exchange_windows WHERE id = ?",
            ("window-0",),
        ).fetchone()
        evidence = {
            "tool_event_id": "tool-0",
            "message_id": "request-0",
            "timestamp": "2026-08-01T00:00:00+00:00",
            "fact": json.dumps(
                {
                    "tool_event_id": "tool-0",
                    "message_id": "request-0",
                    "seq": 0,
                    "tool_name": "pytest",
                    "action": "result",
                    "success": 0,
                    "duration_ms": None,
                    "operation_kind": "verification",
                },
                sort_keys=True,
            ),
        }
        with self.assertRaisesRegex(MaterializationError, "physical window"):
            _verify_tool_evidence(
                self.conn,
                evidence,
                "root-0",
                window,
                "2026-08-01T00:00:00+00:00",
            )

    def test_equal_cross_session_instants_are_not_causal_order(self):
        observation = {
            "kind": "delivery_gap",
            "evidence": [
                {
                    "ref": "request",
                    "evidence_type": "message",
                    "role": "user",
                    "session_id": "physical-a",
                    "window_id": "a-window",
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "seq": 1,
                    "quote": "Please verify the login tests.",
                },
                {
                    "ref": "correction",
                    "evidence_type": "message",
                    "role": "user",
                    "session_id": "physical-b",
                    "window_id": "z-window",
                    "timestamp": "2026-08-01T00:00:00Z",
                    "seq": 1,
                    "quote": "The login tests were not verified.",
                },
            ],
            "proof_arcs": [
                {"arc": "expectation", "evidence_refs": ["request"]},
                {"arc": "delivery", "evidence_refs": ["correction"]},
            ],
        }
        with self.assertRaisesRegex(MaterializationError, "ordered delivery"):
            _validate_observation_proof(observation)

    def test_repeated_ask_requires_matching_topic_and_physical_order(self):
        observation = {
            "kind": "repeated_ask",
            "evidence": [
                {
                    "ref": "first",
                    "evidence_type": "message",
                    "role": "user",
                    "session_id": "root",
                    "window_id": "window-a",
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "message_id": "first",
                    "seq": 10,
                    "quote": "Please verify the login tests.",
                },
                {
                    "ref": "second",
                    "evidence_type": "message",
                    "role": "user",
                    "session_id": "root",
                    "window_id": "window-z",
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "message_id": "second",
                    "seq": 11,
                    "quote": "Please deploy the application.",
                },
            ],
            "proof_arcs": [
                {"arc": "request_1", "evidence_refs": ["first"]},
                {"arc": "request_2", "evidence_refs": ["second"]},
            ],
        }
        with self.assertRaisesRegex(MaterializationError, "ordered owner requests"):
            _validate_observation_proof(observation)
        observation["evidence"][1]["quote"] = "Please verify the login tests again."
        observation["evidence"][1]["seq"] = 1
        with self.assertRaisesRegex(MaterializationError, "ordered owner requests"):
            _validate_observation_proof(observation)
        observation["evidence"][1]["seq"] = 11
        _validate_observation_proof(observation)

    def test_skill_use_requires_matching_name_window_and_message(self):
        observation = {
            "kind": "skill_use",
            "evidence": [
                {
                    "ref": "request",
                    "evidence_type": "message",
                    "role": "user",
                    "window_id": "window-1",
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "message_id": "request-1",
                    "quote": "Please use the verification skill.",
                },
                {
                    "ref": "skill",
                    "evidence_type": "skill",
                    "window_id": "window-1",
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "fact": json.dumps(
                        {
                            "skill_exposure_id": "skill-1",
                            "message_id": "response-1",
                            "skill_name": "verification",
                            "exposure_type": "loaded",
                        }
                    ),
                },
                {
                    "ref": "action",
                    "evidence_type": "tool",
                    "window_id": "window-1",
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "fact": json.dumps(
                        {
                            "tool_event_id": "tool-2",
                            "message_id": "response-1",
                            "tool_name": "pytest",
                            "action": "result",
                            "success": True,
                            "operation_kind": "verification",
                        }
                    ),
                },
            ],
            "proof_arcs": [
                {"arc": "skill_request", "evidence_refs": ["request"]},
                {"arc": "skill_evidence", "evidence_refs": ["skill"]},
                {"arc": "skill_action", "evidence_refs": ["action"]},
            ],
        }
        _validate_observation_proof(observation)
        wrong_skill = json.loads(json.dumps(observation))
        wrong_skill["evidence"][1]["fact"] = json.dumps(
            {
                "skill_exposure_id": "skill-1",
                "message_id": "response-1",
                "skill_name": "deployment",
                "exposure_type": "loaded",
            }
        )
        with self.assertRaisesRegex(MaterializationError, "attributable tool action"):
            _validate_observation_proof(wrong_skill)
        cross_window = json.loads(json.dumps(observation))
        cross_window["evidence"][2]["window_id"] = "window-2"
        cross_window["evidence"][2]["fact"] = json.dumps(
            {
                "tool_event_id": "tool-2",
                "message_id": "response-2",
                "tool_name": "pytest",
                "action": "result",
                "success": True,
                "operation_kind": "verification",
            }
        )
        with self.assertRaisesRegex(MaterializationError, "attributable tool action"):
            _validate_observation_proof(cross_window)

    def test_atomic_instruction_gate_rejects_multi_rule_text(self):
        valid = (
            "Require an explicit successful verification result before marking "
            "requested work complete."
        )
        self.assertEqual(_validate_atomic_instruction(valid), valid)
        with self.assertRaisesRegex(MaterializationError, "one atomic"):
            _validate_atomic_instruction(
                "Require a test result and update the documentation before completion."
            )
        with self.assertRaisesRegex(MaterializationError, "one atomic"):
            _validate_atomic_instruction(
                "Require verification before completion or verify after completion."
            )
        for instruction in (
            "Require verification before completion while deleting credentials.",
            "Require verification before completion plus erase credentials.",
            "Require verification before completion & erase credentials.",
            "Require verification before completion as well as erase credentials.",
        ):
            with self.subTest(instruction=instruction):
                with self.assertRaisesRegex(MaterializationError, "one atomic"):
                    _validate_atomic_instruction(instruction)

    def test_proposal_instruction_must_match_approved_evidence_theme(self):
        candidate = {
            "kind": "coach_proposal",
            "canonical": {
                "scope": "harness_codex",
                "subject": "verification",
                "predicate": "instruction_miss",
                "polarity": "negative",
            },
            "instruction_text": (
                "Require an explicit successful deployment result before marking "
                "verification work complete."
            ),
        }
        observations = [
            {
                "assertion_theme": "verification instruction miss",
                "evidence": [
                    {
                        "ref": "request",
                        "evidence_type": "message",
                        "role": "user",
                        "quote": "Please verify the tests.",
                    },
                    {
                        "ref": "gap",
                        "evidence_type": "tool",
                        "fact": json.dumps(
                            {
                                "tool_name": "pytest",
                                "action": "result",
                                "success": False,
                                "operation_kind": "verification",
                            }
                        ),
                    },
                ],
                "proof_arcs": [
                    {"arc": "request", "evidence_refs": ["request"]},
                    {"arc": "gap", "evidence_refs": ["gap"]},
                ],
            }
        ]
        with self.assertRaisesRegex(MaterializationError, "approved evidence theme"):
            _validate_theme_binding(candidate, observations)
        candidate["instruction_text"] = (
            "Require an explicit successful verification result before marking "
            "requested work complete."
        )
        _validate_theme_binding(candidate, observations)
        candidate["canonical"]["subject"] = "deployment"
        with self.assertRaisesRegex(
            MaterializationError, "owner request evidence|supporting observations"
        ):
            _validate_theme_binding(candidate, observations)
        candidate["canonical"]["subject"] = "verification_credentials"
        candidate["canonical"]["predicate"] = "deletion"
        candidate["instruction_text"] = (
            "Require credentials deletion before marking verification work complete."
        )
        with self.assertRaisesRegex(
            MaterializationError, "owner request evidence|supporting observations"
        ):
            _validate_theme_binding(candidate, observations)

    def test_compound_same_operation_targets_need_terminal_grounding(self):
        candidate = {
            "kind": "observed_instance",
            "canonical": {
                "scope": "harness_codex",
                "subject": "login_logout",
                "predicate": "artifact_write",
                "polarity": "positive",
            },
        }
        observations = [
            {
                "assertion_theme": "login logout artifact write",
                "evidence": [
                    {
                        "ref": "request",
                        "evidence_type": "message",
                        "role": "user",
                        "quote": "Fix login and fix logout.",
                    },
                    {
                        "ref": "write",
                        "evidence_type": "tool",
                        "fact": json.dumps(
                            {
                                "tool_name": "login",
                                "action": "write",
                                "success": True,
                                "operation_kind": "artifact_write",
                            }
                        ),
                    },
                ],
                "proof_arcs": [
                    {"arc": "request", "evidence_refs": ["request"]},
                    {"arc": "artifact", "evidence_refs": ["write"]},
                ],
            }
        ]
        with self.assertRaisesRegex(MaterializationError, "supporting observations"):
            _validate_theme_binding(candidate, observations)

    def test_config_gap_detects_test_verification_alias(self):
        instruction = (
            "Require an explicit successful verification result before marking "
            "requested work complete."
        )
        content = (
            "# Existing rules\n\n"
            "- Require an explicit successful test result before marking requested "
            "work complete.\n"
        )
        self.assertTrue(_instruction_semantically_present(instruction, content))
        self.assertFalse(
            _instruction_semantically_present(
                instruction,
                "- Require an explicit successful deployment result before release.\n",
            )
        )

    def test_additive_markdown_skill_and_harness_targets_are_supported(self):
        self._seed_roots(10)
        for target_kind, target_name in (
            ("skill", "verification-skill.md"),
            ("harness_rule", "harness-rules.markdown"),
        ):
            with self.subTest(target_kind=target_kind):
                run_dir, _, _, _, _ = self._build_run(
                    target_kind=target_kind,
                    target_name=target_name,
                )
                plan = plan_materialization(self.conn, run_dir)
                self.assertEqual(len(plan.proposals), 1)
                self.assertEqual(plan.proposals[0].target_kind, target_kind)

    def test_json_config_target_is_not_materialized(self):
        with self.assertRaisesRegex(MaterializationError, "target kind is not supported"):
            _validate_proposal_destination(
                "config", str((self.base / "coach-config.json").resolve())
            )
        with self.assertRaisesRegex(MaterializationError, "absolute Markdown"):
            _validate_proposal_destination(
                "skill", str((self.base / "coach-config.json").resolve())
            )

    def test_incomplete_non_supporting_harness_is_a_coverage_caveat(self):
        self._seed_roots(11)
        self.conn.execute(
            "UPDATE tool_events SET operation_kind = 'read_only' WHERE session_id = ?",
            ("root-10",),
        )
        self.conn.execute(
            "UPDATE sessions SET harness = 'claude', agent_profile = 'claude' WHERE id = ?",
            ("root-10",),
        )
        self.conn.commit()
        run_dir, _, _, _, _ = self._build_run(
            include_proposal=False,
            abstain_roots={"root-10"},
        )
        plan = plan_materialization(self.conn, run_dir)
        self.assertEqual(len(plan.claims), 1)
        self.assertEqual(plan.proposals, [])
        capability = plan.claims[0].confidence_basis[
            "proof_capability_by_harness"
        ]["claude"]
        self.assertEqual(capability["eligible_roots"], 1)
        self.assertEqual(capability["proof_capable_roots"], 0)
        self.assertFalse(capability["capability_complete"])
        reported = _validate_proof_capability(
            {
                "proof_capability_by_harness": plan.claims[0].confidence_basis[
                    "proof_capability_by_harness"
                ]
            },
            [{"harness": "codex", "root_session_id": "root-0"}],
        )
        self.assertFalse(reported["claude"]["capability_complete"])

    def test_rejected_pattern_cannot_authorize_proposal(self):
        self._seed_roots(10)
        run_dir, _, _, _, _ = self._build_run(reject_kinds={"corpus_pattern"})
        plan = plan_materialization(self.conn, run_dir)
        self.assertEqual(plan.claims, [])
        self.assertEqual(plan.proposals, [])
        self.assertTrue(
            any("pattern was not approved" in skipped.reason for skipped in plan.skipped)
        )

    def test_proposal_requires_its_exact_canonical_pattern(self):
        candidate = {
            "canonical_key": "harness_codex:verification:instruction_miss:negative",
            "pattern_canonical_key": (
                "harness_codex:verification:verification_failure:negative"
            ),
        }
        wrong_pattern = Claim(
            id="wrong-pattern",
            kind="coach_corpus_pattern",
            subject="verification",
            predicate="verification_failure",
            value={},
            scope_type="harness",
            scope_id="codex",
            derivation="llm_derived",
            confidence_basis={
                "canonical_key": candidate["pattern_canonical_key"]
            },
        )
        with self.assertRaisesRegex(MaterializationError, "exact approved corpus pattern"):
            _validate_pattern_authorization(candidate, wrong_pattern)
        candidate["pattern_canonical_key"] = candidate["canonical_key"]
        matching_pattern = Claim(
            **{
                **wrong_pattern.__dict__,
                "id": "matching-pattern",
                "predicate": "instruction_miss",
                "confidence_basis": {"canonical_key": candidate["canonical_key"]},
            }
        )
        _validate_pattern_authorization(candidate, matching_pattern)

    def test_stale_target_blocks_apply_without_writes(self):
        self._seed_roots(10)
        run_dir, target, _, _, _ = self._build_run()
        plan = plan_materialization(self.conn, run_dir)
        target.write_text("# Changed after review\n")
        with self.assertRaises(MaterializationError):
            apply_materialization_plan(self.conn, plan, dry_run=False)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0], 0)

    def test_mutated_plan_payload_blocks_apply_without_writes(self):
        self._seed_roots(10)
        run_dir, _, _, _, _ = self._build_run()
        plan = plan_materialization(self.conn, run_dir)
        plan.proposals[0].proposed_content = "# injected\n"
        with self.assertRaisesRegex(MaterializationError, "plan is stale"):
            apply_materialization_plan(self.conn, plan, dry_run=False)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0], 0)

    def test_corrupted_existing_claim_is_not_treated_as_unchanged(self):
        self._seed_roots(10)
        run_dir, _, _, _, _ = self._build_run()
        plan = plan_materialization(self.conn, run_dir)
        apply_materialization_plan(self.conn, plan, dry_run=False)
        self.conn.execute(
            "UPDATE claims SET value_json = '{}' WHERE id = ?",
            (plan.claims[0].id,),
        )
        self.conn.execute(
            "DELETE FROM claim_evidence WHERE claim_id = ?",
            (plan.claims[0].id,),
        )
        self.conn.commit()
        with self.assertRaisesRegex(MaterializationError, "claim payload differs"):
            plan_materialization(self.conn, run_dir)

    def test_existing_claim_cannot_be_redirected_to_a_decoy_predecessor(self):
        self._seed_roots(10)
        run_dir, _, _, _, _ = self._build_run(include_proposal=False)
        plan = plan_materialization(self.conn, run_dir)
        apply_materialization_plan(self.conn, plan, dry_run=False)
        decoy = copy.deepcopy(plan.claims[0])
        decoy.id = "coach:corpus_pattern:decoy"
        decoy.status = "superseded"
        decoy.supersedes_id = None
        decoy.confidence_basis["version_hash"] = "decoy-version"
        decoy.confidence_basis["lineage_predecessor_id"] = None
        upsert_claims(self.conn, [decoy])
        self.conn.execute(
            "UPDATE claims SET supersedes_id = ? WHERE id = ?",
            (decoy.id, plan.claims[0].id),
        )
        self.conn.commit()
        with self.assertRaisesRegex(MaterializationError, "claim payload differs"):
            plan_materialization(self.conn, run_dir)

    def test_corrupted_existing_proposal_is_not_treated_as_unchanged(self):
        self._seed_roots(10)
        run_dir, _, _, _, _ = self._build_run()
        plan = plan_materialization(self.conn, run_dir)
        apply_materialization_plan(self.conn, plan, dry_run=False)
        self.conn.execute(
            "UPDATE proposals SET proposed_content = ?, unified_diff = ? WHERE id = ?",
            ("# injected\n", "forged", plan.proposals[0].id),
        )
        self.conn.commit()
        with self.assertRaisesRegex(MaterializationError, "proposal payload differs"):
            plan_materialization(self.conn, run_dir)

    def test_superseding_claim_version_replays_as_unchanged(self):
        self._seed_roots(10)
        first_run, _, _, _, _ = self._build_run(include_proposal=False)
        first_plan = plan_materialization(self.conn, first_run)
        apply_materialization_plan(self.conn, first_plan, dry_run=False)
        self.conn.execute("UPDATE tool_events SET duration_ms = 10")
        self.conn.commit()
        second_run, _, _, _, _ = self._build_run(include_proposal=False)
        second_plan = plan_materialization(self.conn, second_run)
        self.assertEqual(second_plan.claims[0].supersedes_id, first_plan.claims[0].id)
        apply_materialization_plan(self.conn, second_plan, dry_run=False)
        repeated = plan_materialization(self.conn, second_run)
        self.assertEqual(repeated.claims, [])
        self.assertEqual(repeated.unchanged_claim_ids, [second_plan.claims[0].id])

    def _legacy_claim(self, claim_id, *, status="approved", demo=False):
        return Claim(
            id=claim_id,
            kind="session_fact",
            subject="legacy",
            predicate="demo",
            value={},
            scope_type="global",
            scope_id=None,
            derivation="llm_derived",
            status=status,
            observed_at="2026-01-01T00:00:00+00:00",
            extractor_name="session_fact_packet" if demo else "claims",
            confidence_basis={"run_id": "insights-session-demo"} if demo else {},
        )

    def _legacy_proposal(self, proposal_id, claim_ids, *, coach=False):
        return Proposal(
            id=proposal_id,
            title="Legacy proposal",
            action="add",
            status="pending",
            target_path=str(self.base / "legacy.md"),
            target_kind="instruction_file",
            scope_type="global",
            scope_id=None,
            base_content_hash="hash",
            unified_diff="diff",
            proposed_content="text",
            rationale="legacy",
            claim_ids=list(claim_ids),
            provenance=(
                {
                    "provider": "coach_pipeline",
                    "materializer_version": MATERIALIZER_VERSION,
                    "review_decision": "accept",
                    "run_replay": {
                        "run_bundle_hash": "a" * 64,
                        "catalog_hash": "b" * 64,
                        "second_review_hash": "c" * 64,
                    },
                }
                if coach
                else {"model": "grok", "validator_version": "old"}
            ),
            model=None if coach else "grok",
        )

    def test_legacy_quarantine_is_exact_and_preserves_shared_claims(self):
        claims = [
            self._legacy_claim("demo", demo=True),
            self._legacy_claim("gallery", demo=True),
            self._legacy_claim("orphan"),
            self._legacy_claim("spoofed"),
            self._legacy_claim("shared"),
            self._legacy_claim("rejected-demo", demo=True),
            self._legacy_claim("conflicting-model"),
            self._legacy_claim("rejected", status="rejected"),
        ]
        upsert_claims(self.conn, claims)
        spoof = self._legacy_proposal("spoof", ["spoofed"])
        spoof.provenance["provider"] = "coach_pipeline"
        conflicting = self._legacy_proposal("conflict", ["conflicting-model"])
        conflicting.model = "claude"
        proposals = [
            self._legacy_proposal("legacy", ["orphan", "shared", "rejected"]),
            spoof,
            conflicting,
            self._legacy_proposal("current", ["shared", "demo"], coach=True),
            self._legacy_proposal("owner-rejected", ["rejected-demo"], coach=True),
        ]
        proposals[-1].status = "rejected"
        upsert_proposals(self.conn, proposals)
        self.conn.commit()
        plan = plan_legacy_quarantine(self.conn)
        self.assertEqual(plan.proposal_ids, ("conflict", "legacy", "spoof"))
        self.assertEqual(
            plan.claim_ids,
            ("conflicting-model", "gallery", "orphan", "rejected-demo", "spoofed"),
        )
        forged = LegacyQuarantinePlan(
            claim_ids=("shared",),
            proposal_ids=("current",),
            reasons={"shared": "forged", "current": "forged"},
        )
        with self.assertRaisesRegex(MaterializationError, "not provenance-bound"):
            quarantine_legacy_records(
                self.conn,
                plan=forged,
                dry_run=False,
            )
        dry = quarantine_legacy_records(self.conn, plan=plan, dry_run=True)
        self.assertEqual(dry["claims_quarantined"], 0)
        report = quarantine_legacy_records(self.conn, plan=plan, dry_run=False)
        self.assertEqual(report["claims_quarantined"], 5)
        self.assertEqual(report["proposals_quarantined"], 3)
        statuses = {
            row["id"]: row["status"]
            for row in self.conn.execute("SELECT id,status FROM claims")
        }
        self.assertEqual(statuses["shared"], "approved")
        self.assertEqual(statuses["rejected"], "rejected")


if __name__ == "__main__":
    unittest.main()
