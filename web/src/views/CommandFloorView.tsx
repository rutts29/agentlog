import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useOutletContext, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { DiagnosticLoader } from "@/components/DiagnosticLoader";
import { CommandFloor } from "@/components/command-grid/CommandFloor";
import { fetchOrchestration, fetchRecent, fetchSessionDetail, type PresenceEvent } from "@/lib/api";
import { projectCommandFloor, type CommandFloorActor } from "@/lib/commandFloor";
import {
  buildCommandReplay,
  projectReplayFrame,
  type ReplayFrame,
} from "@/lib/commandReplay";
import { useIngestStream } from "@/lib/useIngestStream";
import { useLivePresence } from "@/lib/useLivePresence";
import { formatDayTime } from "@/lib/utils";

type Ctx = { range: string };
type FloorMode = "live" | "replay";

function actorStateLabel(actor: CommandFloorActor): string {
  switch (actor.state.kind) {
    case "working":
      return `${actor.state.mode} · ${actor.state.label}`;
    case "orchestrating":
      return `orchestrating · ${actor.state.workerCount} ${actor.state.workerCount === 1 ? "worker" : "workers"}`;
    case "waiting":
      return "human gate · awaiting input";
    case "idle":
      return "standby · activity unavailable";
  }
}

function replayOutcomeLabel(frame: ReplayFrame): string {
  if (frame.outcome === "success") return "verified success";
  if (frame.outcome === "failure") return "observed failure";
  return "outcome unavailable";
}

