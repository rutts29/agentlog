import { CrewSprite } from "@/components/command-grid/CrewSprite";
import type { CommandFloorActor, CommandFloorProjection, Workstation } from "@/lib/commandFloor";
import type { MissionReplay } from "@/lib/missionReplay";
import { cn } from "@/lib/utils";

const STATIONS: Array<{ id: Workstation; label: string; code: string }> = [
  { id: "command-deck", label: "Command deck", code: "CMD" },
  { id: "archive-console", label: "Archive console", code: "ARC" },
  { id: "fabrication-bay", label: "Fabrication bay", code: "FAB" },
  { id: "diagnostics-bay", label: "Diagnostics", code: "TST" },
  { id: "review-console", label: "Review console", code: "REV" },
  { id: "server-rack", label: "Systems rack", code: "OPS" },
  { id: "human-gate", label: "Human gate", code: "HUM" },
  { id: "standby", label: "Standby dock", code: "STB" },
];

function stateLabel(actor: CommandFloorActor): string {
  switch (actor.state.kind) {
    case "working":
      return actor.state.tool || actor.state.mode;
    case "orchestrating":
      return `${actor.state.workerCount} linked`;
    case "waiting":
      return "human gate";
    case "idle":
      return "standby";
  }
}

function visibleActors(actors: CommandFloorActor[], selectedActorId: string | null): CommandFloorActor[] {
  if (actors.length <= 8) return actors;
  const selected = selectedActorId
    ? actors.find((actor) => actor.id === selectedActorId)
    : undefined;
  const visible = actors.slice(0, selected ? 7 : 8);
  if (selected && !visible.some((actor) => actor.id === selected.id)) visible.push(selected);
  return visible;
}

export function MissionArena({
  projection,
  replay,
  selectedActorId,
  onSelectActor,
}: {
  projection: CommandFloorProjection;
  replay: MissionReplay;
  selectedActorId: string | null;
  onSelectActor: (id: string) => void;
}) {
  const root = projection.actors.find((actor) => actor.kind === "operator");
  const maxDepth = Math.max(0, ...projection.actors.map((actor) => replay.actorById.get(actor.id)?.depth ?? 0));
  const activeEdges = projection.actors.filter(
    (actor) => actor.kind === "familiar" && actor.lineage.kind === "attached",
  ).length;

  return (
    <section className="mission-arena" aria-label="Mission command arena">
      <header className="mission-arena__head">
        <div>
          <span className="mission-arena__eyebrow">Active execution topology</span>
          <strong>{root?.displayName ?? "Mission command"}</strong>
        </div>
        <div className="mission-arena__topology">
          <span><b>{projection.actors.length}</b> on floor</span>
          <span><b>{activeEdges}</b> links</span>
          <span><b>L{maxDepth}</b> depth</span>
        </div>
      </header>

      <div className="mission-arena__rail" aria-hidden>
        <span className="mission-arena__core">{root?.callsign ?? "A-00"}</span>
        <i />
        {STATIONS.slice(1, 6).map((station) => {
          const count = projection.actors.filter((actor) => actor.workstation === station.id).length;
          return <span key={station.id} className={count > 0 ? "is-active" : ""}>{station.code}<b>{count}</b></span>;
        })}
      </div>

      <div className="mission-arena__stations">
        {STATIONS.map((station) => {
          const actors = projection.actors.filter((actor) => actor.workstation === station.id);
          if (actors.length === 0 && station.id !== "command-deck") return null;
          const visible = visibleActors(actors, selectedActorId);
          return (
            <section className={`mission-zone mission-zone--${station.id}`} key={station.id}>
              <header>
                <span>{station.code}</span>
                <strong>{station.label}</strong>
                <small>{actors.length}</small>
              </header>
              <div className="mission-zone__crew">
                {visible.map((actor) => {
                  const meta = replay.actorById.get(actor.id);
                  return (
                    <button
                      key={actor.id}
                      type="button"
                      className={cn(
                        "mission-actor",
                        `mission-actor--${actor.state.kind}`,
                        actor.kind === "familiar" && "mission-actor--familiar",
                        selectedActorId === actor.id && "mission-actor--selected",
                      )}
                      onClick={() => onSelectActor(actor.id)}
                      aria-pressed={selectedActorId === actor.id}
                      title={`${actor.callsign} · ${actor.displayName} · ${stateLabel(actor)}`}
                    >
                      <CrewSprite actor={actor} />
                      <span className="mission-actor__id">{actor.callsign}</span>
                      <span className="mission-actor__state">{stateLabel(actor)}</span>
                      <span className="mission-actor__depth">L{meta?.depth ?? 0}</span>
                    </button>
                  );
                })}
                {actors.length > visible.length ? (
                  <div className="mission-zone__overflow">
                    <strong>+{actors.length - visible.length}</strong>
                    <span>linked familiars</span>
                  </div>
                ) : null}
              </div>
            </section>
          );
        })}
      </div>
    </section>
  );
}
