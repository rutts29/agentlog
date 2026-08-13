import assert from "node:assert/strict";
import test from "node:test";
import { QueryClient, QueryObserver } from "@tanstack/react-query";

import {
  VIEW_RANGE_GC_TIME,
  VIEW_RANGE_STALE_TIME,
  rangeViewQueryOptions,
} from "../src/lib/viewQueries.ts";

test("range views retain a warm range cache", () => {
  const options = rangeViewQueryOptions({
    queryKey: ["skills", "7d"],
    queryFn: async () => ({ ok: true }),
  });

  assert.equal(options.staleTime, VIEW_RANGE_STALE_TIME);
  assert.equal(options.gcTime, VIEW_RANGE_GC_TIME);
});

test("switching range views aborts the superseded request", async () => {
  const signals = [];
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const query = (range) =>
    rangeViewQueryOptions({
      queryKey: ["skills", range],
      queryFn: (signal) => {
        signals.push(signal);
        return new Promise((_resolve, reject) => {
          signal.addEventListener(
            "abort",
            () => reject(new DOMException("Aborted", "AbortError")),
            { once: true },
          );
        });
      },
    });
  const observer = new QueryObserver(client, query("24h"));
  const unsubscribe = observer.subscribe(() => undefined);

  observer.setOptions(query("7d"));
  await Promise.resolve();

  assert.equal(signals.length, 2);
  assert.equal(signals[0].aborted, true);
  assert.equal(signals[1].aborted, false);
  unsubscribe();
  assert.equal(signals[1].aborted, true);
  await client.cancelQueries();
  client.clear();
});
