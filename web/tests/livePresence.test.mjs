import assert from "node:assert/strict";
import test from "node:test";

import {
  acceptsPresenceVersion,
  LIVE_FALLBACK_POLL_MS,
  createPresenceSyncScheduler,
} from "../src/lib/useLivePresence.ts";

test("older HTTP presence cannot overwrite a newer SSE generation", () => {
  assert.equal(
    acceptsPresenceVersion(
      { epoch: "daemon-a", generation: 10, ts: "2026-08-12T12:00:10Z" },
      { epoch: "daemon-a", generation: 9, ts: "2026-08-12T12:00:20Z" },
    ),
    false,
  );
  assert.equal(
    acceptsPresenceVersion(
      { epoch: "daemon-a", generation: 10, ts: "2026-08-12T12:00:10Z" },
      { epoch: "daemon-a", generation: 10, ts: "2026-08-12T12:00:11Z" },
    ),
    true,
  );
});

test("new daemon epoch may restart generation from zero", () => {
  assert.equal(
    acceptsPresenceVersion(
      { epoch: "daemon-a", generation: 99, ts: "2026-08-12T12:00:10Z" },
      { epoch: "daemon-b", generation: 0, ts: "2026-08-12T12:00:11Z" },
    ),
    true,
  );
  assert.equal(
    acceptsPresenceVersion(
      { epoch: "daemon-b", generation: 0, ts: "2026-08-12T12:00:11Z" },
      { epoch: "daemon-a", generation: 99, ts: "2026-08-12T12:00:10Z" },
    ),
    false,
  );
});

class FakeClock {
  now = 0;
  nextId = 1;
  timers = new Map();

  setTimeout = (callback, delay) => {
    const id = this.nextId++;
    this.timers.set(id, { at: this.now + delay, callback });
    return id;
  };

  clearTimeout = (id) => this.timers.delete(id);

  async advance(ms) {
    this.now += ms;
    while (true) {
      const due = [...this.timers.entries()]
        .filter(([, timer]) => timer.at <= this.now)
        .sort(([, left], [, right]) => left.at - right.at);
      if (due.length === 0) return;
      for (const [id, timer] of due) {
        this.timers.delete(id);
        timer.callback();
        await Promise.resolve();
      }
    }
  }
}

test("connected presence uses SSE without a recurring live pull", async () => {
  const clock = new FakeClock();
  let pulls = 0;
  const scheduler = createPresenceSyncScheduler({
    streamConnected: true,
    pull: () => {
      pulls += 1;
    },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  scheduler.start();
  await clock.advance(LIVE_FALLBACK_POLL_MS * 4);

  assert.equal(pulls, 1);
  assert.equal(clock.timers.size, 0);
  scheduler.stop();
});

test("disconnected presence recovers on a bounded fallback cadence", async () => {
  const clock = new FakeClock();
  let pulls = 0;
  const scheduler = createPresenceSyncScheduler({
    streamConnected: false,
    pull: () => {
      pulls += 1;
    },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  scheduler.start();
  assert.equal(pulls, 1);
  await clock.advance(LIVE_FALLBACK_POLL_MS - 1);
  assert.equal(pulls, 1);
  await clock.advance(1);
  assert.equal(pulls, 2);
  await clock.advance(LIVE_FALLBACK_POLL_MS);
  assert.equal(pulls, 3);

  scheduler.stop();
  await clock.advance(LIVE_FALLBACK_POLL_MS * 2);
  assert.equal(pulls, 3);
});

test("reconnecting cancels fallback and reconciles once", async () => {
  const clock = new FakeClock();
  let pulls = 0;
  const scheduler = createPresenceSyncScheduler({
    streamConnected: false,
    pull: () => {
      pulls += 1;
    },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  scheduler.start();
  scheduler.setConnected(true);
  await Promise.resolve();
  await clock.advance(LIVE_FALLBACK_POLL_MS * 2);

  assert.equal(pulls, 2);
  assert.equal(clock.timers.size, 0);
  scheduler.stop();
});

test("stale watcher enables fallback even while SSE is connected", async () => {
  const clock = new FakeClock();
  let pulls = 0;
  const scheduler = createPresenceSyncScheduler({
    streamConnected: true,
    pull: () => {
      pulls += 1;
    },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  scheduler.start();
  scheduler.setRecoveryRequired(true);
  await clock.advance(LIVE_FALLBACK_POLL_MS);
  assert.equal(pulls, 2);

  scheduler.setRecoveryRequired(false);
  await clock.advance(LIVE_FALLBACK_POLL_MS * 2);
  assert.equal(pulls, 2);
  scheduler.stop();
});

test("stale-watcher fallback survives an SSE reconnect", async () => {
  const clock = new FakeClock();
  let pulls = 0;
  const scheduler = createPresenceSyncScheduler({
    streamConnected: false,
    pull: () => {
      pulls += 1;
    },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  scheduler.start();
  scheduler.setRecoveryRequired(true);
  scheduler.setConnected(true);
  for (let i = 0; i < 4; i += 1) await Promise.resolve();
  assert.equal(pulls, 2);
  await clock.advance(LIVE_FALLBACK_POLL_MS);
  for (let i = 0; i < 4; i += 1) await Promise.resolve();

  assert.equal(pulls, 3);
  scheduler.stop();
});

test("a pull requested during an in-flight reconciliation is queued once", async () => {
  const clock = new FakeClock();
  let pulls = 0;
  let release;
  const scheduler = createPresenceSyncScheduler({
    streamConnected: true,
    pull: () => {
      pulls += 1;
      return new Promise((resolve) => {
        release = resolve;
      });
    },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  scheduler.start();
  scheduler.setConnected(false);
  scheduler.setConnected(true);
  assert.equal(pulls, 1);
  release();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(pulls, 2);
  scheduler.stop();
});
