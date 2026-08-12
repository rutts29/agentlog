import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useOutletContext, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  combineAbortSignals,
  fetchFacets,
  fetchSessions,
  logicalHarness,
  runtimeHarness,
  type LiveSession,
  type PresenceEvent,
  type TreeNode,
} from "@/lib/api";
import { PanelCard } from "@/components/ui/card";
import { EmptyState } from "@/components/EmptyState";
import { LoadingOrb } from "@/components/LoadingOrb";
import { HarnessTag, ModelBadge, RuntimeHarnessLabel } from "@/components/ui/badges";
import { cn, formatDuration, formatFullTime, harnessColor } from "@/lib/utils";
import { useViewShortcuts } from "@/lib/keyboard";
import { projectBranchTree } from "@/lib/sessionTree";
import { LiveOrb } from "@/components/LiveOrb";
import { useIngestStream } from "@/lib/useIngestStream";
import { useLivePresence } from "@/lib/useLivePresence";
import {
  canPrefetchSessionDetail,
  createSessionRangeWarmer,
  invalidateSessionDetailCache,
  sessionDetailQueryOptions,
  sessionTreeQueryOptions,
} from "@/lib/sessionQueries";

type Ctx = { range: string };

const SESSION_WARM_RANGES = ["7d", "30d", "all", "24h"] as const;
const SESSION_RANGE_STALE_TIME = 2 * 60_000;
const SESSION_RANGE_GC_TIME = 15 * 60_000;

function multi(params: URLSearchParams, key: string): string[] {
  return params.getAll(key).filter(Boolean);
}

function detailHref(id: string, params: URLSearchParams): string {
  const next = new URLSearchParams(params);
  next.delete("root");
  next.delete("msg");
  const search = next.toString();
  return `/sessions/${encodeURIComponent(id)}${search ? `?${search}` : ""}`;
}

const FILTER_KEYS = ["harness", "model", "effort", "project", "branch", "q"] as const;

