import { queryOptions, type QueryClient } from "@tanstack/react-query";
import {
  fetchSessionDetail,
  fetchSessionTree,
  type LiveSession,
  type PresenceEvent,
  type SessionRow,
} from "./api.ts";

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
      controller = new AbortController();
      const activeController = controller;
      void Promise.resolve()
        .then(() => run(activeController.signal))
        .then(() => {
          if (
            controller === activeController
            && runningKey === key
            && !activeController.signal.aborted
          ) {
            completedKey = key;
          }
        })
        .catch(() => {
          if (controller === activeController && completedKey === key) {
            completedKey = null;
          }
        })
        .finally(() => {
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
export const SOURCE_BACKED_DETAIL_PREFETCH_MAX_AGE = 7 * 24 * 60 * 60_000;
export const SESSION_PRESENCE_REFRESH_DEBOUNCE_MS = 300;

type SessionDetailIdentity = {
  id: string;
  navigation_id?: string | null;
  root_navigation_id?: string | null;
  harness?: string | null;
  external_id?: string | null;
  logical_harness?: string | null;
  runtime_harness?: string | null;
  orchestrator_session_id?: string | null;
  transcript_session_id?: string | null;
  provider_backings?: Array<{
    target_session_id?: string | null;
    target_harness?: string | null;
  }> | null;
  runtime_backing_provenance?: {
    session_id?: string | null;
    harness?: string | null;
  } | null;
};

type SessionRefreshScheduler = {
  schedule(): void;
  cancel(): void;
};

type PresenceVersion = Pick<PresenceEvent, "epoch" | "generation" | "ts">;

export type PresenceVersionGate = {
  accept(frame: PresenceVersion): boolean;
  reset(): void;
};

export type MatchingPresenceRefreshGate = {
  accept(frame: PresenceEvent, session: SessionDetailIdentity): boolean;
  reset(): void;
};

function addIdentity(value: string | null | undefined, target: Set<string>) {
  const normalized = value?.trim();
  if (normalized) target.add(normalized);
}

function intersects(left: Set<string>, right: Set<string>) {
  for (const value of left) if (right.has(value)) return true;
  return false;
}

/** Cache effects advance only with a daemon generation, never a heartbeat. */
export function createPresenceVersionGate(): PresenceVersionGate {
  let previous: PresenceVersion | null = null;

  return {
    accept(frame) {
      const incoming = { ...frame, epoch: frame.epoch ?? null };
      if (!previous) {
        previous = incoming;
        return true;
      }
      if ((previous.epoch ?? null) === incoming.epoch) {
        if (incoming.generation <= previous.generation) return false;
        previous = incoming;
        return true;
      }
      if (!incoming.epoch) return false;
      const incomingTs = incoming.ts ? Date.parse(incoming.ts) : Number.NaN;
      const previousTs = previous.ts ? Date.parse(previous.ts) : Number.NaN;
      if (
        Number.isFinite(incomingTs) &&
        Number.isFinite(previousTs) &&
        incomingTs < previousTs
      ) {
        return false;
      }
      previous = incoming;
      return true;
    },
    reset() {
      previous = null;
    },
  };
}

function presenceFingerprint(live: LiveSession) {
  return [
    live.harness,
    live.external_id,
    live.session_id ?? "",
    live.logical_session_id ?? "",
    live.source_path,
    live.state,
    live.last_activity_at ?? "",
    live.pending_ingest ? "1" : "0",
    live.source_snapshot_status ?? "",
    live.working ? "1" : "0",
    live.activity ?? "",
    live.tool ?? "",
    live.step ?? "",
  ].join("\u0001");
}

export function createMatchingPresenceRefreshGate(): MatchingPresenceRefreshGate {
  const versionGate = createPresenceVersionGate();
  let previousFingerprint: string | null = null;

  return {
    accept(frame, session) {
      if (!versionGate.accept(frame)) return false;
      const fingerprint = frame.sessions
        .filter((live) => presenceMatchesSessionDetail(live, session))
        .map(presenceFingerprint)
        .sort()
        .join("\u0002");
      if (!fingerprint && previousFingerprint === null) return false;
      if (fingerprint === previousFingerprint) return false;
      previousFingerprint = fingerprint;
      return true;
    },
    reset() {
      versionGate.reset();
      previousFingerprint = null;
    },
  };
}

/** A live source may be present before its next debounced ingest lands. */
export function presenceMatchesSessionDetail(
  live: LiveSession,
  session: SessionDetailIdentity,
) {
  const sessionIds = new Set<string>();
  addIdentity(session.id, sessionIds);
  addIdentity(session.navigation_id, sessionIds);
  addIdentity(session.root_navigation_id, sessionIds);
  addIdentity(session.orchestrator_session_id, sessionIds);
  addIdentity(session.transcript_session_id, sessionIds);
  addIdentity(session.runtime_backing_provenance?.session_id, sessionIds);
  for (const backing of session.provider_backings ?? []) {
    addIdentity(backing.target_session_id, sessionIds);
  }

  const liveIds = new Set<string>();
  addIdentity(live.session_id, liveIds);
  addIdentity(live.logical_session_id, liveIds);
  if (intersects(sessionIds, liveIds)) return true;

  const externalId = session.external_id?.trim();
  const liveExternalId = live.external_id.trim();
  if (!externalId || !liveExternalId || externalId !== liveExternalId) return false;

  const sessionHarnesses = new Set<string>();
  addIdentity(session.harness, sessionHarnesses);
  addIdentity(session.logical_harness, sessionHarnesses);
  addIdentity(session.runtime_harness, sessionHarnesses);
  addIdentity(session.runtime_backing_provenance?.harness, sessionHarnesses);
  for (const backing of session.provider_backings ?? []) {
    addIdentity(backing.target_harness, sessionHarnesses);
  }
  return sessionHarnesses.has(live.harness.trim());
}

export function createSessionPresenceRefreshScheduler(options: {
  refresh: () => void | Promise<unknown>;
  debounceMs?: number;
  setTimeout?: (callback: () => void, delay: number) => TimerHandle;
  clearTimeout?: (handle: TimerHandle) => void;
}): SessionRefreshScheduler {
  const debounceMs = options.debounceMs ?? SESSION_PRESENCE_REFRESH_DEBOUNCE_MS;
  const setTimer = options.setTimeout ?? globalThis.setTimeout;
  const clearTimer = options.clearTimeout ?? globalThis.clearTimeout;
  let timer: TimerHandle | null = null;

  return {
    schedule() {
      if (timer !== null) clearTimer(timer);
      timer = setTimer(() => {
        timer = null;
        void Promise.resolve(options.refresh()).catch(() => undefined);
      }, debounceMs);
    },
    cancel() {
      if (timer !== null) clearTimer(timer);
      timer = null;
    },
  };
}

export function sessionDetailQueryKey(sessionId: string) {
  return ["session", sessionId] as const;
}

export function sessionTreeQueryKey(sessionId: string) {
  return ["session-tree", sessionId] as const;
}

export function canPrefetchSessionDetail(
  sourceSnapshotStatus?: string,
  session?: Pick<
    SessionRow,
    "transcript_storage" | "activity_at" | "ended_at" | "started_at"
  >,
  now: () => number = Date.now,
) {
  if (sourceSnapshotStatus === "pending") return false;
  if (session?.transcript_storage !== "source_backed") return true;
  const timestamp = [
    session.activity_at,
    session.ended_at,
    session.started_at,
  ]
    .map((value) => (value ? Date.parse(value) : Number.NaN))
    .find(Number.isFinite);
  if (timestamp === undefined) return false;
  const current = now();
  return (
    Number.isFinite(current)
    && Math.max(0, current - timestamp) <= SOURCE_BACKED_DETAIL_PREFETCH_MAX_AGE
  );
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
