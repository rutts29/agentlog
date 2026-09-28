import { callsignForActor, type CommandFloorProjection } from "@/lib/commandFloor";
import { missionActorName, type MissionFrame, type MissionReplay } from "@/lib/missionReplay";
import { formatDayTime, formatDuration } from "@/lib/utils";

type Lane = {
  actorId: string;
  frames: MissionFrame[];
  score: number;
};

type ActivityPhase = {
  id: string;
  frames: MissionFrame[];
  representative: MissionFrame;
  label: string;
  kind: MissionFrame["kind"];
  outcome: MissionFrame["outcome"];
  start: number;
  end: number;
};

export type ExecutionPhaseSelection = {
  actorId: string;
  label: string;
  representativeIndex: number;
  startSeq: number;
  endSeq: number;
  startedAt: string | null;
  endedAt: string | null;
  eventCount: number;
  toolEventCount: number;
  outcome: MissionFrame["outcome"];
  models: string[];
};

const PHASE_GAP_MS = 45_000;
const TOOL_BURST_GAP_MS = 25_000;
const MAX_PHASES_PER_LANE = 10;

function timestamp(frame: MissionFrame, fallback: number): number {
  const value = frame.timestamp ? Date.parse(frame.timestamp) : Number.NaN;
  return Number.isFinite(value) ? value : fallback;
}

function timelineWindow(replay: MissionReplay, frameIndex: number) {
  const current = replay.frames[frameIndex];
  const currentAt = current?.timestamp ? Date.parse(current.timestamp) : Number.NaN;
  let frames: MissionFrame[] = [];
  if (Number.isFinite(currentAt)) {
    for (const horizon of [5, 15, 60]) {
      const radius = horizon * 60 * 1_000;
      frames = replay.frames.filter((frame) => {
        if (!frame.timestamp) return false;
        const at = Date.parse(frame.timestamp);
        return Number.isFinite(at) && Math.abs(at - currentAt) <= radius;
      });
      if (frames.length >= 10) break;
    }
  }
  if (frames.length === 0) {
    frames = replay.frames.slice(Math.max(0, frameIndex - 130), Math.min(replay.frames.length, frameIndex + 91));
  }
  if (frames.length > 320) {
    const localIndex = Math.max(0, frames.findIndex((frame) => frame.index >= frameIndex));
    frames = frames.slice(Math.max(0, localIndex - 180), localIndex + 140);
  }
  const start = Math.min(...frames.map((frame, index) => timestamp(frame, index)));
  const end = Math.max(...frames.map((frame, index) => timestamp(frame, index)));
  return { frames, start, end: Math.max(start + 1, end) };
}

function lanePriority(frame: MissionFrame): number {
  if (frame.kind === "tool") return 4;
  if (frame.kind === "human") return 3;
  if (frame.kind === "agent") return 2;
  return 1;
}

function activityLabel(frame: MissionFrame): string {
  const text = `${frame.tool ?? ""} ${frame.operationKind ?? ""} ${frame.action ?? ""} ${frame.context ?? ""} ${frame.detail}`.toLowerCase();
  if (/pytest|vitest|jest|cargo test|go test|npm test|run_tests|\btests?\b/.test(text)) return "Run tests";
  if (/apply_patch|\bpatch\b|\bedit(?:ed|ing)?\b|\bwrite(?:s|ing)?\b/.test(text)) return "Edit files";
  if (/git diff|\breview(?:ed|ing)?\b|\baudit\b/.test(text)) return "Review changes";
  if (/read_file|ripgrep|\bgrep\b|\bsearch\b|\binspect(?:ed|ing)?\b|\bfind\b/.test(text)) return "Inspect files";
  if (/spawn_agent|\bspawn\b|\bfork\b|\bdelegate\b/.test(text)) return "Deploy worker";
  if (/wait_agent|\bwait(?:ed|ing)?\b/.test(text)) return "Await worker";
  if (/\bjs\b|browser|playwright|computer use/.test(text)) return "UI automation";
  if (/exec|command|shell|terminal|stdin/.test(text)) return "Shell work";
  switch (frame.kind) {
    case "human": return "Human command";
    case "agent": return "Agent update";
    case "spawn": return "Deploy worker";
    case "complete": return "Worker returned";
    case "context": return "Task brief";
    case "system": return "Runtime context";
    case "tool": return frame.tool?.replaceAll("_", " ") || "Tool work";
  }
}