export function CommandFloorView() {
  const { range } = useOutletContext<Ctx>();
  const [params, setParams] = useSearchParams();
  const mode: FloorMode = params.get("mode") === "replay" ? "replay" : "live";
  const [selectedActorId, setSelectedActorId] = useState<string | null>(params.get("crew"));
  const [playing, setPlaying] = useState(false);
  const presenceHandler = useRef<(data: PresenceEvent) => void>(() => {});
  const { connected } = useIngestStream(() => undefined, (data) => presenceHandler.current(data));
  const { presence, onPresenceEvent } = useLivePresence(connected);
  useEffect(() => {
    presenceHandler.current = onPresenceEvent;
  }, [onPresenceEvent]);

  const liveProjection = useMemo(
    () => projectCommandFloor(presence.sessions),
    [presence.sessions],
  );
  const recent = useQuery({
    queryKey: ["command-floor-replay-sessions", range],
    queryFn: ({ signal }) => fetchRecent(range, 20, signal),
    staleTime: 30_000,
  });
  const coordinators = useQuery({
    queryKey: ["command-floor-replay-coordinators", range],
    queryFn: ({ signal }) => fetchOrchestration(range, signal),
    staleTime: 30_000,
  });
  const replayCandidates = useMemo(() => {
    const seen = new Set<string>();
    const candidates: Array<{ id: string; label: string }> = [];
    for (const session of coordinators.data?.items ?? []) {
      if (seen.has(session.id)) continue;
      seen.add(session.id);
      candidates.push({
        id: session.id,
        label: `${session.project} · coordinator · ${formatDayTime(session.started_at)}`,
      });
    }
    for (const session of recent.data?.items ?? []) {
      const id = session.navigation_id ?? session.id;
      if (seen.has(id)) continue;
      seen.add(id);
      candidates.push({
        id,
        label: `${session.project} · ${session.harness} · ${formatDayTime(session.started_at)}`,
      });
    }
    return candidates;
  }, [coordinators.data, recent.data]);
  const replaySessionId =
    params.get("session") ||
    replayCandidates[0]?.id ||
    null;
  const detail = useQuery({
    queryKey: ["command-floor-replay-detail", replaySessionId],
    queryFn: ({ signal }) => {
      if (!replaySessionId) throw new Error("Replay requires a session id");
      return fetchSessionDetail(replaySessionId, signal);
    },
    enabled: mode === "replay" && Boolean(replaySessionId),
    staleTime: 60_000,
  });
  const replay = useMemo(
    () => detail.data ? buildCommandReplay(detail.data) : null,
    [detail.data],
  );
  const defaultFrame = replay
    ? Math.max(
        0,
        replay.frames.findIndex((candidate) => candidate.kind === "human") >= 0
          ? replay.frames.findIndex((candidate) => candidate.kind === "human")
          : replay.frames.findIndex((candidate) => ["agent", "tool"].includes(candidate.kind)),
      )
    : 0;
  const requestedFrame = Number(params.get("frame") ?? String(defaultFrame));
  const frameIndex = replay
    ? Math.max(0, Math.min(Number.isFinite(requestedFrame) ? requestedFrame : 0, replay.frames.length - 1))
    : 0;
  const frame = replay?.frames[frameIndex] ?? null;
  const replayProjection = useMemo(
    () => detail.data && frame ? projectReplayFrame(detail.data, frame) : null,
    [detail.data, frame],
  );
  const projection = mode === "replay" && replayProjection ? replayProjection : liveProjection;
  const activeActorId =
    selectedActorId && projection.byId.has(selectedActorId)
      ? selectedActorId
      : mode === "replay"
        ? projection.actors[0]?.id ?? null
        : null;
  const selected = activeActorId ? projection.byId.get(activeActorId) ?? null : null;

  useEffect(() => {
    if (mode !== "replay" || !playing || !replay) return;
    if (frameIndex >= replay.frames.length - 1) {
      setPlaying(false);
      return;
    }
    const timer = window.setTimeout(() => {
      const next = new URLSearchParams(params);
      next.set("frame", String(frameIndex + 1));
      setParams(next, { replace: true });
    }, 900);
    return () => window.clearTimeout(timer);
  }, [frameIndex, mode, params, playing, replay, setParams]);

  useEffect(() => {
    setPlaying(false);
  }, [mode, replaySessionId]);

  const traceSearch = (() => {
    const next = new URLSearchParams({ range });
    if (mode === "replay" && frame?.messageId) {
      next.set("msg", frame.messageId);
      next.set("replayFrame", String(frame.index));
    }
    return next.toString();
  })();

  const setMode = (nextMode: FloorMode) => {
    const next = new URLSearchParams(params);
    next.set("range", range);
    next.delete("crew");
    next.delete("frame");
    if (nextMode === "live") {
      next.delete("mode");
      next.delete("session");
    } else {
      next.set("mode", "replay");
      if (replaySessionId) next.set("session", replaySessionId);
    }
    setSelectedActorId(null);
    setParams(next, { replace: true });
  };

  const setReplaySession = (sessionId: string) => {
    const next = new URLSearchParams(params);
    next.set("mode", "replay");
    next.set("session", sessionId);
    next.delete("frame");
    next.delete("crew");
    setSelectedActorId(null);
    setParams(next, { replace: true });
  };

  const setReplayFrame = (index: number) => {
    if (!replay) return;
    const next = new URLSearchParams(params);
    next.set("frame", String(Math.max(0, Math.min(index, replay.frames.length - 1))));
    setParams(next, { replace: true });
  };

  const selectActor = (id: string) => {
    setSelectedActorId(id);
    if (mode === "replay") return;
    const next = new URLSearchParams(params);
    next.set("crew", id);
    setParams(next, { replace: true });
  };

  const replayPending = mode === "replay" && (!replaySessionId || detail.isLoading || !replayProjection);

  return (
    <div className="command-floor-view">
      <header className="command-page-head">
        <div>
          <div className="command-page-head__eyebrow">
            {mode === "live" ? "live local projection" : "source-backed historical projection"}
          </div>
          <h1>Command floor</h1>
          <p>
            {mode === "live"
              ? "Real sessions and spawned workers, mapped to crew and familiars."
              : "Replay observed human, agent, and tool events without inventing activity."}
          </p>
        </div>
        <div className="command-page-head__metrics">
          <span><strong>{projection.operatorCount}</strong> operators</span>
          <span><strong>{projection.familiarCount}</strong> familiars</span>
          <span><strong>{projection.workingCount}</strong> working</span>
          <span className={projection.waitingCount > 0 ? "is-warn" : ""}><strong>{projection.waitingCount}</strong> human gates</span>
        </div>
      </header>

      <div className="floor-modebar">
        <div className="floor-modebar__modes" aria-label="Command floor mode">
          <button type="button" className={mode === "live" ? "is-active" : ""} onClick={() => setMode("live")}>Live</button>
          <button type="button" className={mode === "replay" ? "is-active" : ""} onClick={() => setMode("replay")}>Replay</button>
        </div>
        {mode === "replay" ? (
          <>
            <label className="floor-modebar__session">
              Mission
              <select value={replaySessionId ?? ""} onChange={(event) => setReplaySession(event.target.value)}>
                {replayCandidates.map((session) => (
                  <option key={session.id} value={session.id}>
                    {session.label}
                  </option>
                ))}
              </select>
            </label>
            <div className="floor-replay-controls">
              <button type="button" onClick={() => setReplayFrame(frameIndex - 1)} disabled={frameIndex <= 0}>←</button>
              <button type="button" onClick={() => setPlaying((current) => !current)} disabled={!replay || frameIndex >= replay.frames.length - 1}>{playing ? "Pause" : "Play"}</button>
              <button type="button" onClick={() => setReplayFrame(frameIndex + 1)} disabled={!replay || frameIndex >= replay.frames.length - 1}>→</button>
            </div>
            <label className="floor-replay-scrubber">
              <span>{replay ? `${frameIndex + 1}/${replay.frames.length}` : "0/0"}</span>
              <input type="range" min="0" max={Math.max(0, (replay?.frames.length ?? 1) - 1)} value={frameIndex} onChange={(event) => setReplayFrame(Number(event.target.value))} disabled={!replay} />
            </label>
          </>
        ) : (
          <span className={connected ? "floor-modebar__link is-live" : "floor-modebar__link"}>{connected ? "stream linked" : "fallback link"}</span>
        )}
      </div>

      {mode === "replay" && frame ? (
        <div className={`replay-frame replay-frame--${frame.outcome}`} aria-live="polite">
          <span className="replay-frame__index">F-{String(frame.index + 1).padStart(3, "0")}</span>
          <span><strong>{frame.title}</strong><small>{frame.detail}</small></span>
          <span>{frame.kind}</span>
          <span>{replayOutcomeLabel(frame)}</span>
        </div>
      ) : null}

      {replayPending ? (
        <DiagnosticLoader label="Building source-backed replay" rows={6} />
      ) : (
        <div className="command-floor-layout">
          <CommandFloor
            projection={projection}
            selectedActorId={activeActorId}
            onSelectActor={selectActor}
          />
          <aside className="crew-inspector" aria-live="polite">
            <div className="crew-inspector__head">
              <span>{mode === "live" ? "crew inspector" : "replay inspector"}</span>
              <span className={mode === "live" && connected ? "is-live" : ""}>
                {mode === "live" ? (connected ? "stream linked" : "fallback link") : `frame ${frameIndex + 1}`}
              </span>
            </div>
            {selected ? (
              <div className="crew-inspector__body">
                <div className="crew-inspector__callsign">{selected.callsign}</div>
                <h2>{selected.displayName}</h2>
                <div className="crew-inspector__chips">
                  <span>{selected.kind}</span>
                  <span>{selected.specialty}</span>
                  <span>{selected.harnessDisplay}</span>
                </div>
                <dl>
                  <div><dt>State</dt><dd>{actorStateLabel(selected)}</dd></div>
                  <div><dt>Station</dt><dd>{selected.workstation.replaceAll("-", " ")}</dd></div>
                  <div><dt>Project</dt><dd>{selected.project}</dd></div>
                  <div><dt>Evidence</dt><dd>{selected.specialtyEvidence} specialty · {selected.sourceSnapshotStatus} source</dd></div>
                  <div><dt>Lineage</dt><dd>{selected.lineage.kind === "root" ? "root operator" : selected.lineage.kind === "attached" ? `attached to ${projection.byId.get(selected.lineage.parentId)?.callsign ?? selected.lineage.parentId}` : "parent unavailable"}</dd></div>
                </dl>
                {selected.sessionId ? (
                  <Link className="command-link" to={`/sessions/${encodeURIComponent(selected.sessionId)}?${traceSearch}`}>
                    Open holotrace →
                  </Link>
                ) : (
                  <div className="crew-inspector__pending">Transcript pending ingest</div>
                )}
              </div>
            ) : (
              <div className="crew-inspector__empty">Select an operator or familiar to inspect its state and evidence link.</div>
            )}
            {projection.unattachedFamiliarCount > 0 ? (
              <div className="crew-inspector__warning">{projection.unattachedFamiliarCount} familiar lineage {projection.unattachedFamiliarCount === 1 ? "link is" : "links are"} unavailable.</div>
            ) : null}
          </aside>
        </div>
      )}
    </div>
  );
}
