import { Link, useOutletContext, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  fetchOrchestration,
  fetchSessionTree,
  logicalHarness,
  runtimeHarness,
  type TreeNode,
} from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { DiagnosticLoader } from "@/components/DiagnosticLoader";
import { SectorMap } from "@/components/command-grid/SectorMap";
import { Card, CardTitle, PanelCard } from "@/components/ui/card";
import { Kpi } from "@/components/ui/kpi";
import { HarnessTag, ModelBadge, RuntimeHarnessLabel } from "@/components/ui/badges";
import { cn, formatDayTime, harnessColor } from "@/lib/utils";
import { rangeViewQueryOptions } from "@/lib/viewQueries";
import { overviewGraphQueryOptions } from "@/lib/overviewQueries";

type Ctx = { range: string };

type LineageStats = {
  agents: number;
  messages: number;
  tools: number;
  maxDepth: number;
};

function lineageStats(node: TreeNode, depth = 0): LineageStats {
  return node.children.reduce<LineageStats>(
    (total, child) => {
      const childStats = lineageStats(child, depth + 1);
      return {
        agents: total.agents + childStats.agents,
        messages: total.messages + childStats.messages,
        tools: total.tools + childStats.tools,
        maxDepth: Math.max(total.maxDepth, childStats.maxDepth),
      };
    },
    {
      agents: 1,
      messages: node.message_count,
      tools: node.tool_count,
      maxDepth: depth,
    },
  );
}

