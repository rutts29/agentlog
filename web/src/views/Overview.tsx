import { useOutletContext, Link, useSearchParams } from "react-router-dom";
import { useQueries } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  fetchHeatmap,
  fetchModelMix,
  fetchProjects,
  fetchRecent,
  fetchSummary,
  fetchTimeseries,
} from "@/lib/api";
import { AggregatePanel } from "@/components/AggregatePanel";
import { EmptyState } from "@/components/EmptyState";
import { Card, CardTitle } from "@/components/ui/card";
import { cn, formatDuration, harnessColor } from "@/lib/utils";

type Ctx = { range: string };

const HARNESSES = ["codex", "claude", "cursor"] as const;

export function Overview() {
  const { range } = useOutletContext<Ctx>();
  const [params] = useSearchParams();

  const [summary, timeseries, models, heatmap, projects, recent] = useQueries({
    queries: [
      { queryKey: ["summary", range], queryFn: () => fetchSummary(range) },
      { queryKey: ["timeseries", range], queryFn: () => fetchTimeseries(range) },
      { queryKey: ["models", range], queryFn: () => fetchModelMix(range) },
      { queryKey: ["heatmap", range], queryFn: () => fetchHeatmap(range) },
      { queryKey: ["projects", range], queryFn: () => fetchProjects(range) },
      { queryKey: ["recent", range], queryFn: () => fetchRecent(range) },
    ],
  });

  const loading = [summary, timeseries, models, heatmap, projects, recent].some(
    (q) => q.isLoading,
  );
  if (loading) {
    return <div className="text-[13px] text-muted-foreground">Loading…</div>;
  }

  const kpis = summary.data!.kpis;
  const delta = kpis.sessions.delta_ratio;
  const mix = models.data!.items.slice(0, 6);
  const maxShare = Math.max(...mix.map((m) => m.share), 0.001);
  const heat = heatmap.data!;
  const maxHeat = Math.max(...heat.counts.flat(), 1);

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-5 gap-3">
        <Card>
          <CardTitle>Sessions</CardTitle>
          <div className="mt-2 tabular text-2xl font-semibold">
            {kpis.sessions.value.toLocaleString()}
          </div>
          <div className="mt-1 text-[12px] text-muted-foreground">
            {delta == null
              ? "No prior period"
              : `${delta >= 0 ? "+" : ""}${(delta * 100).toFixed(0)}% vs prior equal window`}
          </div>
        </Card>

        <Card>
          <CardTitle>Tokens (est.)</CardTitle>
          <div className="mt-2">
            <EmptyState
              title="Not available"
              body={kpis.tokens_est.message}
              className="min-h-0 border-0 bg-transparent p-0"
            />
          </div>
        </Card>

        <Card>
          <CardTitle>Cost (est.)</CardTitle>
          <div className="mt-2">
            <EmptyState
              title="Not available"
              body={kpis.cost_est.message}
              className="min-h-0 border-0 bg-transparent p-0"
            />
          </div>
        </Card>

        <Card>
          <AggregatePanel
            title="Redirect / brake rate"
            cell={kpis.interaction_style}
          />
        </Card>

        <Card>
          <CardTitle>{kpis.streak.label}</CardTitle>
          <div className="mt-2 tabular text-2xl font-semibold">
            {kpis.streak.current_days} days
          </div>
          <div className="mt-1 text-[12px] text-muted-foreground">
            longest streak in range: {kpis.streak.longest_days}
          </div>
          <div className="mt-1 text-[11px] text-faint-foreground">
            {kpis.streak.note}
          </div>
        </Card>
      </div>

      <div className="grid grid-cols-5 gap-3">
        <Card className="col-span-3">
          <CardTitle>Sessions by harness</CardTitle>
          <div className="mt-3 h-[220px]">
            {timeseries.data!.series.length === 0 ? (
              <EmptyState
                title="No sessions in range"
                body="Widen the time range or ingest transcripts to populate this chart."
              />
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={timeseries.data!.series} barCategoryGap={2}>
                  <CartesianGrid stroke="var(--border)" vertical={false} />
                  <XAxis
                    dataKey="day"
                    tick={{ fill: "var(--faint-foreground)", fontSize: 11 }}
                    axisLine={false}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{ fill: "var(--faint-foreground)", fontSize: 11 }}
                    axisLine={false}
                    tickLine={false}
                    width={28}
                  />
                  <Tooltip
                    contentStyle={{
                      background: "var(--popover)",
                      border: "1px solid var(--border)",
                      borderRadius: 6,
                      fontSize: 12,
                    }}
                    cursor={{ fill: "var(--muted)" }}
                  />
                  {HARNESSES.map((h) => (
                    <Bar
                      key={h}
                      dataKey={h}
                      stackId="a"
                      fill={harnessColor(h)}
                      fillOpacity={0.75}
                      stroke={harnessColor(h)}
                      strokeWidth={1.5}
                      isAnimationActive={false}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
          <div className="mt-2 flex gap-3 text-[11px] text-muted-foreground">
            {HARNESSES.map((h) => (
              <span key={h} className="inline-flex items-center gap-1.5">
                <span
                  className="inline-block h-2 w-2 rounded-sm"
                  style={{ background: harnessColor(h) }}
                />
                {h}
              </span>
            ))}
          </div>
        </Card>

        <Card className="col-span-2">
          <CardTitle>Model mix</CardTitle>
          <p className="mt-1 text-[11px] text-faint-foreground">
            Selection shares — not a quality ranking.
          </p>
          <div className="mt-3 space-y-2">
            {mix.map((m) => (
              <Link
                key={`${m.harness}-${m.model}`}
                to={{
                  pathname: "/sessions",
                  search: (() => {
                    const p = new URLSearchParams(params);
                    p.set("model", m.model);
                    return p.toString();
                  })(),
                }}
                className="block"
              >
                <div className="mb-1 flex justify-between text-[12px]">
                  <span className="truncate font-mono text-[12px]">
                    {m.model}
                  </span>
                  <span className="tabular text-muted-foreground">
                    {(m.share * 100).toFixed(0)}%
                  </span>
                </div>
                <div className="h-1.5 rounded-full bg-muted">
                  <div
                    className="h-1.5 rounded-full"
                    style={{
                      width: `${(m.share / maxShare) * 100}%`,
                      background: harnessColor(m.harness),
                      opacity: 0.8,
                    }}
                  />
                </div>
              </Link>
            ))}
          </div>
        </Card>
      </div>

      <div className="grid grid-cols-5 gap-3">
        <Card className="col-span-3">
          <CardTitle>Activity heatmap</CardTitle>
          <p className="mt-1 text-[11px] text-faint-foreground">{heat.note}</p>
          <div className="mt-3 overflow-x-auto">
            <div
              className="grid gap-[3px]"
              style={{
                gridTemplateColumns: `40px repeat(24, 12px)`,
              }}
            >
              <div />
              {heat.hours.map((h) => (
                <div
                  key={h}
                  className="text-center text-[10px] text-faint-foreground"
                >
                  {h % 4 === 0 ? h : ""}
                </div>
              ))}
              {heat.weekdays.map((day, di) => (
                <div key={day} className="contents">
                  <div className="text-[11px] text-muted-foreground">{day}</div>
                  {heat.counts[di].map((c, hi) => (
                    <div
                      key={`${day}-${hi}`}
                      title={`${day} ${hi}:00 — ${c} sessions`}
                      className="h-3 w-3 rounded-[2px]"
                      style={{
                        background: "var(--foreground)",
                        opacity: c === 0 ? 0.06 : 0.15 + (c / maxHeat) * 0.75,
                      }}
                    />
                  ))}
                </div>
              ))}
            </div>
          </div>
        </Card>

        <Card className="col-span-2">
          <CardTitle>Top projects</CardTitle>
          <div className="mt-3 space-y-2">
            {projects.data!.items.map((p) => (
              <div key={p.project} className="flex items-center gap-3 text-[12px]">
                <div className="min-w-0 flex-1 truncate">{p.project}</div>
                <div className="tabular text-muted-foreground">{p.sessions}</div>
                <Sparkline values={p.sparkline} />
              </div>
            ))}
          </div>
        </Card>
      </div>

      <Card>
        <div className="mb-3 flex items-baseline justify-between">
          <CardTitle>Recent sessions</CardTitle>
          <Link
            to={{ pathname: "/sessions", search: params.toString() }}
            className="text-[12px] text-muted-foreground hover:text-foreground"
          >
            View all
          </Link>
        </div>
        <table className="w-full text-left text-[13px]">
          <thead className="text-[11px] uppercase tracking-wide text-faint-foreground">
            <tr className="border-b border-border">
              <th className="py-2 font-medium">Time</th>
              <th className="py-2 font-medium">Harness</th>
              <th className="py-2 font-medium">Model</th>
              <th className="py-2 font-medium">Project</th>
              <th className="py-2 font-medium">Dur</th>
              <th className="py-2 font-medium">Msgs</th>
              <th className="py-2 font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {recent.data!.items.map((s) => (
              <tr
                key={s.id}
                className="border-b border-border/60 text-[13px] hover:bg-muted/50"
              >
                <td className="py-2 tabular text-muted-foreground">
                  {s.started_at
                    ? new Date(s.started_at).toLocaleString(undefined, {
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      })
                    : "—"}
                </td>
                <td className="py-2">
                  <span
                    className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full"
                    style={{ background: harnessColor(s.harness) }}
                  />
                  {s.harness}
                </td>
                <td className="py-2 font-mono text-[12px]">{s.model}</td>
                <td className="py-2">{s.project}</td>
                <td className="py-2 tabular text-muted-foreground">
                  {formatDuration(s.duration_seconds)}
                </td>
                <td className="py-2 tabular">{s.message_count}</td>
                <td className="py-2">
                  <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                    <span
                      className={cn(
                        "inline-block h-1.5 w-1.5 rounded-full bg-status-info",
                      )}
                    />
                    {s.status}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

function Sparkline({ values }: { values: number[] }) {
  const max = Math.max(...values, 1);
  const w = 60;
  const h = 16;
  const pts = values
    .map((v, i) => {
      const x = values.length <= 1 ? 0 : (i / (values.length - 1)) * w;
      const y = h - (v / max) * (h - 2) - 1;
      return `${x},${y}`;
    })
    .join(" ");
  return (
    <svg width={w} height={h} className="shrink-0">
      <polyline
        fill="none"
        stroke="var(--muted-foreground)"
        strokeWidth="1.5"
        points={pts}
      />
    </svg>
  );
}
