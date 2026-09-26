import { Link } from "react-router-dom";
import { callsignForActor } from "@/lib/commandFloor";
import type { MissionFrame, MissionReplay } from "@/lib/missionReplay";
import { formatDayTime } from "@/lib/utils";

function isEvidence(frame: MissionFrame): boolean {
  return frame.kind === "tool" && (
    frame.operationKind === "artifact_write" ||
    /\b(write|edit|patch|create|save)\b/i.test(`${frame.tool ?? ""} ${frame.action ?? ""}`)
  );
}

export function EvidenceTrail({
  replay,
  frameIndex,
  range,
}: {
  replay: MissionReplay;
  frameIndex: number;
  range: string;
}) {
  const evidence = replay.frames
    .filter((frame) => frame.index <= frameIndex && isEvidence(frame))
    .slice(-6)
    .reverse();

  return (
    <section className="ops-panel tools-panel evidence-trail-panel">
      <header className="ops-panel__head">
        <div><span className="ops-panel__slash" aria-hidden />Evidence trail <small>recorded writes and patches</small></div>
        <span>{evidence.length} recent</span>
      </header>
      {evidence.length === 0 ? (
        <div className="ops-empty">No recorded write or patch tools in this mission window.</div>
      ) : (
        <div className="evidence-trail-list">
          {evidence.map((frame) => {
            const actor = replay.actorById.get(frame.actorId);
            const callsign = callsignForActor(frame.actorId, (actor?.depth ?? 0) > 0);
            const search = new URLSearchParams({ range, replayFrame: String(frame.index) });
            if (frame.messageId) search.set("msg", frame.messageId);
            return (
              <Link
                key={frame.id}
                to={`/sessions/${encodeURIComponent(frame.actorId)}?${search.toString()}`}
                className={`evidence-trail-row evidence-trail-row--${frame.outcome}`}
              >
                <span>{frame.tool || frame.title}</span>
                <span>{frame.detail}</span>
                <small>{callsign} · {formatDayTime(frame.timestamp)}</small>
              </Link>
            );
          })}
        </div>
      )}
    </section>
  );
}
