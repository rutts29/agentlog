import { Link } from "react-router-dom";
import { callsignForActor } from "@/lib/commandFloor";
import { missionActorName, type MissionReplay } from "@/lib/missionReplay";

export function MissionTopology({
  replay,
  range,
}: {
  replay: MissionReplay;
  range: string;
}) {
  const root = replay.actorById.get(replay.rootId) ?? replay.actors.find((actor) => actor.depth === 0);
  const workers = replay.actors
    .filter((actor) => actor.id !== root?.id)
    .sort((left, right) => left.depth - right.depth || right.tool_count - left.tool_count);
  const shown = workers.slice(0, 6);
  const directWorkers = workers.filter((actor) => actor.depth === 1).length;
  const missionSearch = new URLSearchParams({ root: replay.rootId, range }).toString();

  return (
    <section className="ops-panel sector-map-panel mission-topology-panel">
      <header className="ops-panel__head">
        <div><span className="ops-panel__slash" aria-hidden />Mission map <small>selected execution topology</small></div>
        <span>{replay.actors.length} agents · {Math.max(0, replay.actors.length - 1)} links</span>
      </header>
      <div className="mission-topology" aria-label="Selected mission topology">
        {root ? (
          <Link
            className="mission-topology__root"
            to={`/sessions/${encodeURIComponent(root.navigation_id ?? root.id)}?${new URLSearchParams({ range }).toString()}`}
          >
            <span>{callsignForActor(root.id, false)}</span>
            <strong>{missionActorName(root)}</strong>
            <small>{directWorkers} direct workers · {root.tool_count} tools</small>
          </Link>
        ) : null}
        <div className="mission-topology__trunk" aria-hidden />
        <div className="mission-topology__workers">
          {shown.map((worker) => (
            <Link
              key={worker.id}
              className="mission-topology__worker"
              to={`/sessions/${encodeURIComponent(worker.navigation_id ?? worker.id)}?${new URLSearchParams({ range }).toString()}`}
            >
              <span>{callsignForActor(worker.id, true)}</span>
              <strong>{missionActorName(worker)}</strong>
              <small>L{worker.depth} · {worker.message_count} msgs · {worker.tool_count} tools</small>
            </Link>
          ))}
        </div>
        {workers.length > shown.length ? (
          <div className="mission-topology__overflow">+{workers.length - shown.length} workers in full lineage</div>
        ) : null}
      </div>
      <Link className="mission-topology__open" to={`/orchestration?${missionSearch}`}>
        Open mission lineage →
      </Link>
    </section>
  );
}