function operationalFrames(frames: MissionFrame[]): MissionFrame[] {
  const primary = frames.filter((frame) =>
    frame.kind === "tool" ||
    frame.kind === "human" ||
    frame.kind === "spawn" ||
    frame.kind === "complete",
  );
  if (primary.length > 0) return primary;
  return frames.filter((frame) => frame.kind === "agent" || frame.kind === "context");
}

function phaseOutcome(frames: MissionFrame[]): MissionFrame["outcome"] {
  if (frames.some((frame) => frame.outcome === "failure")) return "failure";
  if (frames.some((frame) => frame.outcome === "success")) return "success";
  return "unknown";
}

function dominantActivityLabel(frames: MissionFrame[]): string {
  const counts = new Map<string, number>();
  for (const frame of frames) {
    const label = activityLabel(frame);
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return [...counts.entries()]
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))[0]?.[0] ?? "Tool work";
}

function collapseToolBursts(phases: ActivityPhase[]): ActivityPhase[] {
  const collapsed: ActivityPhase[] = [];
  for (const phase of phases) {
    const previous = collapsed[collapsed.length - 1];
    if (
      previous &&
      previous.kind === "tool" &&
      phase.kind === "tool" &&
      phase.start - previous.end <= TOOL_BURST_GAP_MS
    ) {
      previous.frames.push(...phase.frames);
      previous.end = Math.max(previous.end, phase.end);
      previous.label = dominantActivityLabel(previous.frames);
      previous.outcome = phaseOutcome(previous.frames);
      const failure = previous.frames.find((frame) => frame.outcome === "failure");
      previous.representative = failure ?? previous.frames[previous.frames.length - 1];
      continue;
    }
    collapsed.push(phase);
  }
  return collapsed;
}

function activityPhases(frames: MissionFrame[]): ActivityPhase[] {
  const phases: ActivityPhase[] = [];
  for (const frame of operationalFrames(frames)) {
    const at = timestamp(frame, frame.index);
    const label = activityLabel(frame);
    const previous = phases[phases.length - 1];
    const lifecycleBoundary = frame.kind === "spawn" || frame.kind === "complete";
    if (
      previous &&
      !lifecycleBoundary &&
      previous.kind !== "spawn" &&
      previous.kind !== "complete" &&
      previous.label === label &&
      at - previous.end <= PHASE_GAP_MS
    ) {
      previous.frames.push(frame);
      previous.end = Math.max(previous.end, at);
      previous.representative = frame.outcome === "failure" ? frame : previous.representative;
      previous.outcome = phaseOutcome(previous.frames);
      continue;
    }
    phases.push({
      id: `${frame.actorId}:${frame.index}`,
      frames: [frame],
      representative: frame,
      label,
      kind: frame.kind,
      outcome: frame.outcome,
      start: at,
      end: at,
    });
  }
  return collapseToolBursts(phases);
}

function focusedPhases(phases: ActivityPhase[], frameIndex: number): ActivityPhase[] {
  if (phases.length <= MAX_PHASES_PER_LANE) return phases;
  const active = Math.max(0, phases.findIndex((phase) =>
    phase.frames.some((frame) => frame.index >= frameIndex),
  ));
  const before = Math.floor(MAX_PHASES_PER_LANE / 2);
  const start = Math.max(0, Math.min(phases.length - MAX_PHASES_PER_LANE, active - before));
  return phases.slice(start, start + MAX_PHASES_PER_LANE);
}

