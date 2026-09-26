import assert from "node:assert/strict";
import test from "node:test";

import { projectCommandFloor } from "../src/lib/commandFloor.ts";

function live(overrides = {}) {
  return {
    harness: "codex",
    harness_display: "Codex",
    external_id: "root-1",
    session_id: "codex:root-1",
    source_path: "/sessions/root-1.jsonl",
    state: "thinking",
    last_activity_at: "2026-09-26T12:00:00Z",
    age_seconds: 2,
    pending_ingest: false,
    source_snapshot_status: "stable",
    title: "Plan the command grid",
    repo: "agentlog",
    role: "session",
    label: "Planning command grid",
    project: "agentlog",
    working: true,
    ...overrides,
  };
}

test("projects operators and attached familiars from one presence snapshot", () => {
  const projection = projectCommandFloor([
    live({ state: "orchestrating", worker_count: 1 }),
    live({
      external_id: "worker-1",
      session_id: "codex:worker-1",
      source_path: "/sessions/subagents/worker-1.jsonl",
      state: "tool_running",
      role: "worker",
      parent_session_id: "codex:root-1",
      tool: "read_file",
      activity: "reading files",
    }),
  ]);

  assert.equal(projection.operatorCount, 1);
  assert.equal(projection.familiarCount, 1);
  assert.equal(projection.workingCount, 2);
  assert.equal(projection.rooms.length, 1);

  const operator = projection.byId.get("codex:root-1");
  const familiar = projection.byId.get("codex:worker-1");
  assert.equal(operator?.kind, "operator");
  assert.deepEqual(operator?.childIds, ["codex:worker-1"]);
  assert.equal(familiar?.kind, "familiar");
  if (familiar?.kind !== "familiar") assert.fail("worker must project to familiar");
  assert.deepEqual(familiar.lineage, { kind: "attached", parentId: "codex:root-1" });
  assert.equal(familiar.specialty, "researcher");
  assert.equal(familiar.specialtyEvidence, "observed");
  assert.equal(familiar.familiar, "archive-owl");
  assert.equal(familiar.workstation, "archive-console");
});

test("missing worker lineage remains explicitly unattached", () => {
  const projection = projectCommandFloor([
    live({
      external_id: "worker-orphan",
      session_id: null,
      source_path: "/sessions/subagents/worker-orphan.jsonl",
      role: "worker",
      parent_external_id: "missing-parent",
    }),
  ]);

  assert.equal(projection.unattachedFamiliarCount, 1);
  const actor = projection.actors[0];
  assert.equal(actor.kind, "familiar");
  if (actor.kind !== "familiar") assert.fail("worker must project to familiar");
  assert.deepEqual(actor.lineage, { kind: "unattached", parentHint: "missing-parent" });
});

test("generic exec does not invent a systems specialty", () => {
  const projection = projectCommandFloor([
    live({ state: "tool_running", tool: "exec", activity: "running a tool" }),
  ]);
  const actor = projection.actors[0];

  assert.equal(actor.specialty, "operator");
  assert.equal(actor.specialtyEvidence, "unknown");
  assert.equal(actor.state.kind, "working");
  assert.equal(actor.workstation, "server-rack");
});

test("waiting actors resolve to the human gate", () => {
  const projection = projectCommandFloor([
    live({ state: "waiting", activity: "waiting on you", working: false }),
  ]);
  const actor = projection.actors[0];

  assert.deepEqual(actor.state, {
    kind: "waiting",
    reason: "human-input",
    label: "waiting on you",
  });
  assert.equal(actor.workstation, "human-gate");
  assert.equal(projection.waitingCount, 1);
});

test("identity and callsign are stable across activity changes", () => {
  const planning = projectCommandFloor([live({ state: "thinking" })]).actors[0];
  const building = projectCommandFloor([
    live({ state: "tool_running", tool: "apply_patch" }),
  ]).actors[0];

  assert.equal(planning.id, building.id);
  assert.equal(planning.callsign, building.callsign);
  assert.equal(building.specialty, "builder");
  assert.equal(building.workstation, "fabrication-bay");
});

test("unknown source and activity stay unknown", () => {
  const projection = projectCommandFloor([
    live({ state: "new-provider-state", source_snapshot_status: "other", tool: null }),
  ]);
  const actor = projection.actors[0];

  assert.equal(actor.state.kind, "idle");
  assert.equal(actor.sourceSnapshotStatus, "unknown");
});

test("separate root sessions in the same project get separate mission rooms", () => {
  const projection = projectCommandFloor([
    live({ session_id: "codex:root-1", external_id: "root-1" }),
    live({ session_id: "codex:root-2", external_id: "root-2" }),
  ]);

  assert.equal(projection.rooms.length, 2);
  assert.deepEqual(
    projection.rooms.map((room) => room.rootId).sort(),
    ["codex:root-1", "codex:root-2"],
  );
});
