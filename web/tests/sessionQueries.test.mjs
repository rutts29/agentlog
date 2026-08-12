import assert from "node:assert/strict";
import test from "node:test";
import { QueryClient, QueryObserver } from "@tanstack/react-query";

import {
  SESSION_DETAIL_GC_TIME,
  SESSION_DETAIL_STALE_TIME,
  SESSION_TREE_GC_TIME,
  SESSION_TREE_STALE_TIME,
  canPrefetchSessionDetail,
  createSessionRangeWarmer,
  invalidateSessionDetailCache,
  refreshActiveSessionQueries,
  sessionDetailQueryKey,
  sessionDetailQueryOptions,
  sessionTreeQueryKey,
  sessionTreeQueryOptions,
} from "../src/lib/sessionQueries.ts";
import { combineAbortSignals } from "../src/lib/api.ts";

test("session detail prefetch and view share one durable query contract", () => {
  const options = sessionDetailQueryOptions("t3code:root");

  assert.deepEqual(sessionDetailQueryKey("t3code:root"), [
    "session",
    "t3code:root",
  ]);
  assert.deepEqual(options.queryKey, ["session", "t3code:root"]);
  assert.equal(options.enabled, true);
  assert.equal(options.staleTime, SESSION_DETAIL_STALE_TIME);
  assert.equal(options.gcTime, SESSION_DETAIL_GC_TIME);
  assert.ok(SESSION_DETAIL_STALE_TIME > 30_000);
  assert.ok(SESSION_DETAIL_GC_TIME > SESSION_DETAIL_STALE_TIME);
});

test("empty route identity cannot run a detail query", () => {
  assert.equal(sessionDetailQueryOptions("").enabled, false);
});

test("session tree views share one bounded query contract", () => {
  const options = sessionTreeQueryOptions("t3code:root");

  assert.deepEqual(sessionTreeQueryKey("t3code:root"), [
    "session-tree",
    "t3code:root",
  ]);
  assert.deepEqual(options.queryKey, ["session-tree", "t3code:root"]);
  assert.equal(options.enabled, true);
  assert.equal(options.staleTime, SESSION_TREE_STALE_TIME);
  assert.equal(options.gcTime, SESSION_TREE_GC_TIME);
});

test("detail intent never prefetches an unstable source snapshot", () => {
  assert.equal(canPrefetchSessionDetail("pending"), false);
  assert.equal(canPrefetchSessionDetail("stable"), true);
  assert.equal(canPrefetchSessionDetail(undefined), true);
});

test("combined range request signal aborts when either owner aborts", () => {
  for (const owner of ["warmer", "react-query"]) {
    const warmer = new AbortController();
    const reactQuery = new AbortController();
    const combined = combineAbortSignals(warmer.signal, reactQuery.signal);
    assert.ok(combined.signal);
    if (owner === "warmer") warmer.abort();
    else reactQuery.abort();
    assert.equal(combined.signal.aborted, true);
    combined.cleanup();
  }

  const first = new AbortController();
  const second = new AbortController();
  const cleaned = combineAbortSignals(first.signal, second.signal);
  cleaned.cleanup();
  first.abort();
  second.abort();
  assert.equal(cleaned.signal.aborted, false);
});

test("ingest marks every cached session detail stale without refetching", async () => {
  const queryClient = new QueryClient();
  const first = sessionDetailQueryKey("t3code:first");
  const second = sessionDetailQueryKey("codex:second");
  queryClient.setQueryData(first, { revision: 1 });
  queryClient.setQueryData(second, { revision: 1 });
  queryClient.setQueryData(["sessions", "24h"], { revision: 1 });

  await invalidateSessionDetailCache(queryClient);

  assert.equal(queryClient.getQueryState(first)?.isInvalidated, true);
  assert.equal(queryClient.getQueryState(second)?.isInvalidated, true);
  assert.equal(
    queryClient.getQueryState(["sessions", "24h"])?.isInvalidated,
    false,
  );
});

test("ingest refetches the exact active transcript and branch tree", async () => {
  const queryClient = new QueryClient();
  const sessionId = "t3code:active";
  let detailFetches = 0;
  let treeFetches = 0;
  const detailOptions = {
    queryKey: sessionDetailQueryKey(sessionId),
    queryFn: async () => ({ revision: ++detailFetches }),
    staleTime: Infinity,
  };
  const treeOptions = {
    queryKey: sessionTreeQueryKey(sessionId),
    queryFn: async () => ({ revision: ++treeFetches }),
    staleTime: Infinity,
  };
  await Promise.all([
    queryClient.fetchQuery(detailOptions),
    queryClient.fetchQuery(treeOptions),
  ]);
  queryClient.setQueryData(sessionTreeQueryKey("t3code:other"), { revision: 1 });
  const detailObserver = new QueryObserver(queryClient, detailOptions);
  const treeObserver = new QueryObserver(queryClient, treeOptions);
  const unsubscribeDetail = detailObserver.subscribe(() => undefined);
  const unsubscribeTree = treeObserver.subscribe(() => undefined);

  try {
    await refreshActiveSessionQueries(queryClient, sessionId);
    assert.equal(detailFetches, 2);
    assert.equal(treeFetches, 2);
    assert.equal(
      queryClient.getQueryState(sessionTreeQueryKey("t3code:other"))
        ?.isInvalidated,
      false,
    );
  } finally {
    unsubscribeDetail();
    unsubscribeTree();
  }
});

class FakeClock {
  nowValue = 0;
  nextId = 1;
  timers = new Map();

  now = () => this.nowValue;

  setTimeout = (callback, delay) => {
    const id = this.nextId++;
    this.timers.set(id, { at: this.nowValue + delay, callback });
    return id;
  };

  clearTimeout = (id) => this.timers.delete(id);

  async advance(ms) {
    this.nowValue += ms;
    const due = [...this.timers.entries()]
      .filter(([, timer]) => timer.at <= this.nowValue)
      .sort(([, left], [, right]) => left.at - right.at);
    for (const [id, timer] of due) {
      this.timers.delete(id);
      timer.callback();
      await Promise.resolve();
    }
  }
}

test("range warming waits for quiet time and aborts on ingest activity", async () => {
  const clock = new FakeClock();
  const warmer = createSessionRangeWarmer({
    now: clock.now,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  const signals = [];
  let release;
  const running = new Promise((resolve) => {
    release = resolve;
  });

  warmer.schedule("epoch-1", async (signal) => {
    signals.push(signal);
    await running;
  });
  await clock.advance(749);
  assert.equal(signals.length, 0);
  await clock.advance(1);
  assert.equal(signals.length, 1);

  warmer.notifyActivity();
  assert.equal(signals[0].aborted, true);
  release();
  warmer.schedule("epoch-1", async (signal) => signals.push(signal));
  await clock.advance(749);
  assert.equal(signals.length, 1);
  await clock.advance(1);
  assert.equal(signals.length, 2);
});

test("range warming switches epochs without retaining an old timer", async () => {
  const clock = new FakeClock();
  const warmer = createSessionRangeWarmer({
    now: clock.now,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  const started = [];

  warmer.schedule("old", async () => started.push("old"));
  warmer.schedule("new", async () => started.push("new"));
  await clock.advance(750);
  assert.deepEqual(started, ["new"]);
});

test("range warming cancel leaves no work after unmount", async () => {
  const clock = new FakeClock();
  const warmer = createSessionRangeWarmer({
    now: clock.now,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  let started = 0;

  warmer.schedule("epoch-1", async () => {
    started += 1;
  });
  warmer.cancel();
  await clock.advance(2_000);
  assert.equal(started, 0);
});
