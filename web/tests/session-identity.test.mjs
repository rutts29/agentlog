import assert from "node:assert/strict";
import test from "node:test";

import {
  authoritativeParentNavigationId,
  displaySessionIdentity,
} from "../src/lib/api.ts";

test("T3 session backed by Codex leads with its logical identity", () => {
  assert.equal(
    displaySessionIdentity({
      id: "codex:019ff411-db9b-71c3-9171-aca94105fa49",
      external_id: "019ff411-db9b-71c3-9171-aca94105fa49",
      harness: "codex",
      logical_harness: "t3code",
      runtime_harness: "codex",
    }),
    "t3code:019ff411-db9b-71c3-9171-aca94105fa49",
  );
});

test("recent-session projection derives logical identity without external_id", () => {
  assert.equal(
    displaySessionIdentity({
      id: "codex:019ff411-db9b-71c3-9171-aca94105fa49",
      harness: "t3code",
      runtime_harness: "codex",
    }),
    "t3code:019ff411-db9b-71c3-9171-aca94105fa49",
  );
});

test("already-logical and ordinary runtime identities stay unchanged", () => {
  assert.equal(
    displaySessionIdentity({
      id: "t3code:conversation-1",
      external_id: "conversation-1",
      harness: "t3code",
      logical_harness: "t3code",
      runtime_harness: "codex",
    }),
    "t3code:conversation-1",
  );
  assert.equal(
    displaySessionIdentity({
      id: "codex:conversation-2",
      external_id: "conversation-2",
      harness: "codex",
      logical_harness: "codex",
      runtime_harness: "codex",
    }),
    "codex:conversation-2",
  );
});

test("v2 navigation parent null rejects a foreign raw parent", () => {
  assert.equal(
    authoritativeParentNavigationId({
      parent_navigation_id: null,
      parent_session_id: "codex:foreign",
    }),
    null,
  );
  assert.equal(
    authoritativeParentNavigationId({ parent_session_id: "codex:legacy" }),
    "codex:legacy",
  );
});
