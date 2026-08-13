import { Link, useNavigate, useOutletContext, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  fetchOrchestration,
  fetchSessionTree,
  logicalHarness,
  runtimeHarness,
  type TreeNode,
} from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { LoadingOrb } from "@/components/LoadingOrb";
import { Card, CardTitle, PanelCard } from "@/components/ui/card";
import { Kpi } from "@/components/ui/kpi";
import { HarnessTag, ModelBadge, RuntimeHarnessLabel } from "@/components/ui/badges";
import { cn, formatDayTime, harnessColor } from "@/lib/utils";
import { rangeViewQueryOptions } from "@/lib/viewQueries";

type Ctx = { range: string };

export function Orchestration() {
  const { range } = useOutletContext<Ctx>();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const root = params.get("root");

  const overview = useQuery(rangeViewQueryOptions({
    queryKey: ["orchestration", range],
    queryFn: (signal) => fetchOrchestration(range, signal),
  }));
  const tree = useQuery({
    queryKey: ["tree", root],
    queryFn: ({ signal }) => fetchSessionTree(root!, signal),
    enabled: Boolean(root),
  });

  if (overview.isLoading) {
    return <LoadingOrb label="Mapping orchestration" />;
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
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h1 className="text-[18px] font-semibold tracking-tight">Orchestration</h1>
        <span className="text-[12px] text-faint-foreground">
          supervisor → worker structure from parent_session_id
        </span>
      </div>

      <div className="grid grid-cols-5 gap-3">
        <Kpi title="Supervisor roots" value={data.supervisor_roots} sub="sessions with children" />
        <Kpi title="Child sessions" value={data.child_sessions} sub="spawned workers" />
        <Kpi title="Worker briefs" value={signals.worker_brief ?? 0} sub="classified request kind" />
        <Kpi title="Handoffs" value={signals.inter_agent_handoff ?? 0} sub="inter-agent" />
        <Kpi title="Task notifications" value={signals.task_notification ?? 0} sub="harness-synthetic" />
      </div>

      <div className="grid grid-cols-5 items-start gap-3">
        <PanelCard
          title="Supervisors"
          aside={`${data.items.length} shown`}
          className="col-span-2"
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
                      <span className="microlabel text-[9px] text-faint-foreground">
                        {item.child_count === 1 ? "child" : "children"}
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
                  <div className="mt-1">
                    <span
                      onClick={(e) => {
                        e.stopPropagation();
                        const p = new URLSearchParams(params);
                        p.delete("root");
                        p.set("focus", item.id);
                        navigate({ pathname: "/", search: p.toString() });
                      }}
                      className="cursor-pointer text-[10px] text-faint-foreground hover:text-foreground"
                    >
                      open in graph →
                    </span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </PanelCard>

        <Card className="col-span-3">
          <CardTitle>Tree</CardTitle>
          {!root ? (
            <div className="mt-3">
              <EmptyState
                title="Select a supervisor"
                body="Pick a root on the left to expand its worker tree — every node links to its transcript."
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
              <TreeView node={tree.data.tree} depth={0} />
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
  railColor,
}: {
  node: TreeNode;
  depth: number;
  railColor?: string;
}) {
  const [params] = useSearchParams();
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
        to={`/sessions/${encodeURIComponent(node.id)}?${params.toString()}`}
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
          <span className="min-w-0 truncate font-mono text-[11px] text-muted-foreground group-hover:text-foreground">
            {node.id}
          </span>
          <span className="tabular ml-auto shrink-0 text-[10px] text-faint-foreground">
            {formatDayTime(node.started_at)}
          </span>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-faint-foreground">
          <ModelBadge
            model={node.model}
            harness={runtimeHarness(node)}
            effort={node.effort}
          />
          {node.relationship ? (
            <span className="text-[10px] text-faint-foreground">
              {node.relationship === "provider_backing"
                ? "backing transcript"
                : node.relationship}
            </span>
          ) : null}
          <span className="tabular">
            {node.message_count} msgs · {node.tool_count} tools
            {node.children.length > 0 ? ` · ${node.children.length} children` : ""}
          </span>
        </div>
      </Link>
      {node.children.map((child) => (
        <TreeView
          key={child.id}
          node={child}
          depth={depth + 1}
          railColor={harnessColor(logicalHarness(node))}
        />
      ))}
    </div>
  );
}
