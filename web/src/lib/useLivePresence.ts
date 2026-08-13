import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchHealth,
  fetchLive,
  type HealthPayload,
  type LiveSession,
  type PresenceEvent,
} from "./api.ts";

export type PresenceSnapshot = {
  sessions: LiveSession[];
  epoch: string | null;
  generation: number;
  activeSeconds: number;
  ts: string | null;
  /** Watcher / presence file freshness. null = unknown. */
  watcherFresh: boolean | null;
  watcherReason: string | null;
  /** Keys that just became active — one-shot UI cues. */
  arrivals: string[];
  /** State changes for one-shot orb cues. */
  transitions: Array<{ key: string; from: string | null; to: string }>;
  /** Every panel resolves liveness through these, so nothing disagrees. */
  bySessionId: Map<string, LiveSession>;
  conversations: LiveSession[];
  workers: LiveSession[];
};

const HEALTH_MS = 8_000;
export const LIVE_FALLBACK_POLL_MS = 15_000;
const PRESENCE_STALE_S = 45;

type PresenceSyncSchedulerOptions = {
  streamConnected: boolean;
  pull: () => void | Promise<void>;
  fallbackMs?: number;
  setTimeout?: (callback: () => void, delay: number) => number;
  clearTimeout?: (handle: number) => void;
};

/**
 * Keep the direct live endpoint as a bounded recovery path, not a second live
 * transport. SSE owns updates while connected; a single delayed pull repairs
 * gaps when the stream is unavailable.
 */
export function createPresenceSyncScheduler({
  streamConnected,
  pull,
  fallbackMs = LIVE_FALLBACK_POLL_MS,
  setTimeout: setTimer = (callback, delay) => window.setTimeout(callback, delay),
  clearTimeout: clearTimer = (handle) => window.clearTimeout(handle),
}: PresenceSyncSchedulerOptions) {
  let timer: number | null = null;
  let stopped = false;
  let connected = streamConnected;
  let recoveryRequired = false;
  let pullActive = false;
  let pullQueued = false;

  const requestPull = () => {
    if (stopped) return;
    if (pullActive) {
      pullQueued = true;
      return;
    }
    pullActive = true;
    let result: void | Promise<void>;
    try {
      result = pull();
    } catch {
      result = undefined;
    }
    void Promise.resolve(result).catch(() => undefined).finally(() => {
      pullActive = false;
      if (pullQueued) {
        pullQueued = false;
        requestPull();
      }
    });
  };

  const fallbackActive = () => !connected || recoveryRequired;

  const scheduleFallback = () => {
    if (!fallbackActive() || stopped || timer !== null) return;
    timer = setTimer(() => {
      timer = null;
      if (stopped || !fallbackActive()) return;
      requestPull();
      scheduleFallback();
    }, fallbackMs);
  };

  return {
    start() {
      requestPull();
      scheduleFallback();
    },
    setConnected(next: boolean) {
      if (connected === next) return;
      connected = next;
      if (connected) {
        if (!recoveryRequired && timer !== null) {
          clearTimer(timer);
          timer = null;
        }
        requestPull();
        scheduleFallback();
      } else {
        requestPull();
        scheduleFallback();
      }
    },
    setRecoveryRequired(next: boolean) {
      if (recoveryRequired === next) return;
      recoveryRequired = next;
      if (fallbackActive()) {
        requestPull();
        scheduleFallback();
      } else if (timer !== null) {
        clearTimer(timer);
        timer = null;
      }
    },
    stop() {
      stopped = true;
      pullQueued = false;
      if (timer !== null) {
        clearTimer(timer);
        timer = null;
      }
    },
  };
}

export function sessionPresenceKey(s: LiveSession): string {
  return s.session_id || `${s.harness}:${s.external_id}`;
}

export function isWorker(s: LiveSession): boolean {
  if (s.role) return s.role === "worker";
  return (s.source_path || "").includes("/subagents/");
}

const EMPTY_SNAPSHOT: PresenceSnapshot = {
  sessions: [],
  epoch: null,
  generation: 0,
  activeSeconds: 90,
  ts: null,
  watcherFresh: null,
  watcherReason: null,
  arrivals: [],
  transitions: [],
  bySessionId: new Map(),
  conversations: [],
  workers: [],
};

