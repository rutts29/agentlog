import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.coach.synthesis import (
    _record_from_observation,
    _observation_terminal_proof,
    build_candidate_catalog,
    build_config_target_map,
    build_synthesis_packets,
    exact_deduplicate_observations,
    has_bounded_gap_evidence,
    has_completion_evidence,
    load_validated_observation_records,
    run_synthesis_pipeline,
    validate_second_review_result,
    validate_terra_result,
)
from agentlog.analysis.coach.preprocess import emit_coach_packets, validate_coach_result as _validate_coach_result
from agentlog.db.schema import init_db
from agentlog.safety.redaction import RedactionReport, redact_text


def _legacy_result_with_dispositions(raw, packet):
    if not isinstance(raw, dict) or "window_dispositions" in raw:
        return raw
    enriched = json.loads(json.dumps(raw))
    supported = {
        str(window.get("window_id") or ""): []
        for window in packet.get("windows", [])
        if isinstance(window, dict) and str(window.get("window_id") or "")
    }
    observations = enriched.get("observations")
    if isinstance(observations, list):
        for index, observation in enumerate(observations, start=1):
            if not isinstance(observation, dict):
                continue
            observation_id = str(observation.get("observation_id") or "").strip()
            if not observation_id:
                observation_id = f"legacy-observation-{index}"
                observation["observation_id"] = observation_id
            for evidence in observation.get("evidence", []):
                if not isinstance(evidence, dict):
                    continue
                window_id = str(evidence.get("window_id") or "")
                if window_id in supported and observation_id not in supported[window_id]:
                    supported[window_id].append(observation_id)
    enriched["window_dispositions"] = [
        {
            "window_id": window_id,
            "observation_ids": observation_ids,
            "no_supported_observation": not observation_ids,
        }
        for window_id, observation_ids in supported.items()
    ]
    return enriched


def validate_coach_result(raw, packet):
    return _validate_coach_result(_legacy_result_with_dispositions(raw, packet), packet)


def _manifest(denominator=20):
    snapshot = {"counts": {"sessions": denominator}, "high_water": {}, "artifacts": []}
    snapshot["snapshot_hash"] = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:24]
    return {
        "run_id": "coach_run_for_test",
        "corpus_snapshot": snapshot,
        "corpus_snapshot_hash": snapshot["snapshot_hash"],
        "coverage": {
            "eligible_roots": denominator,
            "selected_roots": denominator,
            "total_roots": denominator + 2,
            "processed": denominator,
            "proof_capability_by_harness": {
                "codex": {
                    "eligible_roots": denominator,
                    "packetized_roots": denominator,
                    "levels": {"deterministic_terminal": denominator, "owner_message_only": 0, "unknown": 0},
                    "capability": "supported",
                },
            },
            "scope_denominators": {
                "global": {"eligible_roots": denominator, "eligible_windows": denominator},
                "harness_codex": {"eligible_roots": denominator, "eligible_windows": denominator},
                "repo_demo": {"eligible_roots": denominator, "eligible_windows": denominator},
            },
        },
        "excluded_roots": ["filtered-root"],
        "per_harness": {"codex": denominator},
        "per_repo": {"demo": denominator},
        "config_inventory": [
            {
                "path": "/private/demo/AGENTS.md",
                "fingerprint": "config-hash-1",
                "content": "supply chain lock api_key=sk-abcdefghijklmnopqrstuvwxyz123456",
            }
        ],
    }


def _record(
    number,
    *,
    kind="instruction_miss",
    polarity="negative",
    assertion="verification_gap",
    scope="corpus",
    harness="codex",
    repo="demo",
    completion_proof=True,
    response_model="",
    model_ambiguous=False,
):
    ref = f"r-{number}"
    request_ref = f"request-{number}"
    arcs = [{"arc": "request", "evidence_refs": [request_ref]}, {"arc": "gap", "evidence_refs": [ref]}]
    if kind == "instruction_follow":
        arcs = [
            {"arc": "request", "evidence_refs": [request_ref]},
            {"arc": "response", "evidence_refs": [ref]},
            {"arc": "outcome", "evidence_refs": [ref]},
        ]
    if not completion_proof:
        arcs = [{"arc": "request", "evidence_refs": [ref]}]
    return {
        "observation_id": f"obs-{number}",
        "exact_hash": f"hash-{number}",
        "kind": kind,
        "assertion_key": assertion,
        "scope": scope,
        "polarity": polarity,
        "confidence": 0.8,
        "does_not_prove": "This bounded evidence does not establish a cause.",
        "root_session_id": f"root-{number}",
        "harness": harness,
        "repo": repo,
        "model_attribution": {
            "response_model": response_model,
            "response_effort": "",
            "terminal_window_ids": [f"w-{number}"] if response_model else [],
            "ambiguous": model_ambiguous,
            "descriptive_only": True,
        },
        "evidence": [{
            "ref": ref,
            "evidence_type": "tool" if kind == "instruction_miss" else "message",
            "window_id": f"w-{number}",
            "message_id": f"m-{number}",
            "role": "" if kind == "instruction_miss" else "assistant",
            "seq": 2,
            "quote": "The available evidence records an action.",
            "fact": json.dumps({"tool_event_id": f"tool-{number}", "tool_name": "pytest", "action": "end", "success": False, "operation_kind": "verification"}) if kind == "instruction_miss" else "",
        }, {
            "ref": request_ref,
            "evidence_type": "message",
            "window_id": f"w-{number}",
            "message_id": f"request-{number}",
            "role": "user",
            "seq": 1,
            "quote": "Run the verification tests.",
            "fact": "",
        }],
        "proof_arcs": arcs,
        "provenance": {
            "packet_hash": f"packet-{number}",
            "artifact_hashes": [f"artifact-{number}"],
            "source_hashes": [f"source-{number}"],
        },
    }


def _packet(records, *, denominator=20, scope="harness_codex"):
    packets = build_synthesis_packets(records, _manifest(denominator))
    return next(
        packet
        for packet in packets
        if packet["group"]["polarity"] == "negative" and packet["group"]["scope"] == scope
    )


def _validate(raw, packet):
    raw = dict(raw)
    raw.setdefault("producer", dict(packet["synthesis_assignment"]))
    return validate_terra_result(
        raw,
        packet,
        config_targets=build_config_target_map(_manifest()["config_inventory"]),
    )


def _candidate(packet, *, kind="corpus_pattern", global_scope=False):
    supporting = [item["observation_id"] for item in packet["supporting_observations"]]
    counter = [item["observation_id"] for item in packet["counterexample_observations"]]
    population = packet["full_population"]
    cited_n = len({item["root_session_id"] for item in packet["supporting_observations"]})
    n = cited_n if kind == "observed_instance" else population["supporting"]["root_count"]
    counter_n = len({item["root_session_id"] for item in packet["counterexample_observations"]}) if kind == "observed_instance" else population["counterexamples"]["root_count"]
    counter_observations = len(counter) if kind == "observed_instance" else population["counterexamples"]["observation_count"]
    processed = packet["coverage"]["processed_roots"]
    eligible = packet["coverage"]["full_eligible_root_denominator"]
    scope = "global" if global_scope else "harness_codex"
    candidate = {
        "kind": kind,
        "canonical": {
            "scope": scope,
            "subject": "verification",
            "predicate": "instruction_miss",
            "polarity": "negative",
        },
        "title": "Verification requests had explicit miss proof.",
        "summary": (
            f"Across {n} of {processed} processed roots, with {processed} of {eligible} eligible roots sampled, requested verification missed an explicit terminal result after the check was required; this is a partial sample."
            if processed < eligible
            else (
                f"Across {cited_n} cited supporting roots of {n} supporting roots, {n} of {eligible} reviewed roots requested verification that missed an explicit terminal result after the check was required."
                if cited_n < n
                else f"Across {n} of {eligible} reviewed roots, requested verification missed an explicit terminal result after the check was required."
            )
        ),
        "does_not_prove": "This bounded corpus slice cannot establish why the misses happened; contrasting counterevidence may limit how it changes outside reviewed roots.",
        "supporting_observation_ids": supporting,
        "counterevidence_observation_ids": counter,
        "n": n,
        "population_hash": population["hash"] if kind != "observed_instance" else "",
        "cited_supporting_roots": cited_n,
        "counterevidence_roots": counter_n,
        "counterevidence_observations": counter_observations,
        "denominator": packet["coverage"]["full_eligible_root_denominator"],
        "processed_roots": packet["coverage"]["processed_roots"],
        "eligible_roots": packet["coverage"]["eligible_roots"],
    }
    if kind == "coach_proposal":
        candidate.update(
            {
                "instruction_text": "Require an explicit verification result before marking a task complete.",
                "pattern_canonical_key": "harness_codex:verification:instruction_miss:negative",
                "target_ref": packet["config_overlap"]["searched"][0]["target_ref"],
                "target_kind": "instruction_file",
                "action": "add",
                "base_content_hash": "config-hash-1",
                "config_gap": {"available": True, "searched": [], "matches": []},
                "miss_proof_arcs": [
                    {"observation_id": observation_id, "arc": "gap"}
                    for observation_id in supporting[:3]
                ],
            }
        )
    return candidate


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _refresh_scope_population(packet):
    for label in ("supporting", "counterexamples"):
        population = packet["full_population"][label]
        bindings = population["scope_distribution"]["observation_bindings"]

        def counts(field, include_empty=False):
            result = {}
            for binding in bindings.values():
                value = str(binding.get(field) or "")
                if not value and not include_empty:
                    continue
                result[value or "(unknown)"] = result.get(value or "(unknown)", 0) + 1
            return dict(sorted(result.items()))

        population["scope_distribution"] = {
            "observation_bindings": {key: bindings[key] for key in sorted(bindings)},
            "scopes": counts("scope"),
            "harnesses": counts("harness", include_empty=True),
            "repos": counts("repo", include_empty=True),
        }
        population["terminal_model_attribution"] = {
            "response_models": counts("response_model"),
            "ambiguous_observation_ids": sorted(
                observation_id
                for observation_id, binding in bindings.items()
                if binding["model_ambiguous"]
            ),
            "unattributed_observation_ids": sorted(
                observation_id
                for observation_id, binding in bindings.items()
                if not binding["response_model"]
            ),
        }
        body = dict(population)
        body.pop("hash", None)
        population["hash"] = _hash(body)
    body = dict(packet["full_population"])
    body.pop("hash", None)
    packet["full_population"]["hash"] = _hash(body)
    packet_body = dict(packet)
    packet_body.pop("packet_hash", None)
    packet["packet_hash"] = _hash(packet_body)