export function Sessions() {
  const { range } = useOutletContext<Ctx>();
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const harness = multi(params, "harness");
  const model = multi(params, "model");
  const effort = multi(params, "effort");
  const branch = multi(params, "branch");
  const project = multi(params, "project");
  const q = params.get("q") ?? "";
  const sort = params.get("sort") ?? "started_at";
  const order = params.get("order") ?? "desc";
  const cursor = Number(params.get("cursor") || "0");
  const [selected, setSelected] = useState(-1);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const presenceHandler = useRef<(data: PresenceEvent) => void>(() => {});
  const ingestTimer = useRef<number | null>(null);
  const rangeWarmer = useRef(createSessionRangeWarmer());

  const { connected } = useIngestStream(
    ({ events }) => {
      if (!events.length) return;
      rangeWarmer.current.notifyActivity();
      if (ingestTimer.current) window.clearTimeout(ingestTimer.current);
      ingestTimer.current = window.setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ["sessions"], refetchType: "active" });
        queryClient.invalidateQueries({ queryKey: ["facets", "roots"], refetchType: "active" });
        void invalidateSessionDetailCache(queryClient);
      }, 250);
    },
    (data) => presenceHandler.current(data),
  );
  const { presence, onPresenceEvent } = useLivePresence(connected);
  useEffect(() => {
    presenceHandler.current = onPresenceEvent;
    return () => {
      if (ingestTimer.current) window.clearTimeout(ingestTimer.current);
      rangeWarmer.current.cancel();
    };
  }, [onPresenceEvent]);

  const sessionQueryKey = (targetRange: string, targetCursor = cursor) => [
    "sessions",
    targetRange,
    harness,
    model,
    effort,
    branch,
    project,
    q,
    sort,
    order,
    targetCursor,
  ];
  const sessionQueryFn = (
    targetRange: string,
    targetCursor = cursor,
    requestSignal?: AbortSignal,
  ) =>
    async ({ signal }: { signal: AbortSignal }) => {
      const combined = combineAbortSignals(requestSignal, signal);
      try {
        return await fetchSessions(
          targetRange,
          {
            harness,
            model,
            effort,
            branch,
            project,
            q: q || undefined,
            sort,
            order,
            cursor: targetCursor,
            limit: 50,
          },
          combined.signal,
        );
      } finally {
        combined.cleanup();
      }
    };
  const facetQueryKey = (targetRange: string) => ["facets", "roots", targetRange];

  const facets = useQuery({
    queryKey: facetQueryKey(range),
    queryFn: ({ signal }) => fetchFacets(range, "roots", signal),
    staleTime: SESSION_RANGE_STALE_TIME,
    gcTime: SESSION_RANGE_GC_TIME,
  });
  const list = useQuery({
    queryKey: sessionQueryKey(range),
    queryFn: sessionQueryFn(range),
    staleTime: SESSION_RANGE_STALE_TIME,
    gcTime: SESSION_RANGE_GC_TIME,
  });

  const items = list.data?.items ?? [];
  const liveByRoot = useMemo(() => {
    const result = new Map<string, LiveSession>();
    for (const live of presence.conversations) {
      const rootId = live.logical_session_id || live.session_id;
      if (rootId) result.set(rootId, live);
    }
    return result;
  }, [presence.conversations]);
  const facetKey = [harness, model, effort, project, branch]
    .map((values) => values.join("|"))
    .join("~");

  const warmReady = Boolean(
    list.data && !list.isError && !list.isFetching && !facets.isFetching,
  );
  const warmKey = [
    range,
    cursor,
    facetKey,
    q,
    sort,
    order,
    list.dataUpdatedAt,
    facets.dataUpdatedAt,
  ].join("|");

  useEffect(() => {
    if (!warmReady) {
      rangeWarmer.current.pause();
      return;
    }
    const targets = SESSION_WARM_RANGES.filter((targetRange) => targetRange !== range);
    rangeWarmer.current.schedule(warmKey, async (signal) => {
      for (const targetRange of targets) {
        if (signal.aborted) return;
        const listKey = sessionQueryKey(targetRange, 0);
        if (!queryClient.getQueryState(listKey)?.data) {
          try {
            await queryClient.prefetchQuery({
              queryKey: listKey,
              queryFn: sessionQueryFn(targetRange, 0, signal),
              staleTime: SESSION_RANGE_STALE_TIME,
              gcTime: SESSION_RANGE_GC_TIME,
            });
          } catch {
            if (signal.aborted) return;
          }
        }
        if (signal.aborted) return;
        const facetsKey = facetQueryKey(targetRange);
        if (!queryClient.getQueryState(facetsKey)?.data) {
          try {
            await queryClient.prefetchQuery({
              queryKey: facetsKey,
              queryFn: async ({ signal: querySignal }) => {
                const combined = combineAbortSignals(signal, querySignal);
                try {
                  return await fetchFacets(targetRange, "roots", combined.signal);
                } finally {
                  combined.cleanup();
                }
              },
              staleTime: SESSION_RANGE_STALE_TIME,
              gcTime: SESSION_RANGE_GC_TIME,
            });
          } catch {
            if (signal.aborted) return;
          }
        }
      }
    });
    return () => rangeWarmer.current.pause();
  }, [
    cursor,
    facetKey,
    facets.dataUpdatedAt,
    list.dataUpdatedAt,
    order,
    q,
    queryClient,
    range,
    sort,
    warmKey,
    warmReady,
  ]);

  // j/k row navigation, Enter opens the selected session.
  useViewShortcuts((e) => {
    if (e.key === "j" || e.key === "k") {
      setSelected((s) => {
        const next =
          e.key === "j" ? Math.min(items.length - 1, s + 1) : Math.max(0, s - 1);
        document
          .getElementById(`session-row-${next}`)
          ?.scrollIntoView({ block: "nearest" });
        return next;
      });
      return true;
    }
    if (e.key === "Enter" && selected >= 0 && items[selected]) {
      const item = items[selected];
      if (liveByRoot.get(item.id)?.source_snapshot_status === "pending") return true;
      navigate(detailHref(item.navigation_id ?? item.id, params));
      return true;
    }
    return false;
  });

  /* Row selection is positional, so it must reset whenever the underlying rows
     can shift — facet changes included, or Enter opens whatever slid under it. */
  useEffect(() => {
    setSelected(-1);
  }, [cursor, range, q, sort, order, facetKey]);

  /* The text box owns its own value and writes the URL on a trailing debounce;
     without it every keystroke was a query. */
  const [qDraft, setQDraft] = useState(q);
  const qTimer = useRef<number | null>(null);
  useEffect(() => setQDraft(q), [q]);
  useEffect(
    () => () => {
      if (qTimer.current) window.clearTimeout(qTimer.current);
    },
    [],
  );
  function setTextFilter(value: string) {
    setQDraft(value);
    if (qTimer.current) window.clearTimeout(qTimer.current);
    qTimer.current = window.setTimeout(() => setFilter("q", value), 220);
  }

  function setFilter(key: string, value: string) {
    const next = new URLSearchParams(params);
    next.delete("cursor");
    if (!value) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  }

  function toggleSort(col: string) {
    const next = new URLSearchParams(params);
    if (sort === col) {
      next.set("order", order === "asc" ? "desc" : "asc");
    } else {
      next.set("sort", col);
      next.set("order", "desc");
    }
    next.delete("cursor");
    setParams(next, { replace: true });
  }

  const activeFilters = FILTER_KEYS.flatMap((key) =>
    params.getAll(key).filter(Boolean).map((value) => ({ key, value })),
  );

  if (list.isLoading) {
    return <LoadingOrb label="Reading sessions" />;
  }
  if (list.isError || !list.data) {
    return (
      <EmptyState
        title="Could not load sessions"
        body="The API did not return a sessions list. Confirm agentlog serve is running against the ledger."
      />
    );
  }

  const f = facets.data ?? {
    harness: [],
    model: [],
    effort: [],
    branch: [],
    project: [],
  };
  const facetsPending = !facets.data && facets.isFetching;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight">Sessions</h1>
          <p className="mt-0.5 text-[11px] text-faint-foreground">
            {list.data.note}
          </p>
        </div>
        <div className="flex items-center gap-3 text-[12px] text-muted-foreground">
          <span className="text-faint-foreground">
            <kbd>j</kbd> <kbd>k</kbd> rows · <kbd>↵</kbd> open
          </span>
          <span className="tabular">
            {list.data.total.toLocaleString()} root conversations
          </span>
        </div>
      </div>

      <div className="rounded-card border border-border bg-card p-3">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-6">
          <FacetSelect
            label="Harness"
            value={harness[0] ?? ""}
            options={f.harness}
            onChange={(v) => setFilter("harness", v)}
            disabled={facetsPending}
          />
          <FacetSelect
            label="Model"
            value={model[0] ?? ""}
            options={f.model}
            onChange={(v) => setFilter("model", v)}
            disabled={facetsPending}
          />
          <FacetSelect
            label="Effort"
            value={effort[0] ?? ""}
            options={f.effort}
            onChange={(v) => setFilter("effort", v)}
            disabled={facetsPending}
          />
          <FacetSelect
            label="Project"
            value={project[0] ?? ""}
            options={f.project}
            onChange={(v) => setFilter("project", v)}
            disabled={facetsPending}
          />
          <FacetSelect
            label="Branch"
            value={branch[0] ?? ""}
            options={f.branch}
            onChange={(v) => setFilter("branch", v)}
            disabled={facetsPending}
          />
          <label className="microlabel block text-[10px] text-faint-foreground">
            Filter text
            <input
              value={qDraft}
              onChange={(e) => setTextFilter(e.target.value)}
              placeholder="id, repo, model, branch…"
              className="mt-1 w-full rounded-control border border-border bg-background px-2 py-1.5 text-[12px] normal-case tracking-normal text-foreground placeholder:text-faint-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </label>
        </div>
        {activeFilters.length > 0 ? (
          <div className="mt-2.5 flex flex-wrap items-center gap-1.5 border-t border-border-faint pt-2.5">
            {activeFilters.map(({ key, value }) => (
              <button
                key={`${key}=${value}`}
                type="button"
                onClick={() => setFilter(key, "")}
                className="inline-flex items-center gap-1.5 rounded-[4px] border border-border px-1.5 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
              >
                <span className="text-faint-foreground">{key}</span>
                <span className="max-w-[180px] truncate font-mono">{value}</span>
                <span aria-hidden>×</span>
              </button>
            ))}
            <button
              type="button"
              onClick={() => {
                const next = new URLSearchParams(params);
                for (const k of FILTER_KEYS) next.delete(k);
                next.delete("cursor");
                setParams(next, { replace: true });
              }}
              className="ml-1 text-[11px] text-faint-foreground hover:text-muted-foreground"
            >
              Clear all
            </button>
          </div>
        ) : null}
      </div>

      <PanelCard
        title="Conversations"
        aside={`${cursor + 1}–${Math.min(cursor + items.length, list.data.total)} of ${list.data.total.toLocaleString()}`}
      >
        {items.length === 0 ? (
          <div className="p-4">
            <EmptyState
              title="No sessions match"
              body="Widen the time range or clear a filter — every chip above narrows the ledger."
            />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[12px]">
              <thead>
                <tr className="microlabel border-b border-border text-[10px] text-faint-foreground">
                  <SortTh label="Activity" col="started_at" sort={sort} order={order} onClick={toggleSort} first />
                  <SortTh label="Harness" col="harness" sort={sort} order={order} onClick={toggleSort} />
                  <SortTh label="Model" col="model" sort={sort} order={order} onClick={toggleSort} />
                  <SortTh label="Project" col="project" sort={sort} order={order} onClick={toggleSort} />
                  <SortTh label="Branch" col="branch" sort={sort} order={order} onClick={toggleSort} />
                  <SortTh label="Dur" col="duration" sort={sort} order={order} onClick={toggleSort} right />
                  <SortTh label="Msgs" col="messages" sort={sort} order={order} onClick={toggleSort} right />
                  <SortTh label="Tools" col="tools" sort={sort} order={order} onClick={toggleSort} right />
                  <SortTh label="Win" col="windows" sort={sort} order={order} onClick={toggleSort} right />
                  <th className="microlabel py-2 pr-4 text-right text-[10px] font-medium">
                    Branches
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((s, i) => {
                  const descendantCount = s.descendant_count ?? s.child_count ?? 0;
                  const isExpanded = expanded.has(s.id);
                  const live = liveByRoot.get(s.id);
                  const syncing = live?.source_snapshot_status === "pending";
                  const navigationId = s.navigation_id ?? s.id;
                  const prefetchDetail = () => {
                    if (!canPrefetchSessionDetail(live?.source_snapshot_status)) return;
                    void queryClient.prefetchQuery(
                      sessionDetailQueryOptions(navigationId),
                    );
                  };
                  return (
                  <Fragment key={s.id}>
                  <tr
                    id={`session-row-${i}`}
                    onClick={() => {
                      if (syncing) return;
                      navigate(detailHref(navigationId, params));
                    }}
                    onMouseEnter={prefetchDetail}
                    aria-disabled={syncing || undefined}
                    className={cn(
                      "border-b border-border-faint last:border-0 hover:bg-muted/40",
                      syncing ? "cursor-wait opacity-65" : "cursor-pointer",
                      selected === i && "bg-muted/60",
                    )}
                    style={
                      selected === i
                        ? { boxShadow: `inset 2px 0 0 ${harnessColor(logicalHarness(s))}` }
                        : undefined
                    }
                  >
                    <td className="px-4 py-1.5">
                      <div className="flex min-w-[180px] items-start gap-2">
                        {descendantCount > 0 ? (
                          <button
                            type="button"
                            aria-expanded={isExpanded}
                            aria-controls={`session-tree-${i}`}
                            aria-label={`${isExpanded ? "Collapse" : "Expand"} child sessions for ${s.id}`}
                            onClick={(e) => {
                              e.stopPropagation();
                              setExpanded((current) => {
                                const next = new Set(current);
                                if (next.has(s.id)) next.delete(s.id);
                                else next.add(s.id);
                                return next;
                              });
                            }}
                            className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded border border-border text-[12px] text-muted-foreground hover:border-ring hover:text-foreground"
                          >
                            <span aria-hidden>{isExpanded ? "−" : "+"}</span>
                          </button>
                        ) : (
                          <span aria-hidden className="inline-block h-5 w-5 shrink-0" />
                        )}
                        <div className="flex min-w-0 flex-col items-start gap-0.5">
                          <div className="flex items-center gap-1.5">
                            <span className="inline-flex items-center rounded-[4px] border border-border px-1.5 py-[2px] text-[10px] leading-none text-muted-foreground">
                              Main
                            </span>
                            {live ? (
                              <span className="inline-flex items-center gap-1 text-[10px] text-accent-live">
                                <LiveOrb
                                  state={live.state}
                                  harnessColor={harnessColor(live.logical_harness || live.harness)}
                                  size={15}
                                  worker={live.role === "worker"}
                                  title={syncing ? "Transcript syncing" : live.activity || live.state}
                                />
                                {syncing ? "Syncing…" : "Live"}
                              </span>
                            ) : null}
                          </div>
                        <Link
                          to={detailHref(navigationId, params)}
                          onFocus={prefetchDetail}
                          onClick={(e) => {
                            e.stopPropagation();
                            if (syncing) e.preventDefault();
                          }}
                          aria-disabled={syncing || undefined}
                          tabIndex={syncing ? -1 : undefined}
                          title={syncing ? "Transcript is syncing; it will be available shortly" : undefined}
                          className="tabular whitespace-nowrap text-muted-foreground hover:text-foreground"
                        >
                          {formatFullTime(s.activity_at ?? s.started_at)}
                        </Link>
                        {s.activity_at && s.started_at && s.activity_at !== s.started_at ? (
                          <span className="tabular whitespace-nowrap text-[10px] text-faint-foreground">
                            started {formatFullTime(s.started_at)}
                          </span>
                          ) : null}
                        </div>
                      </div>
                    </td>
                    <td className="py-1.5">
                      <div className="flex flex-col items-start gap-1">
                        <HarnessTag harness={logicalHarness(s)} />
                        <RuntimeHarnessLabel
                          logicalHarness={logicalHarness(s)}
                          runtimeHarness={runtimeHarness(s)}
                        />
                        {s.is_orphan ? (
                          <span className="text-[10px] text-status-warn">
                            Unlinked branch
                          </span>
                        ) : null}
                        {s.matched_in_descendant ? (
                          <span className="whitespace-nowrap text-[10px] text-muted-foreground">
                            Matched in branch
                            {(s.matching_descendant_count ?? 0) > 1
                              ? ` ×${s.matching_descendant_count}`
                              : ""}
                          </span>
                        ) : null}
                      </div>
                    </td>
                    <td className="max-w-[220px] py-1.5 pr-2">
                      <ModelBadge
                        model={s.model}
                        harness={runtimeHarness(s)}
                        effort={s.effort}
                      />
                    </td>
                    <td className="max-w-[160px] truncate py-1.5 pr-2 text-muted-foreground">
                      {s.project}
                    </td>
                    <td className="max-w-[140px] truncate py-1.5 pr-2 font-mono text-[11px] text-faint-foreground">
                      {s.branch ?? "—"}
                    </td>
                    <td className="tabular py-1.5 text-right text-muted-foreground">
                      {formatDuration(s.duration_seconds)}
                    </td>
                    <td className="tabular py-1.5 text-right">{s.message_count}</td>
                    <td className="tabular py-1.5 text-right">{s.tool_count}</td>
                    <td className="tabular px-4 py-1.5 text-right text-muted-foreground">
                      {s.window_count ?? "—"}
                    </td>
                    <td className="tabular py-1.5 pr-4 text-right text-muted-foreground">
                      {descendantCount > 0
                        ? descendantCount.toLocaleString()
                        : "—"}
                    </td>
                  </tr>
                  {isExpanded ? (
                    <SessionTreeRows
                      id={`session-tree-${i}`}
                      rootId={s.id}
                      params={params}
                    />
                  ) : null}
                  </Fragment>
                );})}
              </tbody>
            </table>
          </div>
        )}
        <div className="flex items-center justify-between border-t border-border px-4 py-2.5 text-[12px] text-muted-foreground">
          <button
            type="button"
            disabled={cursor <= 0}
            className="hover:text-foreground disabled:opacity-40"
            onClick={() => {
              const next = new URLSearchParams(params);
              next.set("cursor", String(Math.max(0, cursor - 50)));
              setParams(next);
            }}
          >
            ← Previous
          </button>
          <button
            type="button"
            disabled={list.data.next_cursor == null}
            className="hover:text-foreground disabled:opacity-40"
            onClick={() => {
              if (list.data!.next_cursor == null) return;
              const next = new URLSearchParams(params);
              next.set("cursor", String(list.data!.next_cursor));
              setParams(next);
            }}
          >
            Next →
          </button>
        </div>
      </PanelCard>
    </div>
  );
}