type PresenceVersion = {
  epoch?: string | null;
  generation?: number;
  ts?: string | null;
};

export function acceptsPresenceVersion(
  current: PresenceVersion | null,
  incoming: PresenceVersion,
): boolean {
  if (!current) return true;
  const currentEpoch = current.epoch ?? null;
  const incomingEpoch = incoming.epoch ?? null;
  const incomingTs = incoming.ts ? Date.parse(incoming.ts) : Number.NaN;
  const currentTs = current.ts ? Date.parse(current.ts) : Number.NaN;
  if (currentEpoch !== incomingEpoch) {
    if (!incomingEpoch) return false;
    if (!currentEpoch) return true;
    return (
      Number.isFinite(incomingTs) &&
      Number.isFinite(currentTs) &&
      incomingTs >= currentTs
    );
  }
  if (
    Number.isFinite(incoming.generation) &&
    Number.isFinite(current.generation) &&
    incoming.generation! < current.generation!
  ) {
    return false;
  }
  if (
    Number.isFinite(incoming.generation) &&
    Number.isFinite(current.generation) &&
    incoming.generation === current.generation &&
    Number.isFinite(incomingTs) &&
    Number.isFinite(currentTs) &&
    incomingTs < currentTs
  ) {
    return false;
  }
  return true;
}

/** Derived indexes rebuilt whenever the session list changes. */
function indexes(sessions: LiveSession[]) {
  const bySessionId = new Map<string, LiveSession>();
  for (const s of sessions) if (s.session_id) bySessionId.set(s.session_id, s);
  return {
    bySessionId,
    conversations: sessions.filter((s) => !isWorker(s)),
    workers: sessions.filter(isWorker),
  };
}

/**
 * Live agent presence: GET /api/live seed + optional SSE presence frames
 * (from useIngestStream) + health freshness. No ambient animation.
 */
