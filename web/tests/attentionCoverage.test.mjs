import assert from "node:assert/strict";
import test from "node:test";

import { attentionCoverageNote } from "../src/lib/attentionCoverage.ts";

test("attention coverage note appears only for incomplete indexed signals", () => {
  assert.equal(
    attentionCoverageNote({
      eligible_sessions: 3,
      covered_sessions: 2,
      missing_sessions: 1,
      ignored_sessions: 1,
      complete: false,
    }),
    "partial source signals · 2/3 indexed · 1 unverified",
  );
  assert.equal(
    attentionCoverageNote({
      eligible_sessions: 3,
      covered_sessions: 3,
      missing_sessions: 0,
      ignored_sessions: 1,
      complete: true,
    }),
    "partial source signals · 3/3 indexed · 1 unverified",
  );
  assert.equal(
    attentionCoverageNote({
      eligible_sessions: 3,
      covered_sessions: 3,
      missing_sessions: 0,
      ignored_sessions: 0,
      complete: true,
    }),
    null,
  );
});
