import { queryOptions, type QueryClient } from "@tanstack/react-query";
import { fetchSessionDetail, fetchSessionTree } from "./api.ts";

export const SESSION_RANGE_WARM_DELAY = 750;

type TimerHandle = ReturnType<typeof globalThis.setTimeout>;

export type SessionRangeWarmer = {
  schedule(key: string, run: (signal: AbortSignal) => Promise<void>): void;
  pause(): void;
  notifyActivity(at?: number): void;
  cancel(): void;
};

export function createSessionRangeWarmer(options: {
  quietMs?: number;
  now?: () => number;
  setTimeout?: (callback: () => void, delay: number) => TimerHandle;
  clearTimeout?: (handle: TimerHandle) => void;
} = {}): SessionRangeWarmer {
  const quietMs = options.quietMs ?? SESSION_RANGE_WARM_DELAY;
  const now = options.now ?? Date.now;
  const scheduleTimer = options.setTimeout ?? globalThis.setTimeout;
  const clearTimer = options.clearTimeout ?? globalThis.clearTimeout;
  let timer: TimerHandle | null = null;
  let controller: AbortController | null = null;
  let pendingKey: string | null = null;
  let runningKey: string | null = null;
  let completedKey: string | null = null;
  let lastActivityAt = 0;
  let generation = 0;

  function clearPendingTimer() {
    if (timer === null) return;
    clearTimer(timer);
    timer = null;
  }

  function pause() {
    const wasRunning = runningKey !== null;
    generation += 1;
    clearPendingTimer();
    pendingKey = null;
    controller?.abort();
    controller = null;
    runningKey = null;
    if (wasRunning) completedKey = null;
  }

  function schedule(key: string, run: (signal: AbortSignal) => Promise<void>) {
    if (completedKey === key || pendingKey === key || runningKey === key) return;
    pause();
    pendingKey = key;
    const runGeneration = generation;
    const delay = Math.max(0, quietMs - Math.max(0, now() - lastActivityAt));
    timer = scheduleTimer(() => {
      timer = null;
      if (runGeneration !== generation || pendingKey !== key) return;
      pendingKey = null;
      runningKey = key;
      completedKey = key;
      controller = new AbortController();
      const activeController = controller;
      void run(activeController.signal).catch(() => undefined).finally(() => {
        if (controller === activeController) {
          controller = null;
          runningKey = null;
        }
      });
    }, delay);
  }

  return {
    schedule,
    pause,
    notifyActivity(at = now()) {
      lastActivityAt = at;
      completedKey = null;
      pause();
    },
    cancel() {
      pause();
      completedKey = null;
      lastActivityAt = 0;
    },
  };
}

export const SESSION_DETAIL_STALE_TIME = 5 * 60_000;
export const SESSION_DETAIL_GC_TIME = 30 * 60_000;
export const SESSION_TREE_STALE_TIME = 60_000;
export const SESSION_TREE_GC_TIME = 15 * 60_000;

export function sessionDetailQueryKey(sessionId: string) {
  return ["session", sessionId] as const;
}

export function sessionTreeQueryKey(sessionId: string) {
  return ["session-tree", sessionId] as const;
}

export function canPrefetchSessionDetail(sourceSnapshotStatus?: string) {
  return sourceSnapshotStatus !== "pending";
}

export function invalidateSessionDetailCache(queryClient: QueryClient) {
  return queryClient.invalidateQueries({
    queryKey: ["session"],
    refetchType: "none",
  });
}

export function refreshActiveSessionQueries(
  queryClient: QueryClient,
  sessionId: string,
) {
  return Promise.all([
    queryClient.invalidateQueries({
      queryKey: sessionDetailQueryKey(sessionId),
      exact: true,
      refetchType: "active",
    }),
    queryClient.invalidateQueries({
      queryKey: sessionTreeQueryKey(sessionId),
      exact: true,
      refetchType: "active",
    }),
  ]);
}

export function sessionDetailQueryOptions(sessionId: string) {
  return queryOptions({
    queryKey: sessionDetailQueryKey(sessionId),
    queryFn: ({ signal }) => fetchSessionDetail(sessionId, signal),
    enabled: Boolean(sessionId),
    staleTime: SESSION_DETAIL_STALE_TIME,
    gcTime: SESSION_DETAIL_GC_TIME,
  });
}

export function sessionTreeQueryOptions(sessionId: string) {
  return queryOptions({
    queryKey: sessionTreeQueryKey(sessionId),
    queryFn: ({ signal }) => fetchSessionTree(sessionId, signal),
    enabled: Boolean(sessionId),
    staleTime: SESSION_TREE_STALE_TIME,
    gcTime: SESSION_TREE_GC_TIME,
  });
}
