import assert from "node:assert/strict";
import test from "node:test";

import { buildCommandReplay, projectReplayFrame } from "../src/lib/commandReplay.ts";

function detail() {
  return {
    session: {
      id: "codex:root-1",
      harness: "codex",
      logical_harness: "codex",
      model: "gpt-test",
      effort: "high",
      project: "agentlog",
      repo: "agentlog",
      cwd: "/project",
      branch: "main",
      commit_sha: null,
      started_at: "2026-09-26T10:00:00Z",
      ended_at: "2026-09-26T10:01:00Z",
      duration_seconds: 60,
      parent_session_id: null,
      artifact_id: 1,
      artifact_path: "/sessions/root-1.jsonl",
      external_id: "root-1",
      source: { status: "ready" },
    },
    timeline: [
      {
        kind: "message",
        id: "human-1",
        seq: 1,
        role: "user",
        timestamp: "2026-09-26T10:00:01Z",
        model: null,
        effort: null,
        text: "Inspect the project",
        is_tool_plumbing: false,
        authored_by_agent: false,
        tool_events: [],
      },
      {
        kind: "message",
        id: "agent-1",
        seq: 2,
        role: "assistant",
        timestamp: "2026-09-26T10:00:02Z",
        model: "gpt-test",
        effort: "high",
        text: "I will inspect it.",
        is_tool_plumbing: false,
        authored_by_agent: true,
        tool_events: [
          {
            id: "tool-1",
            message_id: "agent-1",
            seq: 3,
            tool_name: "read_file",
            action: "src/app.ts",
            success: 1,
            duration_ms: 420,
          },
          {
            id: "tool-2",
            message_id: "agent-1",
            seq: 4,
            tool_name: "run_tests",
            action: "npm test",
            success: 0,
            duration_ms: 3100,
          },
        ],
      },
    ],
    skills: [],
    children: [],
    anatomy: { message_count: 2, tool_count: 2, window_count: 1, child_count: 0 },
  };
}

test("replay preserves human, agent, tool, and completion order", () => {
  const replay = buildCommandReplay(detail());
  assert.deepEqual(replay.frames.map((frame) => frame.kind), [
    "human",
    "agent",
    "tool",
    "tool",
    "complete",
  ]);
  assert.deepEqual(replay.frames.map((frame) => frame.index), [0, 1, 2, 3, 4]);
});

test("replay retains tool outcome, duration, and source message", () => {
  const replay = buildCommandReplay(detail());
  const failed = replay.frames[3];
  assert.equal(failed.title, "run_tests");
  assert.equal(failed.detail, "npm test · 3.1s");
  assert.equal(failed.outcome, "failure");
  assert.equal(failed.messageId, "agent-1");
});

test("replay projection keeps identity stable while stations change", () => {
  const payload = detail();
  const replay = buildCommandReplay(payload);
  const humanActor = projectReplayFrame(payload, replay.frames[0]).actors[0];
  const readActor = projectReplayFrame(payload, replay.frames[2]).actors[0];
  const testActor = projectReplayFrame(payload, replay.frames[3]).actors[0];

  assert.equal(humanActor.id, readActor.id);
  assert.equal(humanActor.callsign, readActor.callsign);
  assert.equal(readActor.workstation, "archive-console");
  assert.equal(testActor.workstation, "diagnostics-bay");
});

test("completion is explicit and source readiness survives replay", () => {
  const payload = detail();
  const replay = buildCommandReplay(payload);
  const finalProjection = projectReplayFrame(payload, replay.frames.at(-1)).actors[0];
  assert.equal(finalProjection.state.kind, "idle");
  assert.equal(finalProjection.sourceSnapshotStatus, "stable");
});
