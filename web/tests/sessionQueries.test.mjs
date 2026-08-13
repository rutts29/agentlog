import assert from "node:assert/strict";
import test from "node:test";
import { QueryClient, QueryObserver } from "@tanstack/react-query";

import {
  SESSION_DETAIL_GC_TIME,
  SESSION_DETAIL_STALE_TIME,
  SESSION_TREE_GC_TIME,
  SESSION_TREE_STALE_TIME,
  canPrefetchSessionDetail,
  createMatchingPresenceRefreshGate,
  createPresenceVersionGate,
  createSessionPresenceRefreshScheduler,
  createSessionRangeWarmer,
  invalidateSessionDetailCache,
  presenceMatchesSessionDetail,
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

test("detail prefetch keeps only hot stable source-backed transcripts", () => {
  const now = () => Date.parse("2026-08-12T12:00:00.000Z");
  const sourceBacked = (timestamps) => ({
    transcript_storage: "source_backed",
    ...timestamps,
  });

  assert.equal(
    canPrefetchSessionDetail(
      "stable",
      sourceBacked({ activity_at: "2026-08-05T12:00:00.000Z" }),
      now,
    ),
    true,
  );
  assert.equal(
    canPrefetchSessionDetail(
      "stable",
      sourceBacked({ activity_at: "2026-08-05T11:59:59.999Z" }),
      now,
    ),
    false,
  );
  assert.equal(
    canPrefetchSessionDetail(
      "stable",
      sourceBacked({
        activity_at: "not-a-date",
        ended_at: "2026-08-12T11:00:00.000Z",
      }),
      now,
    ),
    true,
  );
  assert.equal(
    canPrefetchSessionDetail(
      "stable",
      sourceBacked({ started_at: "2026-08-12T11:00:00.000Z" }),
      now,
    ),
    true,
  );
  assert.equal(
    canPrefetchSessionDetail("stable", sourceBacked({}), now),
    false,
  );
  assert.equal(
    canPrefetchSessionDetail(
      "stable",
      { transcript_storage: "legacy_materialized", started_at: "2020-01-01T00:00:00.000Z" },
      now,
    ),
    true,
  );
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

test("presence marks every cached detail, including closed sessions, stale without refetching", async () => {
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

test("equal-generation presence heartbeats do not repeat session cache invalidation", async () => {
  const queryClient = new QueryClient();
  const key = sessionDetailQueryKey("t3code:closed");
  const gate = createPresenceVersionGate();
  const frame = (generation, ts) => ({
    epoch: "watcher-a",
    generation,
    ts,
  });
  queryClient.setQueryData(key, { revision: 1 });

  if (gate.accept(frame(4, "2026-08-13T10:00:00Z"))) {
    await invalidateSessionDetailCache(queryClient);
  }
  assert.equal(queryClient.getQueryState(key)?.isInvalidated, true);
  queryClient.setQueryData(key, { revision: 2 });
  assert.equal(queryClient.getQueryState(key)?.isInvalidated, false);

  if (gate.accept(frame(4, "2026-08-13T10:00:15Z"))) {
    await invalidateSessionDetailCache(queryClient);
  }
  assert.equal(queryClient.getQueryState(key)?.isInvalidated, false);

  if (gate.accept(frame(5, "2026-08-13T10:00:16Z"))) {
    await invalidateSessionDetailCache(queryClient);
  }
  assert.equal(queryClient.getQueryState(key)?.isInvalidated, true);
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

test("a relevant presence source refreshes the warm detail before ingest", async () => {
  const clock = new FakeClock();
  const queryClient = new QueryClient();
  const sessionId = "t3code:logical-a";
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
  const unsubscribeDetail = new QueryObserver(queryClient, detailOptions).subscribe(() => undefined);
  const unsubscribeTree = new QueryObserver(queryClient, treeOptions).subscribe(() => undefined);
  const detail = {
    id: sessionId,
    harness: "t3code",
    runtime_harness: "codex",
    external_id: "source-a",
    transcript_session_id: "codex:source-a",
  };
  const scheduler = createSessionPresenceRefreshScheduler({
    refresh: () => refreshActiveSessionQueries(queryClient, sessionId),
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  try {
    const relevant = {
      harness: "codex",
      external_id: "source-a",
      session_id: null,
      source_path: "/tmp/source-a.jsonl",
      state: "streaming",
      last_activity_at: null,
      age_seconds: 0,
      pending_ingest: true,
      title: null,
      repo: null,
    };
    assert.equal(presenceMatchesSessionDetail(relevant, detail), true);
    scheduler.schedule();
    await clock.advance(299);
    assert.equal(detailFetches, 1);
    await clock.advance(1);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(detailFetches, 2);
    assert.equal(treeFetches, 2);
  } finally {
    scheduler.cancel();
    unsubscribeDetail();
    unsubscribeTree();
  }
});

test("unrelated presence does not refetch an open session", async () => {
  const clock = new FakeClock();
  const detail = {
    id: "t3code:logical-a",
    harness: "t3code",
    runtime_harness: "codex",
    external_id: "source-a",
  };
  let refreshes = 0;
  const scheduler = createSessionPresenceRefreshScheduler({
    refresh: () => { refreshes += 1; },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  const unrelated = {
    harness: "codex",
    external_id: "worker-b",
    session_id: "codex:worker-b",
    logical_session_id: "t3code:logical-b",
    source_path: "/tmp/worker-b.jsonl",
    state: "tool_running",
    last_activity_at: null,
    age_seconds: 0,
    pending_ingest: true,
    title: null,
    repo: null,
  };

  if (presenceMatchesSessionDetail(unrelated, detail)) scheduler.schedule();
  await clock.advance(1_000);
  assert.equal(refreshes, 0);
});

test("related presence frames coalesce into one detail refresh", async () => {
  const clock = new FakeClock();
  let refreshes = 0;
  const scheduler = createSessionPresenceRefreshScheduler({
    refresh: () => { refreshes += 1; },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  scheduler.schedule();
  await clock.advance(200);
  scheduler.schedule();
  await clock.advance(200);
  scheduler.schedule();
  await clock.advance(299);
  assert.equal(refreshes, 0);
  await clock.advance(1);
  assert.equal(refreshes, 1);
});

test("matching detail refresh ignores heartbeats and unrelated generation changes", () => {
  const gate = createMatchingPresenceRefreshGate();
  const detail = {
    id: "t3code:logical-a",
    harness: "t3code",
    runtime_harness: "codex",
    external_id: "source-a",
  };
  const relevant = (lastActivityAt) => ({
    harness: "codex",
    external_id: "source-a",
    session_id: "codex:source-a",
    logical_session_id: "t3code:logical-a",
    source_path: "/tmp/source-a.jsonl",
    state: "streaming",
    last_activity_at: lastActivityAt,
    age_seconds: 0,
    pending_ingest: true,
    title: null,
    repo: null,
  });
  const frame = (generation, ts, sessions) => ({
    epoch: "watcher-a",
    generation,
    ts,
    sessions,
    transitions: [],
  });

  assert.equal(
    gate.accept(frame(1, "2026-08-13T10:00:00Z", [relevant("2026-08-13T10:00:00Z")]), detail),
    true,
  );
  assert.equal(
    gate.accept(frame(1, "2026-08-13T10:00:15Z", [relevant("2026-08-13T10:00:00Z")]), detail),
    false,
  );
  assert.equal(
    gate.accept(frame(2, "2026-08-13T10:00:16Z", [relevant("2026-08-13T10:00:00Z")]), detail),
    false,
  );
  assert.equal(
    gate.accept(frame(3, "2026-08-13T10:00:17Z", [relevant("2026-08-13T10:00:17Z")]), detail),
    true,
  );
});

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

test("range warming retries the same key after a rejected fill", async () => {
  const clock = new FakeClock();
  const warmer = createSessionRangeWarmer({
    now: clock.now,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  let attempts = 0;

  warmer.schedule("epoch-1", async () => {
    attempts += 1;
    throw new Error("temporary warm failure");
  });
  await clock.advance(750);
  await new Promise((resolve) => setImmediate(resolve));

  warmer.schedule("epoch-1", async () => {
    attempts += 1;
  });
  await clock.advance(750);

  assert.equal(attempts, 2);
});

test("range warming retries a failed QueryClient fill for the same key", async () => {
  const clock = new FakeClock();
  const warmer = createSessionRangeWarmer({
    now: clock.now,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const key = ["sessions", "7d"];
  let fetches = 0;
  const fill = () => queryClient.fetchQuery({
    queryKey: key,
    queryFn: async () => {
      fetches += 1;
      if (fetches === 1) throw new Error("temporary warm failure");
      return { warmed: true };
    },
  });

  warmer.schedule("epoch-1", async () => {
    await fill();
  });
  await clock.advance(750);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(queryClient.getQueryState(key)?.status, "error");

  warmer.schedule("epoch-1", async () => {
    await fill();
  });
  await clock.advance(750);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(fetches, 2);
  assert.deepEqual(queryClient.getQueryData(key), { warmed: true });
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
