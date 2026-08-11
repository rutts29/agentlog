import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.coach import CoachPreprocessConfig, emit_coach_packets, validate_coach_result as _validate_coach_result
from agentlog.db.schema import init_db
from agentlog.source_reader import SourceReadResult


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


class CoachPreprocessTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def add_session(self, sid, parent=None, *, profile="codex", transcript_storage="legacy_materialized"):
        self.conn.execute(
            "INSERT INTO sessions(id,harness,external_id,parent_session_id,repo,agent_profile,transcript_storage) VALUES(?,?,?,?,?,?,?)",
            (sid, "codex", sid, parent, "demo", profile, transcript_storage),
        )

    def add_window(self, sid, number, *, authored=False):
        user_id, assistant_id, window_id = f"{sid}-u{number}", f"{sid}-a{number}", f"{sid}-w{number}"
        self.conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash,authored_by_agent) VALUES(?,?,?,?,?,?,?)",
            (user_id, sid, number * 2 - 1, "user", "Please verify the tests", "uh", int(authored)),
        )
        self.conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            (assistant_id, sid, number * 2, "assistant", "Verified the tests and reported the result", "ah"),
        )
        self.conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
            (window_id, sid, user_id, assistant_id, "ih", f"ch-{window_id}"),
        )

    def packet(self):
        self.conn.commit()
        root = Path(tempfile.mkdtemp())
        manifest = emit_coach_packets(self.conn, root)
        path = root / manifest["packets"][0]["path"]
        return manifest, json.loads(path.read_text())

    def test_coverage_root_cluster_and_owner_filters(self):
        self.add_session("root")
        self.add_session("child", "root")
        self.add_session("review", profile="auto-review")
        self.add_window("root", 1)
        self.add_window("child", 1)
        self.add_window("review", 1)
        self.add_session("agent-msg")
        self.add_window("agent-msg", 1, authored=True)
        manifest, packet = self.packet()
        self.assertEqual(manifest["coverage"]["total"], 4)
        self.assertEqual(manifest["coverage"]["eligible"], 2)
        self.assertEqual(manifest["coverage"]["scanned"], 2)
        self.assertEqual(manifest["coverage"]["joined_windows"], 4)
        self.assertEqual(manifest["coverage"]["selected"], 2)
        self.assertEqual(packet["root_session_ids"], ["root"])

    def test_synthetic_owner_looking_requests_do_not_reach_coach_coverage(self):
        self.add_session("root")
        synthetic_requests = [
            "Perform any necessary follow-up actions in response to the subagent completion above.",
            '<codex_internal_context source="goal">resume</codex_internal_context>',
            "<subagent_notification>done</subagent_notification>",
            "<system-reminder>ignore</system-reminder>",
            '<skill name="verification">body</skill>',
            "<recommended_plugins>bundle</environment_context>",
        ]
        for number, text in enumerate(synthetic_requests, start=1):
            user_id, assistant_id = f"synthetic-u{number}", f"synthetic-a{number}"
            self.conn.execute(
                "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
                (user_id, "root", number * 2 - 1, "user", text, user_id),
            )
            self.conn.execute(
                "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
                (assistant_id, "root", number * 2, "assistant", "Completed.", assistant_id),
            )
            self.conn.execute(
                "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
                (f"synthetic-w{number}", "root", user_id, assistant_id, user_id, assistant_id),
            )
        self.conn.commit()
        root = Path(tempfile.mkdtemp())
        manifest = emit_coach_packets(self.conn, root)
        self.assertEqual(manifest["coverage"]["eligible_windows"], 0)
        self.assertEqual(manifest["coverage"]["excluded_synthetic_windows"], len(synthetic_requests))
        self.assertEqual(manifest["packets"], [])
        self.assertEqual(
            len(manifest["eligibility_commitment"]["excluded_synthetic_window_ids"]),
            len(synthetic_requests),
        )

    def test_duplicate_root_is_one_cluster_and_quotes_are_exact(self):
        self.add_session("root")
        self.add_session("child", "root")
        self.add_window("root", 1)
        self.add_window("child", 1)
        _, packet = self.packet()
        self.assertEqual(len(packet["root_session_ids"]), 1)
        self.assertIn("Please verify", packet["windows"][0]["messages"][0]["source_text"])

    def test_source_backed_session_hydrates_without_reading_message_text_or_fts(self):
        self.add_session("source", transcript_storage="source_backed")
        self.conn.executemany(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            [
                ("source:m:1", "source", 1, "user", "persisted text must not be read", "request-hash"),
                ("source:m:2", "source", 2, "assistant", "persisted text must not be read", "response-hash"),
            ],
        )
        self.conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
            ("persisted-window", "source", "source:m:1", "source:m:2", "request-hash", "persisted-content"),
        )
        source_messages = [
            {"id": "source:m:1", "seq": 1, "role": "user", "timestamp": None,
             "model": None, "model_canonical": None, "effort": None,
             "text": "Please verify the source-backed tests.", "content_hash": "request-hash",
             "is_tool_plumbing": False, "authored_by_agent": False},
            {"id": "source:m:2", "seq": 2, "role": "assistant", "timestamp": None,
             "model": None, "model_canonical": None, "effort": None,
             "text": "The source-backed tests passed.", "content_hash": "response-hash",
             "is_tool_plumbing": False, "authored_by_agent": False},
        ]
        calls = []

        def reader(conn, session_id):
            calls.append(session_id)
            return SourceReadResult(
                "ready", source_messages, source_identity="source-identity", source_hash="source-hash"
            )

        statements = []
        self.conn.commit()
        self.conn.set_trace_callback(statements.append)
        root = Path(tempfile.mkdtemp())
        manifest = emit_coach_packets(
            self.conn,
            root,
            config=CoachPreprocessConfig(source_transcript_reader=reader),
        )
        self.conn.set_trace_callback(None)
        packet = json.loads((root / manifest["packets"][0]["path"]).read_text())
        self.assertEqual(calls, ["source"])
        self.assertEqual(packet["windows"][0]["messages"][0]["source_text"], source_messages[0]["text"])
        self.assertEqual(packet["windows"][0]["messages"][0]["content_hash"], "request-hash")
        self.assertEqual(packet["windows"][0]["source_provenance"], {
            "source_identity": "source-identity", "source_hash": "source-hash",
        })
        self.assertFalse(any("messages_fts" in statement.lower() for statement in statements))
        self.assertFalse(any("select id, text from messages" in statement.lower() for statement in statements))

    def test_source_backed_session_fails_closed_when_reader_is_unavailable(self):
        self.add_session("source", transcript_storage="source_backed")
        self.conn.commit()
        root = Path(tempfile.mkdtemp())

        def reader(conn, session_id):
            return SourceReadResult("source_changed", [], warning="source changed")

        with self.assertRaisesRegex(ValueError, "coach_source_transcript_unavailable.*status=source_changed"):
            emit_coach_packets(
                self.conn,
                root,
                config=CoachPreprocessConfig(source_transcript_reader=reader),
            )

    def test_source_backed_append_ahead_of_the_ledger_fails_closed(self):
        self.add_session("source", transcript_storage="source_backed")
        self.conn.executemany(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            [
                ("source:m:1", "source", 1, "user", "", "request-hash"),
                ("source:m:2", "source", 2, "assistant", "", "response-hash"),
            ],
        )
        self.conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
            ("source-window", "source", "source:m:1", "source:m:2", "request-hash", "window-hash"),
        )
        self.conn.commit()

        def reader(conn, session_id):
            messages = [
                {"id": "source:m:1", "seq": 1, "role": "user", "timestamp": None, "model": None, "model_canonical": None, "effort": None, "text": "Verify.", "content_hash": "request-hash", "is_tool_plumbing": False, "authored_by_agent": False},
                {"id": "source:m:2", "seq": 2, "role": "assistant", "timestamp": None, "model": None, "model_canonical": None, "effort": None, "text": "Passed.", "content_hash": "response-hash", "is_tool_plumbing": False, "authored_by_agent": False},
                {"id": "source:m:3", "seq": 3, "role": "user", "timestamp": None, "model": None, "model_canonical": None, "effort": None, "text": "Un-ingested append.", "content_hash": "append-hash", "is_tool_plumbing": False, "authored_by_agent": False},
            ]
            return SourceReadResult("ready", messages, source_identity="source-identity", source_hash="source-hash")

        with self.assertRaisesRegex(ValueError, "coach_source_transcript_ledger_mismatch"):
            emit_coach_packets(
                self.conn,
                Path(tempfile.mkdtemp()),
                config=CoachPreprocessConfig(source_transcript_reader=reader),
            )

    def test_ancestral_copied_history_window_is_excluded_but_same_session_repeats_remain(self):
        self.add_session("parent")
        self.add_session("child", "parent")
        for session_id, suffix in (("parent", "p"), ("child", "c")):
            self.conn.execute(
                "INSERT INTO messages(id,session_id,seq,role,text,content_hash,timestamp) VALUES(?,?,?,?,?,?,?)",
                (f"{suffix}-request", session_id, 1, "user", "Verify the copied login test.", "request-hash", "2026-01-01T00:00:00Z"),
            )
            self.conn.execute(
                "INSERT INTO messages(id,session_id,seq,role,text,content_hash,timestamp) VALUES(?,?,?,?,?,?,?)",
                (f"{suffix}-response", session_id, 2, "assistant", "I will verify it.", "response-hash", "2026-01-01T00:00:00Z"),
            )
            self.conn.execute(
                "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
                (f"{suffix}-window", session_id, f"{suffix}-request", f"{suffix}-response", "input", f"{suffix}-content"),
            )
        self.conn.commit()
        root = Path(tempfile.mkdtemp())
        manifest = emit_coach_packets(self.conn, root)
        window_ids = [window_id for packet_info in manifest["packets"] for window_id in packet_info["window_ids"]]
        self.assertEqual(window_ids, ["p-window"])
        self.assertEqual(manifest["coverage"]["excluded_duplicate_windows"], 1)
        commitment = manifest["eligibility_commitment"]
        self.assertEqual(commitment["duplicate_window_canonical_ids"], {"c-window": "p-window"})

    def test_intention_only_follow_claim_is_rejected(self):
        self.add_session("root")
        self.add_window("root", 1)
        _, packet = self.packet()
        w = packet["windows"][0]
        result = {
            "packet_id": packet["packet_id"], "result_id": "r1", "abstain": False,
            "observations": [{
                "kind": "instruction_follow", "assertion_key": "verify",
                "confidence": 0.9, "does_not_prove": "one exchange only",
                "evidence": [{"window_id": w["window_id"], "message_id": w["messages"][0]["message_id"], "role": "user", "seq": 1, "quote": "Please verify"}],
                "proof_arcs": [{"arc": "request", "evidence_refs": [f"{w['window_id']}:{w['messages'][0]['message_id']}"]}, {"arc": "response", "evidence_refs": ["missing"]}, {"arc": "outcome", "evidence_refs": ["missing"]}],
            }],
        }
        cleaned, failures = validate_coach_result(result, packet)
        self.assertIsNone(cleaned)
        self.assertTrue(failures)

    def test_valid_result_persists_offsets_and_provenance(self):
        self.add_session("root")
        self.add_window("root", 1)
        self.conn.execute(
            "INSERT INTO tool_events(id,session_id,message_id,seq,tool_name,action,success,operation_kind) VALUES(?,?,?,?,?,?,?,?)",
            ("tool-valid", "root", "root-a1", 2, "pytest", "result", 1, "verification"),
        )
        _, packet = self.packet()
        window = packet["windows"][0]
        user, assistant = window["messages"]
        result = {
            "packet_id": packet["packet_id"], "result_id": "r-valid", "abstain": False,
            "producer": dict(packet["producer_contract"]["expected"]),
            "window_dispositions": [
                {"window_id": window["window_id"], "observation_ids": ["obs-valid"], "no_supported_observation": False}
            ],
            "observations": [{
                "observation_id": "obs-valid",
                "kind": "instruction_follow", "assertion_key": "verify",
                "confidence": 0.9, "does_not_prove": "one exchange only",
                "evidence": [
                    {"window_id": window["window_id"], "message_id": user["message_id"], "role": "user", "seq": user["seq"], "quote": "Please verify"},
                    {"window_id": window["window_id"], "message_id": assistant["message_id"], "role": "assistant", "seq": assistant["seq"], "quote": "Verified the tests"},
                    {"window_id": window["window_id"], "tool_event_id": "tool-valid", "fact": window["tool_timeline"][0]["fact"]},
                ],
                "proof_arcs": [
                    {"arc": "request", "evidence_refs": [f"{window['window_id']}:{user['message_id']}"]},
                    {"arc": "response", "evidence_refs": [f"{window['window_id']}:{assistant['message_id']}"]},
                    {"arc": "outcome", "evidence_refs": [f"{window['window_id']}:tool:tool-valid"]},
                ],
            }],
        }
        cleaned, failures = _validate_coach_result(result, packet)
        self.assertEqual(failures, [])
        evidence = cleaned["observations"][0]["evidence"]
        self.assertEqual(evidence[0]["quote_end"] - evidence[0]["quote_start"], len("Please verify"))
        self.assertEqual(evidence[0]["content_hash"], "uh")
        self.assertIn("parser_version", evidence[0])
        mismatched = json.loads(json.dumps(result))
        mismatched["result_id"] = "r-valid-mismatched-disposition"
        mismatched["window_dispositions"][0] = {
            "window_id": window["window_id"], "observation_ids": [], "no_supported_observation": True,
        }
        cleaned, failures = _validate_coach_result(mismatched, packet)
        self.assertIsNone(cleaned)
        self.assertIn("window_disposition_observation_mismatch", {failure.reason for failure in failures})

    def test_tool_and_skill_facts_are_packetized(self):
        self.add_session("root")
        self.add_window("root", 1)
        self.conn.execute(
            "INSERT INTO tool_events(id,session_id,message_id,seq,tool_name,action,success,operation_kind) VALUES(?,?,?,?,?,?,?,?)",
            ("tool-1", "root", "root-a1", 2, "pytest", "result", 1, "verification"),
        )
        self.conn.execute(
            "INSERT INTO skill_exposures(id,session_id,message_id,skill_name,exposure_type) VALUES(?,?,?,?,?)",
            ("skill-1", "root", "root-a1", "verification", "loaded"),
        )
        manifest, packet = self.packet()
        window = packet["windows"][0]
        self.assertEqual(window["tool_timeline"][0]["tool_event_id"], "tool-1")
        self.assertEqual(window["skill_exposures"][0]["skill_exposure_id"], "skill-1")
        self.assertIn('"action": "result"', window["tool_timeline"][0]["fact"])
        capability = manifest["proof_capability_by_harness"]["codex"]
        self.assertEqual(capability["capability"], "supported")
        self.assertEqual(capability["levels"]["deterministic_terminal"], 1)
        self.assertEqual(manifest["eligibility_commitment"]["proof_capability"]["by_harness"]["codex"], capability)

    def test_plumbing_tool_result_and_request_attached_skill_are_attributable(self):
        self.add_session("root")
        rows = [
            ("request", 1, "user", "Please use the verification skill to verify the login tests.", 0),
            ("tool-result", 2, "user", "tool result", 1),
            ("response", 3, "assistant", "The login tests passed.", 0),
        ]
        for message_id, seq, role, text, plumbing in rows:
            self.conn.execute(
                "INSERT INTO messages(id,session_id,seq,role,text,content_hash,is_tool_plumbing) VALUES(?,?,?,?,?,?,?)",
                (message_id, "root", seq, role, text, message_id, plumbing),
            )
        self.conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
            ("window", "root", "request", "response", "input", "content"),
        )
        self.conn.execute(
            "INSERT INTO skill_exposures(id,session_id,message_id,skill_name,exposure_type) VALUES(?,?,?,?,?)",
            ("attached-skill", "root", "request", "verification", "attached"),
        )
        self.conn.execute(
            "INSERT INTO tool_events(id,session_id,message_id,seq,tool_name,action,success,operation_kind) VALUES(?,?,?,?,?,?,?,?)",
            ("terminal-tool", "root", "tool-result", 1, "pytest", "result", 1, "verification"),
        )
        _, packet = self.packet()
        window = packet["windows"][0]
        self.assertEqual([message["message_id"] for message in window["messages"]], ["request", "response"])
        self.assertEqual(window["tool_timeline"][0]["message_role"], "user")
        self.assertTrue(window["tool_timeline"][0]["message_is_tool_plumbing"])
        self.assertEqual(window["skill_exposures"][0]["exposure_type"], "attached")
        request, skill, tool = window["messages"][0], window["skill_exposures"][0], window["tool_timeline"][0]
        result = {
            "packet_id": packet["packet_id"], "result_id": "attached-skill", "abstain": False,
            "producer": dict(packet["producer_contract"]["expected"]),
            "observations": [{
                "kind": "skill_use", "assertion_key": "verification_skill_login_tests", "confidence": 0.8,
                "does_not_prove": "The terminal result proves this bounded verification action, not every use of the skill.",
                "evidence": [
                    {"window_id": window["window_id"], "message_id": request["message_id"], "role": "user", "seq": request["seq"], "quote": "Please use the verification skill"},
                    {"window_id": window["window_id"], "skill_exposure_id": skill["skill_exposure_id"], "fact": skill["fact"]},
                    {"window_id": window["window_id"], "tool_event_id": tool["tool_event_id"], "fact": tool["fact"]},
                ],
                "proof_arcs": [
                    {"arc": "skill_request", "evidence_refs": [f"{window['window_id']}:{request['message_id']}"]},
                    {"arc": "skill_evidence", "evidence_refs": [f"{window['window_id']}:skill:{skill['skill_exposure_id']}"]},
                    {"arc": "skill_action", "evidence_refs": [f"{window['window_id']}:tool:{tool['tool_event_id']}"]},
                ],
            }],
        }
        cleaned, failures = validate_coach_result(result, packet)
        self.assertEqual(failures, [])
        self.assertEqual(cleaned["observations"][0]["kind"], "skill_use")

    def test_window_carries_late_assistant_tool_without_synthetic_boundary_leakage(self):
        self.add_session("root")
        rows = [
            ("owner-request", 1, "user", "Please verify the migration tests."),
            ("first-assistant", 2, "assistant", "I will run them."),
            ("notification", 3, "user", "<subagent_notification>done</subagent_notification>"),
            ("final-assistant", 4, "assistant", "The migration tests passed."),
            ("next-request", 5, "user", "Start the next task."),
            ("next-assistant", 6, "assistant", "Acknowledged."),
        ]
        for message_id, seq, role, text in rows:
            self.conn.execute(
                "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
                (message_id, "root", seq, role, text, message_id),
            )
        self.conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
            ("first-window", "root", "owner-request", "first-assistant", "first", "first"),
        )
        self.conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
            ("second-window", "root", "next-request", "next-assistant", "second", "second"),
        )
        self.conn.execute(
            "INSERT INTO tool_events(id,session_id,message_id,seq,tool_name,action,success,operation_kind) VALUES(?,?,?,?,?,?,?,?)",
            ("late-tool", "root", "final-assistant", 1, "pytest", "end", 1, "verification"),
        )
        manifest, packet = self.packet()
        first_window = next(window for window in packet["windows"] if window["window_id"] == "first-window")
        self.assertEqual([message["message_id"] for message in first_window["messages"]], ["owner-request", "first-assistant", "final-assistant"])
        self.assertEqual([tool["tool_event_id"] for tool in first_window["tool_timeline"]], ["late-tool"])
        self.assertNotIn("subagent_notification", json.dumps(first_window))
        self.assertEqual(manifest["coverage"]["eligible_windows"], 2)

    def test_skill_fact_can_support_skill_use_arc(self):
        self.add_session("root")
        self.add_window("root", 1)
        self.conn.execute(
            "INSERT INTO tool_events(id,session_id,message_id,seq,tool_name,action,success,operation_kind) VALUES(?,?,?,?,?,?,?,?)",
            ("tool-skill", "root", "root-a1", 2, "pytest", "result", 1, "verification"),
        )
        self.conn.execute(
            "INSERT INTO skill_exposures(id,session_id,message_id,skill_name,exposure_type) VALUES(?,?,?,?,?)",
            ("skill-1", "root", "root-a1", "verification", "loaded"),
        )
        self.conn.execute(
            "INSERT INTO tool_events(id,session_id,message_id,seq,tool_name,action,success,operation_kind) VALUES(?,?,?,?,?,?,?,?)",
            ("tool-unrelated", "root", "root-a1", 3, "apply_patch", "end", 1, "artifact_write"),
        )
        _, packet = self.packet()
        window = packet["windows"][0]
        user = window["messages"][0]
        skill = window["skill_exposures"][0]
        result = {
            "packet_id": packet["packet_id"], "result_id": "r-skill", "abstain": False,
            "producer": dict(packet["producer_contract"]["expected"]),
            "observations": [{
                "kind": "skill_use", "assertion_key": "verification",
                "confidence": 0.8, "does_not_prove": "loading is not successful use",
                "evidence": [
                    {"window_id": window["window_id"], "message_id": user["message_id"], "role": "user", "seq": user["seq"], "quote": "Please verify"},
                    {"window_id": window["window_id"], "skill_exposure_id": skill["skill_exposure_id"], "fact": skill["fact"]},
                    {"window_id": window["window_id"], "tool_event_id": "tool-skill", "fact": window["tool_timeline"][0]["fact"]},
                ],
                "proof_arcs": [
                    {"arc": "skill_request", "evidence_refs": [f"{window['window_id']}:{user['message_id']}"]},
                    {"arc": "skill_evidence", "evidence_refs": [f"{window['window_id']}:skill:{skill['skill_exposure_id']}"]},
                    {"arc": "skill_action", "evidence_refs": [f"{window['window_id']}:tool:tool-skill"]},
                ],
            }],
        }
        cleaned, failures = validate_coach_result(result, packet)
        self.assertEqual(failures, [])
        self.assertEqual(cleaned["observations"][0]["evidence"][1]["evidence_type"], "skill")
        unrelated = json.loads(json.dumps(result))
        unrelated["result_id"] = "r-skill-unrelated-action"
        unrelated["observations"][0]["evidence"][2] = {
            "window_id": window["window_id"], "tool_event_id": "tool-unrelated", "fact": window["tool_timeline"][1]["fact"],
        }
        unrelated["observations"][0]["proof_arcs"][2]["evidence_refs"] = [f"{window['window_id']}:tool:tool-unrelated"]
        cleaned, failures = validate_coach_result(unrelated, packet)
        self.assertIsNone(cleaned)
        self.assertIn("skill_use_requires_attributable_action", {failure.reason for failure in failures})

    def test_skill_use_rejects_cross_window_or_wrong_skill_action(self):
        self.add_session("root")
        self.add_window("root", 1)
        self.add_window("root", 2)
        self.conn.execute(
            "INSERT INTO skill_exposures(id,session_id,message_id,skill_name,exposure_type) VALUES(?,?,?,?,?)",
            ("skill-1", "root", "root-a1", "verification", "loaded"),
        )
        self.conn.execute(
            "INSERT INTO tool_events(id,session_id,message_id,seq,tool_name,action,success,operation_kind) VALUES(?,?,?,?,?,?,?,?)",
            ("tool-2", "root", "root-a2", 4, "pytest", "result", 1, "verification"),
        )
        _, packet = self.packet()
        first, second = packet["windows"]
        result = {
            "packet_id": packet["packet_id"], "result_id": "r-cross-window-skill", "abstain": False,
            "producer": dict(packet["producer_contract"]["expected"]),
            "observations": [{
                "kind": "skill_use", "assertion_key": "verification", "confidence": 0.8,
                "does_not_prove": "A loaded skill does not establish that this unrelated later result used it.",
                "evidence": [
                    {"window_id": first["window_id"], "message_id": first["messages"][0]["message_id"], "role": "user", "seq": first["messages"][0]["seq"], "quote": "Please verify"},
                    {"window_id": first["window_id"], "skill_exposure_id": "skill-1", "fact": first["skill_exposures"][0]["fact"]},
                    {"window_id": second["window_id"], "tool_event_id": "tool-2", "fact": second["tool_timeline"][0]["fact"]},
                ],
                "proof_arcs": [
                    {"arc": "skill_request", "evidence_refs": [f"{first['window_id']}:{first['messages'][0]['message_id']}"]},
                    {"arc": "skill_evidence", "evidence_refs": [f"{first['window_id']}:skill:skill-1"]},
                    {"arc": "skill_action", "evidence_refs": [f"{second['window_id']}:tool:tool-2"]},
                ],
            }],
        }
        cleaned, failures = validate_coach_result(result, packet)
        self.assertIsNone(cleaned)
        self.assertIn("skill_use_requires_attributable_action", {failure.reason for failure in failures})

        self.conn.execute("DELETE FROM tool_events")
        self.conn.execute("DELETE FROM skill_exposures")
        self.conn.execute(
            "INSERT INTO skill_exposures(id,session_id,message_id,skill_name,exposure_type) VALUES(?,?,?,?,?)",
            ("wrong-skill", "root", "root-a1", "deployment", "loaded"),
        )
        self.conn.execute(
            "INSERT INTO tool_events(id,session_id,message_id,seq,tool_name,action,success,operation_kind) VALUES(?,?,?,?,?,?,?,?)",
            ("tool-1", "root", "root-a1", 2, "pytest", "result", 1, "verification"),
        )
        _, wrong_packet = self.packet()
        window = wrong_packet["windows"][0]
        wrong_result = {
            **result,
            "packet_id": wrong_packet["packet_id"], "result_id": "r-wrong-skill",
            "producer": dict(wrong_packet["producer_contract"]["expected"]),
            "observations": [{
                **result["observations"][0],
                "evidence": [
                    {"window_id": window["window_id"], "message_id": window["messages"][0]["message_id"], "role": "user", "seq": window["messages"][0]["seq"], "quote": "Please verify"},
                    {"window_id": window["window_id"], "skill_exposure_id": "wrong-skill", "fact": window["skill_exposures"][0]["fact"]},
                    {"window_id": window["window_id"], "tool_event_id": "tool-1", "fact": window["tool_timeline"][0]["fact"]},
                ],
                "proof_arcs": [
                    {"arc": "skill_request", "evidence_refs": [f"{window['window_id']}:{window['messages'][0]['message_id']}"]},
                    {"arc": "skill_evidence", "evidence_refs": [f"{window['window_id']}:skill:wrong-skill"]},
                    {"arc": "skill_action", "evidence_refs": [f"{window['window_id']}:tool:tool-1"]},
                ],
            }],
        }
        cleaned, failures = validate_coach_result(wrong_result, wrong_packet)
        self.assertIsNone(cleaned)
        self.assertIn("skill_use_requires_attributable_action", {failure.reason for failure in failures})

    def test_generic_terminal_verification_category_accepts_sentence_punctuation(self):
        self.add_session("root")
        self.add_window("root", 1)
        self.conn.execute("UPDATE messages SET text=? WHERE id=?", ("Run the login tests.", "root-u1"))
        self.conn.execute(
            "INSERT INTO tool_events(id,session_id,message_id,seq,tool_name,action,success,operation_kind) VALUES(?,?,?,?,?,?,?,?)",
            ("tool-generic", "root", "root-a1", 2, "exec_command", "end", 1, "verification"),
        )
        _, packet = self.packet()
        window = packet["windows"][0]
        user = window["messages"][0]
        result = {
            "packet_id": packet["packet_id"], "result_id": "r-generic-verification", "abstain": False,
            "producer": dict(packet["producer_contract"]["expected"]),
            "observations": [{
                "kind": "verification", "assertion_key": "login_tests", "confidence": 0.8,
                "does_not_prove": "This category result does not prove the exact login suite or later task outcome.",
                "evidence": [
                    {"window_id": window["window_id"], "message_id": user["message_id"], "role": "user", "seq": user["seq"], "quote": "Run the login tests."},
                    {"window_id": window["window_id"], "tool_event_id": "tool-generic", "fact": window["tool_timeline"][0]["fact"]},
                ],
                "proof_arcs": [
                    {"arc": "verification_request", "evidence_refs": [f"{window['window_id']}:{user['message_id']}"]},
                    {"arc": "verification_result", "evidence_refs": [f"{window['window_id']}:tool:tool-generic"]},
                ],
            }],
        }
        cleaned, failures = validate_coach_result(result, packet)
        self.assertEqual(failures, [])
        self.assertEqual(cleaned["observations"][0]["kind"], "verification")

    def test_skill_exposure_alone_cannot_support_skill_use(self):
        self.add_session("root")
        self.add_window("root", 1)
        self.conn.execute(
            "INSERT INTO skill_exposures(id,session_id,message_id,skill_name,exposure_type) VALUES(?,?,?,?,?)",
            ("skill-only", "root", "root-a1", "verification", "loaded"),
        )
        _, packet = self.packet()
        window = packet["windows"][0]
        user, skill = window["messages"][0], window["skill_exposures"][0]
        result = {
            "packet_id": packet["packet_id"], "result_id": "r-skill-only", "abstain": False,
            "observations": [{
                "kind": "skill_use", "assertion_key": "verification", "confidence": 0.8,
                "does_not_prove": "exposure is not action",
                "evidence": [
                    {"window_id": window["window_id"], "message_id": user["message_id"], "role": "user", "seq": user["seq"], "quote": "Please verify"},
                    {"window_id": window["window_id"], "skill_exposure_id": skill["skill_exposure_id"], "fact": skill["fact"]},
                ],
                "proof_arcs": [
                    {"arc": "skill_request", "evidence_refs": [f"{window['window_id']}:{user['message_id']}"]},
                    {"arc": "skill_evidence", "evidence_refs": [f"{window['window_id']}:skill:{skill['skill_exposure_id']}"]},
                    {"arc": "skill_action", "evidence_refs": [f"{window['window_id']}:skill:{skill['skill_exposure_id']}"]},
                ],
            }],
        }
        cleaned, failures = validate_coach_result(result, packet)
        self.assertIsNone(cleaned)
        self.assertTrue(failures)

    def test_unlinked_skill_exposure_is_not_packetized(self):
        self.add_session("root")
        self.add_window("root", 1)
        self.conn.execute(
            "INSERT INTO skill_exposures(id,session_id,message_id,skill_name,exposure_type) VALUES(?,?,?,?,?)",
            ("unlinked-skill", "root", None, "verification", "loaded"),
        )
        _, packet = self.packet()
        self.assertEqual(packet["windows"][0]["skill_exposures"], [])

    def test_verification_assistant_self_report_is_rejected(self):
        self.add_session("root")
        self.add_window("root", 1)
        _, packet = self.packet()
        window = packet["windows"][0]
        user, assistant = window["messages"]
        result = {
            "packet_id": packet["packet_id"], "result_id": "r-verification", "abstain": False,
            "observations": [{
                "kind": "verification", "assertion_key": "tests", "confidence": 0.8,
                "does_not_prove": "self-report is not a deterministic result",
                "evidence": [
                    {"window_id": window["window_id"], "message_id": user["message_id"], "role": "user", "seq": user["seq"], "quote": "Please verify"},
                    {"window_id": window["window_id"], "message_id": assistant["message_id"], "role": "assistant", "seq": assistant["seq"], "quote": "Verified the tests"},
                ],
                "proof_arcs": [
                    {"arc": "verification_request", "evidence_refs": [f"{window['window_id']}:{user['message_id']}"]},
                    {"arc": "verification_result", "evidence_refs": [f"{window['window_id']}:{assistant['message_id']}"]},
                ],
            }],
        }
        cleaned, failures = validate_coach_result(result, packet)
        self.assertIsNone(cleaned)
        self.assertTrue(failures)

    def test_owner_correction_uses_utc_timestamp_order(self):
        self.add_session("root")
        rows = [
            ("request", 1, "user", "Please deliver the login migration.", "2026-01-01T01:00:00+00:00"),
            ("response", 2, "assistant", "I will deliver it.", "2026-01-01T01:00:00+00:00"),
            ("correction", 3, "user", "No, the login migration is still missing.", "2026-01-01T06:00:00+05:30"),
            ("reply", 4, "assistant", "I will investigate.", "2026-01-01T06:00:00+05:30"),
        ]
        for message_id, seq, role, text, timestamp in rows:
            self.conn.execute(
                "INSERT INTO messages(id,session_id,seq,role,text,content_hash,timestamp) VALUES(?,?,?,?,?,?,?)",
                (message_id, "root", seq, role, text, message_id, timestamp),
            )
        for window_id, request, response in (("first", "request", "response"), ("second", "correction", "reply")):
            self.conn.execute(
                "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
                (window_id, "root", request, response, window_id, window_id),
            )
        _, packet = self.packet()
        first = next(window for window in packet["windows"] if window["window_id"] == "first")
        second = next(window for window in packet["windows"] if window["window_id"] == "second")
        result = {
            "packet_id": packet["packet_id"], "result_id": "offset-order", "abstain": False,
            "producer": dict(packet["producer_contract"]["expected"]),
            "observations": [{
                "kind": "delivery_gap", "assertion_key": "login_migration_delivery_gap", "confidence": 0.8,
                "does_not_prove": "The correction alone does not identify the source of the missing delivery.",
                "evidence": [
                    {"window_id": "first", "message_id": "request", "role": "user", "seq": 1, "quote": "Please deliver the login migration."},
                    {"window_id": "second", "message_id": "correction", "role": "user", "seq": 3, "quote": "the login migration is still missing"},
                ],
                "proof_arcs": [
                    {"arc": "expectation", "evidence_refs": ["first:request"]},
                    {"arc": "delivery", "evidence_refs": ["second:correction"]},
                ],
            }],
        }
        cleaned, failures = validate_coach_result(result, packet)
        self.assertIsNone(cleaned)
        self.assertIn("owner_evidence_must_be_later", {failure.reason for failure in failures})

    def test_abstention_is_valid_with_reason(self):
        self.add_session("root")
        self.add_window("root", 1)
        self.add_window("root", 2)
        _, packet = self.packet()
        raw = {
            "packet_id": packet["packet_id"], "result_id": "r2", "abstain": True,
            "producer": dict(packet["producer_contract"]["expected"]),
            "abstain_reason": "insufficient proof",
        }
        cleaned, failures = _validate_coach_result(raw, packet)
        self.assertIsNone(cleaned)
        self.assertIn("window_dispositions_not_list", {failure.reason for failure in failures})
        raw["window_dispositions"] = [
            {"window_id": window["window_id"], "observation_ids": [], "no_supported_observation": True}
            for window in packet["windows"]
        ]
        incomplete = json.loads(json.dumps(raw))
        incomplete["window_dispositions"].pop()
        cleaned, failures = _validate_coach_result(incomplete, packet)
        self.assertIsNone(cleaned)
        self.assertIn("window_disposition_missing_local_window", {failure.reason for failure in failures})
        cleaned, failures = _validate_coach_result(raw, packet)
        self.assertEqual(failures, [])
        self.assertTrue(cleaned["abstain"])

    def test_window_dispositions_are_exact_and_locally_owned(self):
        self.add_session("root")
        self.add_window("root", 1)
        self.add_window("root", 2)
        self.conn.execute(
            "INSERT INTO tool_events(id,session_id,message_id,seq,tool_name,action,success,operation_kind) VALUES(?,?,?,?,?,?,?,?)",
            ("verify", "root", "root-a1", 2, "pytest", "end", 1, "verification"),
        )
        _, packet = self.packet()
        base_abstain = {
            "packet_id": packet["packet_id"], "result_id": "dispositions", "abstain": True,
            "producer": dict(packet["producer_contract"]["expected"]), "abstain_reason": "No bounded proof.",
        }
        cleaned, failures = _validate_coach_result(base_abstain, packet)
        self.assertIsNone(cleaned)
        self.assertIn("window_dispositions_not_list", {failure.reason for failure in failures})
        bad_unknown = {
            **base_abstain,
            "window_dispositions": [{"window_id": "unknown", "observation_ids": [], "no_supported_observation": True}],
        }
        _, failures = _validate_coach_result(bad_unknown, packet)
        self.assertIn("window_disposition_unknown_window", {failure.reason for failure in failures})
        good_dispositions = [
            {"window_id": window["window_id"], "observation_ids": [], "no_supported_observation": True}
            for window in packet["windows"]
        ]
        cleaned, failures = _validate_coach_result({**base_abstain, "window_dispositions": good_dispositions}, packet)
        self.assertEqual(failures, [])
        self.assertTrue(cleaned["abstain"])

        first, second = packet["windows"]
        user, assistant = first["messages"][:2]
        tool = first["tool_timeline"][0]
        claimed = {
            "packet_id": packet["packet_id"], "result_id": "wrong-window", "abstain": False,
            "producer": dict(packet["producer_contract"]["expected"]),
            "observations": [{
                "observation_id": "obs-local", "kind": "instruction_follow", "assertion_key": "verification_follow",
                "confidence": 0.8, "does_not_prove": "This single result does not establish later behavior.",
                "evidence": [
                    {"window_id": first["window_id"], "message_id": user["message_id"], "role": "user", "seq": user["seq"], "quote": "Please verify"},
                    {"window_id": first["window_id"], "message_id": assistant["message_id"], "role": "assistant", "seq": assistant["seq"], "quote": "Verified the tests"},
                    {"window_id": first["window_id"], "tool_event_id": tool["tool_event_id"], "fact": tool["fact"]},
                ],
                "proof_arcs": [
                    {"arc": "request", "evidence_refs": [f"{first['window_id']}:{user['message_id']}"]},
                    {"arc": "response", "evidence_refs": [f"{first['window_id']}:{assistant['message_id']}"]},
                    {"arc": "outcome", "evidence_refs": [f"{first['window_id']}:tool:{tool['tool_event_id']}"]},
                ],
            }],
            "window_dispositions": [
                {"window_id": first["window_id"], "observation_ids": [], "no_supported_observation": True},
                {"window_id": second["window_id"], "observation_ids": ["obs-local"], "no_supported_observation": False},
            ],
        }
        cleaned, failures = _validate_coach_result(claimed, packet)
        self.assertIsNone(cleaned)
        self.assertIn("window_disposition_observation_mismatch", {failure.reason for failure in failures})

    def test_result_requires_bound_producer_metadata(self):
        self.add_session("root")
        self.add_window("root", 1)
        _, packet = self.packet()
        cleaned, failures = validate_coach_result(
            {"packet_id": packet["packet_id"], "result_id": "unbound", "abstain": True, "abstain_reason": "no proof"},
            packet,
        )
        self.assertIsNone(cleaned)
        self.assertIn("missing_producer_metadata", {failure.reason for failure in failures})

    def test_provider_backing_and_workers_share_t3_logical_root(self):
        self.conn.executescript(
            """
            INSERT INTO sessions(id,harness,external_id,parent_session_id,repo)
            VALUES ('t3-root','t3code','root',NULL,'demo'),
                   ('codex-backing','codex','backing',NULL,'demo'),
                   ('codex-worker','codex','worker','backing','demo'),
                   ('codex-grandchild','codex','grandchild','worker','demo');
            INSERT INTO session_links(source_session_id,target_session_id,link_type,target_harness,target_external_id,link_role)
            VALUES ('t3-root','codex-backing','provider_backing','codex','backing','root'),
                   ('t3-root','codex-worker','provider_backing','codex','worker','worker');
            """
        )
        for sid in ("t3-root", "codex-backing", "codex-worker", "codex-grandchild"):
            self.add_window(sid, 1)
        manifest, packet = self.packet()
        self.assertEqual(manifest["coverage"]["total_roots"], 1)
        self.assertEqual(manifest["coverage"]["physical_root_count"], 2)
        self.assertEqual(manifest["coverage"]["eligible"], 3)
        self.assertEqual(packet["root_session_ids"], ["t3-root"])
        self.assertEqual(manifest["per_harness"], {"t3code": 3})
        self.assertEqual(
            {window["session_id"] for window in packet["windows"]},
            {"codex-backing", "codex-worker", "codex-grandchild"},
        )
        self.assertEqual({window["logical_harness"] for window in packet["windows"]}, {"t3code"})
        self.assertEqual({window["runtime_harness"] for window in packet["windows"]}, {"codex"})
        self.assertEqual(
            {window["physical_root_session_id"] for window in packet["windows"]},
            {"codex-backing"},
        )

    def test_fallback_uses_current_t3_episode_once(self):
        self.conn.executescript(
            """
            INSERT INTO sessions
              (id,harness,external_id,repo,model,model_canonical,provider,agent_profile)
            VALUES
              ('t3-root','t3code','root','demo','grok-4.5','grok-4.5','xai','grok'),
              ('codex-backing','codex','backing','demo','gpt-5.6-sol','gpt-5.6-sol','openai','codex');
            INSERT INTO session_links
              (source_session_id,target_session_id,link_type,target_harness,target_external_id,link_role)
            VALUES ('t3-root','codex-backing','provider_backing','codex','backing','root');
            """
        )
        self.add_window("t3-root", 1)
        self.add_window("codex-backing", 1)
        manifest, packet = self.packet()
        self.assertEqual(manifest["coverage"]["total_roots"], 1)
        self.assertEqual(manifest["coverage"]["eligible"], 1)
        self.assertEqual(packet["root_session_ids"], ["t3-root"])
        self.assertEqual([window["session_id"] for window in packet["windows"]], ["t3-root"])
        self.assertEqual(packet["windows"][0]["logical_harness"], "t3code")
        self.assertEqual(packet["windows"][0]["runtime_harness"], "t3code")
        self.assertEqual(packet["windows"][0]["session"]["model_canonical"], "grok-4.5")

    def test_multiple_historical_roots_emit_current_t3_episode_once(self):
        self.conn.executescript(
            """
            INSERT INTO sessions
              (id,harness,external_id,repo,model,model_canonical,provider,agent_profile)
            VALUES
              ('t3-root','t3code','root','demo','grok-4.5','grok-4.5','xai','grok'),
              ('codex-first','codex','first','demo','gpt-5.6-sol','gpt-5.6-sol','openai','codex'),
              ('codex-second','codex','second','demo','gpt-5.6-sol','gpt-5.6-sol','openai','codex');
            INSERT INTO session_links
              (source_session_id,target_session_id,link_type,target_harness,target_external_id,link_role)
            VALUES
              ('t3-root','codex-first','provider_backing','codex','first','root'),
              ('t3-root','codex-second','provider_backing','codex','second','root');
            """
        )
        for sid in ("t3-root", "codex-first", "codex-second"):
            self.add_window(sid, 1)
        manifest, packet = self.packet()
        self.assertEqual(manifest["coverage"]["total_roots"], 1)
        self.assertEqual(manifest["coverage"]["eligible"], 1)
        self.assertEqual([window["session_id"] for window in packet["windows"]], ["t3-root"])

    def test_ambiguous_bare_external_parent_is_not_resolved(self):
        self.conn.executescript(
            """
            INSERT INTO sessions(id,harness,external_id,parent_session_id,repo)
            VALUES ('t3-dup','t3code','dup',NULL,'demo'),
                   ('codex-dup','codex','dup',NULL,'demo'),
                   ('warp-child','warp','child','dup','demo');
            """
        )
        for sid in ("t3-dup", "codex-dup", "warp-child"):
            self.add_window(sid, 1)
        self.conn.commit()
        root = Path(tempfile.mkdtemp())
        manifest = emit_coach_packets(self.conn, root)
        self.assertEqual(manifest["coverage"]["total_roots"], 3)
        windows = []
        for packet_meta in manifest["packets"]:
            windows.extend(json.loads((root / packet_meta["path"]).read_text())["windows"])
        child = next(window for window in windows if window["session_id"] == "warp-child")
        self.assertEqual(child["physical_root_session_id"], "warp-child")

    def test_packet_does_not_emit_absolute_cwd_or_artifact_paths(self):
        self.conn.execute(
            "INSERT INTO artifacts(id,harness,path,size,mtime_ns,content_hash,parser_version) VALUES(?,?,?,?,?,?,?)",
            (7, "codex", "/Users/private-secret/project/transcript.jsonl", 10, 1, "artifact-hash", "parser-v"),
        )
        self.add_session("root")
        self.conn.execute(
            "UPDATE sessions SET cwd = ?, artifact_id = ? WHERE id = ?",
            ("/Users/private-secret/project", 7, "root"),
        )
        self.add_window("root", 1)
        self.conn.execute(
            "UPDATE messages SET text = ? WHERE id = ?",
            ("Visit https://private.example.test/secret and /opt/private/transcript; "
             "'/Users/alice/Documents/Secret Project/file.txt' "
             "C:\\Users\\alice\\Secret\\x.txt git@github.com:private/repo.git "
             "/Volumes/Client Secret/project/file.txt /mnt/private data/file.txt "
             "/srv/acme/secret.txt \\\\server\\share\\Client Secret\\x.txt", "root-u1"),
        )
        self.conn.commit()
        root = Path(tempfile.mkdtemp())
        manifest = emit_coach_packets(self.conn, root)
        payload = (root / manifest["packets"][0]["path"]).read_text()
        self.assertNotIn("/Users/private-secret", payload)
        self.assertNotIn("https://private.example.test", payload)
        self.assertNotIn("/opt/private/transcript", payload)
        self.assertNotIn("Secret Project", payload)
        self.assertNotIn("C:\\Users\\alice", payload)
        self.assertNotIn("git@github.com:private/repo.git", payload)
        self.assertNotIn("/Volumes/Client Secret", payload)
        self.assertNotIn("/mnt/private data", payload)
        self.assertNotIn("/srv/acme/secret.txt", payload)
        self.assertNotIn("\\\\server\\share\\Client Secret", payload)
        self.assertRegex(payload, r'"cwd": "cwd:[0-9a-f]{24}"')
        self.assertRegex(payload, r'"artifact_path": "artifact:[0-9a-f]{24}"')

    def test_corpus_snapshot_is_stable_and_changes_with_ledger(self):
        self.add_session("root")
        self.add_window("root", 1)
        self.conn.commit()
        first, _ = self.packet()
        second, _ = self.packet()
        self.assertEqual(first["corpus_snapshot_hash"], second["corpus_snapshot_hash"])
        self.add_window("root", 2)
        self.conn.commit()
        changed, _ = self.packet()
        self.assertNotEqual(first["corpus_snapshot_hash"], changed["corpus_snapshot_hash"])

    def test_response_model_and_effort_are_window_attributed(self):
        self.add_session("root")
        self.add_window("root", 1)
        self.add_window("root", 2)
        self.conn.execute(
            "UPDATE messages SET model_canonical = ?, effort = ? WHERE id = ?",
            ("sol", "high", "root-a1"),
        )
        self.conn.execute(
            "UPDATE messages SET model_canonical = ?, effort = ? WHERE id = ?",
            ("grok", "low", "root-a2"),
        )
        self.conn.execute("UPDATE messages SET seq = ? WHERE id = ?", (5, "root-u2"))
        self.conn.execute("UPDATE messages SET seq = ? WHERE id = ?", (6, "root-a2"))
        self.conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash,model_canonical,effort) VALUES(?,?,?,?,?,?,?,?)",
            ("root-a1-tail", "root", 3, "assistant", "The prior check completed.", "tail", "grok", "low"),
        )
        manifest, packet = self.packet()
        responses = {message["message_id"]: message for window in packet["windows"] for message in window["messages"] if message["role"] == "assistant"}
        self.assertEqual(responses["root-a1"]["model_canonical"], "sol")
        self.assertEqual(responses["root-a1"]["effort"], "high")
        self.assertEqual(responses["root-a2"]["model_canonical"], "grok")
        self.assertEqual(packet["windows"][0]["session"]["models_seen"], ["sol", "grok"])
        self.assertEqual(manifest["coverage"]["scope_denominators"]["model_sol"], {"eligible_roots": 1, "eligible_windows": 1})
        self.assertEqual(manifest["coverage"]["scope_denominators"]["model_grok"], {"eligible_roots": 1, "eligible_windows": 2})
        self.assertEqual(
            manifest["eligibility_commitment"]["scope_denominators"],
            manifest["coverage"]["scope_denominators"],
        )

    def test_max_packets_coverage_matches_emitted_packets(self):
        self.add_session("root-a")
        self.add_session("root-b")
        self.add_window("root-a", 1)
        self.add_window("root-b", 1)
        self.conn.commit()
        root = Path(tempfile.mkdtemp())
        manifest = emit_coach_packets(self.conn, root, config=CoachPreprocessConfig(max_packets=1))
        self.assertEqual(len(manifest["packets"]), 1)
        self.assertEqual(manifest["coverage"]["selected"], 2)
        self.assertEqual(manifest["coverage"]["packetized"], 1)
        self.assertEqual(manifest["coverage"]["selected_roots"], 2)
        self.assertEqual(manifest["coverage"]["packetized_roots"], 1)

    def test_full_packet_byte_budget_splits_before_window_count(self):
        self.add_session("root")
        for number in range(1, 4):
            self.add_window("root", number)
        self.conn.commit()
        one_window_root = Path(tempfile.mkdtemp())
        one_window = emit_coach_packets(
            self.conn,
            one_window_root,
            config=CoachPreprocessConfig(max_windows_per_packet=1),
        )
        byte_budget = max(
            len((one_window_root / entry["path"]).read_bytes())
            for entry in one_window["packets"]
        )

        root = Path(tempfile.mkdtemp())
        manifest = emit_coach_packets(
            self.conn,
            root,
            config=CoachPreprocessConfig(
                publication_mode="full",
                max_windows_per_packet=10,
                max_packet_chars=byte_budget,
            ),
        )

        self.assertEqual(len(manifest["packets"]), 3)
        self.assertEqual(manifest["coverage"]["packetized_windows"], 3)
        self.assertEqual(manifest["selection_config"]["max_packet_chars"], byte_budget)
        for entry in manifest["packets"]:
            payload = (root / entry["path"]).read_bytes()
            packet = json.loads(payload)
            self.assertLessEqual(len(payload), byte_budget)
            self.assertEqual(entry["serialized_bytes"], len(payload))
            self.assertEqual(len(packet["windows"]), 1)
            self.assertEqual(len(packet["root_request_index"]["root"]), 3)
            self.assertFalse(packet["windows"][0]["messages"][0]["source_truncated"])

    def test_packet_byte_budget_rejects_single_local_window_or_context(self):
        self.add_session("root")
        self.add_window("root", 1)
        self.conn.commit()
        baseline_root = Path(tempfile.mkdtemp())
        baseline = emit_coach_packets(self.conn, baseline_root)
        packet_bytes = len((baseline_root / baseline["packets"][0]["path"]).read_bytes())

        failed_root = Path(tempfile.mkdtemp())
        with self.assertRaisesRegex(ValueError, "coach_packet_byte_budget_exceeded"):
            emit_coach_packets(
                self.conn,
                failed_root,
                config=CoachPreprocessConfig(max_packet_chars=packet_bytes - 1),
            )
        self.assertEqual(list((failed_root / "packets").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
