import { FormEvent, useState } from "react";
import { Link, useOutletContext, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchFacets, fetchSearch } from "@/lib/api";
import { Snippet } from "@/components/Snippet";
import { EmptyState } from "@/components/EmptyState";
import { LoadingOrb } from "@/components/LoadingOrb";
import { PanelCard } from "@/components/ui/card";
import { HarnessTag, ModelBadge, TranscriptStorageBadge } from "@/components/ui/badges";
import { formatDayTime } from "@/lib/utils";
import { rangeViewQueryOptions } from "@/lib/viewQueries";

type Ctx = { range: string };

export function Search() {
  const { range } = useOutletContext<Ctx>();
  const [params, setParams] = useSearchParams();
  const qParam = params.get("q") ?? "";
  const [draft, setDraft] = useState(qParam);
  const harness = params.get("harness") ?? "";
  const model = params.get("model") ?? "";
  const project = params.get("project") ?? "";
  const cursor = Number(params.get("cursor") || "0");

  const facets = useQuery(rangeViewQueryOptions({
    queryKey: ["facets", range],
    queryFn: (signal) => fetchFacets(range, undefined, signal),
  }));

  const results = useQuery({
    queryKey: ["search", range, qParam, harness, model, project, cursor],
    queryFn: ({ signal }) =>
      fetchSearch(range, qParam, {
        harness: harness || undefined,
        model: model || undefined,
        project: project || undefined,
        cursor,
        limit: 40,
      }, signal),
    enabled: qParam.trim().length > 0,
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const next = new URLSearchParams(params);
    if (draft.trim()) next.set("q", draft.trim());
    else next.delete("q");
    next.delete("cursor");
    setParams(next);
  }

  function setFilter(key: string, value: string) {
    const next = new URLSearchParams(params);
    next.delete("cursor");
    if (!value) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  }

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h1 className="text-[18px] font-semibold tracking-tight">Search</h1>
        <span className="text-[12px] text-faint-foreground">
          legacy index + canonical transcript sources
        </span>
      </div>

      <div className="rounded-card border border-border bg-card p-3">
        <form onSubmit={onSubmit} className="flex gap-2">
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Search messages…"
            className="min-w-[240px] flex-1 rounded-control border border-border bg-background px-3 py-2 text-[13px] placeholder:text-faint-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
          <button
            type="submit"
            className="rounded-control border border-border bg-muted px-3.5 py-2 text-[13px] text-foreground hover:bg-popover"
          >
            Search
          </button>
        </form>
        {facets.data ? (
          <div className="mt-2.5 grid grid-cols-3 gap-2">
            <Filter
              label="Harness"
              value={harness}
              options={facets.data.harness}
              onChange={(v) => setFilter("harness", v)}
            />
            <Filter
              label="Model"
              value={model}
              options={facets.data.model}
              onChange={(v) => setFilter("model", v)}
            />
            <Filter
              label="Project"
              value={project}
              options={facets.data.project}
              onChange={(v) => setFilter("project", v)}
            />
          </div>
        ) : null}
      </div>

      {!qParam.trim() ? (
        <EmptyState
          title="Enter a query"
          body="Legacy sessions use the local search index; new sessions are read from their canonical harness transcripts. Hits open the matching turn. Try: refactor, parent_session_id, auto-review."
          missing={["legacy FTS + source scan"]}
        />
      ) : results.isLoading ? (
        <LoadingOrb label="Searching transcripts" compact />
      ) : results.isError ? (
        <EmptyState
          title="Search failed"
          body="The search endpoint returned an error. Check the canonical source status, legacy index, and query syntax."
        />
      ) : results.data!.items.length === 0 ? (
        <EmptyState
          title="No matches"
          body="Nothing in the ledger contains all of those terms. Fewer filters or a broader term will widen the net."
        />
      ) : (
        <PanelCard
          title="Results"
          aside={`${results.data!.total.toLocaleString()}${results.data!.truncated ? "+" : ""} hits`}
        >
          <div className="divide-y divide-border-faint">
            {results.data!.items.map((hit) => (
              <Link
                key={hit.message_id}
                to={`/sessions/${encodeURIComponent(hit.session_id)}?${(() => {
                  const p = new URLSearchParams(params);
                  p.set("msg", hit.message_id);
                  return p.toString();
                })()}`}
                className="block px-4 py-2.5 hover:bg-muted/40"
              >
                <div className="mb-1 flex flex-wrap items-center gap-x-2.5 gap-y-1">
                  <HarnessTag harness={hit.harness} />
                  <ModelBadge model={hit.model} harness={hit.harness} />
                  <TranscriptStorageBadge
                    storage={hit.transcript_storage ?? hit.provenance?.session_storage}
                    sourceStatus={hit.provenance?.source_status}
                  />
                  <span className="text-[11px] text-muted-foreground">{hit.project}</span>
                  <span
                    className="microlabel text-[9px]"
                    style={{
                      color:
                        hit.role === "user"
                          ? "var(--speaker-human)"
                          : "var(--muted-foreground)",
                    }}
                  >
                    {hit.role === "user" ? "human-side" : hit.role}
                  </span>
                  <span className="tabular ml-auto text-[11px] text-faint-foreground">
                    {formatDayTime(hit.timestamp ?? hit.started_at)}
                  </span>
                </div>
                <Snippet text={hit.snippet} />
              </Link>
            ))}
          </div>
          <div className="flex justify-between border-t border-border px-4 py-2.5 text-[12px] text-muted-foreground">
            <button
              type="button"
              disabled={cursor <= 0}
              className="hover:text-foreground disabled:opacity-40"
              onClick={() => {
                const next = new URLSearchParams(params);
                next.set("cursor", String(Math.max(0, cursor - 40)));
                setParams(next);
              }}
            >
              ← Previous
            </button>
            <button
              type="button"
              disabled={results.data!.next_cursor == null}
              className="hover:text-foreground disabled:opacity-40"
              onClick={() => {
                if (results.data!.next_cursor == null) return;
                const next = new URLSearchParams(params);
                next.set("cursor", String(results.data!.next_cursor));
                setParams(next);
              }}
            >
              Next →
            </button>
          </div>
        </PanelCard>
      )}
    </div>
  );
}

function Filter({
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