function SessionTreeRows({
  id,
  rootId,
  params,
}: {
  id: string;
  rootId: string;
  params: URLSearchParams;
}) {
  const tree = useQuery(sessionTreeQueryOptions(rootId));
  const projection = useMemo(
    () => projectBranchTree(tree.data?.tree),
    [tree.data?.tree],
  );

  return (
    <tr id={id} className="border-b border-border-faint bg-muted/10">
      <td colSpan={10} className="px-4 py-2">
        {tree.isLoading ? (
          <div className="pl-7 text-[11px] text-muted-foreground">Loading child sessions…</div>
        ) : tree.isError || !tree.data ? (
          <div className="pl-7 text-[11px] text-status-warn">Child sessions unavailable.</div>
        ) : (
          <div className="space-y-1">
            <TreeRows nodes={tree.data.tree.children} visibleIds={new Set(projection.rows.map(({ node }) => node.id))} params={params} depth={1} parentHarness={logicalHarness(tree.data.tree)} />
            {projection.omittedNodeCount > 0 ? (
              <div className="pl-7 text-[10px] text-faint-foreground">
                +{projection.omittedNodeCount.toLocaleString()} child sessions omitted by the 500-node / 64-level bound.
              </div>
            ) : null}
          </div>
        )}
      </td>
    </tr>
  );
}

