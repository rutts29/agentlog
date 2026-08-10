import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useOutletContext, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchFacets, fetchSessions } from "@/lib/api";
import { PanelCard } from "@/components/ui/card";
import { EmptyState } from "@/components/EmptyState";
import { HarnessTag, ModelBadge } from "@/components/ui/badges";
import { cn, formatDuration, formatFullTime, harnessColor } from "@/lib/utils";
import { useViewShortcuts } from "@/lib/keyboard";

type Ctx = { range: string };

function multi(params: URLSearchParams, key: string): string[] {
  return params.getAll(key).filter(Boolean);
}

const FILTER_KEYS = ["harness", "model", "effort", "project", "branch", "q"] as const;

export function Sessions() {
  const { range } = useOutletContext<Ctx>();
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

  const facets = useQuery({
    queryKey: ["facets", range],
    queryFn: () => fetchFacets(range),
  });
  const list = useQuery({
    queryKey: [
      "sessions",
      range,
      harness,
      model,
      effort,
      branch,
      project,
      q,
      sort,
      order,
      cursor,
    ],
    queryFn: () =>
      fetchSessions(range, {
        harness,
        model,
        effort,
        branch,
        project,
        q: q || undefined,
        sort,
        order,
        cursor,
        limit: 50,
      }),
  });

  const items = list.data?.items ?? [];

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
      navigate(
        `/sessions/${encodeURIComponent(items[selected].id)}?${params.toString()}`,
      );
      return true;
    }
    return false;
  });

  /* Row selection is positional, so it must reset whenever the underlying rows
     can shift — facet changes included, or Enter opens whatever slid under it. */
  const facetKey = [harness, model, effort, project, branch]
    .map((values) => values.join("|"))
    .join("~");
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

  if (list.isLoading || facets.isLoading) {
    return <div className="text-[13px] text-muted-foreground">Loading…</div>;
  }
  if (list.isError || !list.data || !facets.data) {
    return (
      <EmptyState
        title="Could not load sessions"
        body="The API did not return a sessions list. Confirm agentlog serve is running against the ledger."
      />
    );
  }

  const f = facets.data;

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h1 className="text-[18px] font-semibold tracking-tight">Sessions</h1>
        <div className="flex items-center gap-3 text-[12px] text-muted-foreground">
          <span className="text-faint-foreground">
            <kbd>j</kbd> <kbd>k</kbd> rows · <kbd>↵</kbd> open
          </span>
          <span className="tabular">{list.data.total.toLocaleString()} matching</span>
        </div>
      </div>

      <div className="rounded-card border border-border bg-card p-3">
        <div className="grid grid-cols-6 gap-2">
          <FacetSelect
            label="Harness"
            value={harness[0] ?? ""}
            options={f.harness}
            onChange={(v) => setFilter("harness", v)}
          />
          <FacetSelect
            label="Model"
            value={model[0] ?? ""}
            options={f.model}
            onChange={(v) => setFilter("model", v)}
          />
          <FacetSelect
            label="Effort"
            value={effort[0] ?? ""}
            options={f.effort}
            onChange={(v) => setFilter("effort", v)}
          />
          <FacetSelect
            label="Project"
            value={project[0] ?? ""}
            options={f.project}
            onChange={(v) => setFilter("project", v)}
          />
          <FacetSelect
            label="Branch"
            value={branch[0] ?? ""}
            options={f.branch}
            onChange={(v) => setFilter("branch", v)}
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
        title="Ledger"
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
                  <SortTh label="Start" col="started_at" sort={sort} order={order} onClick={toggleSort} first />
                  <SortTh label="Harness" col="harness" sort={sort} order={order} onClick={toggleSort} />
                  <SortTh label="Model" col="model" sort={sort} order={order} onClick={toggleSort} />
                  <SortTh label="Project" col="project" sort={sort} order={order} onClick={toggleSort} />
                  <SortTh label="Branch" col="branch" sort={sort} order={order} onClick={toggleSort} />
                  <SortTh label="Dur" col="duration" sort={sort} order={order} onClick={toggleSort} right />
                  <SortTh label="Msgs" col="messages" sort={sort} order={order} onClick={toggleSort} right />
                  <SortTh label="Tools" col="tools" sort={sort} order={order} onClick={toggleSort} right />
                  <SortTh label="Win" col="windows" sort={sort} order={order} onClick={toggleSort} right last />
                </tr>
              </thead>
              <tbody>
                {items.map((s, i) => (
                  <tr
                    key={s.id}
                    id={`session-row-${i}`}
                    onClick={() =>
                      navigate(
                        `/sessions/${encodeURIComponent(s.id)}?${params.toString()}`,
                      )
                    }
                    className={cn(
                      "cursor-pointer border-b border-border-faint last:border-0 hover:bg-muted/40",
                      selected === i && "bg-muted/60",
                    )}
                    style={
                      selected === i
                        ? { boxShadow: `inset 2px 0 0 ${harnessColor(s.harness)}` }
                        : undefined
                    }
                  >
                    <td className="px-4 py-1.5">
                      <Link
                        to={`/sessions/${encodeURIComponent(s.id)}?${params.toString()}`}
                        onClick={(e) => e.stopPropagation()}
                        className="tabular whitespace-nowrap text-muted-foreground hover:text-foreground"
                      >
                        {formatFullTime(s.started_at)}
                      </Link>
                    </td>
                    <td className="py-1.5">
                      <HarnessTag harness={s.harness} />
                    </td>
                    <td className="max-w-[220px] py-1.5 pr-2">
                      <ModelBadge model={s.model} harness={s.harness} effort={s.effort} />
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
                  </tr>
                ))}
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

function FacetSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: Array<{ value: string; count: number }>;
  onChange: (v: string) => void;
}) {
  return (
    <label className="microlabel block text-[10px] text-faint-foreground">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded-control border border-border bg-background px-2 py-1.5 text-[12px] normal-case tracking-normal text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
      >
        <option value="">All</option>
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
