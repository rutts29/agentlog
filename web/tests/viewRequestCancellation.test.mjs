import assert from "node:assert/strict";
import test from "node:test";

import {
  fetchAdjudicationQueue,
  fetchAdjudicationReport,
  fetchAdjudicationTaxonomy,
  fetchInsights,
  fetchProposals,
} from "../src/lib/api.ts";

globalThis.window = { location: { origin: "http://127.0.0.1" } };

test("analysis view requests preserve the cancellation signal", async (t) => {
  const signals = [];
  t.mock.method(globalThis, "fetch", async (_input, init) => {
    signals.push(init.signal);
    return new Response(JSON.stringify({ items: [], counts_by_status: {} }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });

  const controller = new AbortController();
  await Promise.all([
    fetchInsights("7d", controller.signal),
    fetchProposals("pending", controller.signal),
    fetchAdjudicationQueue(false, controller.signal),
    fetchAdjudicationTaxonomy(controller.signal),
    fetchAdjudicationReport(controller.signal),
  ]);

  assert.equal(signals.length, 5);
  assert.ok(signals.every((signal) => signal === controller.signal));
});
