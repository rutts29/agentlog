import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useOutletContext, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { DiagnosticLoader } from "@/components/DiagnosticLoader";
import { CommandFloor } from "@/components/command-grid/CommandFloor";
import {
  ExecutionTimeline,
  type ExecutionPhaseSelection,
} from "@/components/command-grid/ExecutionTimeline";
import { MissionArena } from "@/components/command-grid/MissionArena";
import { PhaseToolTrace } from "@/components/command-grid/PhaseToolTrace";
import { fetchMissionReplay, fetchOrchestration, fetchRecent, type PresenceEvent } from "@/lib/api";
import { projectCommandFloor, type CommandFloorActor } from "@/lib/commandFloor";
import {
  buildMissionReplay,
  findMissionFrame,
  latestMissionFrameForActor,
  projectMissionReplayFrame,
  type MissionFrame,
} from "@/lib/missionReplay";
import { useIngestStream } from "@/lib/useIngestStream";
import { useLivePresence } from "@/lib/useLivePresence";
import { formatDayTime, formatDuration } from "@/lib/utils";

type Ctx = { range: string };
type FloorMode = "live" | "replay";

function actorStateLabel(actor: CommandFloorActor): string {
  switch (actor.state.kind) {
    case "working": {
      const mode = actor.state.mode === "tool" ? "working" : actor.state.mode;
      const detail = /^(call|result|end)$/i.test(actor.state.label)
        ? actor.state.tool || "tool activity"
        : actor.state.label;
      return `${mode} · ${detail}`;
    }
    case "orchestrating":
      return `orchestrating · ${actor.state.workerCount} ${actor.state.workerCount === 1 ? "worker" : "workers"}`;
    case "waiting":
      return "human gate · awaiting input";
    case "idle":
      return "standby · activity unavailable";
  }
}

function replayOutcomeLabel(frame: MissionFrame): string {
  if (frame.outcome === "success") return "verified success";
  if (frame.outcome === "failure") return "observed failure";
  return "outcome unavailable";
}

function operationLabel(value: string | null): string {
  const labels: Record<string, string> = {
    artifact_read: "read artifact",
    artifact_write: "write artifact",
    execute_other: "execute command",
    read_only: "read-only operation",
    verification: "verification",
  };
  if (!value || value === "unknown") return "not classified";
  return labels[value] ?? value.replaceAll("_", " ");
}

