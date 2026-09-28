import assert from "node:assert/strict";
import test from "node:test";

import {
  buildMissionReplay,
  findMissionFrame,
  latestMissionFrameForActor,
  missionActorName,
  projectMissionReplayFrame,
} from "../src/lib/missionReplay.ts";

function payload() {
  return {
    root_id: "codex:root",
    requested_id: "codex:root",
    actors: [
      {
        id: "codex:root",
        navigation_id: "codex:root",
        parent_navigation_id: null,
        root_navigation_id: "codex:root",
        harness: "codex",
        logical_harness: "codex",
        model: "gpt-root",
        effort: "high",
        project: "agentlog",
        started_at: "2026-09-26T10:00:00Z",
        ended_at: "2026-09-26T10:10:00Z",
        parent_session_id: null,
        message_count: 1,
        tool_count: 0,
        child_count: 1,
        descendant_count: 1,
        depth: 0,
      },
      {
        id: "codex:worker",
        navigation_id: "codex:worker",
        parent_navigation_id: "codex:root",
        root_navigation_id: "codex:root",
        harness: "codex",
        logical_harness: "codex",
        model: "gpt-worker",
        effort: "medium",
        project: "agentlog",
        started_at: "2026-09-26T10:01:00Z",
        ended_at: "2026-09-26T10:05:00Z",
        parent_session_id: "root",
        thread_source: "subagent",
        message_count: 1,
        tool_count: 1,
        child_count: 0,
        descendant_count: 0,
        relationship: "child",
        depth: 1,
      },
    ],
    edges: [{ source: "codex:root", target: "codex:worker", relationship: "child" }],
    events: [
      { kind: "lifecycle", phase: "spawn", id: "spawn:root", actor_id: "codex:root", timestamp: "2026-09-26T10:00:00Z", seq: -1 },
      { kind: "message", id: "root-human", actor_id: "codex:root", timestamp: "2026-09-26T10:00:05Z", seq: 1, role: "user", model: null, effort: null, text: "Run the mission", text_truncated: false, is_tool_plumbing: false, authored_by_agent: false, source_message_id: "root-human" },
      { kind: "lifecycle", phase: "spawn", id: "spawn:worker", actor_id: "codex:worker", timestamp: "2026-09-26T10:01:00Z", seq: -1 },
      { kind: "message", id: "worker-agent", actor_id: "codex:worker", timestamp: "2026-09-26T10:02:00Z", seq: 1, role: "assistant", model: "gpt-worker", effort: "medium", text: "Running tests", text_truncated: false, is_tool_plumbing: false, authored_by_agent: true, source_message_id: "worker-agent" },
      { kind: "tool", id: "worker-tool", actor_id: "codex:worker", timestamp: "2026-09-26T10:02:01Z", time_evidence: "message", seq: 2, tool_name: "run_tests", action: "pytest -q", action_truncated: false, context: "Running the focused tests", context_truncated: false, success: 1, duration_ms: 4200, operation_kind: "execute", model: "gpt-7-review", effort: "high", source_message_id: "worker-agent" },
      { kind: "lifecycle", phase: "complete", id: "complete:worker", actor_id: "codex:worker", timestamp: "2026-09-26T10:05:00Z", seq: 999 },
      { kind: "lifecycle", phase: "complete", id: "complete:root", actor_id: "codex:root", timestamp: "2026-09-26T10:10:00Z", seq: 999 },
    ],
    counts: { actors: 2, messages: 2, tools: 1, events: 7 },
    bounds: { max_nodes: 500, returned_node_count: 2, total_node_count: 2, nodes_truncated: false, max_events: 50000, returned_event_count: 7, total_event_count: 7, events_truncated: false },
    note: "fixture",
  };
}

test("mission replay keeps actors, lifecycle, tool evidence, and a useful default frame", () => {
  const replay = buildMissionReplay(payload());
  assert.equal(replay.actors.length, 2);
  assert.equal(replay.frames[4].tool, "run_tests");
  assert.equal(replay.frames[4].durationMs, 4200);
  assert.equal(replay.frames[4].outcome, "success");
  assert.equal(replay.recommendedFrameIndex, 4);
});

test("mission projection adds and removes familiars across recorded lifecycle", () => {
  const replay = buildMissionReplay(payload());
  const beforeSpawn = projectMissionReplayFrame(replay, 1);
  const duringTool = projectMissionReplayFrame(replay, 4);
  const afterReturn = projectMissionReplayFrame(replay, 6);

  assert.equal(beforeSpawn.operatorCount, 1);
  assert.equal(beforeSpawn.familiarCount, 0);
  assert.equal(duringTool.familiarCount, 1);
  const activeWorker = duringTool.actors.find((actor) => actor.kind === "familiar");
  assert.equal(activeWorker?.workstation, "diagnostics-bay");
  assert.equal(activeWorker?.model, "gpt-7-review");
  assert.equal(afterReturn.familiarCount, 0);
});

test("mission replay resolves exact cross-view frames and actor-local evidence", () => {
  const replay = buildMissionReplay(payload());
  assert.equal(findMissionFrame(replay, "worker-agent", "codex:worker"), 3);
  assert.equal(latestMissionFrameForActor(replay, "codex:worker", 4)?.id, "worker-tool");
});

test("guardian and missing-model workers keep role labels instead of unknown model labels", () => {
  const actor = {
    ...payload().actors[1],
    model: "(unknown)",
    agent_profile: "codex-auto-review",
    thread_source: "guardian_review",
  };

  assert.equal(missionActorName(actor), "Review familiar");
});
