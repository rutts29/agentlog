import { callsignForActor, type CommandFloorProjection } from "@/lib/commandFloor";
import type { MissionFrame, MissionReplay } from "@/lib/missionReplay";
import { formatDayTime } from "@/lib/utils";

type Lane = {
  actorId: string;
  frames: MissionFrame[];
  score: number;
};

type FrameCluster = {
  id: string;
  frames: MissionFrame[];
  representative: MissionFrame;
  kind: MissionFrame["kind"];
  outcome: MissionFrame["outcome"];
};

function timestamp(frame: MissionFrame, fallback: number): number {
  const value = frame.timestamp ? Date.parse(frame.timestamp) : Number.NaN;
  return Number.isFinite(value) ? value : fallback;
}

function timelineWindow(replay: MissionReplay, frameIndex: number) {
  const current = replay.frames[frameIndex];
  const currentAt = current?.timestamp ? Date.parse(current.timestamp) : Number.NaN;
  let frames: MissionFrame[] = [];
  if (Number.isFinite(currentAt)) {
    for (const horizon of [15, 60]) {
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

function blockPosition(frame: MissionFrame, localIndex: number, count: number, start: number, end: number) {
  const at = timestamp(frame, start + localIndex);
  const left = Math.max(0, Math.min(99, ((at - start) / (end - start)) * 100));
  const durationWidth = frame.durationMs && frame.durationMs > 0
    ? (frame.durationMs / (end - start)) * 100
    : 0;
  const clusterWidth = count > 1 ? Math.min(12, 2.4 + Math.log2(count) * 1.5) : 0;
  return { left, width: Math.max(frame.kind === "tool" ? 1.8 : 0.8, clusterWidth, Math.min(24, durationWidth)) };
}

function lanePriority(frame: MissionFrame): number {
  if (frame.kind === "tool") return 4;
  if (frame.kind === "human") return 3;
  if (frame.kind === "agent") return 2;
  return 1;
}

function clusterFrames(frames: MissionFrame[], start: number, end: number): FrameCluster[] {
  const bucketMs = Math.max(1_000, (end - start) / 48);
  const buckets = new Map<number, MissionFrame[]>();
  frames.forEach((frame, index) => {
    const at = timestamp(frame, start + index);
    const bucket = Math.floor((at - start) / bucketMs);
    const values = buckets.get(bucket) ?? [];
    values.push(frame);
    buckets.set(bucket, values);
  });
  return [...buckets.entries()].map(([bucket, values]) => {
    const failure = values.find((frame) => frame.outcome === "failure");
    const human = values.find((frame) => frame.kind === "human");
    const tool = values.find((frame) => frame.kind === "tool");
    const representative = failure ?? human ?? tool ?? values[values.length - 1];
    return {
      id: `${values[0].actorId}:${bucket}`,
      frames: values,
      representative,
      kind: tool ? "tool" : human ? "human" : representative.kind,
      outcome: failure ? "failure" : values.some((frame) => frame.outcome === "success") ? "success" : "unknown",
    };
  });
}

function compactToolName(tool: string): string {
  const normalized = tool.toLowerCase();
  if (/test|pytest|jest|vitest/.test(normalized)) return "tests";
  if (/patch|edit|write/.test(normalized)) return "patch";
  if (/read|search|grep|glob|find/.test(normalized)) return "inspect";
  if (/spawn|fork|delegate/.test(normalized)) return "spawn";
  if (/wait/.test(normalized)) return "wait";
  if (/exec|command|shell|terminal|stdin/.test(normalized)) return "exec";
  return tool.replaceAll("_", " ").slice(0, 12);
}

function clusterLabel(cluster: FrameCluster): string {
  const semanticText = cluster.frames
    .map((frame) => `${frame.tool ?? ""} ${frame.operationKind ?? ""} ${frame.action ?? ""} ${frame.context ?? ""} ${frame.detail}`)
    .join(" ")
    .toLowerCase();
  if (/pytest|vitest|jest|cargo test|go test|npm test|run_tests|\btests?\b/.test(semanticText)) return "tests";
  if (/apply_patch|\bpatch\b|\bedit(?:ed|ing)?\b|\bwrite(?:s|ing)?\b/.test(semanticText)) return "patch";
  if (/git diff|\breview(?:ed|ing)?\b/.test(semanticText)) return "review";
  if (/read_file|ripgrep|\bgrep\b|\bsearch\b|\binspect(?:ed|ing)?\b|\bfind\b/.test(semanticText)) return "inspect";
  if (/spawn_agent|\bspawn\b|\bfork\b|\bdelegate\b/.test(semanticText)) return "spawn";
  if (/wait_agent|\bwait(?:ed|ing)?\b/.test(semanticText)) return "wait";
  const tools = cluster.frames
    .map((frame) => frame.tool)
    .filter((tool): tool is string => Boolean(tool));
  if (tools.length > 0) {
    const counts = new Map<string, number>();
    for (const tool of tools) counts.set(tool, (counts.get(tool) ?? 0) + 1);
    const dominant = [...counts.entries()].sort((left, right) => right[1] - left[1])[0]?.[0];
    return dominant ? compactToolName(dominant) : "tool";
  }
  if (cluster.outcome === "failure") return "failed";
  switch (cluster.kind) {
    case "human": return "command";
    case "agent": return "response";
    case "spawn": return "spawn";
    case "complete": return "returned";
    case "context": return "brief";
    case "system": return "system";
    case "tool": return "tool";
  }
}

export function ExecutionTimeline({
  replay,
  projection,
  frameIndex,
  selectedActorId,
  onSelectFrame,
  heading = "Execution timeline",
  description = "recorded multi-agent events",
}: {
  replay: MissionReplay;
  projection: CommandFloorProjection;
  frameIndex: number;
  selectedActorId: string | null;
  onSelectFrame: (index: number) => void;
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
  const requiredIds = new Set([replay.rootId, replay.frames[frameIndex]?.actorId, selectedActorId].filter((id): id is string => Boolean(id)));
  const lanes = [...laneMap.values()]
    .sort((left, right) => {
      const leftRequired = requiredIds.has(left.actorId) ? 1 : 0;
      const rightRequired = requiredIds.has(right.actorId) ? 1 : 0;
      return rightRequired - leftRequired || right.score - left.score || left.actorId.localeCompare(right.actorId);
    })
    .slice(0, 12);
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
        <span>{lanes.length} crew lanes · {window.frames.length} events in view</span>
      </header>
      <div className="execution-timeline__axis">
        <span>Crew</span>
        <div>{ticks.map((tick) => <time key={tick.left} style={{ left: `${tick.left}%` }}>{tick.label}</time>)}</div>
      </div>
      <div className="execution-timeline__lanes">
        {lanes.map((lane) => {
          const actor = projection.byId.get(lane.actorId);
          const meta = replay.actorById.get(lane.actorId);
          const clusters = clusterFrames(lane.frames, window.start, window.end);
          return (
            <div className="execution-lane" key={lane.actorId}>
              <div className="execution-lane__actor">
                <strong>{actor?.callsign ?? callsignForActor(lane.actorId, (meta?.depth ?? 0) > 0)}</strong>
                <small>{actor?.displayName ?? meta?.model ?? meta?.harness ?? "agent"}</small>
              </div>
              <div className="execution-lane__track">
                <span className="execution-lane__cursor" style={{ left: `${cursorLeft}%` }} />
                {clusters.map((cluster, localIndex) => {
                  const frame = cluster.representative;
                  const position = blockPosition(frame, localIndex, cluster.frames.length, window.start, window.end);
                  const toolNames = [...new Set(cluster.frames.map((item) => item.tool).filter((value): value is string => Boolean(value)))];
                  const summary = cluster.frames.length > 1
                    ? `${cluster.frames.length} events${toolNames.length > 0 ? ` · ${toolNames.slice(0, 3).join(", ")}` : ""}`
                    : `${frame.title} · ${frame.detail}`;
                  return (
                    <button
                      key={cluster.id}
                      type="button"
                      className={`execution-block execution-block--${cluster.kind} execution-block--${cluster.outcome}`}
                      style={{ left: `${position.left}%`, width: `${position.width}%` }}
                      onClick={() => onSelectFrame(frame.index)}
                      title={summary}
                      aria-label={`${actor?.callsign ?? lane.actorId}: ${summary}`}
                    >
                      {clusterLabel(cluster)}
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
      {laneMap.size > lanes.length ? (
        <div className="execution-timeline__overflow">+{laneMap.size - lanes.length} lower-activity lanes hidden in this window</div>
      ) : null}
    </section>
  );
}
