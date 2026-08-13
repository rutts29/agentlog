import assert from "node:assert/strict";
import test from "node:test";

import { fetchSearch } from "../src/lib/api.ts";

globalThis.window = { location: { origin: "http://127.0.0.1" } };

test("search forwards React Query cancellation to its API request", async (t) => {
  let requestSignal;
  t.mock.method(globalThis, "fetch", (_input, init) => {
    requestSignal = init.signal;
    return new Promise((_resolve, reject) => {
      requestSignal.addEventListener(
        "abort",
        () => reject(new DOMException("Aborted", "AbortError")),
        { once: true },
      );
    });
  });

  const controller = new AbortController();
  const request = fetchSearch("all", "payload", undefined, controller.signal);
  controller.abort();

  await assert.rejects(request, { name: "AbortError" });
  assert.equal(requestSignal.aborted, true);
});