function phasePosition(phase: ActivityPhase, start: number, end: number) {
  const left = Math.max(0, Math.min(98, ((phase.start - start) / (end - start)) * 100));
  const recordedWidth = ((phase.end - phase.start) / (end - start)) * 100;
  const contentWidth = Math.min(18, 5.5 + Math.log2(phase.frames.length + 1) * 1.4);
  return { left, width: Math.max(4.5, recordedWidth, contentWidth) };
}

function phaseLayout(phases: ActivityPhase[], start: number, end: number): Map<string, { left: number; width: number }> {
  const layout = phases.map((phase) => {
    const position = phasePosition(phase, start, end);
    const marker = phase.kind === "human" || phase.kind === "spawn" || phase.kind === "complete";
    const width = marker ? position.width : Math.min(position.width, 24);
    return {
      id: phase.id,
      marker,
      left: Math.min(position.left, 100 - width),
      width,
    };
  });
  const cards = layout.filter((item) => !item.marker);
  for (let index = cards.length - 2; index >= 0; index -= 1) {
    const current = cards[index];
    const next = cards[index + 1];
    const latestLeft = next.left - current.width - 0.7;
    current.left = Math.max(0, Math.min(current.left, latestLeft));
  }
  return new Map(layout.map(({ id, left, width }) => [id, { left, width }]));
}

function phaseMeta(phase: ActivityPhase): string {
  const toolCalls = phase.frames.filter((frame) => frame.kind === "tool" && frame.action === "call").length;
  const elapsedSeconds = Math.max(0, (phase.end - phase.start) / 1_000);
  const duration = elapsedSeconds >= 1 ? formatDuration(elapsedSeconds) : null;
  const count = toolCalls > 0
    ? `${toolCalls} ${toolCalls === 1 ? "tool" : "tools"}`
    : `${phase.frames.length} ${phase.frames.length === 1 ? "event" : "events"}`;
  return duration ? `${count} · ${duration}` : count;
}

function phaseSelection(phase: ActivityPhase): ExecutionPhaseSelection {
  const sequences = phase.frames.map((frame) => frame.seq);
  return {
    actorId: phase.representative.actorId,
    label: phase.label,
    representativeIndex: phase.representative.index,
    startSeq: Math.min(...sequences),
    endSeq: Math.max(...sequences),
    startedAt: phase.frames[0]?.timestamp ?? null,
    endedAt: phase.frames[phase.frames.length - 1]?.timestamp ?? null,
    eventCount: phase.frames.length,
    toolEventCount: phase.frames.filter((frame) => frame.kind === "tool").length,
    outcome: phase.outcome,
    models: [...new Set(phase.frames.map((frame) => frame.model).filter((model): model is string => Boolean(model)))],
  };
}

