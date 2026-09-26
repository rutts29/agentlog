import { CrewSprite } from "@/components/command-grid/CrewSprite";
import type { CommandFloorActor, CommandFloorProjection } from "@/lib/commandFloor";
import { cn } from "@/lib/utils";

function stateLabel(actor: CommandFloorActor): string {
  switch (actor.state.kind) {
    case "working":
      return actor.state.label;
    case "orchestrating":
      return `${actor.state.workerCount} familiar${actor.state.workerCount === 1 ? "" : "s"} deployed`;
    case "waiting":
      return "awaiting human command";
    case "idle":
      return "signal unavailable";
  }
}

function stationLabel(actor: CommandFloorActor): string {
  return actor.workstation.replaceAll("-", " ");
}

export function CommandFloor({
  projection,
  selectedActorId,
  onSelectActor,
}: {
  projection: CommandFloorProjection;
  selectedActorId: string | null;
  onSelectActor: (id: string) => void;
}) {
  if (projection.actors.length === 0) {
    return (
      <div className="command-floor command-floor--empty">
        <div className="command-floor__empty-mark" aria-hidden>+</div>
        <div className="command-floor__empty-title">Command floor standing by</div>
        <div className="command-floor__empty-copy">No active local sessions are currently observed.</div>
      </div>
    );
  }

  return (
    <div className="command-floor" aria-label="Live command floor">
      <div className="command-floor__grid">
        {projection.rooms.map((room, roomIndex) => (
          <section className="command-room" key={room.id} aria-label={`${room.project} sector`}>
            <header className="command-room__head">
              <div>
                <span className="command-room__sector">Sector {String.fromCharCode(65 + (roomIndex % 26))}</span>
                <span className="command-room__project">{room.project}</span>
              </div>
              <span className="command-room__count">{room.actorIds.length} active</span>
            </header>
            <div className="command-room__stations">
              {room.actorIds.map((id) => {
                const actor = projection.byId.get(id);
                if (!actor) return null;
                const parent =
                  actor.kind === "familiar" && actor.lineage.kind === "attached"
                    ? projection.byId.get(actor.lineage.parentId)
                    : null;
                return (
                  <button
                    key={actor.id}
                    type="button"
                    className={cn(
                      "crew-station",
                      `crew-station--${actor.state.kind}`,
                      actor.kind === "familiar" && "crew-station--familiar",
                      selectedActorId === actor.id && "crew-station--selected",
                    )}
                    onClick={() => onSelectActor(actor.id)}
                    aria-pressed={selectedActorId === actor.id}
                  >
                    <span className="crew-station__platform" aria-hidden />
                    <CrewSprite actor={actor} />
                    <span className="crew-station__identity">
                      <span className="crew-station__callsign">{actor.callsign}</span>
                      <span className="crew-station__name">{actor.displayName}</span>
                    </span>
                    <span className="crew-station__activity">{stateLabel(actor)}</span>
                    <span className="crew-station__station">{stationLabel(actor)}</span>
                    {parent ? (
                      <span className="crew-station__parent">linked to {parent.callsign}</span>
                    ) : null}
                  </button>
                );
              })}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}

