import type { CommandFloorProjection } from "@/lib/commandFloor";
import { callsignForActor } from "@/lib/commandFloor";
import type { MissionFrame, MissionReplay } from "@/lib/missionReplay";
import { formatDayTime, formatDuration } from "@/lib/utils";

function sourceLabel(
  replay: MissionReplay,
  projection: CommandFloorProjection,
  frame: MissionFrame,
): string {
  const active = projection.byId.get(frame.actorId);
  if (active) return active.callsign;
  const actor = replay.actorById.get(frame.actorId);
  return callsignForActor(frame.actorId, (actor?.depth ?? 0) > 0);
}

export function TraceShards({
  replay,
  projection,
  frameIndex,
  onSelectFrame,
}: {
  replay: MissionReplay;
  projection: CommandFloorProjection;
  frameIndex: number;
  onSelectFrame: (index: number) => void;
}) {
  const frames = replay.frames
    .filter((frame) =>
      frame.index <= frameIndex &&
      frame.kind !== "context" &&
      frame.kind !== "system" &&
      frame.detail !== "No text captured"
    )
    .slice(-10)
    .reverse();

  return (
    <section className="ops-panel trace-panel trace-shards-panel">
      <header className="ops-panel__head">
        <div><span className="ops-panel__slash" aria-hidden />Trace shards <small>selected mission event stream</small></div>
        <span>{frames.length} recent events</span>
      </header>
      <div className="trace-shards-table" role="table" aria-label="Recent mission events">
        <div className="trace-shards-row trace-shards-row--head" role="row">
          <span>Time</span><span>Source</span><span>Event</span><span>Details</span><span>Duration</span>
        </div>
        {frames.map((frame) => (
          <button
            key={frame.id}
            type="button"
            className={`trace-shards-row trace-shards-row--${frame.kind} trace-shards-row--${frame.outcome}`}
            onClick={() => onSelectFrame(frame.index)}
            role="row"
          >
            <time>{formatDayTime(frame.timestamp)}</time>
            <span className="trace-shards-row__source">{sourceLabel(replay, projection, frame)}</span>
            <span>{frame.title}</span>
            <span>{frame.detail}</span>
            <span>{frame.durationMs == null ? "—" : formatDuration(frame.durationMs / 1000)}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