export function ExecutionTimeline({
  replay,
  projection,
  frameIndex,
  selectedActorId,
  onSelectFrame,
  onSelectPhase,
  heading = "Execution timeline",
  description = "recorded multi-agent events",
}: {
  replay: MissionReplay;
  projection: CommandFloorProjection;
  frameIndex: number;
  selectedActorId: string | null;
  onSelectFrame: (index: number) => void;
  onSelectPhase?: (phase: ExecutionPhaseSelection) => void;
  heading?: string;
  description?: string;
}) {
  const window = timelineWindow(replay, frameIndex);
  const laneMap = new Map<string, Lane>();
  for (const frame of window.frames) {
    const lane = laneMap.get(frame.actorId) ?? { actorId: frame.actorId, frames: [], score: 0 };
    lane.frames.push(frame);
    lane.score += lanePriority(frame);
    laneMap.set(frame.actorId, lane);
  }
  const targetLaneCount = Math.min(6, replay.actors.length, 12);
  if (laneMap.size < targetLaneCount) {
    const additionalActors = [...replay.actors]
      .filter((actor) => !laneMap.has(actor.id))
      .sort((left, right) => left.depth - right.depth || right.tool_count - left.tool_count);
    for (const actor of additionalActors) {
      laneMap.set(actor.id, { actorId: actor.id, frames: [], score: 0 });
      if (laneMap.size >= targetLaneCount) break;
    }
  }
  const requiredIds = new Set([replay.rootId, replay.frames[frameIndex]?.actorId, selectedActorId].filter((id): id is string => Boolean(id)));
  const lanes = [...laneMap.values()]
    .sort((left, right) => {
      const leftRequired = requiredIds.has(left.actorId) ? 1 : 0;
      const rightRequired = requiredIds.has(right.actorId) ? 1 : 0;
      return rightRequired - leftRequired || right.score - left.score || left.actorId.localeCompare(right.actorId);
    })
    .slice(0, 12);
  const phasesByActor = new Map(lanes.map((lane) => [lane.actorId, focusedPhases(activityPhases(lane.frames), frameIndex)]));
  const visiblePhaseCount = [...phasesByActor.values()].reduce((total, phases) => total + phases.length, 0);
  const currentFrame = replay.frames[frameIndex];
  const currentAt = currentFrame ? timestamp(currentFrame, window.start) : window.start;
  const cursorLeft = Math.max(0, Math.min(100, ((currentAt - window.start) / (window.end - window.start)) * 100));
  const ticks = Array.from({ length: 5 }, (_, index) => {
    const value = window.start + ((window.end - window.start) * index) / 4;
    return { left: index * 25, label: formatDayTime(new Date(value).toISOString()) };
  });

  return (
    <section className="execution-timeline">
      <header className="execution-timeline__head">
        <div><span className="ops-panel__slash" aria-hidden />{heading} <small>{description}</small></div>
        <span>{lanes.length} crew lanes · {visiblePhaseCount} work phases</span>
      </header>
      <div className="execution-timeline__axis">
        <span>Crew</span>
        <div>{ticks.map((tick) => <time key={tick.left} style={{ left: `${tick.left}%` }}>{tick.label}</time>)}</div>
      </div>
      <div className="execution-timeline__lanes">
        {lanes.map((lane) => {
          const actor = projection.byId.get(lane.actorId);
          const meta = replay.actorById.get(lane.actorId);
          const phases = phasesByActor.get(lane.actorId) ?? [];
          const layout = phaseLayout(phases, window.start, window.end);
          return (
            <div className="execution-lane" key={lane.actorId}>
              <div className="execution-lane__actor">
                <strong>{actor?.callsign ?? callsignForActor(lane.actorId, (meta?.depth ?? 0) > 0)}</strong>
                <small>{actor?.displayName ?? (meta ? missionActorName(meta) : "Agent")}</small>
              </div>
              <div className="execution-lane__track">
                <span className="execution-lane__cursor" style={{ left: `${cursorLeft}%` }} />
                {phases.length === 0 ? <span className="execution-lane__empty">No recorded work in this window</span> : null}
                {phases.map((phase) => {
                  const position = layout.get(phase.id) ?? phasePosition(phase, window.start, window.end);
                  const summary = `${phase.label} · ${phaseMeta(phase)}`;
                  return (
                    <button
                      key={phase.id}
                      type="button"
                      className={`execution-block execution-block--${phase.kind} execution-block--${phase.outcome}`}
                      style={{ left: `${position.left}%`, width: `${position.width}%` }}
                      onClick={() => {
                        onSelectFrame(phase.representative.index);
                        onSelectPhase?.(phaseSelection(phase));
                      }}
                      title={summary}
                      aria-label={`${actor?.callsign ?? lane.actorId}: ${summary}`}
                    >
                      <strong>{phase.label}</strong>
                      <small>{phaseMeta(phase)}</small>
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
      {laneMap.size > lanes.length ? (
        <div className="execution-timeline__overflow">+{laneMap.size - lanes.length} lower-activity lanes outside this window</div>
      ) : null}
    </section>
  );
}