export function Orchestration() {
  const { range } = useOutletContext<Ctx>();
  const [params, setParams] = useSearchParams();
  const root = params.get("root");

  const overview = useQuery({
    ...rangeViewQueryOptions({
      queryKey: ["orchestration", range],
      queryFn: (signal) => fetchOrchestration(range, signal),
    }),
    enabled: !root,
  });
  const tree = useQuery({
    queryKey: ["tree", root],
    queryFn: ({ signal }) => {
      if (!root) throw new Error("Mission lineage requires a root session");
      return fetchSessionTree(root, signal);
    },
    enabled: Boolean(root),
  });
  const graph = useQuery(overviewGraphQueryOptions(range, !root));

  if (root) {
    if (tree.isLoading) return <DiagnosticLoader label="Mapping mission lineage" />;
    if (tree.isError || !tree.data) {
      return (
        <EmptyState
          title="Mission lineage unavailable"
          body="The selected mission root is not in the local ledger."
        />
      );
    }
    const stats = lineageStats(tree.data.tree);
    const rangeSearch = new URLSearchParams({ range }).toString();
    const replaySearch = new URLSearchParams({
      range,
      mode: "replay",
      session: tree.data.root_id,
    }).toString();
    return (
      <div className="mission-lineage-page space-y-4">
        <header className="command-page-head">
          <div>
            <div className="command-page-head__eyebrow">one mission · recorded parent and worker links</div>
            <h1>Mission lineage</h1>
            <p>One coordinator and the workers recorded under it. Counts below apply only to this mission.</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Link className="command-link" to={`/floor?${replaySearch}`}>Replay mission →</Link>
            <button
              type="button"
              className="command-link command-link--muted"
              onClick={() => setParams(new URLSearchParams({ range }))}
            >
              Back to fleet
            </button>
          </div>
        </header>

        <section className="mission-scope" aria-label="Selected mission">
          <div><span>Project</span><strong>{tree.data.tree.project}</strong></div>
          <div><span>Coordinator</span><strong>{tree.data.tree.model || tree.data.tree.harness}</strong></div>
          <div><span>Started</span><strong>{formatDayTime(tree.data.tree.started_at)}</strong></div>
          <div className="mission-scope__id"><span>Root ID</span><strong>{tree.data.root_id}</strong></div>
        </section>

        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
          <Kpi title="Agents in mission" value={stats.agents} sub="coordinator plus workers" />
          <Kpi title="Direct workers" value={tree.data.tree.children.length} sub="attached to coordinator" />
          <Kpi title="Messages" value={stats.messages} sub="across this mission" />
          <Kpi title="Tool calls" value={stats.tools} sub="across this mission" />
          <Kpi title="Maximum depth" value={stats.maxDepth} sub="worker generations" />
        </div>

        <PanelCard
          title="Mission tree"
          aside={`${stats.agents.toLocaleString()} agents · ${stats.tools.toLocaleString()} tools`}
        >
          <div className="mission-tree-help">
            Open any row for its Holotrace, or replay the mission to see agents and tool packets on the shared timeline.
          </div>
          <div className="mission-tree-scroll">
            <TreeView node={tree.data.tree} depth={0} search={rangeSearch} />
          </div>
        </PanelCard>
      </div>
    );
  }

  if (overview.isLoading) {
    return <DiagnosticLoader label="Mapping orchestration" />;
  }
  if (overview.isError || !overview.data) {
    return (
      <EmptyState
        title="Could not load orchestration"
        body="Supervisor trees require parent_session_id links between sessions."
        missing={["sessions.parent_session_id"]}
      />
    );
  }

  const data = overview.data;
  const signals = data.signals;

  return (
    <div className="sector-view space-y-3">
      <div className="command-page-head">
        <div>
          <div className="command-page-head__eyebrow">fleet totals · {range} range</div>
          <h1>Orchestration fleet</h1>
          <p>Mission coordinators with recorded workers. Select one mission to inspect its own lineage.</p>
        </div>
        <span className="text-[13px] text-faint-foreground">Fleet-wide totals, not one selected mission</span>
      </div>

      <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
        <Kpi title="Missions with workers" value={data.supervisor_roots} sub="coordinator sessions" />
        <Kpi title="Worker sessions" value={data.child_sessions} sub="recorded across fleet" />
        <Kpi title="Worker briefs" value={signals.worker_brief ?? 0} sub="delegated task messages" />
        <Kpi title="Recorded handoffs" value={signals.inter_agent_handoff ?? 0} sub="agent-to-agent transfers" />
        <Kpi title="Task notifications" value={signals.task_notification ?? 0} sub="synthetic runtime notices" />
      </div>

      {graph.data ? <SectorMap data={graph.data} search={new URLSearchParams({ range }).toString()} /> : graph.isLoading ? <DiagnosticLoader label="Mapping sectors" compact rows={3} /> : null}

      <div className="grid grid-cols-1 items-start gap-3 lg:grid-cols-5">
        <PanelCard
          title="Missions"
          aside={`${data.items.length} largest by direct workers`}
          className="lg:col-span-2"
        >
          {data.items.length === 0 ? (
            <div className="p-4">
              <EmptyState
                title="No parent links in range"
                body="Supervisor rows appear when a session in this window has at least one child linked via parent_session_id."
                missing={["parent_session_id"]}
              />
            </div>
          ) : (
            <div className="max-h-[560px] divide-y divide-border-faint overflow-y-auto">
              {data.items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => {
                    const next = new URLSearchParams(params);
                    next.set("root", item.id);
                    setParams(next);
                  }}
                  className={cn(
                    "block w-full px-4 py-2.5 text-left hover:bg-muted/40",
                    root === item.id && "bg-muted/60",
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <HarnessTag harness={logicalHarness(item)} />
                      <RuntimeHarnessLabel
                        logicalHarness={logicalHarness(item)}
                        runtimeHarness={runtimeHarness(item)}
                      />
                    </div>
                    <span className="flex items-baseline gap-1.5">
                      <span className="display-md display-ink">{item.child_count}</span>
                      <span className="microlabel text-[10px] text-faint-foreground">
                        {item.child_count === 1 ? "direct worker" : "direct workers"}
                      </span>
                    </span>
                  </div>
                  <div className="mt-1 truncate font-mono text-[11px] text-muted-foreground">
                    {item.id}
                  </div>
                  <div className="mt-0.5 flex items-center gap-2 text-[11px] text-faint-foreground">
                    <span className="truncate font-mono">{item.model}</span>
                    <span>·</span>
                    <span className="truncate">{item.project}</span>
                    <span className="tabular ml-auto shrink-0">
                      {formatDayTime(item.started_at)}
                    </span>
                  </div>
                  <div className="mt-1 text-[12px] text-faint-foreground">Inspect this mission →</div>
                </button>
              ))}
            </div>
          )}
        </PanelCard>

        <Card className="lg:col-span-3">
          <CardTitle>Selected mission</CardTitle>
          {!root ? (
            <div className="mt-3">
              <EmptyState
                title="Select a mission"
                body="Pick a mission on the left to inspect only its coordinator and workers."
                className="min-h-[120px]"
              />
            </div>
          ) : tree.isLoading ? (
            <div className="mt-3 text-[13px] text-muted-foreground">Loading tree…</div>
          ) : tree.isError || !tree.data ? (
            <div className="mt-3">
              <EmptyState
                title="Tree unavailable"
                body="This session id was not found in the ledger."
              />
            </div>
          ) : (
            <div className="mt-3">
              <TreeView node={tree.data.tree} depth={0} search={new URLSearchParams({ range }).toString()} />
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

function TreeView({
  node,
  depth,
  search,
  railColor,
}: {
  node: TreeNode;
  depth: number;
  search: string;
  railColor?: string;
}) {
  return (
    <div
      className={cn(depth > 0 && "ml-4 border-l pl-3")}
      style={
        depth > 0
          ? {
              borderLeftColor: `color-mix(in srgb, ${railColor ?? "var(--border-faint)"} 45%, transparent)`,
            }
          : undefined
      }
    >
      <Link
        to={`/sessions/${encodeURIComponent(node.id)}?${search}`}
        className="group mb-1.5 block rounded-control border border-border-faint px-3 py-2 hover:border-border hover:bg-muted/30"
        style={{
          borderLeftWidth: 2,
          borderLeftColor: harnessColor(logicalHarness(node)),
        }}
      >
        <div className="flex flex-wrap items-center gap-2">
          <HarnessTag harness={logicalHarness(node)} />
          <RuntimeHarnessLabel
            logicalHarness={logicalHarness(node)}
            runtimeHarness={runtimeHarness(node)}
          />
          <span className="min-w-0 truncate text-[13px] font-medium text-foreground">
            {depth === 0 ? "Coordinator" : "Worker"} · {node.project}
          </span>
          <span className="tabular ml-auto shrink-0 text-[12px] text-faint-foreground">
            {formatDayTime(node.started_at)}
          </span>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] text-faint-foreground">
          <ModelBadge
            model={node.model}
            harness={runtimeHarness(node)}
            effort={node.effort}
          />
          {node.relationship ? (
            <span className="text-[11px] text-faint-foreground">
              {node.relationship === "provider_backing"
                ? "backing transcript"
                : node.relationship === "agent_worker"
                  ? "Worker"
                  : node.relationship}
            </span>
          ) : null}
          <span className="tabular">
            {node.message_count} msgs · {node.tool_count} tools
            {node.children.length > 0 ? ` · ${node.children.length} children` : ""}
          </span>
          <span className="truncate font-mono text-[10px] text-faint-foreground">{node.id}</span>
        </div>
      </Link>
      {node.children.map((child) => (
        <TreeView
          key={child.id}
          node={child}
          depth={depth + 1}
          search={search}
          railColor={harnessColor(logicalHarness(node))}
        />
      ))}
    </div>
  );
}
