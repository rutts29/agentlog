import { CommandDeckScene } from "@/components/command-grid/CommandDeckScene";
import type { CommandFloorProjection, Workstation } from "@/lib/commandFloor";
import type { MissionReplay } from "@/lib/missionReplay";

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
  const depthByActor = new Map(
    replay.actors.map((actor) => [actor.id, actor.depth]),
  );

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

      <div className="mission-arena__decks">
        {projection.rooms.map((room) => (
          <CommandDeckScene
            key={room.id}
            room={room}
            projection={projection}
            selectedActorId={selectedActorId}
            onSelectActor={onSelectActor}
            depthByActor={depthByActor}
          />
        ))}
      </div>
    </section>
  );
}
