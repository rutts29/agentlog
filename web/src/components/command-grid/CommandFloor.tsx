import { CommandDeckScene } from "@/components/command-grid/CommandDeckScene";
import type { CommandFloorProjection } from "@/lib/commandFloor";

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
          <section className="command-room" key={room.id} aria-label={`${room.project} mission section`}>
            <header className="command-room__head">
              <div>
                <span className="command-room__sector">Deck {String(roomIndex + 1).padStart(2, "0")}</span>
                <span className="command-room__project">{room.project}</span>
              </div>
              <span className="command-room__count">
                {room.operatorIds.length} coordinator · {room.familiarIds.length} workers
              </span>
            </header>
            <CommandDeckScene
              room={room}
              projection={projection}
              selectedActorId={selectedActorId}
              onSelectActor={onSelectActor}
            />
          </section>
        ))}
      </div>
    </div>
  );
}
