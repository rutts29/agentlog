import assert from "node:assert/strict";
import test from "node:test";
import { QueryClient, QueryObserver } from "@tanstack/react-query";

import {
  overviewGraphQueryOptions,
  overviewQueryOptions,
  queryContentState,
} from "../src/lib/overviewQueries.ts";

globalThis.window = { location: { origin: "http://127.0.0.1" } };

function response(body) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

test("overview core loads through one aggregate request", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (input) => {
    calls.push(new URL(String(input)).pathname);
    return response({ summary: { kpis: {} } });
  });

  const client = new QueryClient();
  await client.fetchQuery(overviewQueryOptions("7d"));

  assert.deepEqual(calls, ["/api/overview"]);
});

test("graph waits for core overview success", async (t) => {
  const calls = [];
  let releaseCore;
  const coreResponse = new Promise((resolve) => {
    releaseCore = () => resolve(response({ summary: { kpis: {} } }));
  });
  t.mock.method(globalThis, "fetch", (input) => {
    const path = new URL(String(input)).pathname;
    calls.push(path);
    return path === "/api/overview" ? coreResponse : Promise.resolve(response({}));
  });

  const client = new QueryClient();
  const graphObserver = new QueryObserver(
    client,
    overviewGraphQueryOptions("7d", false),
  );
  const unsubscribe = graphObserver.subscribe(() => undefined);
  const core = client.fetchQuery(overviewQueryOptions("7d"));

  await Promise.resolve();
  assert.deepEqual(calls, ["/api/overview"]);

  releaseCore();
  await core;
  graphObserver.setOptions(overviewGraphQueryOptions("7d", true));
  await Promise.resolve();

  assert.deepEqual(calls, ["/api/overview", "/api/graph"]);
  unsubscribe();
});

test("switching ranges aborts the obsolete overview request", async (t) => {
  const signals = [];
  t.mock.method(globalThis, "fetch", (_input, init) => {
    signals.push(init.signal);
    return new Promise((_resolve, reject) => {
      init.signal.addEventListener(
        "abort",
        () => reject(new DOMException("Aborted", "AbortError")),
        { once: true },
      );
    });
  });

  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const observer = new QueryObserver(client, overviewQueryOptions("24h"));
  const unsubscribe = observer.subscribe(() => undefined);

  observer.setOptions(overviewQueryOptions("7d"));
  await Promise.resolve();

  assert.equal(signals.length, 2);
  assert.equal(signals[0].aborted, true);
  assert.equal(signals[1].aborted, false);

  unsubscribe();
  assert.equal(signals[1].aborted, true);
});

test("switching ranges aborts the obsolete graph request", async (t) => {
  const requests = [];
  t.mock.method(globalThis, "fetch", (input, init) => {
    requests.push({ path: new URL(String(input)).pathname, signal: init.signal });
    return new Promise((_resolve, reject) => {
      init.signal.addEventListener(
        "abort",
        () => reject(new DOMException("Aborted", "AbortError")),
        { once: true },
      );
    });
  });

  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const observer = new QueryObserver(client, overviewGraphQueryOptions("24h"));
  const unsubscribe = observer.subscribe(() => undefined);

  observer.setOptions(overviewGraphQueryOptions("7d"));
  await Promise.resolve();

  assert.deepEqual(requests.map(({ path }) => path), ["/api/graph", "/api/graph"]);
  assert.equal(requests[0].signal.aborted, true);
  assert.equal(requests[1].signal.aborted, false);

  unsubscribe();
  assert.equal(requests[1].signal.aborted, true);
});

test("rejected panels render as errors instead of pending loaders", () => {
  assert.equal(queryContentState({ data: undefined, isError: false }), "pending");
  assert.equal(queryContentState({ data: undefined, isError: true }), "error");
  assert.equal(queryContentState({ data: { stale: true }, isError: true }), "ready");
});