export function useLivePresence(streamConnected: boolean): {
  presence: PresenceSnapshot;
  onPresenceEvent: (data: PresenceEvent) => void;
} {
  const [snap, setSnap] = useState<PresenceSnapshot>(EMPTY_SNAPSHOT);
  const prevKeysRef = useRef<Map<string, string>>(new Map());
  const lastVersionRef = useRef<PresenceVersion | null>(null);
  const lastTsRef = useRef<string | null>(null);
  const clearTimer = useRef<number | null>(null);
  const schedulerRef = useRef<ReturnType<typeof createPresenceSyncScheduler> | null>(null);

  const applySessions = useCallback(
    (
      sessionsRaw: LiveSession[],
      meta: {
        epoch?: string | null;
        generation?: number;
        active_seconds?: number;
        ts?: string | null;
        transitionHints?: Array<{ action: string; key: string }>;
      },
    ) => {
      const incomingVersion = {
        epoch: meta.epoch,
        generation: meta.generation,
        ts: meta.ts,
      };
      if (!acceptsPresenceVersion(lastVersionRef.current, incomingVersion)) {
        return;
      }
      lastVersionRef.current = incomingVersion;
      if (meta.ts) lastTsRef.current = meta.ts;
      const activeSeconds = meta.active_seconds ?? 90;
      // The endpoint already decided what is live (including mid-turn agents
      // whose harness has not flushed yet); re-filtering here would undo that.
      const sessions = sessionsRaw;
      const nextKeys = new Map<string, string>();
      for (const s of sessions) nextKeys.set(sessionPresenceKey(s), s.state);

      const arrivals: string[] = [];
      const transitions: Array<{
        key: string;
        from: string | null;
        to: string;
      }> = [];
      for (const [key, state] of nextKeys) {
        const prev = prevKeysRef.current.get(key);
        if (prev === undefined) arrivals.push(key);
        else if (prev !== state)
          transitions.push({ key, from: prev, to: state });
      }
      if (meta.transitionHints) {
        for (const t of meta.transitionHints) {
          if (t.action === "active" && !arrivals.includes(t.key))
            arrivals.push(t.key);
        }
      }
      prevKeysRef.current = nextKeys;

      if (clearTimer.current) window.clearTimeout(clearTimer.current);
      clearTimer.current = window.setTimeout(() => {
        setSnap((s) =>
          s.arrivals.length || s.transitions.length
            ? { ...s, arrivals: [], transitions: [] }
            : s,
        );
      }, 700);

      setSnap((prev) => ({
        ...prev,
        sessions,
        ...indexes(sessions),
        epoch: meta.epoch ?? prev.epoch,
        generation: meta.generation ?? prev.generation,
        activeSeconds,
        ts: meta.ts ?? prev.ts,
        arrivals,
        transitions,
      }));
    },
    [],
  );

  const onPresenceEvent = useCallback(
    (data: PresenceEvent) => {
      applySessions(data.sessions, {
        epoch: data.epoch,
        generation: data.generation,
        ts: data.ts,
        transitionHints: data.transitions,
      });
    },
    [applySessions],
  );

  useEffect(() => {
    let disposed = false;
    const controller = new AbortController();
    const pull = () => {
      if (disposed) return Promise.resolve();
      return fetchLive(controller.signal)
        .then((live) => {
          if (disposed) return;
          applySessions(live.sessions, {
            epoch: live.epoch,
            generation: live.generation,
            active_seconds: live.active_seconds,
            ts: live.ts,
          });
        })
        .catch(() => undefined)
        .then(() => undefined);
    };
    const scheduler = createPresenceSyncScheduler({
      streamConnected,
      pull,
    });
    schedulerRef.current = scheduler;
    scheduler.start();
    return () => {
      disposed = true;
      controller.abort();
      scheduler.stop();
      schedulerRef.current = null;
    };
  }, [applySessions]);

  useEffect(() => {
    schedulerRef.current?.setConnected(streamConnected);
  }, [streamConnected]);

  useEffect(() => {
    let disposed = false;
    const controller = new AbortController();
    const applyHealth = (h: HealthPayload) => {
      if (disposed) return;
      if (h.watcher) {
        const watcherFresh = Boolean(
          h.watcher?.presence_fresh ?? h.watcher?.alive,
        );
        schedulerRef.current?.setRecoveryRequired(
          !watcherFresh || Boolean(h.degraded),
        );
        setSnap((prev) => ({
          ...prev,
          watcherFresh,
          watcherReason: h.reason ?? null,
        }));
        return;
      }
      const age = lastTsRef.current
        ? (Date.now() - new Date(lastTsRef.current).getTime()) / 1000
        : Number.POSITIVE_INFINITY;
      const fresh = Number.isFinite(age) && age <= PRESENCE_STALE_S;
      schedulerRef.current?.setRecoveryRequired(!fresh || Boolean(h.degraded));
      setSnap((prev) => {
        if (!lastTsRef.current) {
          return {
            ...prev,
            watcherFresh: prev.sessions.length > 0 ? true : null,
            watcherReason: null,
          };
        }
        return {
          ...prev,
          watcherFresh: fresh,
          watcherReason: fresh ? null : `presence stale (${Math.round(age)}s)`,
        };
      });
    };

    const checkHealth = () => {
      void fetchHealth(controller.signal).then(applyHealth).catch(() => {
        if (disposed) return;
        schedulerRef.current?.setRecoveryRequired(true);
        setSnap((prev) => {
          if (!lastTsRef.current) return { ...prev, watcherFresh: null };
          const age = lastTsRef.current
            ? (Date.now() - new Date(lastTsRef.current).getTime()) / 1000
            : Number.POSITIVE_INFINITY;
          const fresh = Number.isFinite(age) && age <= PRESENCE_STALE_S;
          return {
            ...prev,
            watcherFresh: fresh,
            watcherReason: fresh
              ? null
              : `presence stale (${Math.round(age)}s)`,
          };
        });
      });
    };
    checkHealth();
    const healthTimer = window.setInterval(checkHealth, HEALTH_MS);
    return () => {
      disposed = true;
      window.clearInterval(healthTimer);
      controller.abort();
    };
  }, []);

  useEffect(() => {
    return () => {
      if (clearTimer.current) window.clearTimeout(clearTimer.current);
    };
  }, []);

  return { presence: snap, onPresenceEvent };
}
