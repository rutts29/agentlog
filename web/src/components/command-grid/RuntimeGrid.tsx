import { Link } from "react-router-dom";
import type { SessionRow } from "@/lib/api";
import type { CommandFloorActor } from "@/lib/commandFloor";
import { formatDuration } from "@/lib/utils";

function actorState(actor: CommandFloorActor): string {
  switch (actor.state.kind) {
    case "working":
      return actor.state.label;
    case "orchestrating":
      return `${actor.state.workerCount} familiar${actor.state.workerCount === 1 ? "" : "s"} deployed`;
    case "waiting":
      return "awaiting human command";
    case "idle":
      return "activity unavailable";
  }
}

export function RuntimeGrid({
  actors,
  missions,
  search,
}: {
  actors: CommandFloorActor[];
  missions: SessionRow[];
  search: string;
}) {
  const shownActors = actors.slice(0, 6);
  const shownMissions = missions.slice(0, Math.max(0, 6 - shownActors.length));
  const maxDuration = Math.max(...shownMissions.map((mission) => mission.duration_seconds ?? 0), 1);

  return (
    <section className="ops-panel runtime-grid-panel">
      <header className="ops-panel__head">
        <div><span className="ops-panel__slash" aria-hidden />Live grid <small>observed execution lanes</small></div>
        <span>{actors.length} active · {missions.length} recent</span>
      </header>
      <div className="runtime-axis" aria-hidden>
        <span>crew / mission</span><span>start</span><span>tools</span><span>handoff</span><span>now</span>
      </div>
      <div className="runtime-lanes">
        {shownActors.map((actor) => (
          <Link
            key={actor.id}
            className={`runtime-lane runtime-lane--${actor.state.kind}`}
            to={`/floor?${new URLSearchParams({ range: new URLSearchParams(search).get("range") ?? "24h", crew: actor.id }).toString()}`}
          >
            <span className="runtime-lane__identity">
              <strong>{actor.callsign}</strong>
              <small>{actor.displayName}</small>
            </span>
            <span className="runtime-lane__track">
              <span className="runtime-lane__block runtime-lane__block--live">
                {actorState(actor)}
              </span>
              {actor.childIds.length > 0 ? (
                <span className="runtime-lane__handoff">{actor.childIds.length} spawned</span>
              ) : null}
              <span className="runtime-lane__now" />
            </span>
            <span className="runtime-lane__status">{actor.kind}</span>
          </Link>
        ))}
        {shownMissions.map((mission) => {
          const width = Math.max(18, ((mission.duration_seconds ?? 0) / maxDuration) * 68);
          return (
            <Link
              key={mission.id}
              className="runtime-lane runtime-lane--history"
              to={`/sessions/${encodeURIComponent(mission.navigation_id ?? mission.id)}${search ? `?${search}` : ""}`}
            >
              <span className="runtime-lane__identity"><strong>{mission.harness}</strong><small>{mission.project}</small></span>
              <span className="runtime-lane__track">
                <span className="runtime-lane__block" style={{ width: `${width}%` }}>{mission.model}</span>
                {mission.tool_count > 0 ? <span className="runtime-lane__tools">{mission.tool_count} tools</span> : null}
              </span>
              <span className="runtime-lane__status">{formatDuration(mission.duration_seconds)}</span>
            </Link>
          );
        })}
        {shownActors.length === 0 && shownMissions.length === 0 ? (
          <div className="runtime-lanes__empty">No mission lanes in this range.</div>
        ) : null}
      </div>
    </section>
  );
}