export function CommandFloorView() {
  const { range } = useOutletContext<Ctx>();
  const [params, setParams] = useSearchParams();
  const mode: FloorMode = params.get("mode") === "replay" ? "replay" : "live";
  const [selectedActorId, setSelectedActorId] = useState<string | null>(params.get("crew"));
  const [selectedPhase, setSelectedPhase] = useState<ExecutionPhaseSelection | null>(null);
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
        label: `${session.project} · ${session.child_count} direct workers · ${formatDayTime(session.started_at)}`,
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
  const mission = useQuery({
    queryKey: ["command-floor-mission-replay", replaySessionId],
    queryFn: ({ signal }) => {
      if (!replaySessionId) throw new Error("Replay requires a session id");
      return fetchMissionReplay(replaySessionId, signal);
    },
    enabled: mode === "replay" && Boolean(replaySessionId),
    staleTime: 60_000,
  });
  const replay = useMemo(
    () => mission.data ? buildMissionReplay(mission.data) : null,
    [mission.data],
  );
  const linkedFrame = replay
    ? findMissionFrame(replay, params.get("msg"), params.get("actor"))
    : null;
  const defaultFrame = linkedFrame ?? replay?.recommendedFrameIndex ?? 0;
  const requestedFrame = Number(params.get("frame") ?? String(defaultFrame));
  const frameIndex = replay
    ? Math.max(0, Math.min(Number.isFinite(requestedFrame) ? requestedFrame : 0, replay.frames.length - 1))
    : 0;
  const frame = replay?.frames[frameIndex] ?? null;
  const replayProgress = replay && replay.frames.length > 1
    ? Math.round((frameIndex / (replay.frames.length - 1)) * 100)
    : 0;
  const replayProjection = useMemo(
    () => replay && frame ? projectMissionReplayFrame(replay, frame.index) : null,
    [frame, replay],
  );
  const projection = mode === "replay" && replayProjection ? replayProjection : liveProjection;
  const activeActorId =
    selectedActorId && projection.byId.has(selectedActorId)
      ? selectedActorId
      : mode === "replay"
        ? frame && projection.byId.has(frame.actorId)
          ? frame.actorId
          : projection.actors[0]?.id ?? null
        : null;
  const selected = activeActorId ? projection.byId.get(activeActorId) ?? null : null;
  const selectedFrame = replay && activeActorId
    ? latestMissionFrameForActor(replay, activeActorId, frameIndex)
    : null;
  const selectedMeta = replay && activeActorId ? replay.actorById.get(activeActorId) ?? null : null;
  const recentSelectedTools = replay && activeActorId
    ? replay.frames
        .filter((candidate) => candidate.actorId === activeActorId && candidate.kind === "tool" && candidate.index <= frameIndex)
        .slice(-4)
        .reverse()
    : [];

  useEffect(() => {
    if (mode !== "replay" || !playing || !replay) return;
    if (frameIndex >= replay.frames.length - 1) {
      setPlaying(false);
      return;
    }
    const timer = window.setTimeout(() => {
      const next = new URLSearchParams(params);
      const step = Math.max(1, Math.floor(replay.frames.length / 1_000));
      next.set("frame", String(Math.min(replay.frames.length - 1, frameIndex + step)));
      next.delete("msg");
      next.delete("actor");
      setParams(next, { replace: true });
    }, 240);
    return () => window.clearTimeout(timer);
  }, [frameIndex, mode, params, playing, replay, setParams]);

  useEffect(() => {
    setPlaying(false);
    setSelectedPhase(null);
  }, [mode, replaySessionId]);

  const traceSearch = (() => {
    const next = new URLSearchParams({ range });
    if (mode === "replay" && selectedFrame?.messageId) {
      next.set("msg", selectedFrame.messageId);
      next.set("replayFrame", String(frameIndex));
    }
    return next.toString();
  })();

  const setMode = (nextMode: FloorMode) => {
    const next = new URLSearchParams(params);
    next.set("range", range);
    next.delete("crew");
    next.delete("frame");
    next.delete("msg");
    next.delete("actor");
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
    next.delete("msg");
    next.delete("actor");
    setSelectedActorId(null);
    setParams(next, { replace: true });
  };

  const setReplayFrame = (index: number) => {
    if (!replay) return;
    const next = new URLSearchParams(params);
    next.set("frame", String(Math.max(0, Math.min(index, replay.frames.length - 1))));
    next.delete("msg");
    next.delete("actor");
    setParams(next, { replace: true });
  };

  const selectActor = (id: string) => {
    setSelectedActorId(id);
    if (mode === "replay") return;
    const next = new URLSearchParams(params);
    next.set("crew", id);
    setParams(next, { replace: true });
  };

  const replayPending = mode === "replay" && (!replaySessionId || mission.isLoading || !replayProjection);

  return (
    <div className="command-floor-view">
      <header className="command-page-head">
        <div>
          <div className="command-page-head__eyebrow">
            {mode === "live" ? "live local projection" : "indexed multi-session mission replay"}
          </div>
          <h1>Command floor</h1>
          <p>
            {mode === "live"
              ? "Real sessions and spawned workers, mapped to crew and familiars."
              : "Replay the full recorded crew, tool traffic, lineage, and execution timing."}
          </p>
        </div>
        <div className="command-page-head__metrics">
          <span><strong>{projection.operatorCount}</strong> operators</span>
          <span><strong>{projection.familiarCount}</strong> active familiars</span>
          <span><strong>{projection.workingCount}</strong> working</span>
          {mode === "replay" && replay ? (
            <span><strong>{replay.actors.length - 1}</strong> mission workers</span>
          ) : (
            <span className={projection.waitingCount > 0 ? "is-warn" : ""}><strong>{projection.waitingCount}</strong> human gates</span>
          )}
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
              <button type="button" aria-label="Previous event" title="Previous event" onClick={() => setReplayFrame(frameIndex - 1)} disabled={frameIndex <= 0}>←</button>
              <button type="button" onClick={() => setPlaying((current) => !current)} disabled={!replay || frameIndex >= replay.frames.length - 1}>{playing ? "Pause" : "Play"}</button>
              <button type="button" aria-label="Next event" title="Next event" onClick={() => setReplayFrame(frameIndex + 1)} disabled={!replay || frameIndex >= replay.frames.length - 1}>→</button>
              <button type="button" onClick={() => replay && setReplayFrame(replay.recommendedFrameIndex)} disabled={!replay}>Peak activity</button>
            </div>
            <label className="floor-replay-scrubber">
              <span>{frame ? `${formatDayTime(frame.timestamp)} · ${replayProgress}%` : "Replay timeline"}</span>
              <input type="range" min="0" max={Math.max(0, (replay?.frames.length ?? 1) - 1)} value={frameIndex} onChange={(event) => setReplayFrame(Number(event.target.value))} disabled={!replay} />
            </label>
          </>
        ) : (
          <span className={connected ? "floor-modebar__link is-live" : "floor-modebar__link"}>{connected ? "stream linked" : "fallback link"}</span>
        )}
      </div>

      {mode === "live" ? (
        <div className="live-mode-note">
          Live shows current presence only. Switch to Replay for recorded timelines, tool packets, and complete mission lineage.
        </div>
      ) : null}

      {mode === "replay" && frame ? (
        <div className={`replay-frame replay-frame--${frame.outcome}`} aria-live="polite">
          <time className="replay-frame__index">{formatDayTime(frame.timestamp)}</time>
          <span><strong>{frame.title}</strong><small>{frame.detail}</small></span>
          <span>{projection.byId.get(frame.actorId)?.callsign ?? frame.kind}</span>
          {frame.outcome === "unknown" ? null : <span>{replayOutcomeLabel(frame)}</span>}
        </div>
      ) : null}

      {mode === "replay" && mission.isError ? (
        <div className="ops-empty">Mission replay could not be read from the local ledger.</div>
      ) : replayPending ? (
        <DiagnosticLoader label="Indexing mission replay" rows={6} />
      ) : (
        <>
          {mode === "replay" && replay ? (
            <ExecutionTimeline
              replay={replay}
              projection={projection}
              frameIndex={frameIndex}
              selectedActorId={activeActorId}
              onSelectFrame={setReplayFrame}
              onSelectPhase={(phase) => {
                setSelectedActorId(phase.actorId);
                setSelectedPhase(phase);
              }}
            />
          ) : null}
          {mode === "replay" && replay && selectedPhase ? (
            <PhaseToolTrace
              phase={selectedPhase}
              replay={replay}
              range={range}
              onClose={() => setSelectedPhase(null)}
            />
          ) : null}
          <div className="command-floor-layout">
            {mode === "replay" && replay ? (
              <MissionArena
                projection={projection}
                replay={replay}
                selectedActorId={activeActorId}
                onSelectActor={selectActor}
              />
            ) : (
              <CommandFloor
                projection={projection}
                selectedActorId={activeActorId}
                onSelectActor={selectActor}
              />
            )}
          <aside className="crew-inspector" aria-live="polite">
            <div className="crew-inspector__head">
              <span>{mode === "live" ? "crew inspector" : "replay inspector"}</span>
              <span className={mode === "live" && connected ? "is-live" : ""}>
                {mode === "live" ? (connected ? "stream linked" : "fallback link") : `${replayProgress}% · ${formatDayTime(frame?.timestamp)}`}
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
                  {selected.model ? <span>{selected.model}</span> : null}
                  {selected.effort ? <span>{selected.effort}</span> : null}
                  {selected.agentProfile ? <span>{selected.agentProfile.replaceAll("-", " ")}</span> : null}
                </div>
                <dl>
                  <div><dt>State</dt><dd>{actorStateLabel(selected)}</dd></div>
                  <div><dt>Station</dt><dd>{selected.workstation.replaceAll("-", " ")}</dd></div>
                  <div><dt>Project</dt><dd>{selected.project}</dd></div>
                  <div><dt>Evidence</dt><dd>{selected.specialtyEvidence} specialty · {selected.sourceSnapshotStatus} source</dd></div>
                  <div><dt>Lineage</dt><dd>{selected.lineage.kind === "root" ? "root operator" : selected.lineage.kind === "attached" ? `attached to ${projection.byId.get(selected.lineage.parentId)?.callsign ?? selected.lineage.parentId}` : "parent unavailable"}</dd></div>
                  {selectedMeta ? <div><dt>Trace</dt><dd>{selectedMeta.message_count} messages · {selectedMeta.tool_count} tools · L{selectedMeta.depth}</dd></div> : null}
                </dl>
                {selectedFrame?.kind === "tool" ? (
                  <section className={`inspector-tool inspector-tool--${selectedFrame.outcome}`}>
                    <header><span>Tool packet</span><strong>{selectedFrame.tool}</strong></header>
                    <p>{selectedFrame.context || selectedFrame.action || "No call context was captured in the normalized ledger."}</p>
                    <dl>
                      <div><dt>Action</dt><dd>{selectedFrame.action || "not recorded"}</dd></div>
                      {selectedFrame.outcome === "unknown" ? null : <div><dt>Outcome</dt><dd>{replayOutcomeLabel(selectedFrame)}</dd></div>}
                      {selectedFrame.durationMs == null ? null : <div><dt>Duration</dt><dd>{formatDuration(selectedFrame.durationMs / 1000)}</dd></div>}
                      {!selectedFrame.operationKind || selectedFrame.operationKind === "unknown" ? null : <div><dt>Operation</dt><dd>{operationLabel(selectedFrame.operationKind)}</dd></div>}
                      <div><dt>Timing</dt><dd>{selectedFrame.timeEvidence}</dd></div>
                    </dl>
                  </section>
                ) : null}
                {mode === "replay" && recentSelectedTools.length > 0 ? (
                  <section className="inspector-tool-history">
                    <header>Recent tool packets</header>
                    {recentSelectedTools.map((toolFrame) => (
                      <button type="button" key={toolFrame.id} onClick={() => setReplayFrame(toolFrame.index)}>
                        <span>{toolFrame.tool}</span>
                        {toolFrame.outcome !== "unknown" || toolFrame.durationMs != null ? (
                          <small>
                            {[
                              toolFrame.outcome === "unknown" ? null : replayOutcomeLabel(toolFrame),
                              toolFrame.durationMs == null ? null : formatDuration(toolFrame.durationMs / 1000),
                            ].filter(Boolean).join(" · ")}
                          </small>
                        ) : null}
                      </button>
                    ))}
                  </section>
                ) : null}
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
          {mode === "replay" && replay && (replay.bounds.nodes_truncated || replay.bounds.events_truncated) ? (
            <div className="crew-inspector__warning">
              Replay is bounded to {replay.bounds.returned_node_count}/{replay.bounds.total_node_count} agents and {replay.bounds.returned_event_count}/{replay.bounds.total_event_count} events.
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
