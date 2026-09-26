import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type { PresenceEvent } from "@/lib/api";
import { CommandFloor } from "@/components/command-grid/CommandFloor";
import { projectCommandFloor, type CommandFloorActor } from "@/lib/commandFloor";
import { useIngestStream } from "@/lib/useIngestStream";
import { useLivePresence } from "@/lib/useLivePresence";

function actorStateLabel(actor: CommandFloorActor): string {
  switch (actor.state.kind) {
    case "working":
      return `${actor.state.mode} · ${actor.state.label}`;
    case "orchestrating":
      return `orchestrating · ${actor.state.workerCount} ${actor.state.workerCount === 1 ? "worker" : "workers"}`;
    case "waiting":
      return "human gate · awaiting input";
    case "idle":
      return "standby · activity unavailable";
  }
}

export function CommandFloorView() {
  const [params, setParams] = useSearchParams();
  const [selectedActorId, setSelectedActorId] = useState<string | null>(params.get("crew"));
  const presenceHandler = useRef<(data: PresenceEvent) => void>(() => {});
  const { connected } = useIngestStream(() => undefined, (data) => presenceHandler.current(data));
  const { presence, onPresenceEvent } = useLivePresence(connected);
  useEffect(() => {
    presenceHandler.current = onPresenceEvent;
  }, [onPresenceEvent]);

  const projection = useMemo(
    () => projectCommandFloor(presence.sessions),
    [presence.sessions],
  );
  const selected = selectedActorId ? projection.byId.get(selectedActorId) ?? null : null;
  const traceSearch = (() => {
    const next = new URLSearchParams(params);
    next.delete("crew");
    return next.toString();
  })();

  useEffect(() => {
    if (selectedActorId && !projection.byId.has(selectedActorId)) setSelectedActorId(null);
  }, [projection.byId, selectedActorId]);

  const selectActor = (id: string) => {
    setSelectedActorId(id);
    const next = new URLSearchParams(params);
    next.set("crew", id);
    setParams(next, { replace: true });
  };

  return (
    <div className="command-floor-view">
      <header className="command-page-head">
        <div>
          <div className="command-page-head__eyebrow">live local projection</div>
          <h1>Command floor</h1>
          <p>Real sessions and spawned workers, mapped to crew and familiars.</p>
        </div>
        <div className="command-page-head__metrics">
          <span><strong>{projection.operatorCount}</strong> operators</span>
          <span><strong>{projection.familiarCount}</strong> familiars</span>
          <span><strong>{projection.workingCount}</strong> working</span>
          <span className={projection.waitingCount > 0 ? "is-warn" : ""}><strong>{projection.waitingCount}</strong> human gates</span>
        </div>
      </header>

      <div className="command-floor-layout">
        <CommandFloor
          projection={projection}
          selectedActorId={selectedActorId}
          onSelectActor={selectActor}
        />
        <aside className="crew-inspector" aria-live="polite">
          <div className="crew-inspector__head">
            <span>crew inspector</span>
            <span className={connected ? "is-live" : ""}>{connected ? "stream linked" : "fallback link"}</span>
          </div>
          {selected ? (
            <div className="crew-inspector__body">
              <div className="crew-inspector__callsign">{selected.callsign}</div>
              <h2>{selected.displayName}</h2>
              <div className="crew-inspector__chips">
                <span>{selected.kind}</span>
                <span>{selected.specialty}</span>
                <span>{selected.harnessDisplay}</span>
              </div>
              <dl>
                <div><dt>State</dt><dd>{actorStateLabel(selected)}</dd></div>
                <div><dt>Station</dt><dd>{selected.workstation.replaceAll("-", " ")}</dd></div>
                <div><dt>Project</dt><dd>{selected.project}</dd></div>
                <div><dt>Evidence</dt><dd>{selected.specialtyEvidence} specialty · {selected.sourceSnapshotStatus} source</dd></div>
                <div><dt>Lineage</dt><dd>{selected.lineage.kind === "root" ? "root operator" : selected.lineage.kind === "attached" ? `attached to ${projection.byId.get(selected.lineage.parentId)?.callsign ?? selected.lineage.parentId}` : "parent unavailable"}</dd></div>
              </dl>
              {selected.sessionId ? (
                <Link className="command-link" to={`/sessions/${encodeURIComponent(selected.sessionId)}${traceSearch ? `?${traceSearch}` : ""}`}>
                  Open holotrace →
                </Link>
              ) : (
                <div className="crew-inspector__pending">Transcript pending ingest</div>
              )}
            </div>
          ) : (
            <div className="crew-inspector__empty">Select an operator or familiar to inspect its live state and evidence link.</div>
          )}
          {projection.unattachedFamiliarCount > 0 ? (
            <div className="crew-inspector__warning">{projection.unattachedFamiliarCount} familiar lineage {projection.unattachedFamiliarCount === 1 ? "link is" : "links are"} unavailable.</div>
          ) : null}
        </aside>
      </div>
    </div>
  );
}
