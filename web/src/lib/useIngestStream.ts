import { useEffect, useRef, useState } from "react";
import { withApiToken, type IngestEvent, type PresenceEvent } from "@/lib/api";

export type IngestBatch = {
  events: IngestEvent[];
  /** False when the burst cap tripped or the tab was hidden — apply data
      without animation. */
  animate: boolean;
};

const DRAIN_MS = 2_000;
const BURST_CAP = 25;

/**
 * Subscribe to the ingest SSE stream with the §2.5 animation budget:
 * events queue and drain at most once per 2s; bursts over 25 drop the
 * animation flag; a hidden tab drains without animation on return.
 * Also forwards `presence` frames when a handler is supplied.
 */
export function useIngestStream(
  onBatch: (batch: IngestBatch) => void,
  onPresence?: (data: PresenceEvent) => void,
): { connected: boolean } {
  const [connected, setConnected] = useState(false);
  const onBatchRef = useRef(onBatch);
  onBatchRef.current = onBatch;
  const onPresenceRef = useRef(onPresence);
  onPresenceRef.current = onPresence;

  useEffect(() => {
    const since = new Date().toISOString();
    const es = new EventSource(
      withApiToken(
        `/api/events/stream?since=${encodeURIComponent(since)}`,
      ),
    );
    let queue: IngestEvent[] = [];
    let hiddenWhileQueued = false;

    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.addEventListener("ingest", (e) => {
      try {
        queue.push(JSON.parse((e as MessageEvent).data) as IngestEvent);
      } catch {
        /* malformed frame — skip */
      }
    });
    es.addEventListener("presence", (e) => {
      if (!onPresenceRef.current) return;
      try {
        onPresenceRef.current(
          JSON.parse((e as MessageEvent).data) as PresenceEvent,
        );
      } catch {
        /* malformed */
      }
    });

    const drain = (forceNoAnimation = false) => {
      if (queue.length === 0) return;
      const events = queue;
      queue = [];
      const animate =
        !forceNoAnimation &&
        !hiddenWhileQueued &&
        events.length <= BURST_CAP &&
        !document.hidden;
      hiddenWhileQueued = false;
      onBatchRef.current({ events, animate });
    };

    const timer = window.setInterval(() => drain(), DRAIN_MS);
    const onVisibility = () => {
      if (document.hidden) {
        if (queue.length > 0) hiddenWhileQueued = true;
      } else {
        drain(true);
      }
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      es.close();
    };
  }, []);

  return { connected };
}
