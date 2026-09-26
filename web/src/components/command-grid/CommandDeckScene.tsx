import { CrewSprite } from "@/components/command-grid/CrewSprite";
import type {
  CommandFloorActor,
  CommandFloorProjection,
  CommandFloorRoom,
  Workstation,
} from "@/lib/commandFloor";
import { cn } from "@/lib/utils";

const STATIONS: Array<{ id: Workstation; code: string; label: string }> = [
  { id: "command-deck", code: "CMD", label: "Command console" },
  { id: "archive-console", code: "ARC", label: "Archive terminal" },
  { id: "fabrication-bay", code: "FAB", label: "Fabrication bay" },
  { id: "diagnostics-bay", code: "TST", label: "Diagnostics rig" },
  { id: "review-console", code: "REV", label: "Review console" },
  { id: "server-rack", code: "OPS", label: "Systems rack" },
  { id: "human-gate", code: "HUM", label: "Human gate" },
  { id: "standby", code: "STB", label: "Standby dock" },
];

function actorActivity(actor: CommandFloorActor): string {
  switch (actor.state.kind) {
    case "working":
      return actor.state.label;
    case "orchestrating":
      return `${actor.state.workerCount} linked workers active`;
    case "waiting":
      return actor.state.label;
    case "idle":
      return "No current activity signal";
  }
}

function actorState(actor: CommandFloorActor): string {
  switch (actor.state.kind) {
    case "working":
      return actor.state.tool || actor.state.mode;
    case "orchestrating":
      return "coordinating";
    case "waiting":
      return "waiting";
    case "idle":
      return "idle";
  }
}

export function CommandDeckScene({
  room,
  projection,
  selectedActorId,
  onSelectActor,
  depthByActor,
}: {
  room: CommandFloorRoom;
  projection: CommandFloorProjection;
  selectedActorId: string | null;
  onSelectActor: (id: string) => void;
  depthByActor?: Map<string, number>;
}) {
  const actors = room.actorIds
    .map((id) => projection.byId.get(id))
    .filter((actor): actor is CommandFloorActor => Boolean(actor));
  const visibleActorIds = new Set<string>();
  for (const station of STATIONS) {
    const stationActors = actors.filter((actor) => actor.workstation === station.id);
    const selected = selectedActorId
      ? stationActors.find((actor) => actor.id === selectedActorId)
      : undefined;
    const visible = stationActors.slice(0, selected ? 3 : 4);
    if (selected && !visible.some((actor) => actor.id === selected.id)) visible.push(selected);
    visible.forEach((actor) => visibleActorIds.add(actor.id));
  }
  const slots = new Map<Workstation, number>();

  return (
    <section className="command-deck-scene" aria-label={`${room.project} mission deck`}>
      <div className="command-deck-scene__routes" aria-hidden>
        <i className="route route--left" />
        <i className="route route--right" />
        <i className="route route--cross" />
      </div>
      <div className="command-deck-scene__core" aria-hidden>
        <span>LOCAL GRID</span>
        <b>{room.operatorIds.length > 0 ? "LINKED" : "UNATTACHED"}</b>
      </div>

      {STATIONS.map((station) => {
        const count = actors.filter((actor) => actor.workstation === station.id).length;
        return (
          <div
            key={station.id}
            className={cn(
              "deck-station",
              `deck-station--${station.id}`,
              count > 0 && "deck-station--occupied",
            )}
            aria-hidden
          >
            <span className="deck-station__screen"><i /><i /><i /></span>
            <span className="deck-station__label">{station.code} · {station.label}</span>
            {count > 0 ? <b>{count}</b> : null}
          </div>
        );
      })}

      {actors.filter((actor) => visibleActorIds.has(actor.id)).map((actor) => {
        const slot = slots.get(actor.workstation) ?? 0;
        slots.set(actor.workstation, slot + 1);
        const parent = actor.kind === "familiar" && actor.lineage.kind === "attached"
          ? projection.byId.get(actor.lineage.parentId)
          : null;
        const activity = actorActivity(actor);
        return (
          <button
            key={actor.id}
            type="button"
            className={cn(
              "deck-agent",
              `deck-agent--${actor.workstation}`,
              `deck-agent--slot-${slot % 8}`,
              `deck-agent--${actor.state.kind}`,
              actor.kind === "familiar" && "deck-agent--familiar",
              selectedActorId === actor.id && "deck-agent--selected",
            )}
            onClick={() => onSelectActor(actor.id)}
            aria-pressed={selectedActorId === actor.id}
            aria-label={`${actor.callsign}, ${actor.displayName}, ${activity}`}
          >
            <span className="deck-agent__shadow" aria-hidden />
            <CrewSprite actor={actor} />
            <span className="deck-agent__callsign">{actor.callsign}</span>
            <span className="deck-agent__state">{actorState(actor)}</span>
            <span className="deck-agent__tooltip" role="tooltip">
              <strong>{actor.callsign} · {actor.displayName}</strong>
              <span>{activity}</span>
              <small>
                {actor.specialty} · {actor.harnessDisplay} · {actor.workstation.replaceAll("-", " ")}
              </small>
              <small>
                {parent ? `reports to ${parent.callsign}` : "root operator"}
                {depthByActor ? ` · level ${depthByActor.get(actor.id) ?? 0}` : ""}
              </small>
            </span>
          </button>
        );
      })}
    </section>
  );
}
