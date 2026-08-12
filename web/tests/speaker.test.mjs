import assert from "node:assert/strict";
import test from "node:test";

import { classifySpeaker } from "../src/lib/speaker.ts";

const collaborationBrief = `Message Type: NEW_TASK
Task name: /root/worker
Sender: /root
Payload:
Inspect the session parser.`;

test("agent-authored collaboration payload is a child brief", () => {
  const speaker = classifySpeaker(
    { role: "user", text: collaborationBrief, authored_by_agent: true },
    { isChildSession: true },
  );
  assert.equal(speaker.kind, "worker_brief");
  assert.equal(speaker.label, "brief");
});

test("the same unclassified human text remains human", () => {
  const speaker = classifySpeaker(
    { role: "user", text: collaborationBrief, authored_by_agent: false },
    { isChildSession: true },
  );
  assert.equal(speaker.kind, "human");
});

test("authored plain user text remains human", () => {
  const speaker = classifySpeaker({
    role: "user",
    text: "Please keep the migration plan concise.",
    authored_by_agent: true,
  });
  assert.equal(speaker.kind, "human");
  assert.equal(speaker.label, "human");
});

test("unauthored plain user text remains human", () => {
  const speaker = classifySpeaker({
    role: "user",
    text: "Please keep the migration plan concise.",
    authored_by_agent: false,
  });
  assert.equal(speaker.kind, "human");
  assert.equal(speaker.label, "human");
});

test("recognized runtime context is labeled context", () => {
  const speaker = classifySpeaker({
    role: "user",
    text: "<recommended_plugins>\n- browser\n</recommended_plugins>",
    authored_by_agent: true,
  });
  assert.equal(speaker.kind, "synthetic");
  assert.equal(speaker.label, "context");
});

test("human-pasted runtime context remains human", () => {
  const speaker = classifySpeaker({
    role: "user",
    text: "<recommended_plugins>\n- browser\n</recommended_plugins>",
    authored_by_agent: false,
  });
  assert.equal(speaker.kind, "human");
  assert.equal(speaker.label, "human");
});

test("synthetic request kinds are labeled context", () => {
  const speaker = classifySpeaker({
    role: "user",
    text: "<task_notification>worker completed</task_notification>",
    request_kind: "task_notification",
  });
  assert.equal(speaker.kind, "synthetic");
  assert.equal(speaker.label, "context");
});

test("developer messages are system", () => {
  const speaker = classifySpeaker({
    role: "developer",
    text: "Follow the repository policy.",
  });
  assert.equal(speaker.kind, "system");
  assert.equal(speaker.label, "system");
});
