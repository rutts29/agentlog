from pathlib import Path
import unittest


WORKFLOW = Path(__file__).parents[1] / ".grok" / "workflows" / "owner-insights-review.rhai"


class OwnerInsightsWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = WORKFLOW.read_text(encoding="utf-8")

    def test_batch_workers_are_unbound_and_final_reviewer_binds_targets(self):
        self.assertIn("proposal_signals", self.script)
        self.assertIn("Do not emit target_id, target_kind, target_path, base_content_hash, proposed_content", self.script)
        self.assertIn("proposal_targets.json", self.script)
        self.assertIn("Only you may add target_id, target_kind, and proposed_content", self.script)
        discovery = self.script.split("let discover_prompt", 1)[1].split("let discovered", 1)[0]
        self.assertIn("Do not read proposal_targets.json or the facts template", discovery)

    def test_final_packet_matches_importer_contract_and_preserves_targets(self):
        self.assertIn("source:'owner_notes'", self.script)
        self.assertIn("owner_insight_targets:[{id,base_content_hash}", self.script)
        self.assertIn("Each item may cite multiple sessions using evidence", self.script)
        self.assertIn("proposal_key,title,action,target_id,target_kind,rationale,does_not_prove", self.script)
        self.assertIn("imported: false", self.script)
        self.assertIn("applied: false", self.script)

    def test_workflow_is_manual_and_does_not_schedule_or_apply(self):
        self.assertIn("never schedule from Agentlog", self.script)
        self.assertIn("run insights-import manually", self.script)
        self.assertNotIn("insights-import", self.script.split("synth_prompt +=", 1)[0])

    def test_review_fanout_reserves_synthesis_capacity_and_chunks_pending_work(self):
        self.assertIn("let run_budget = budget();", self.script)
        self.assertIn("let review_capacity = run_budget.remaining - 1;", self.script)
        self.assertIn("for entry in selected_pending", self.script)
        self.assertIn("let has_more_pending = pending.len() > selected_pending.len();", self.script)
        self.assertIn("status: \"review_chunk_complete\"", self.script)
        self.assertIn("synthesis starts only after every manifest batch is complete", self.script)
        chunk = self.script.split('if has_more_pending {', 2)[2].split('phase("Synthesize")', 1)[0]
        self.assertIn("complete(#{", chunk)
        self.assertNotIn("agent(synth_prompt", chunk)


if __name__ == "__main__":
    unittest.main()
