import { Link } from "react-router-dom";
import type { GraphPayload, GraphRepoNode, GraphSessionNode } from "@/lib/api";
import { cn, harnessColor } from "@/lib/utils";

type Point = { x: number; y: number };

const SECTOR_POINTS: Point[] = [
  { x: 20, y: 20 },
  { x: 74, y: 20 },
  { x: 20, y: 66 },
  { x: 74, y: 66 },
];

function repoNodes(data: GraphPayload): GraphRepoNode[] {
  return data.nodes.filter((node): node is GraphRepoNode => node.kind === "repo").slice(0, 4);
}

function sessionNodes(data: GraphPayload): GraphSessionNode[] {
  return data.nodes.filter((node): node is GraphSessionNode => node.kind === "session").slice(0, 16);
}

function positionsFor(data: GraphPayload) {
  const repos = repoNodes(data);
  const sessions = sessionNodes(data);
  const positions = new Map<string, Point>();
  const repoBySession = new Map<string, string>();
  for (const edge of data.edges) {
    if (edge.kind !== "membership") continue;
    repoBySession.set(edge.source, edge.target);
  }
  repos.forEach((repo, index) => positions.set(repo.id, SECTOR_POINTS[index]));
  const grouped = new Map<string, GraphSessionNode[]>();
  for (const session of sessions) {
    const repoId = repoBySession.get(session.id) ?? repos[0]?.id ?? "unassigned";
    const list = grouped.get(repoId) ?? [];
    list.push(session);
    grouped.set(repoId, list);
  }
  for (const [repoId, nodes] of grouped) {
    const anchor = positions.get(repoId) ?? { x: 48, y: 42 };
    nodes.forEach((node, index) => {
      const column = index % 3;
      const row = Math.floor(index / 3);
      positions.set(node.id, {
        x: Math.min(94, anchor.x + 8 + column * 7),
        y: Math.min(80, anchor.y + 3 + row * 7),
      });
    });
  }
  return { repos, sessions, positions };
}

export function SectorMap({ data, search, compact = false }: { data: GraphPayload; search: string; compact?: boolean }) {
  const { repos, sessions, positions } = positionsFor(data);
  const visibleIds = new Set([...repos, ...sessions].map((node) => node.id));
  const edges = data.edges.filter(
    (edge) => edge.kind === "orchestration" && visibleIds.has(edge.source) && visibleIds.has(edge.target),
  );

  return (
    <section className={cn("ops-panel sector-map-panel", compact && "sector-map-panel--compact")}>
      <header className="ops-panel__head">
        <div><span className="ops-panel__slash" aria-hidden />Sector map <small>session topology</small></div>
        <span>{data.counts.sessions} sessions · {data.counts.orchestration_edges} links</span>
      </header>
      <div className="sector-map-wrap">
        <svg className="sector-map" viewBox="0 0 100 86" role="img" aria-label="Session topology by project sector">
          <path className="sector-map__divider" d="M50 4V82M4 43H96" />
          {edges.map((edge) => {
            const source = positions.get(edge.source);
            const target = positions.get(edge.target);
            if (!source || !target) return null;
            return <line key={`${edge.source}-${edge.target}`} className="sector-map__edge" x1={source.x} y1={source.y} x2={target.x} y2={target.y} />;
          })}
          {repos.map((repo, index) => {
            const point = positions.get(repo.id);
            if (!point) return null;
            return (
              <g key={repo.id}>
                <rect className="sector-map__repo" x={point.x - 2.25} y={point.y - 2.25} width="4.5" height="4.5" />
                <text className="sector-map__label" x={point.x} y={point.y - 4.5} textAnchor="middle">{`S${index + 1}`}</text>
              </g>
            );
          })}
          {sessions.map((session) => {
            const point = positions.get(session.id);
            if (!point) return null;
            return <rect key={session.id} x={point.x - 1} y={point.y - 1} width="2" height="2" fill={harnessColor(session.logical_harness || session.harness)} />;
          })}
        </svg>
        <div className="sector-map__legend">
          {repos.map((repo, index) => (
            <Link key={repo.id} to={`/sessions?${new URLSearchParams({ range: new URLSearchParams(search).get("range") ?? "24h", project: repo.label }).toString()}`}>
              <span>S{index + 1}</span><strong>{repo.label}</strong><small>{repo.sessions} missions</small>
            </Link>
          ))}
        </div>
      </div>
    </section>
  );
}