function TreeRows({
  nodes,
  visibleIds,
  params,
  depth,
  parentHarness,
}: {
  nodes: TreeNode[];
  visibleIds: Set<string>;
  params: URLSearchParams;
  depth: number;
  parentHarness: string;
}) {
  return (
    <>
      {nodes.map((node) => {
        if (!visibleIds.has(node.id)) return null;
        const role = node.thread_source === "subagent" || node.relationship === "provider_worker" ? "Worker" : "Branch";
        return (
          <div
            key={node.id}
            className="min-w-0 border-l-2 pl-3"
            style={{
              marginLeft: `${Math.min(depth - 1, 63) * 12}px`,
              borderLeftColor: `color-mix(in srgb, ${harnessColor(parentHarness)} 45%, transparent)`,
            }}
          >
            <Link
              to={detailHref(node.navigation_id ?? node.id, params)}
              className="group block min-w-0 rounded-control border border-border-faint px-3 py-2 hover:border-border hover:bg-muted/30"
              style={{ borderLeftWidth: 2, borderLeftColor: harnessColor(logicalHarness(node)) }}
            >
              <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                <span className="inline-flex shrink-0 items-center rounded-[4px] border border-border px-1.5 py-[2px] text-[10px] leading-none text-muted-foreground">
                  {role}
                </span>
                <HarnessTag harness={logicalHarness(node)} />
                <RuntimeHarnessLabel logicalHarness={logicalHarness(node)} runtimeHarness={runtimeHarness(node)} />
                <span className="min-w-0 truncate font-mono text-[11px] text-muted-foreground group-hover:text-foreground">
                  {node.id}
                </span>
                <span className="tabular ml-auto shrink-0 text-[10px] text-faint-foreground">
                  {formatFullTime(node.started_at)}
                </span>
              </div>
              <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-faint-foreground">
                <ModelBadge model={node.model} harness={runtimeHarness(node)} effort={node.effort} />
                <span className="tabular">{node.message_count} msgs · {node.tool_count} tools</span>
                {node.children.length > 0 ? <span className="tabular">· {node.descendant_count ?? node.children.length} descendants</span> : null}
              </div>
            </Link>
            <TreeRows nodes={node.children} visibleIds={visibleIds} params={params} depth={depth + 1} parentHarness={logicalHarness(node)} />
          </div>
        );
      })}
    </>
  );
}