class CoachSynthesisTests(unittest.TestCase):
    def test_config_target_map_deduplicates_only_identical_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "AGENTS.md"
            target.write_text("# Rules\n")
            fingerprint = hashlib.sha256(target.read_bytes()).hexdigest()
            entry = {
                "path": str(target),
                "content": target.read_text(),
                "fingerprint": fingerprint,
                "target_kind": "instruction_file",
            }
            exact = build_config_target_map([entry, dict(entry)])
            conflicting = build_config_target_map(
                [entry, {**entry, "target_kind": "harness_rule"}]
            )

        self.assertEqual(len(exact["targets"]), 1)
        self.assertEqual(len(conflicting["targets"]), 2)
        self.assertEqual(
            {item["target_ref"] for item in conflicting["targets"]},
            {exact["targets"][0]["target_ref"]},
        )

    def test_exact_dedup_and_redacted_packet_contract(self):
        first = _record(1)
        duplicate = dict(first)
        duplicate["result_id"] = "duplicate-result"
        records = [first, duplicate, _record(2, kind="instruction_follow", polarity="positive")]
        deduped = exact_deduplicate_observations(records)
        self.assertEqual(len(deduped), 2)
        packet = _packet(deduped)
        self.assertEqual(packet["coverage"]["full_eligible_root_denominator"], 20)
        self.assertEqual(packet["exclusions"], ["filtered-root"])
        self.assertTrue(packet["counterexample_observations"])
        serialized = json.dumps(packet)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", serialized)
        self.assertNotIn("/private/demo/AGENTS.md", serialized)
        self.assertIn("fingerprint", serialized)
        self.assertIn("packet_hash", serialized)

    def test_synonymous_assertion_keys_share_one_family_packet(self):
        records = [
            _record(0, assertion="verify_tests_before_done"),
            _record(1, assertion="run_the_test_before_completion"),
            _record(2, assertion="check_test_result"),
            _record(3, assertion="testing_required"),
            _record(4, assertion="verify_the_check"),
            _record(99, kind="instruction_follow", polarity="positive"),
        ]
        packet = _packet(records)
        self.assertEqual(packet["group"]["evidence_family"], "instruction_compliance")
        self.assertEqual(len(packet["supporting_observations"]), 5)
        self.assertIn("run_the_test_before_completion", packet["group"]["assertion_keys"])
        candidate = _candidate(packet)
        raw = {"packet_id": packet["packet_id"], "result_id": "terra-family", "abstain": False, "candidates": [candidate]}
        cleaned, failures = _validate(raw, packet)
        self.assertEqual(failures, [])
        self.assertEqual(cleaned["candidates"][0]["n"], 5)

    def test_verified_assertion_variants_are_server_grouped_but_configuration_checks_are_not(self):
        def sealed_record(number, assertion_key, request_text):
            window_id = f"window-{number}"
            request_id = f"request-{number}"
            assistant_id = f"assistant-{number}"
            packet = {
                "packet_id": f"packet-{number}",
                "windows": [{
                    "window_id": window_id,
                    "session_id": f"session-{number}",
                    "root_session_id": f"root-{number}",
                    "harness": "codex",
                    "repo": "demo",
                    "timestamp": "2026-08-01T00:00:00Z",
                    "messages": [
                        {
                            "message_id": request_id,
                            "role": "user",
                            "seq": 1,
                            "timestamp": "2026-08-01T00:00:00Z",
                            "source_text": request_text,
                        },
                        {
                            "message_id": assistant_id,
                            "role": "assistant",
                            "seq": 2,
                            "timestamp": "2026-08-01T00:00:01Z",
                            "source_text": "I ran the requested command.",
                        },
                    ],
                }],
            }
            observation = {
                "kind": "instruction_miss",
                "assertion_key": assertion_key,
                "assertion_theme": "verification",
                "confidence": 0.8,
                "does_not_prove": "This bounded evidence does not establish a cause.",
                "evidence": [
                    {
                        "ref": "request",
                        "evidence_type": "message",
                        "window_id": window_id,
                        "message_id": request_id,
                        "role": "user",
                        "seq": 1,
                        "quote": request_text.split()[0],
                    },
                    {
                        "ref": "gap",
                        "evidence_type": "tool",
                        "window_id": window_id,
                        "message_id": assistant_id,
                        "tool_event_id": f"tool-{number}",
                        "fact": {
                            "tool_event_id": f"tool-{number}",
                            "tool_name": "pytest",
                            "action": "end",
                            "success": False,
                            "operation_kind": "verification",
                        },
                    },
                ],
                "proof_arcs": [
                    {"arc": "request", "evidence_refs": ["request"]},
                    {"arc": "gap", "evidence_refs": ["gap"]},
                ],
            }
            record, failure = _record_from_observation(observation, packet, f"result-{number}")
            self.assertIsNone(failure)
            self.assertIsNotNone(record)
            return record

        verification_records = [
            sealed_record(0, "verification", "Verify the requested work before completion."),
            sealed_record(1, "checks", "Run the requested tests before completion."),
            sealed_record(2, "pytest", "Run pytest before completion."),
            sealed_record(3, "validation_finish", "Validate the requested work before completion."),
            sealed_record(4, "run_checks_before_done", "Run the requested checks before completion."),
        ]
        configuration = sealed_record(
            99,
            "validation_finish",
            "Check deployment configuration.",
        )
        self.assertEqual({record["server_theme"] for record in verification_records}, {"verification"})
        self.assertEqual(configuration["server_theme"], "deployment_configuration")
        packets = build_synthesis_packets([*verification_records, configuration], _manifest())
        verification = next(
            packet
            for packet in packets
            if packet["group"]["scope"] == "harness_codex"
            and packet["group"]["polarity"] == "negative"
            and packet["group"]["assertion_theme"] == "verification"
        )
        self.assertEqual(len(verification["supporting_observations"]), 5)
        self.assertEqual(
            verification["group"]["assertion_keys"],
            ["checks", "pytest", "run_checks_before_done", "validation_finish", "verification"],
        )
        configuration_packet = next(
            packet
            for packet in packets
            if packet["group"]["scope"] == "harness_codex"
            and packet["group"]["polarity"] == "negative"
            and "deployment_configuration" in packet["group"]["assertion_theme"]
        )
        self.assertEqual(len(configuration_packet["supporting_observations"]), 1)

    def test_processed_and_eligible_denominators_remain_distinct(self):
        records = [_record(number) for number in range(5)]
        packet = next(
            item for item in build_synthesis_packets(
                records,
                _manifest(),
                processing_coverage={
                    "processed_packets": 1,
                    "processed_windows": 5,
                    "processed_roots": 5,
                    "abstained_packets": 0,
                    "valid_observations": 5,
                },
            )
            if item["group"]["polarity"] == "negative" and item["group"]["scope"] == "harness_codex"
        )
        self.assertEqual(packet["coverage"]["processed_roots"], 5)
        self.assertEqual(packet["coverage"]["eligible_roots"], 20)
        self.assertTrue(packet["coverage"]["processing_incomplete"])
        candidate = _candidate(packet)
        raw = {"packet_id": packet["packet_id"], "result_id": "terra-coverage", "abstain": False, "candidates": [candidate]}
        cleaned, failures = _validate(raw, packet)
        self.assertEqual(failures, [])
        self.assertEqual(cleaned["candidates"][0]["processed_roots"], 5)
        self.assertEqual(cleaned["candidates"][0]["eligible_roots"], 20)

    def test_result_loader_counts_only_processed_packets_and_roots(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        for session_id in ("root-a", "root-b"):
            conn.execute(
                "INSERT INTO sessions(id,harness,external_id,repo,agent_profile) VALUES(?,?,?,?,?)",
                (session_id, "codex", session_id, "demo", "codex"),
            )
            conn.execute(
                "INSERT INTO messages(id,session_id,seq,role,text,content_hash,model_canonical) VALUES(?,?,?,?,?,?,?)",
                (f"{session_id}-u", session_id, 1, "user", "Please verify the result", f"u-{session_id}", None),
            )
            conn.execute(
                "INSERT INTO messages(id,session_id,seq,role,text,content_hash,model_canonical) VALUES(?,?,?,?,?,?,?)",
                (f"{session_id}-a", session_id, 2, "assistant", "I will verify it", f"a-{session_id}", "sol"),
            )
            conn.execute(
                "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
                (f"{session_id}-w", session_id, f"{session_id}-u", f"{session_id}-a", f"i-{session_id}", f"w-{session_id}"),
            )
        conn.commit()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = emit_coach_packets(conn, root)
            results = root / "results"
            results.mkdir()
            for index, entry in enumerate(manifest["packets"], start=1):
                preprocess_packet = json.loads((root / entry["path"]).read_text())
                (results / f"{index}.json").write_text(
                    json.dumps({
                        "packet_id": entry["packet_id"], "result_id": f"abstain-{index}", "abstain": True,
                        "producer": preprocess_packet["producer_contract"]["expected"],
                        "abstain_reason": "insufficient proof",
                        "window_dispositions": [
                            {"window_id": window["window_id"], "observation_ids": [], "no_supported_observation": True}
                            for window in preprocess_packet["windows"]
                        ],
                    })
                )
            from agentlog.analysis.coach.synthesis import summarize_result_processing_coverage

            processing, failures = summarize_result_processing_coverage(root)
            scoped_packet = next(
                packet
                for packet in build_synthesis_packets(
                    [_record(1, response_model="sol")],
                    manifest,
                    processing_coverage=processing,
                )
                if packet["group"]["scope"] == "model_sol"
            )
        self.assertEqual(failures, [])
        self.assertEqual(processing["processed_packets"], 2)
        self.assertEqual(processing["processed_windows"], 2)
        self.assertEqual(processing["processed_roots"], 2)
        self.assertEqual(processing["abstained_packets"], 2)
        self.assertEqual(processing["valid_observations"], 0)
        repo_scope = next(scope for scope in processing["scope_counts"] if scope.startswith("repo_"))
        self.assertEqual(
            processing["scope_counts"],
            {
                "global": {"processed_roots": 2, "processed_windows": 2},
                "harness_codex": {"processed_roots": 2, "processed_windows": 2},
                "model_sol": {"processed_roots": 2, "processed_windows": 2},
                repo_scope: {"processed_roots": 2, "processed_windows": 2},
            },
        )
        self.assertEqual(scoped_packet["coverage"]["processed_roots"], 2)
        self.assertEqual(scoped_packet["coverage"]["full_eligible_root_denominator"], 2)
        self.assertFalse(scoped_packet["coverage"]["processing_incomplete"])
        conn.close()

    def test_pattern_and_proposal_catalog_and_second_review(self):
        records = [_record(i) for i in range(10)]
        records.append(_record(99, kind="instruction_follow", polarity="positive"))
        packet = _packet(records, denominator=11)
        raw = {
            "packet_id": packet["packet_id"],
            "result_id": "terra-1",
            "abstain": False,
            "candidates": [
                _candidate(packet),
                _candidate(packet, kind="coach_proposal"),
            ],
        }
        cleaned, failures = _validate(raw, packet)
        self.assertEqual(failures, [])
        self.assertEqual(len(cleaned["candidates"]), 2)
        synthesis_manifest = {
            "schema_version": "coach.synthesis.v1",
            "packets": [],
            "corpus_snapshot": packet["corpus_snapshot"],
            "corpus_snapshot_hash": packet["corpus_snapshot_hash"],
            "config_target_map": {"path": "synthesis_config_targets.json", "hash": packet["config_target_map_hash"]},
        }
        catalog, failures = build_candidate_catalog(synthesis_manifest, [cleaned])
        self.assertEqual(failures, [])
        self.assertEqual(len(catalog["candidates"]), 2)
        self.assertIn("obs-0", catalog["observation_index"])
        self.assertEqual(catalog["candidates"][0]["source_packet_ids"], [packet["packet_id"]])
        self.assertEqual(catalog["candidates"][0]["source_packet_coverage"]["processed_roots"], 11)
        invalid_target = _candidate(packet, kind="coach_proposal")
        invalid_target["target_ref"] = "cfg_unsearched"
        invalid = {
            "packet_id": packet["packet_id"],
            "result_id": "terra-unsearched-target",
            "abstain": False,
            "candidates": [invalid_target],
        }
        _, failures = _validate(invalid, packet)
        self.assertIn("proposal_target_not_in_config_search", {failure.reason for failure in failures})
        invalid_kind = _candidate(packet, kind="coach_proposal")
        invalid_kind["target_kind"] = "skill"
        invalid["result_id"] = "terra-invalid-kind"
        invalid["candidates"] = [invalid_kind]
        _, failures = _validate(invalid, packet)
        self.assertIn("proposal_target_kind_mismatch", {failure.reason for failure in failures})
        invalid_action = _candidate(packet, kind="coach_proposal")
        invalid_action["action"] = "update"
        invalid["result_id"] = "terra-invalid-action"
        invalid["candidates"] = [invalid_action]
        _, failures = _validate(invalid, packet)
        self.assertIn("proposal_config_contract_invalid", {failure.reason for failure in failures})
        review = {
            "catalog_id": catalog["catalog_id"],
            "review_id": "review-1",
            "producer": dict(catalog["review_assignment"]),
            "decisions": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "canonical_key": candidate["canonical_key"],
                    "decision": "accept",
                    "observation_ids": sorted(
                        set(candidate["supporting_observation_ids"])
                        | set(candidate["counterevidence_observation_ids"])
                    ),
                }
                for candidate in catalog["candidates"]
            ],
        }
        cleaned_review, failures = validate_second_review_result(review, catalog)
        self.assertEqual(failures, [])
        self.assertEqual({item["decision"] for item in cleaned_review["decisions"]}, {"accept"})

    def test_hard_gates_reject_intention_global_and_conflicting_rewrites(self):
        records = [_record(i, completion_proof=False) for i in range(5)]
        for record in records:
            record["evidence"][0].update({"evidence_type": "message", "role": "assistant", "fact": ""})
        records.append(_record(99, kind="instruction_follow", polarity="positive"))
        packet = _packet(records, denominator=10)
        completion = _candidate(packet)
        completion["title"] = "Verification completed for these roots."
        raw = {"packet_id": packet["packet_id"], "result_id": "terra-2", "abstain": False, "candidates": [completion]}
        cleaned, failures = _validate(raw, packet)
        self.assertIsNone(cleaned)
        self.assertIn("candidate_missing_typed_terminal_proof", {failure.reason for failure in failures})

        valid_packet = _packet([_record(i) for i in range(5)])
        global_packet = _packet([_record(i) for i in range(5)], scope="global")
        global_candidate = _candidate(global_packet, global_scope=True)
        raw["packet_id"] = global_packet["packet_id"]
        raw["candidates"] = [global_candidate]
        cleaned, failures = _validate(raw, global_packet)
        self.assertIsNone(cleaned)
        self.assertIn("global_routing_gate_failed", {failure.reason for failure in failures})

        first = _candidate(valid_packet)
        second = _candidate(valid_packet)
        second["title"] = "A conflicting rewrite."
        raw["packet_id"] = valid_packet["packet_id"]
        raw["candidates"] = [first, second]
        cleaned, failures = _validate(raw, valid_packet)
        self.assertIsNone(cleaned)
        self.assertIn("conflicting_canonical_rewrite", {failure.reason for failure in failures})

    def test_thin_card_text_is_rejected(self):
        packet = _packet([_record(number) for number in range(5)])
        candidate = _candidate(packet)
        candidate["summary"] = "Observed a miss."
        raw = {"packet_id": packet["packet_id"], "result_id": "terra-thin", "abstain": False, "candidates": [candidate]}
        cleaned, failures = _validate(raw, packet)
        self.assertIsNone(cleaned)
        self.assertIn("candidate_text_too_thin_or_unbounded", {failure.reason for failure in failures})

    def test_card_rubric_rejects_pipeline_narration_duplicate_canonicals_and_non_atomic_proposals(self):
        packet = _packet([_record(number) for number in range(10)], denominator=10)
        narrated = _candidate(packet)
        narrated["summary"] = "Across 10 of 20 roots, the evidence contains a proof arc after the verification request was recorded in the packet."
        cleaned, failures = _validate(
            {"packet_id": packet["packet_id"], "result_id": "terra-narrated", "abstain": False, "candidates": [narrated]},
            packet,
        )
        self.assertIsNone(cleaned)
        self.assertIn("candidate_text_too_thin_or_unbounded", {failure.reason for failure in failures})

        first = _candidate(packet)
        duplicate = _candidate(packet)
        duplicate["canonical"].update({"subject": "tests", "predicate": "miss"})
        cleaned, failures = _validate(
            {"packet_id": packet["packet_id"], "result_id": "terra-semantic-duplicate", "abstain": False, "candidates": [first, duplicate]},
            packet,
        )
        self.assertIsNone(cleaned)
        self.assertIn("semantically_duplicate_canonical_rewrite", {failure.reason for failure in failures})

        proposal = _candidate(packet, kind="coach_proposal")
        proposal["instruction_text"] = "Require an explicit verification result before marking a task complete and archive the prior instruction."
        cleaned, failures = _validate(
            {"packet_id": packet["packet_id"], "result_id": "terra-non-atomic", "abstain": False, "candidates": [proposal]},
            packet,
        )
        self.assertIsNone(cleaned)
        self.assertIn("candidate_text_too_thin_or_unbounded", {failure.reason for failure in failures})

    def test_empty_counterevidence_is_explicit_and_assistant_completion_is_not_proof(self):
        records = [_record(i) for i in range(5)]
        packet = _packet(records, denominator=10)
        candidate = _candidate(packet)
        self.assertEqual(candidate["counterevidence_observation_ids"], [])
        raw = {"packet_id": packet["packet_id"], "result_id": "terra-empty-counter", "abstain": False, "candidates": [candidate]}
        cleaned, failures = _validate(raw, packet)
        self.assertEqual(failures, [])
        self.assertEqual(cleaned["candidates"][0]["counterevidence_observation_ids"], [])

        assistant_records = [_record(i) for i in range(5)]
        for record in assistant_records:
            record["evidence"][0].update({"evidence_type": "message", "role": "assistant", "fact": ""})
        assistant_packet = _packet(assistant_records)
        assistant_candidate = _candidate(assistant_packet)
        assistant_candidate["title"] = "Verification completed for these roots."
        raw = {
            "packet_id": assistant_packet["packet_id"],
            "result_id": "terra-assistant-only",
            "abstain": False,
            "candidates": [assistant_candidate],
        }
        cleaned, failures = _validate(raw, assistant_packet)
        self.assertIsNone(cleaned)
        self.assertIn("candidate_missing_typed_terminal_proof", {failure.reason for failure in failures})

    def test_successful_tool_result_can_prove_completion_but_failed_tool_cannot(self):
        record = _record(1, kind="instruction_follow", polarity="positive")
        record["evidence"][0].update(
            {
                "evidence_type": "tool",
                "role": "",
                "fact": json.dumps({"tool_event_id": "tool-1", "tool_name": "pytest", "action": "end", "success": True, "operation_kind": "verification"}),
            }
        )
        packet = next(
            item for item in build_synthesis_packets([record], _manifest())
            if item["group"]["polarity"] == "positive" and item["group"]["scope"] == "harness_codex"
        )
        candidate = _candidate(packet, kind="observed_instance")
        candidate["title"] = "Verification completed for this root."
        raw = {"packet_id": packet["packet_id"], "result_id": "terra-tool-success", "abstain": False, "candidates": [candidate]}
        cleaned, failures = _validate(raw, packet)
        self.assertEqual(failures, [])
        self.assertEqual(cleaned["candidates"][0]["n"], 1)

        record["evidence"][0]["fact"] = json.dumps({"tool_event_id": "tool-1", "tool_name": "pytest", "action": "end", "success": False, "operation_kind": "verification"})
        failed_packet = next(
            item for item in build_synthesis_packets([record], _manifest())
            if item["group"]["polarity"] == "positive" and item["group"]["scope"] == "harness_codex"
        )
        failed_candidate = _candidate(failed_packet, kind="observed_instance")
        failed_candidate["title"] = "Verification completed for this root."
        failed_raw = {"packet_id": failed_packet["packet_id"], "result_id": "terra-tool-failed", "abstain": False, "candidates": [failed_candidate]}
        cleaned, failures = _validate(failed_raw, failed_packet)
        self.assertIsNone(cleaned)
        self.assertIn("candidate_missing_typed_terminal_proof", {failure.reason for failure in failures})

    def test_same_window_verification_category_is_bounded_and_private_miss_is_not_linked(self):
        generic = _record(7, kind="instruction_follow", polarity="positive")
        generic["evidence"][0].update(
            {"evidence_type": "tool", "role": "", "fact": json.dumps({"tool_event_id": "tool-7", "tool_name": "exec_command", "action": "end", "success": True, "operation_kind": "verification"})}
        )
        generic_packet = next(
            packet
            for packet in build_synthesis_packets([generic], _manifest())
            if packet["group"]["polarity"] == "positive" and packet["group"]["scope"] == "harness_codex"
        )
        generic_candidate = _candidate(generic_packet, kind="observed_instance")
        generic_candidate["title"] = "A verification category result completed in one root."
        generic_candidate["does_not_prove"] = "This terminal category result does not prove the exact target or suite that ran, nor whether a later session preserved the outcome."
        cleaned, failures = _validate(
            {"packet_id": generic_packet["packet_id"], "result_id": "terra-generic-category", "abstain": False, "candidates": [generic_candidate]},
            generic_packet,
        )
        self.assertEqual(failures, [])
        self.assertEqual(cleaned["candidates"][0]["kind"], "observed_instance")

        private_records = [_record(number, assertion="private_deployment_instruction") for number in range(5)]
        for record in private_records:
            record["evidence"][0]["fact"] = json.dumps({"tool_event_id": record["observation_id"], "tool_name": "pytest", "action": "end", "success": False, "operation_kind": "verification"})
            record["evidence"][1]["quote"] = "Follow the private deployment instruction."
        private_packet = _packet(private_records)
        private_candidate = _candidate(private_packet)
        private_candidate["canonical"].update({"subject": "private_deployment", "predicate": "instruction_miss"})
        cleaned, failures = _validate(
            {"packet_id": private_packet["packet_id"], "result_id": "terra-private-unrelated-tool", "abstain": False, "candidates": [private_candidate]},
            private_packet,
        )
        self.assertIsNone(cleaned)
        self.assertIn("candidate_missing_typed_terminal_proof", {failure.reason for failure in failures})

        deployment_check = _record(8)
        deployment_check["evidence"][1]["quote"] = "Check the deployment configuration."
        self.assertFalse(has_bounded_gap_evidence(deployment_check))

    def test_sentence_punctuation_and_compound_request_require_all_operation_categories(self):
        compound = _record(8, kind="instruction_follow", polarity="positive")
        compound["evidence"][0].update(
            {"evidence_type": "tool", "role": "", "fact": json.dumps({"tool_event_id": "verify-8", "tool_name": "exec_command", "action": "end", "success": True, "operation_kind": "verification"})}
        )
        compound["evidence"][1]["quote"] = "Run tests and apply patch."
        self.assertFalse(has_completion_evidence(compound))
        compound["evidence"].append(
            {"ref": "patch-8", "evidence_type": "tool", "window_id": "w-8", "message_id": "m-8", "role": "", "seq": 3, "quote": "", "fact": json.dumps({"tool_event_id": "patch-8", "tool_name": "apply_patch", "action": "end", "success": True, "operation_kind": "artifact_write"})}
        )
        compound["proof_arcs"][2]["evidence_refs"].append("patch-8")
        self.assertTrue(has_completion_evidence(compound))

        owner_follow = {
            "kind": "instruction_follow",
            "evidence": [
                {"ref": "request", "evidence_type": "message", "role": "user", "window_id": "w-1", "timestamp": "2026-01-01T00:00:00Z", "quote": "Fix login and run tests."},
                {"ref": "outcome", "evidence_type": "message", "role": "user", "window_id": "w-2", "timestamp": "2026-01-01T00:01:00Z", "quote": "Login tests passed."},
            ],
            "proof_arcs": [{"arc": "request", "evidence_refs": ["request"]}, {"arc": "outcome", "evidence_refs": ["outcome"]}],
        }
        self.assertFalse(has_completion_evidence(owner_follow))
        owner_follow["evidence"][1]["quote"] = "The login fix completed and tests passed."
        self.assertTrue(has_completion_evidence(owner_follow))

        owner_gap = {
            "kind": "delivery_gap",
            "evidence": [
                {"ref": "expectation", "evidence_type": "message", "role": "user", "window_id": "w-1", "timestamp": "2026-01-01T00:00:00Z", "quote": "Fix login and run tests."},
                {"ref": "gap", "evidence_type": "message", "role": "user", "window_id": "w-2", "timestamp": "2026-01-01T00:01:00Z", "quote": "Login tests failed."},
            ],
            "proof_arcs": [{"arc": "expectation", "evidence_refs": ["expectation"]}, {"arc": "delivery", "evidence_refs": ["gap"]}],
        }
        self.assertFalse(has_bounded_gap_evidence(owner_gap))
        owner_gap["evidence"][1]["quote"] = "The login fix failed and tests failed."
        self.assertTrue(has_bounded_gap_evidence(owner_gap))

    def test_equal_utc_cross_session_owner_outcome_is_not_causal(self):
        observation = {
            "kind": "instruction_follow",
            "evidence": [
                {"ref": "request", "evidence_type": "message", "role": "user", "session_id": "sol", "window_id": "w-1", "seq": 5, "timestamp": "2026-01-01T00:00:00Z", "quote": "Fix login and run tests."},
                {"ref": "outcome", "evidence_type": "message", "role": "user", "session_id": "grok", "window_id": "w-2", "seq": 6, "timestamp": "2026-01-01T00:00:00Z", "quote": "The login fix completed and tests passed."},
            ],
            "proof_arcs": [{"arc": "request", "evidence_refs": ["request"]}, {"arc": "outcome", "evidence_refs": ["outcome"]}],
        }
        self.assertFalse(has_completion_evidence(observation))
        observation["evidence"][1]["session_id"] = "sol"
        self.assertTrue(has_completion_evidence(observation))

    def test_skill_action_requires_matching_skill_name_window_and_message(self):
        observation = {
            "kind": "skill_use",
            "evidence": [
                {"ref": "request", "evidence_type": "message", "role": "user", "window_id": "w", "quote": "Use the verification skill."},
                {"ref": "skill", "evidence_type": "skill", "window_id": "w", "message_id": "assistant", "fact": json.dumps({"message_id": "assistant", "skill_name": "verification", "exposure_type": "loaded"})},
                {"ref": "action", "evidence_type": "tool", "window_id": "w", "message_id": "assistant", "fact": json.dumps({"message_id": "assistant", "tool_name": "pytest", "action": "end", "success": True, "operation_kind": "verification"})},
            ],
            "proof_arcs": [
                {"arc": "skill_request", "evidence_refs": ["request"]},
                {"arc": "skill_evidence", "evidence_refs": ["skill"]},
                {"arc": "skill_action", "evidence_refs": ["action"]},
            ],
        }
        self.assertTrue(_observation_terminal_proof(observation))
        wrong_skill = json.loads(json.dumps(observation))
        wrong_skill["evidence"][1]["fact"] = json.dumps({"message_id": "assistant", "skill_name": "deployment", "exposure_type": "loaded"})
        self.assertFalse(_observation_terminal_proof(wrong_skill))
        cross_window = json.loads(json.dumps(observation))
        cross_window["evidence"][2]["window_id"] = "other"
        self.assertFalse(_observation_terminal_proof(cross_window))
        unrelated_action = json.loads(json.dumps(observation))
        unrelated_action["evidence"][2]["fact"] = json.dumps({"message_id": "assistant", "tool_name": "apply_patch", "action": "end", "success": True, "operation_kind": "artifact_write"})
        self.assertFalse(_observation_terminal_proof(unrelated_action))

    def test_same_theme_counterevidence_partial_proposals_and_sampled_global_routing(self):
        negative = [_record(number) for number in range(5)]
        positive = _record(99, kind="instruction_follow", polarity="positive")
        positive["evidence"][0].update(
            {"evidence_type": "tool", "role": "", "fact": json.dumps({"tool_event_id": "tool-positive", "tool_name": "pytest", "action": "end", "success": True, "operation_kind": "verification"})}
        )
        packet = _packet([*negative, positive])
        one_sided = _candidate(packet)
        one_sided["counterevidence_observation_ids"] = []
        cleaned, failures = _validate(
            {"packet_id": packet["packet_id"], "result_id": "terra-one-sided", "abstain": False, "candidates": [one_sided]},
            packet,
        )
        self.assertIsNone(cleaned)
        self.assertIn("same_theme_counterevidence_omitted", {failure.reason for failure in failures})

        partial_packet = next(
            item for item in build_synthesis_packets(
                [_record(number) for number in range(10)],
                _manifest(),
                processing_coverage={"processed_packets": 1, "processed_windows": 10, "processed_roots": 10, "abstained_packets": 0, "valid_observations": 10},
            ) if item["group"]["polarity"] == "negative" and item["group"]["scope"] == "harness_codex"
        )
        partial_proposal = _candidate(partial_packet, kind="coach_proposal")
        cleaned, failures = _validate(
            {"packet_id": partial_packet["packet_id"], "result_id": "terra-partial-proposal", "abstain": False, "candidates": [partial_proposal]},
            partial_packet,
        )
        self.assertIsNone(cleaned)
        self.assertIn("proposal_requires_complete_processing", {failure.reason for failure in failures})

        sampled_records = [_record(number, harness="codex" if number < 13 else "claude", repo="repo-a" if number < 13 else "repo-b") for number in range(25)]
        sampled_records.extend(_record(number + 100, kind="instruction_follow", polarity="positive", harness="codex" if number < 5 else "claude", repo="repo-a" if number < 5 else "repo-b") for number in range(9))
        for record in sampled_records[25:]:
            record["evidence"][0].update({"evidence_type": "tool", "role": "", "fact": json.dumps({"tool_event_id": record["observation_id"], "tool_name": "pytest", "action": "end", "success": True, "operation_kind": "verification"})})
        sampled_packet = _packet(sampled_records, denominator=34, scope="global")
        self.assertTrue(sampled_packet["sampling"]["supporting_roots_truncated"])
        self.assertEqual(len(sampled_packet["group_membership"]["supporting_root_session_ids"]), 25)
        sampled_candidate = _candidate(sampled_packet, global_scope=True)
        sampled_candidate["summary"] = "Across 24 cited supporting roots of 25 supporting roots, 25 of 34 reviewed roots requested verification that missed an explicit terminal result after the check was required."
        cleaned, failures = _validate(
            {"packet_id": sampled_packet["packet_id"], "result_id": "terra-truncated-pattern", "abstain": False, "candidates": [sampled_candidate]},
            sampled_packet,
        )
        self.assertEqual(failures, [])
        self.assertEqual(cleaned["candidates"][0]["n"], 25)

    def test_scoped_packets_bind_uncited_full_population_metadata(self):
        manifest = _manifest(25)
        manifest["coverage"]["scope_denominators"]["model_sol"] = {
            "eligible_roots": 25,
            "eligible_windows": 25,
        }
        records = [_record(number, response_model="sol") for number in range(25)]
        packets = {
            packet["group"]["scope"]: packet
            for packet in build_synthesis_packets(records, manifest)
            if packet["group"]["polarity"] == "negative"
        }
        for scope, field, replacement, failure in (
            ("model_sol", "response_model", "grok", "model_scope_full_population_mismatch"),
            ("harness_codex", "harness", "claude", "scope_full_population_mismatch"),
            ("repo_demo", "repo", "other", "scope_full_population_mismatch"),
        ):
            packet = packets[scope]
            self.assertEqual(packet["full_population"]["supporting"]["root_count"], 25)
            self.assertEqual(len(packet["supporting_observations"]), 24)
            candidate = _candidate(packet)
            candidate["canonical"]["scope"] = scope
            cleaned, failures = _validate(
                {"packet_id": packet["packet_id"], "result_id": f"terra-{scope}", "abstain": False, "candidates": [candidate]},
                packet,
            )
            self.assertEqual(failures, [])
            self.assertEqual(cleaned["candidates"][0]["n"], 25)

            tampered = json.loads(json.dumps(packet))
            cited = {item["observation_id"] for item in tampered["supporting_observations"]}
            omitted = next(
                observation_id
                for observation_id in tampered["full_population"]["supporting"]["observation_ids"]
                if observation_id not in cited
            )
            tampered["full_population"]["supporting"]["scope_distribution"]["observation_bindings"][omitted][field] = replacement
            _refresh_scope_population(tampered)
            forged_candidate = _candidate(tampered)
            forged_candidate["canonical"]["scope"] = scope
            cleaned, failures = _validate(
                {"packet_id": tampered["packet_id"], "result_id": f"terra-forged-{scope}", "abstain": False, "candidates": [forged_candidate]},
                tampered,
            )
            self.assertIsNone(cleaned)
            self.assertIn(failure, {item.reason for item in failures})

    def test_global_gate_accepts_fifteen_roots_only_when_diverse(self):
        records = [
            _record(
                number,
                harness="codex" if number < 8 else "claude",
                repo="repo-a" if number < 8 else "repo-b",
            )
            for number in range(15)
        ]
        records.append(_record(99, kind="instruction_follow", polarity="positive"))
        packet = _packet(records, scope="global")
        candidate = _candidate(packet, global_scope=True)
        raw = {"packet_id": packet["packet_id"], "result_id": "terra-global", "abstain": False, "candidates": [candidate]}
        cleaned, failures = _validate(raw, packet)
        self.assertEqual(failures, [])
        self.assertEqual(cleaned["candidates"][0]["n"], 15)

    def test_unknown_harness_cannot_supply_global_diversity(self):
        records = [
            _record(
                number,
                harness="codex" if number < 8 else "(unknown)",
                repo="repo-a" if number < 8 else "repo-b",
            )
            for number in range(15)
        ]
        packet = _packet(records, scope="global")
        candidate = _candidate(packet, global_scope=True)
        raw = {"packet_id": packet["packet_id"], "result_id": "terra-unknown-diversity", "abstain": False, "candidates": [candidate]}
        cleaned, failures = _validate(raw, packet)
        self.assertIsNone(cleaned)
        self.assertIn("global_routing_gate_failed", {failure.reason for failure in failures})

        diluted = [
            _record(
                number,
                harness="codex" if number < 8 else "claude" if number == 8 else "(unknown)",
                repo="repo-a" if number < 8 else "repo-b" if number == 8 else "(unknown)",
            )
            for number in range(15)
        ]
        diluted_packet = _packet(diluted, scope="global")
        diluted_candidate = _candidate(diluted_packet, global_scope=True)
        diluted_raw = {"packet_id": diluted_packet["packet_id"], "result_id": "terra-diluted-global", "abstain": False, "candidates": [diluted_candidate]}
        cleaned, failures = _validate(diluted_raw, diluted_packet)
        self.assertIsNone(cleaned)
        self.assertIn("global_routing_gate_failed", {failure.reason for failure in failures})

    def test_corpus_scope_alias_normalizes_to_global_and_cannot_bypass_gate(self):
        records = [_record(number) for number in range(10)]
        packet = _packet(records, scope="global")
        candidate = _candidate(packet)
        candidate["canonical"]["scope"] = "global_corpus"
        raw = {"packet_id": packet["packet_id"], "result_id": "terra-corpus-alias", "abstain": False, "candidates": [candidate]}
        cleaned, failures = _validate(raw, packet)
        self.assertIsNone(cleaned)
        self.assertIn("global_routing_gate_failed", {failure.reason for failure in failures})

    def test_proposal_miss_proof_arcs_must_cover_three_roots(self):
        records = [_record(number) for number in range(10)]
        for number in ("extra-a", "extra-b"):
            duplicate_root = _record(number)
            duplicate_root["root_session_id"] = "root-0"
            records.append(duplicate_root)
        packet = _packet(records, denominator=10)
        candidate = _candidate(packet, kind="coach_proposal")
        raw = {"packet_id": packet["packet_id"], "result_id": "terra-miss-independence", "abstain": False, "candidates": [candidate]}
        cleaned, failures = _validate(raw, packet)
        self.assertIsNone(cleaned)
        self.assertIn("proposal_miss_proof_arcs_not_independent", {failure.reason for failure in failures})

    def test_proposal_miss_proof_arcs_must_be_candidate_local_support(self):
        packet = _packet([_record(number) for number in range(12)], denominator=12)
        candidate = _candidate(packet, kind="coach_proposal")
        candidate["supporting_observation_ids"] = candidate["supporting_observation_ids"][:10]
        candidate["cited_supporting_roots"] = 10
        candidate["summary"] = "Across 10 cited supporting roots of 12 supporting roots, 12 of 12 reviewed roots requested verification that missed an explicit terminal result after the check was required."
        outside_support = next(
            observation_id
            for observation_id in packet["full_population"]["supporting"]["observation_ids"]
            if observation_id not in candidate["supporting_observation_ids"]
        )
        candidate["miss_proof_arcs"] = [
            {"observation_id": "obs-0", "arc": "gap"},
            {"observation_id": "obs-1", "arc": "gap"},
            {"observation_id": outside_support, "arc": "gap"},
        ]
        cleaned, failures = _validate(
            {"packet_id": packet["packet_id"], "result_id": "terra-nonlocal-miss-proof", "abstain": False, "candidates": [candidate]},
            packet,
        )
        self.assertIsNone(cleaned)
        self.assertIn("unverified_miss_proof_arc", {failure.reason for failure in failures})

    def test_catalog_merges_cross_polarity_observation_packet_memberships(self):
        negative = [_record(number) for number in range(5)]
        positive = [_record(number + 10, kind="instruction_follow", polarity="positive") for number in range(5)]
        for record in positive:
            record["evidence"][0].update(
                {
                    "evidence_type": "tool",
                    "role": "",
                    "fact": json.dumps(
                        {"tool_event_id": record["observation_id"], "tool_name": "pytest", "action": "end", "success": True, "operation_kind": "verification"}
                    ),
                }
            )
        packets = build_synthesis_packets([*negative, *positive], _manifest())
        negative_packet = next(
            packet for packet in packets
            if packet["group"]["polarity"] == "negative" and packet["group"]["scope"] == "harness_codex"
        )
        positive_packet = next(
            packet for packet in packets
            if packet["group"]["polarity"] == "positive" and packet["group"]["scope"] == "harness_codex"
        )
        negative_candidate = _candidate(negative_packet)
        positive_candidate = _candidate(positive_packet)
        positive_candidate["canonical"].update({"predicate": "instruction_follow", "polarity": "positive"})
        negative_result, failures = _validate(
            {"packet_id": negative_packet["packet_id"], "result_id": "negative-result", "abstain": False, "candidates": [negative_candidate]},
            negative_packet,
        )
        self.assertEqual(failures, [])
        positive_result, failures = _validate(
            {"packet_id": positive_packet["packet_id"], "result_id": "positive-result", "abstain": False, "candidates": [positive_candidate]},
            positive_packet,
        )
        self.assertEqual(failures, [])
        manifest = {
            "schema_version": "coach.synthesis.v1",
            "packets": [],
            "corpus_snapshot": negative_packet["corpus_snapshot"],
            "corpus_snapshot_hash": negative_packet["corpus_snapshot_hash"],
            "config_target_map": {"path": "synthesis_config_targets.json", "hash": negative_packet["config_target_map_hash"]},
        }
        catalog, failures = build_candidate_catalog(manifest, [negative_result, positive_result])
        self.assertEqual(failures, [])
        self.assertEqual(
            set(catalog["observation_index"]["obs-0"]["source_synthesis_packet_ids"]),
            {negative_packet["packet_id"], positive_packet["packet_id"]},
        )

    def test_process_fact_accepts_terminal_artifact_tool_from_preprocess(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute(
            "INSERT INTO sessions(id,harness,external_id,repo,agent_profile) VALUES(?,?,?,?,?)",
            ("root", "codex", "root", "demo", "codex"),
        )
        conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            ("user", "root", 1, "user", "Apply the configuration patch", "user-hash"),
        )
        conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            ("assistant", "root", 2, "assistant", "Applied the configuration patch.", "assistant-hash"),
        )
        conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
            ("window", "root", "user", "assistant", "input-hash", "window-hash"),
        )
        conn.execute(
            "INSERT INTO tool_events(id,session_id,message_id,seq,tool_name,action,success,operation_kind) VALUES(?,?,?,?,?,?,?,?)",
            ("patch-tool", "root", "assistant", 3, "apply_patch", "end", 1, "artifact_write"),
        )
        conn.execute(
            "INSERT INTO tool_events(id,session_id,message_id,seq,tool_name,action,success,operation_kind) VALUES(?,?,?,?,?,?,?,?)",
            ("read-tool", "root", "assistant", 4, "cat", "end", 1, "read_only"),
        )
        conn.commit()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = emit_coach_packets(conn, root)
            preprocess_packet = json.loads((root / manifest["packets"][0]["path"]).read_text())
            window = preprocess_packet["windows"][0]
            assistant = window["messages"][1]
            tool = next(tool for tool in window["tool_timeline"] if tool["tool_event_id"] == "patch-tool")
            read_tool = next(tool for tool in window["tool_timeline"] if tool["tool_event_id"] == "read-tool")
            raw_luna = {
                "packet_id": preprocess_packet["packet_id"],
                "result_id": "luna-process-fact",
                "producer": dict(preprocess_packet["producer_contract"]["expected"]),
                "abstain": False,
                "observations": [{
                    "kind": "process_fact",
                    "assertion_key": "configuration_patch_applied",
                    "confidence": 0.8,
                    "does_not_prove": "This one terminal tool record does not establish that the configuration was later reviewed or retained.",
                    "evidence": [
                        {"window_id": window["window_id"], "message_id": assistant["message_id"], "role": "assistant", "seq": assistant["seq"], "quote": "Applied the configuration patch."},
                        {"window_id": window["window_id"], "tool_event_id": "patch-tool", "fact": tool["fact"]},
                    ],
                    "proof_arcs": [
                        {"arc": "action", "evidence_refs": [f"{window['window_id']}:tool:patch-tool"]},
                        {"arc": "artifact", "evidence_refs": [f"{window['window_id']}:tool:patch-tool"]},
                    ],
                }],
            }
            laundered = json.loads(json.dumps(raw_luna))
            laundered["result_id"] = "luna-process-laundered"
            laundered["observations"][0]["evidence"][1] = {
                "window_id": window["window_id"], "tool_event_id": "read-tool", "fact": read_tool["fact"],
            }
            laundered["observations"][0]["proof_arcs"][0]["evidence_refs"] = [f"{window['window_id']}:tool:read-tool"]
            cleaned_laundered, laundering_failures = validate_coach_result(laundered, preprocess_packet)
            self.assertIsNone(cleaned_laundered)
            self.assertIn(
                "process_fact_requires_shared_successful_artifact",
                {failure.reason for failure in laundering_failures},
            )
            result_dir = root / "results"
            result_dir.mkdir()
            (result_dir / "process.json").write_text(
                json.dumps(_legacy_result_with_dispositions(raw_luna, preprocess_packet))
            )
            records, failures = load_validated_observation_records(root)
            self.assertEqual(failures, [])
            synthesis_packet = next(
                packet
                for packet in build_synthesis_packets(records, manifest)
                if packet["group"]["polarity"] == "positive" and packet["group"]["scope"] == "harness_codex"
            )
            candidate = _candidate(synthesis_packet, kind="observed_instance")
            candidate["canonical"].update({"subject": "configuration", "predicate": "process_fact", "polarity": "positive"})
            candidate["title"] = "A configuration patch had a terminal artifact result."
            candidate["summary"] = "Across 1 of 1 reviewed roots, apply_patch returned a terminal result after the configuration patch was requested."
            candidate["does_not_prove"] = "This single root does not establish that the changed configuration remained correct in later sessions or repositories."
            cleaned, failures = _validate(
                {"packet_id": synthesis_packet["packet_id"], "result_id": "terra-process-fact", "abstain": False, "candidates": [candidate]},
                synthesis_packet,
            )
        self.assertEqual(failures, [])
        self.assertEqual(cleaned["candidates"][0]["kind"], "observed_instance")
        conn.close()

    def test_shared_redactor_masks_generic_schemes_and_scp_locators(self):
        report = RedactionReport()
        raw = "file:///Users/alice/secret.txt ftp://private.example.test/a custom+ssh://host/private git@github.com:private/repo.git"
        redacted = redact_text(raw, report)
        self.assertNotIn("file:///Users/alice/secret.txt", redacted)
        self.assertNotIn("ftp://private.example.test/a", redacted)
        self.assertNotIn("custom+ssh://host/private", redacted)
        self.assertNotIn("git@github.com:private/repo.git", redacted)
        self.assertGreaterEqual(report.counts.get("url", 0), 4)

    def test_owner_adjudication_requires_target_link_and_stable_timestamp_ties(self):
        unrelated_confirmation = {
            "kind": "instruction_follow",
            "evidence": [
                {"ref": "request", "evidence_type": "message", "role": "user", "window_id": "w-1", "timestamp": "2026-01-01T00:00:00Z", "quote": "Verify the migration checksum."},
                {"ref": "outcome", "evidence_type": "message", "role": "user", "window_id": "w-2", "timestamp": "2026-01-01T00:01:00Z", "quote": "Yes, the unrelated editor works."},
            ],
            "proof_arcs": [{"arc": "request", "evidence_refs": ["request"]}, {"arc": "outcome", "evidence_refs": ["outcome"]}],
        }
        unrelated_correction = {
            "kind": "delivery_gap",
            "evidence": [
                {"ref": "expectation", "evidence_type": "message", "role": "user", "window_id": "w-1", "timestamp": "2026-01-01T00:00:00Z", "quote": "Deliver the migration report."},
                {"ref": "gap", "evidence_type": "message", "role": "user", "window_id": "w-2", "timestamp": "2026-01-01T00:01:00Z", "quote": "No, the unrelated editor is still broken."},
            ],
            "proof_arcs": [{"arc": "expectation", "evidence_refs": ["expectation"]}, {"arc": "delivery", "evidence_refs": ["gap"]}],
        }
        equal_timestamp = {
            "kind": "instruction_follow",
            "evidence": [
                {"ref": "request", "evidence_type": "message", "role": "user", "window_id": "w-z", "timestamp": "2026-01-01T00:00:00Z", "quote": "Verify the migration checksum."},
                {"ref": "outcome", "evidence_type": "message", "role": "user", "window_id": "w-a", "timestamp": "2026-01-01T00:00:00Z", "quote": "Yes, the migration checksum passed."},
            ],
            "proof_arcs": [{"arc": "request", "evidence_refs": ["request"]}, {"arc": "outcome", "evidence_refs": ["outcome"]}],
        }
        shared_generic_word = {
            "kind": "instruction_follow",
            "evidence": [
                {"ref": "request", "evidence_type": "message", "role": "user", "window_id": "w-1", "timestamp": "2026-01-01T00:00:00Z", "quote": "Run the login tests."},
                {"ref": "outcome", "evidence_type": "message", "role": "user", "window_id": "w-2", "timestamp": "2026-01-01T00:01:00Z", "quote": "Yes, the dashboard tests passed."},
            ],
            "proof_arcs": [{"arc": "request", "evidence_refs": ["request"]}, {"arc": "outcome", "evidence_refs": ["outcome"]}],
        }
        self.assertFalse(has_completion_evidence(unrelated_confirmation))
        self.assertFalse(has_bounded_gap_evidence(unrelated_correction))
        self.assertFalse(has_completion_evidence(equal_timestamp))
        self.assertFalse(has_completion_evidence(shared_generic_word))

    def test_preprocess_packet_substitution_is_rejected_against_manifest_hash_and_membership(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute(
            "INSERT INTO sessions(id,harness,external_id,repo,agent_profile) VALUES(?,?,?,?,?)",
            ("root", "codex", "root", "demo", "codex"),
        )
        for message_id, seq, role, text in (("user", 1, "user", "Please check"), ("assistant", 2, "assistant", "I will check")):
            conn.execute(
                "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
                (message_id, "root", seq, role, text, message_id),
            )
        conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
            ("window", "root", "user", "assistant", "input", "content"),
        )
        conn.commit()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = emit_coach_packets(conn, root)
            entry = manifest["packets"][0]
            path = root / entry["path"]
            substituted = json.loads(path.read_text())
            substituted["root_session_ids"] = ["substituted-root"]
            substituted.pop("packet_hash")
            substituted["packet_hash"] = hashlib.sha256(
                json.dumps(substituted, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()[:24]
            path.write_text(json.dumps(substituted))
            results = root / "results"
            results.mkdir()
            (results / "abstain.json").write_text(json.dumps({
                "packet_id": entry["packet_id"],
                "result_id": "tampered-packet-result",
                "producer": dict(substituted["producer_contract"]["expected"]),
                "abstain": True,
                "abstain_reason": "No bounded observation.",
            }))
            _, failures = load_validated_observation_records(root)
        self.assertIn("result_packet_not_in_manifest", {failure.reason for failure in failures})
        conn.close()

    def test_cross_chunk_root_request_context_is_locality_bound_and_recorded(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute(
            "INSERT INTO sessions(id,harness,external_id,repo,agent_profile) VALUES(?,?,?,?,?)",
            ("root", "codex", "root", "demo", "codex"),
        )
        for number in range(25):
            request_id, response_id = f"request-{number}", f"response-{number}"
            request = (
                "Verify the login migration checksum."
                if number == 0
                else "Verify the login migration checksum again."
                if number == 24
                else f"Handle unrelated task {number}."
            )
            timestamp = f"2026-01-01T00:00:{number:02d}Z"
            conn.execute(
                "INSERT INTO messages(id,session_id,seq,role,text,content_hash,timestamp) VALUES(?,?,?,?,?,?,?)",
                (request_id, "root", number * 2 + 1, "user", request, request_id, timestamp),
            )
            conn.execute(
                "INSERT INTO messages(id,session_id,seq,role,text,content_hash,model_canonical,effort,timestamp) VALUES(?,?,?,?,?,?,?,?,?)",
                (response_id, "root", number * 2 + 2, "assistant", "Acknowledged.", response_id, "grok" if number == 24 else "sol", "high", timestamp),
            )
            conn.execute(
                "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
                (f"window-{number}", "root", request_id, response_id, request_id, response_id),
            )
        conn.commit()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = emit_coach_packets(conn, root)
            self.assertEqual(len(manifest["packets"]), 2)
            first_packet = json.loads((root / manifest["packets"][0]["path"]).read_text())
            second_packet = json.loads((root / manifest["packets"][1]["path"]).read_text())
            self.assertEqual(len(second_packet["root_request_index"]["root"]), 25)
            self.assertTrue(second_packet["root_request_index"]["root"][0]["request"]["source_text"].startswith("Verify"))
            local_window = second_packet["windows"][0]
            local_request = local_window["request"]
            context_request = second_packet["root_request_index"]["root"][0]["request"]
            result = {
                "packet_id": second_packet["packet_id"], "result_id": "late-repeat",
                "producer": dict(second_packet["producer_contract"]["expected"]), "abstain": False,
                "observations": [{
                    "kind": "repeated_ask", "assertion_key": "login_migration_verification_repeated",
                    "confidence": 0.8,
                    "does_not_prove": "The repeated request does not establish why the earlier attempt was incomplete.",
                    "evidence": [
                        {"window_id": "window-0", "message_id": context_request["message_id"], "role": "user", "seq": context_request["seq"], "quote": "Verify the login migration checksum."},
                        {"window_id": local_window["window_id"], "message_id": local_request["message_id"], "role": "user", "seq": local_request["seq"], "quote": "Verify the login migration checksum again."},
                    ],
                    "proof_arcs": [
                        {"arc": "request_1", "evidence_refs": ["window-0:request-0"]},
                        {"arc": "request_2", "evidence_refs": [f"{local_window['window_id']}:{local_request['message_id']}"]},
                    ],
                }],
            }
            cleaned, failures = validate_coach_result(result, second_packet)
            self.assertEqual(failures, [])
            self.assertTrue(cleaned["observations"][0]["evidence"][0]["context_only"])
            results_dir = root / "results"
            results_dir.mkdir()
            (results_dir / "first.json").write_text(json.dumps({
                "packet_id": first_packet["packet_id"], "result_id": "first-abstain",
                "producer": dict(first_packet["producer_contract"]["expected"]),
                "abstain": True, "abstain_reason": "No bounded observation.",
                "window_dispositions": [
                    {"window_id": window["window_id"], "observation_ids": [], "no_supported_observation": True}
                    for window in first_packet["windows"]
                ],
            }))
            (results_dir / "second.json").write_text(
                json.dumps(_legacy_result_with_dispositions(result, second_packet))
            )
            records, record_failures = load_validated_observation_records(root)
            self.assertEqual(record_failures, [])
            self.assertEqual(len(records), 1)
            self.assertTrue(records[0]["evidence"][0]["context_only"])
            self.assertEqual(records[0]["luna_producer"], second_packet["producer_contract"]["expected"])
            self.assertEqual(records[0]["model_attribution"]["response_model"], "sol")
            all_context = json.loads(json.dumps(result))
            all_context["result_id"] = "all-context-repeat"
            all_context["observations"][0]["evidence"][1] = {
                "window_id": "window-1", "message_id": "request-1", "role": "user", "seq": 3,
                "quote": "Handle unrelated task 1.",
            }
            all_context["observations"][0]["proof_arcs"][1]["evidence_refs"] = ["window-1:request-1"]
            cleaned, failures = validate_coach_result(all_context, second_packet)
            self.assertIsNone(cleaned)
            self.assertIn("repeated_ask_requires_local_second_request", {failure.reason for failure in failures})
        conn.close()

    def test_terminal_model_attribution_uses_cited_assistant_and_plumbing_tool_call(self):
        first_window = {
            "window_id": "first", "session_id": "root", "root_session_id": "root",
            "harness": "codex", "repo": "demo", "response_seq": 2,
            "response": {"message_id": "sol-call", "role": "assistant", "seq": 2, "model_canonical": "sol", "effort": "low"},
            "messages": [
                {"message_id": "request", "role": "user", "seq": 1, "source_text": "Verify the login migration."},
                {"message_id": "sol-call", "role": "assistant", "seq": 2, "source_text": "I will run the check.", "model_canonical": "sol", "effort": "low"},
                {"message_id": "grok-proof", "role": "assistant", "seq": 4, "source_text": "The migration verification passed.", "model_canonical": "grok", "effort": "high"},
            ],
            "artifact": {},
        }
        correction_window = {
            "window_id": "correction", "session_id": "root", "root_session_id": "root",
            "harness": "codex", "repo": "demo", "response_seq": 6,
            "response": {"message_id": "luna-reply", "role": "assistant", "seq": 6, "model_canonical": "luna", "effort": "medium"},
            "messages": [
                {"message_id": "owner-correction", "role": "user", "seq": 5, "source_text": "The login migration is still missing."},
                {"message_id": "luna-reply", "role": "assistant", "seq": 6, "source_text": "I will investigate.", "model_canonical": "luna", "effort": "medium"},
            ],
            "artifact": {},
        }
        packet = {"packet_id": "model-attribution", "redaction": {}, "windows": [first_window, correction_window]}

        assistant_observation = {
            "kind": "instruction_follow", "assertion_key": "login_migration_verification",
            "does_not_prove": "The response does not establish later durability.",
            "evidence": [{"ref": "response", "evidence_type": "message", "window_id": "first", "message_id": "grok-proof", "role": "assistant", "seq": 4, "quote": "verification passed"}],
            "proof_arcs": [{"arc": "response", "evidence_refs": ["response"]}],
        }
        record, failure = _record_from_observation(assistant_observation, packet, "assistant-proof")
        self.assertIsNone(failure)
        self.assertEqual(record["model_attribution"]["response_model"], "grok")

        grok_terminal_tool = json.dumps({
            "tool_event_id": "grok-terminal-tool", "message_id": "grok-proof", "message_seq": 4,
            "message_role": "assistant", "message_is_tool_plumbing": False,
            "tool_name": "pytest", "action": "end", "success": True, "operation_kind": "verification",
        })
        follow_outcome = {
            "kind": "instruction_follow", "assertion_key": "login_migration_verification",
            "does_not_prove": "The terminal result does not establish later durability.",
            "evidence": [
                {"ref": "request", "evidence_type": "message", "window_id": "first", "message_id": "request", "role": "user", "seq": 1, "quote": "Verify the login migration"},
                {"ref": "response", "evidence_type": "message", "window_id": "first", "message_id": "sol-call", "role": "assistant", "seq": 2, "quote": "run the check"},
                {"ref": "outcome", "evidence_type": "tool", "window_id": "first", "message_id": "grok-proof", "fact": grok_terminal_tool},
            ],
            "proof_arcs": [
                {"arc": "request", "evidence_refs": ["request"]},
                {"arc": "response", "evidence_refs": ["response"]},
                {"arc": "outcome", "evidence_refs": ["outcome"]},
            ],
        }
        record, failure = _record_from_observation(follow_outcome, packet, "follow-outcome")
        self.assertIsNone(failure)
        self.assertEqual(record["model_attribution"]["response_model"], "grok")

        tool_gap = json.loads(json.dumps(follow_outcome))
        tool_gap["kind"] = "instruction_miss"
        tool_gap["evidence"][-1]["ref"] = "gap"
        tool_gap["evidence"][-1]["fact"] = grok_terminal_tool.replace('"success": true', '"success": false')
        tool_gap["proof_arcs"][-1] = {"arc": "gap", "evidence_refs": ["gap"]}
        record, failure = _record_from_observation(tool_gap, packet, "miss-tool-gap")
        self.assertIsNone(failure)
        self.assertEqual(record["model_attribution"]["response_model"], "grok")

        miss_gap = {
            "kind": "instruction_miss", "assertion_key": "login_migration_verification",
            "does_not_prove": "The owner correction does not establish the cause.",
            "evidence": [
                {"ref": "request", "evidence_type": "message", "window_id": "first", "message_id": "request", "role": "user", "seq": 1, "quote": "Verify the login migration"},
                {"ref": "response", "evidence_type": "message", "window_id": "first", "message_id": "sol-call", "role": "assistant", "seq": 2, "quote": "run the check"},
                {"ref": "gap", "evidence_type": "message", "window_id": "correction", "message_id": "owner-correction", "role": "user", "seq": 5, "quote": "still missing"},
            ],
            "proof_arcs": [
                {"arc": "request", "evidence_refs": ["request"]},
                {"arc": "response", "evidence_refs": ["response"]},
                {"arc": "gap", "evidence_refs": ["gap"]},
            ],
        }
        record, failure = _record_from_observation(miss_gap, packet, "miss-gap")
        self.assertIsNone(failure)
        self.assertEqual(record["model_attribution"]["response_model"], "grok")

        repeated = {
            "kind": "repeated_ask", "assertion_key": "login_migration_verification_repeated",
            "does_not_prove": "The second request does not establish the cause of repetition.",
            "evidence": [
                {"ref": "request_1", "evidence_type": "message", "window_id": "first", "message_id": "request", "role": "user", "seq": 1, "quote": "Verify the login migration"},
                {"ref": "request_2", "evidence_type": "message", "window_id": "correction", "message_id": "owner-correction", "role": "user", "seq": 5, "quote": "login migration"},
            ],
            "proof_arcs": [
                {"arc": "request_1", "evidence_refs": ["request_1"]},
                {"arc": "request_2", "evidence_refs": ["request_2"]},
            ],
        }
        record, failure = _record_from_observation(repeated, packet, "repeated-request")
        self.assertIsNone(failure)
        self.assertEqual(record["model_attribution"]["response_model"], "grok")

        plumbing_tool = json.dumps({
            "tool_event_id": "terminal-tool", "message_id": "synthetic-callback", "message_seq": 3,
            "message_role": "user", "message_is_tool_plumbing": True,
            "tool_name": "pytest", "action": "end", "success": True, "operation_kind": "verification",
        })
        tool_observation = {
            "kind": "verification", "assertion_key": "login_migration_verification",
            "does_not_prove": "The tool result does not establish later durability.",
            "evidence": [{"ref": "verification_result", "evidence_type": "tool", "window_id": "first", "message_id": "synthetic-callback", "fact": plumbing_tool}],
            "proof_arcs": [{"arc": "verification_result", "evidence_refs": ["verification_result"]}],
        }
        record, failure = _record_from_observation(tool_observation, packet, "tool-proof")
        self.assertIsNone(failure)
        self.assertEqual(record["model_attribution"]["response_model"], "sol")

        owner_correction = {
            "kind": "delivery_gap", "assertion_key": "login_migration_delivery",
            "does_not_prove": "The correction does not establish the cause.",
            "evidence": [
                {"ref": "expectation", "evidence_type": "message", "window_id": "first", "message_id": "request", "role": "user", "seq": 1, "quote": "Verify the login migration"},
                {"ref": "delivery", "evidence_type": "message", "window_id": "correction", "message_id": "owner-correction", "role": "user", "seq": 5, "quote": "still missing"},
            ],
            "proof_arcs": [
                {"arc": "expectation", "evidence_refs": ["expectation"]},
                {"arc": "delivery", "evidence_refs": ["delivery"]},
            ],
        }
        record, failure = _record_from_observation(owner_correction, packet, "owner-correction")
        self.assertIsNone(failure)
        self.assertEqual(record["model_attribution"]["response_model"], "grok")

    def test_terra_result_requires_assigned_producer_and_all_packet_results(self):
        records = [_record(number) for number in range(5)]
        records.append(_record(99, kind="instruction_follow", polarity="positive"))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initial = run_synthesis_pipeline(root, records=records, coverage_manifest=_manifest())
            first_entry = initial["synthesis_manifest"]["packets"][0]
            first_packet = json.loads((root / first_entry["path"]).read_text())
            partial = run_synthesis_pipeline(
                root,
                records=records,
                coverage_manifest=_manifest(),
                terra_results=[{
                    "packet_id": first_packet["packet_id"],
                    "result_id": "terra-only-one",
                    "producer": dict(first_packet["synthesis_assignment"]),
                    "abstain": True,
                    "abstain_reason": "No bounded candidate from this packet.",
                }],
            )
            forged = dict(first_packet["synthesis_assignment"])
            forged["model"] = "unassigned-model"
            _, failures = validate_terra_result(
                {
                    "packet_id": first_packet["packet_id"],
                    "result_id": "forged-producer",
                    "producer": forged,
                    "abstain": True,
                    "abstain_reason": "No bounded candidate from this packet.",
                },
                first_packet,
                config_targets=build_config_target_map(_manifest()["config_inventory"]),
            )
            bundle_written = (root / "synthesis_run_bundle.json").is_file()
        self.assertIsNone(partial["catalog"])
        self.assertIn("missing_synthesis_packet_results", {item["reason"] for item in partial["validation_failures"]})
        self.assertTrue(bundle_written)
        self.assertIn("synthesis_producer_assignment_mismatch", {failure.reason for failure in failures})

    def test_synthesis_reuses_one_shot_luna_result_paths_for_processing_coverage(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute(
            "INSERT INTO sessions(id,harness,external_id,repo) VALUES(?,?,?,?)",
            ("root", "codex", "root", "demo"),
        )
        conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            ("request", "root", 1, "user", "Please verify the tests", "request-hash"),
        )
        conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            ("response", "root", 2, "assistant", "I will verify the tests", "response-hash"),
        )
        conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
            ("window", "root", "request", "response", "input-hash", "window-hash"),
        )
        conn.commit()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = emit_coach_packets(conn, root)
            entry = manifest["packets"][0]
            packet = json.loads((root / entry["path"]).read_text())
            result_path = root / "luna.json"
            result_path.write_text(json.dumps({
                "packet_id": entry["packet_id"],
                "result_id": "luna-abstain",
                "producer": packet["producer_contract"]["expected"],
                "abstain": True,
                "abstain_reason": "No bounded observation.",
                "window_dispositions": [
                    {
                        "window_id": window["window_id"],
                        "observation_ids": [],
                        "no_supported_observation": True,
                    }
                    for window in packet["windows"]
                ],
            }))
            summary = run_synthesis_pipeline(
                root, luna_results=(path for path in [result_path])
            )
        self.assertEqual(summary["validation_failures"], [])
        self.assertEqual(summary["synthesis_manifest"]["coverage"]["processed_packets"], 1)
        self.assertEqual(summary["synthesis_manifest"]["coverage"]["abstained_packets"], 1)
        conn.close()

    def test_orchestrator_emits_packets_without_model_or_catalog_until_results(self):
        records = [_record(i) for i in range(5)]
        records.append(_record(99, kind="instruction_follow", polarity="positive"))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            summary = run_synthesis_pipeline(root, records=records, coverage_manifest=_manifest())
            self.assertEqual(summary["validation_failures"], [])
            self.assertIsNone(summary["catalog"])
            self.assertTrue((root / "synthesis_manifest.json").is_file())
            self.assertEqual(len(summary["synthesis_manifest"]["packets"]), 6)


if __name__ == "__main__":
    unittest.main()
