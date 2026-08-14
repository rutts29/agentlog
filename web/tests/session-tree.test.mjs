import assert from "node:assert/strict";
import test from "node:test";

import {
  findBranchPath,
  isWorkerTreeNode,
  projectBranchTree,
  sessionTreeLabel,
} from "../src/lib/sessionTree.ts";

function node(id, children = [], descendantCount = children.length) {
  return {
    id,
    harness: "codex",
    model: "gpt-5.6-sol",
    effort: null,
    project: "test",
    started_at: null,
    ended_at: null,
    message_count: 0,
    tool_count: 0,
    descendant_count: descendantCount,
    children,
  };
}

test("worker tree nodes include workflow and provider workers but not ordinary branches", () => {
  assert.equal(isWorkerTreeNode({ thread_source: "subagent", relationship: null }), true);
  assert.equal(isWorkerTreeNode({ thread_source: "workflow_subagent", relationship: null }), true);
  assert.equal(isWorkerTreeNode({ thread_source: null, relationship: "provider_worker" }), true);
  assert.equal(isWorkerTreeNode({ thread_source: null, relationship: "child" }), false);
});

test("tree labels distinguish autonomous roots from main roots and workers", () => {
  assert.equal(sessionTreeLabel({ thread_source: "autonomous_agent_unlinked", relationship: null }, 0), "Agent run");
  assert.equal(sessionTreeLabel({ thread_source: null, relationship: null }, 0), "Main");
  assert.equal(sessionTreeLabel({ thread_source: "subagent", relationship: null }, 1), "Worker");
  assert.equal(sessionTreeLabel({ thread_source: "workflow_subagent", relationship: null }, 1), "Worker");
  assert.equal(sessionTreeLabel({ thread_source: null, relationship: "provider_worker" }, 1), "Worker");
  assert.equal(sessionTreeLabel({ thread_source: null, relationship: "child" }, 1), "Branch");
});

test("deep branch projection stops at the client depth budget", () => {
  let root = node("deep-79");
  for (let depth = 78; depth >= 0; depth -= 1) {
    root = node(`deep-${depth}`, [root], 79 - depth);
  }

  const projection = projectBranchTree(root);
  assert.equal(projection.rows.length, 65);
  assert.equal(projection.omittedNodeCount, 15);
  assert.equal(projection.truncated, true);
  assert.equal(
    findBranchPath(projection.rows, new Set(["deep-64"]))?.length,
    65,
  );
  assert.equal(findBranchPath(projection.rows, new Set(["deep-79"])), null);
});

test("wide branch projection stops at the client node budget", () => {
  const children = Array.from({ length: 600 }, (_, index) => node(`wide-${index}`));
  const projection = projectBranchTree(node("root", children, 600));
  assert.equal(projection.rows.length, 500);
  assert.equal(projection.omittedNodeCount, 101);
  assert.equal(projection.truncated, true);
});