function FacetSelect({
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  label: string;
  value: string;
  options: Array<{ value: string; count: number }>;
  onChange: (v: string) => void;
  disabled?: boolean;
}) {
  return (
    <label className="microlabel block text-[10px] text-faint-foreground">
      {label}
      <select
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded-control border border-border bg-background px-2 py-1.5 text-[12px] normal-case tracking-normal text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
      >
        <option value="">{disabled ? "Loading…" : "All"}</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.value} ({o.count})
          </option>
        ))}
      </select>
    </label>
  );
}

function SortTh({
  label,
  col,
  sort,
  order,
  onClick,
  right,
  first,
  last,
}: {
  label: string;
  col: string;
  sort: string;
  order: string;
  onClick: (col: string) => void;
  right?: boolean;
  first?: boolean;
  last?: boolean;
}) {
  const active = sort === col;
  return (
    <th
      className={cn(
        "py-2 pr-2 font-medium",
        right && "text-right",
        first && "pl-4",
        last && "pr-4",
      )}
    >
      <button
        type="button"
        onClick={() => onClick(col)}
        className={cn(
          "microlabel inline-flex items-center gap-1 text-[10px] hover:text-foreground",
          active && "text-muted-foreground",
        )}
      >
        {label}
        {active ? (
          <span className="text-faint-foreground">{order === "asc" ? "↑" : "↓"}</span>
        ) : null}
      </button>
    </th>
  );
}
