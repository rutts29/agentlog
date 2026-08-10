import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchHealth,
  fetchLive,
  type HealthPayload,
  type LiveSession,
  type PresenceEvent,
} from "@/lib/api";

export type PresenceSnapshot = {
  sessions: LiveSession[];
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
const LIVE_POLL_MS = 2_000;
const PRESENCE_STALE_S = 45;

export function sessionPresenceKey(s: LiveSession): string {
  return s.session_id || `${s.harness}:${s.external_id}`;
}

export function isWorker(s: LiveSession): boolean {
  if (s.role) return s.role === "worker";
  return (s.source_path || "").includes("/subagents/");
}

const EMPTY_SNAPSHOT: PresenceSnapshot = {
  sessions: [],
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
  const clearTimer = useRef<number | null>(null);

  const applySessions = useCallback(
    (
      sessionsRaw: LiveSession[],
      meta: {
        generation?: number;
        active_seconds?: number;
        ts?: string | null;
        transitionHints?: Array<{ action: string; key: string }>;
      },
    ) => {
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
        generation: data.generation,
        ts: data.ts,
        transitionHints: data.transitions,
      });
    },
    [applySessions],
  );

  useEffect(() => {
    const applyHealth = (h: HealthPayload) => {
      if (h.watcher) {
        setSnap((prev) => ({
          ...prev,
          watcherFresh: Boolean(
            h.watcher?.presence_fresh ?? h.watcher?.alive,
          ),
          watcherReason: h.reason ?? null,
        }));
        return;
      }
      setSnap((prev) => {
        if (!prev.ts) {
          return {
            ...prev,
            watcherFresh: prev.sessions.length > 0 ? true : null,
            watcherReason: null,
          };
        }
        const age = (Date.now() - new Date(prev.ts).getTime()) / 1000;
        const fresh = Number.isFinite(age) && age <= PRESENCE_STALE_S;
        return {
          ...prev,
          watcherFresh: fresh,
          watcherReason: fresh ? null : `presence stale (${Math.round(age)}s)`,
        };
      });
    };

    const pull = () =>
      fetchLive()
        .then((live) =>
          applySessions(live.sessions, {
            generation: live.generation,
            active_seconds: live.active_seconds,
            ts: live.ts,
          }),
        )
        .catch(() => undefined);

    const tick = () => {
      // Always poll: /api/live scans the filesystem itself, so it stays right
      // even when the watch daemon (and therefore SSE) has nothing to say.
      void pull();
      fetchHealth().then(applyHealth).catch(() => {
        setSnap((prev) => {
          if (!prev.ts) return { ...prev, watcherFresh: null };
          const age = (Date.now() - new Date(prev.ts).getTime()) / 1000;
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
    tick();
    const healthTimer = window.setInterval(tick, HEALTH_MS);
    const liveTimer = window.setInterval(pull, LIVE_POLL_MS);
    return () => {
      window.clearInterval(healthTimer);
      window.clearInterval(liveTimer);
      if (clearTimer.current) window.clearTimeout(clearTimer.current);
    };
  }, [applySessions, streamConnected]);

  return { presence: snap, onPresenceEvent };
}
