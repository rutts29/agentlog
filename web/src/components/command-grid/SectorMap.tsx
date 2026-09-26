import { Link } from "react-router-dom";
import type { GraphPayload, GraphRepoNode, GraphSessionNode } from "@/lib/api";
import { cn, formatDuration } from "@/lib/utils";

type Sector = {
  repo: GraphRepoNode;
  sessions: GraphSessionNode[];
};

function sectorsFor(data: GraphPayload): Sector[] {
  const repos = data.nodes.filter((node): node is GraphRepoNode => node.kind === "repo").slice(0, 4);
  const sessions = data.nodes.filter((node): node is GraphSessionNode => node.kind === "session");
  const repoBySession = new Map<string, string>();
  for (const edge of data.edges) {
    if (edge.kind === "membership") repoBySession.set(edge.source, edge.target);
  }
  return repos.map((repo) => ({
    repo,
    sessions: sessions
      .filter((session) => repoBySession.get(session.id) === repo.id)
      .sort((left, right) => {
        const parentOrder = Number(Boolean(left.parent_id)) - Number(Boolean(right.parent_id));
        return parentOrder || String(left.started_at).localeCompare(String(right.started_at));
      })
      .slice(0, 16),
  }));
}

function sessionCode(session: GraphSessionNode, index: number): string {
  return `${session.parent_id ? "F" : "A"}-${String(index + 1).padStart(2, "0")}`;
}

export function SectorMap({
  data,
  search,
  compact = false,
}: {
  data: GraphPayload;
  search: string;
  compact?: boolean;
}) {
  const sectors = sectorsFor(data);
  const range = new URLSearchParams(search).get("range") ?? "24h";

  return (
    <section className={cn("ops-panel sector-map-panel", compact && "sector-map-panel--compact")}>
      <header className="ops-panel__head">
        <div><span className="ops-panel__slash" aria-hidden />Sector map <small>session topology</small></div>
        <span>{data.counts.sessions} sessions · {data.counts.orchestration_edges} links</span>
      </header>
      <div className="sector-topology" aria-label="Session topology by project sector">
        {sectors.map((sector, sectorIndex) => (
          <section className="sector-topology__sector" key={sector.repo.id}>
            <Link
              className="sector-hub"
              to={`/sessions?${new URLSearchParams({ range, project: sector.repo.label }).toString()}`}
            >
              <span>S{sectorIndex + 1}</span>
              <strong>{sector.repo.label}</strong>
              <small>{sector.repo.sessions} missions · {sector.repo.tools} tools</small>
            </Link>
            <div className="sector-branches">
              {sector.sessions.map((session, sessionIndex) => (
                <Link
                  key={session.id}
                  className={cn("sector-node", session.parent_id && "sector-node--child")}
                  to={`/sessions/${encodeURIComponent(session.id)}?${search}`}
                >
                  <span className="sector-node__code">{sessionCode(session, sessionIndex)}</span>
                  <span className="sector-node__copy">
                    <strong>{session.model || session.logical_harness || session.harness}</strong>
                    <small>{session.messages} msgs · {session.tools} tools</small>
                  </span>
                  <span className="sector-node__duration">{formatDuration(session.duration_seconds)}</span>
                  {session.parent_id ? <i aria-label="Worker link">↳</i> : <i aria-label="Coordinator">◆</i>}
                </Link>
              ))}
              {sector.repo.sessions > sector.sessions.length ? (
                <div className="sector-node sector-node--overflow">
                  +{sector.repo.sessions - sector.sessions.length} missions outside this map window
                </div>
              ) : null}
            </div>
          </section>
        ))}
        {sectors.length === 0 ? <div className="ops-empty">No session topology in this range.</div> : null}
      </div>
    </section>
  );
}
