import { Link, useOutletContext, useSearchParams } from "react-router-dom";
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
import { fetchModelMix, fetchModelMonthly, fetchTimeseries } from "@/lib/api";
import { AggregatePanel } from "@/components/AggregatePanel";
import { EmptyState } from "@/components/EmptyState";
import { LoadingOrb } from "@/components/LoadingOrb";
import { Card, CardTitle, PanelCard } from "@/components/ui/card";
import { HarnessTag } from "@/components/ui/badges";
import { Meter } from "@/components/ui/spark";
import { CHART_TOOLTIP } from "@/components/ui/chartdefs";
import { dayTickFormatter, harnessColor } from "@/lib/utils";
import { rangeViewQueryOptions } from "@/lib/viewQueries";

type Ctx = { range: string };

const MODEL_COLORS = [
  "#5b9dff",
  "#2dd4bf",
  "#f5a623",
  "#e45cc3",
  "#a78bfa",
  "#4ade80",
  "#fb7185",
  "#fbbf24",
  "#7dd3fc",
  "#c084fc",
] as const;

function modelHue(model: string): string {
  if (model === "(other)") return "var(--harness-other)";
  if (model === "(unknown)") return "#94a3b8";
  let hash = 0;
  for (const char of model) hash = (hash * 31 + char.charCodeAt(0)) | 0;
  return MODEL_COLORS[(hash >>> 0) % MODEL_COLORS.length];
}

type ModelTooltipItem = {
  name?: string | number;
  value?: string | number;
};

function ModelTooltip({
  active,
  label,
  payload,
}: {
  active?: boolean;
  label?: string | number;
  payload?: ModelTooltipItem[];
}) {
  if (!active || !payload?.length) return null;
  const items = payload
    .filter((item) => Number(item.value) > 0)
    .sort((a, b) => Number(b.value) - Number(a.value));
  return (
    <div className="rounded-control border border-border bg-popover px-3 py-2 text-[12px] shadow-lg">
      <div className="mb-2 text-muted-foreground">{label}</div>
      <div className="space-y-1.5">
        {items.map((item) => {
          const name = String(item.name ?? "(unknown)");
          return (
            <div key={name} className="flex min-w-[150px] items-center gap-2">
              <span
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ backgroundColor: modelHue(name) }}
                aria-hidden
              />
              <span className="min-w-0 flex-1 truncate text-foreground">{name}</span>
              <span className="tabular text-muted-foreground">{item.value}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ModelLegend({ models }: { models: string[] }) {
  return (
    <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1.5 text-[10px] text-muted-foreground">
      {models.map((model) => (
        <span key={model} className="inline-flex items-center gap-1.5">
          <span
            className="h-2 w-2 rounded-full"
            style={{ backgroundColor: modelHue(model) }}
            aria-hidden
          />
          <span className="font-mono">{model}</span>
        </span>
      ))}
    </div>
  );
}

export function Models() {
  const { range } = useOutletContext<Ctx>();
  const [params] = useSearchParams();
  const [mixQ, monthlyQ, byModelQ] = useQueries({
    queries: [
      rangeViewQueryOptions({
        queryKey: ["models-page", range],
        queryFn: (signal) => fetchModelMix(range, signal),
      }),
      rangeViewQueryOptions({
        queryKey: ["models-monthly", range],
        queryFn: (signal) => fetchModelMonthly(range, signal),
      }),
      rangeViewQueryOptions({
        queryKey: ["timeseries-model", range],
        queryFn: (signal) => fetchTimeseries(range, "model", signal),
      }),
    ],
  });

  if (mixQ.isLoading || monthlyQ.isLoading || byModelQ.isLoading) {
    return <LoadingOrb label="Reading model activity" />;
  }
  if (!mixQ.data || !monthlyQ.data || !byModelQ.data) {
    return (
      <EmptyState
        title="Could not load model usage"
        body="One of the model endpoints failed — refresh or check agentlog serve."
      />
    );
  }

  const data = mixQ.data;
  const monthly = monthlyQ.data.series;
  const modelSeries = byModelQ.data.series;
  const modelKeys = Array.from(
    new Set(
      modelSeries.flatMap((row) =>
        Object.keys(row).filter((k) => k !== "day" && k !== "total"),
      ),
    ),
  );

  const monthlyTotals = new Map<string, number>();
  for (const month of monthly) {
    for (const item of month.items) {
      monthlyTotals.set(
        item.model,
        (monthlyTotals.get(item.model) ?? 0) + item.sessions,
      );
    }
  }
  const monthlyModels = new Set(
    [...monthlyTotals.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 8)
      .map(([model]) => model),
  );
  const monthlyChart = monthly.map((m) => {
    const row: Record<string, string | number> = { month: m.month, total: m.total };
    let other = 0;
    for (const item of m.items) {
      if (monthlyModels.has(item.model)) {
        row[item.model] = (Number(row[item.model]) || 0) + item.sessions;
      } else {
        other += item.sessions;
      }
    }
    if (other > 0) row["(other)"] = other;
    return row;
  });
  const monthKeys = Array.from(
    new Set(
      monthlyChart.flatMap((row) =>
        Object.keys(row).filter((k) => k !== "month" && k !== "total"),
      ),
    ),
  );
  const dailyHasOther = modelKeys.includes("(other)");
  const monthlyHasOther = monthlyChart.some((row) => row["(other)"] != null);
  const maxShare = Math.max(...data.items.map((i) => i.share), 0.001);

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h1 className="text-[18px] font-semibold tracking-tight">Models</h1>
        <span className="max-w-xl text-right text-[12px] text-faint-foreground">
          assistant-turn model mix — describes usage, not which model is better
        </span>
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <Card>
          <div className="flex items-baseline justify-between gap-2">
            <CardTitle>Session starts by recorded model, daily</CardTitle>
            {dailyHasOther ? (
              <span className="font-mono text-[10px] text-faint-foreground">
                top 8 + other
              </span>
            ) : null}
          </div>
          <div className="mt-3 h-[230px]">
            {modelSeries.length === 0 ? (
              <EmptyState
                title="No sessions in range"
                body="Daily stacks use each session's recorded model. Turn-level fallback attribution appears in the usage table below."
              />
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={modelSeries} barCategoryGap={2}>
                  <CartesianGrid stroke="var(--border-faint)" vertical={false} />
                  <XAxis
                    dataKey="day"
                    tick={{ fill: "var(--faint-foreground)", fontSize: 10 }}
                    tickFormatter={dayTickFormatter(modelSeries)}
                    axisLine={false}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{ fill: "var(--faint-foreground)", fontSize: 10 }}
                    axisLine={false}
                    tickLine={false}
                    width={26}
                  />
                  <Tooltip {...CHART_TOOLTIP} content={<ModelTooltip />} />
                  <defs>
                    {modelKeys.map((k, i) => (
                      <linearGradient key={k} id={`mgrad-${i}`} x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={modelHue(k)} stopOpacity={0.32} />
                        <stop offset="100%" stopColor={modelHue(k)} stopOpacity={0} />
                      </linearGradient>
                    ))}
                  </defs>
                  {modelKeys.map((k, i) => (
                    <Bar
                      key={k}
                      dataKey={k}
                      stackId="a"
                      fill={`url(#mgrad-${i})`}
                      stroke={modelHue(k)}
                      strokeWidth={1}
                      isAnimationActive={false}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
          {modelSeries.length > 0 ? <ModelLegend models={modelKeys} /> : null}
        </Card>

        <Card>
          <div className="flex items-baseline justify-between gap-2">
            <CardTitle>Session starts by recorded model, monthly</CardTitle>
            {monthlyHasOther ? (
              <span className="font-mono text-[10px] text-faint-foreground">
                top 8 + other
              </span>
            ) : null}
          </div>
          <div className="mt-3 h-[230px]">
            {monthlyChart.length === 0 ? (
              <EmptyState
                title="No monthly buckets yet"
                body="Monthly stacks use each session's recorded model."
              />
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={monthlyChart} barCategoryGap={8}>
                  <CartesianGrid stroke="var(--border-faint)" vertical={false} />
                  <XAxis
                    dataKey="month"
                    tick={{ fill: "var(--faint-foreground)", fontSize: 10 }}
                    axisLine={false}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{ fill: "var(--faint-foreground)", fontSize: 10 }}
                    axisLine={false}
                    tickLine={false}
                    width={26}
                  />
                  <Tooltip {...CHART_TOOLTIP} content={<ModelTooltip />} />
                  <defs>
                    {monthKeys.map((k, i) => (
                      <linearGradient key={k} id={`mograd-${i}`} x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={modelHue(k)} stopOpacity={0.32} />
                        <stop offset="100%" stopColor={modelHue(k)} stopOpacity={0} />
                      </linearGradient>
                    ))}
                  </defs>
                  {monthKeys.map((k, i) => (
                    <Bar
                      key={k}
                      dataKey={k}
                      stackId="a"
                      fill={`url(#mograd-${i})`}
                      stroke={modelHue(k)}
                      strokeWidth={1}
                      isAnimationActive={false}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
          {monthlyChart.length > 0 ? <ModelLegend models={monthKeys} /> : null}
        </Card>
      </div>

      <PanelCard title="Usage table" aside={`${data.items.length} models`}>
        <div className="overflow-x-auto">
          <table className="min-w-[720px] w-full text-left text-[12px]">
          <thead>
            <tr className="microlabel border-b border-border text-[10px] text-faint-foreground">
              <th className="px-4 py-2 font-medium">Model</th>
              <th className="py-2 font-medium">Harnesses</th>
              <th className="py-2 text-right font-medium">Assistant turns</th>
              <th className="py-2 text-right font-medium">Sessions seen</th>
              <th className="w-[30%] py-2 pl-6 font-medium">Share</th>
              <th className="px-4 py-2 text-right font-medium">%</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((item, rank) => (
              <tr
                key={item.model}
                className="border-b border-border-faint last:border-0 hover:bg-muted/40"
              >
                <td className="px-4 py-1.5">
                  <Link
                    to={{
                      pathname: "/sessions",
                      search: (() => {
                        const p = new URLSearchParams(params);
                        p.set("model", item.model);
                        return p.toString();
                      })(),
                    }}
                    className="font-mono text-[12px] text-muted-foreground hover:text-foreground"
                  >
                    {item.model}
                  </Link>
                </td>
                <td className="py-1.5">
                  <span className="flex flex-wrap items-center gap-1">
                    {item.harnesses.map((h) => (
                      <span
                        key={h.harness}
                        className="flex items-center gap-1"
                        title={`${h.sessions} sessions`}
                      >
                        <HarnessTag harness={h.harness} />
                        <span className="tabular font-mono text-[10px] text-faint-foreground">
                          {h.sessions}
                        </span>
                      </span>
                    ))}
                  </span>
                </td>
                <td className="tabular py-1.5 text-right">{item.messages}</td>
                <td className="tabular py-1.5 text-right">{item.sessions}</td>
                <td className="py-1.5 pl-6">
                  <Meter
                    ratio={item.share / maxShare}
                    color={harnessColor(item.harnesses[0]?.harness ?? "other")}
                  />
                </td>
                <td className="tabular px-4 py-1.5 text-right text-muted-foreground">
                  {rank === 0 ? (
                    <span className="display-md display-ink">
                      {(item.share * 100).toFixed(1)}%
                    </span>
                  ) : (
                    `${(item.share * 100).toFixed(1)}%`
                  )}
                </td>
              </tr>
            ))}
          </tbody>
          </table>
        </div>
      </PanelCard>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <PanelCard
          title="Agent profiles"
          aside={`${data.profiles.length} profiles`}
        >
          <p className="px-4 pt-3 text-[11px] leading-relaxed text-faint-foreground">
            {data.profiles_note}
          </p>
          <div className="space-y-1.5 p-4 pt-2">
            {data.profiles.length === 0 ? (
              <div className="font-mono text-[11px] text-faint-foreground">
                no agent profiles recorded in range
              </div>
            ) : (
              data.profiles.map((p) => (
                <div
                  key={p.agent_profile}
                  className="flex items-baseline justify-between gap-2"
                >
                  <span className="min-w-0 truncate font-mono text-[11px] text-muted-foreground">
                    {p.agent_profile}
                  </span>
                  <span className="flex shrink-0 items-center gap-2">
                    {p.harnesses.map((h) => (
                      <HarnessTag key={h.harness} harness={h.harness} />
                    ))}
                    <span className="tabular font-mono text-[11px] text-faint-foreground">
                      {p.sessions}
                    </span>
                  </span>
                </div>
              ))
            )}
          </div>
        </PanelCard>

        <PanelCard
          title="Unknown model"
          aside={`${data.unknown.messages} turns · ${data.unknown.sessions} sessions`}
        >
          <p className="px-4 pt-3 text-[11px] leading-relaxed text-faint-foreground">
            {data.unknown_note}
          </p>
          <div className="space-y-2 p-4 pt-2">
            {data.unknown.reasons.length === 0 ? (
              <div className="font-mono text-[11px] text-faint-foreground">
                every session in range resolved to a model
              </div>
            ) : (
              data.unknown.reasons.map((r) => (
                <div key={r.reason}>
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="min-w-0 truncate font-mono text-[11px] text-muted-foreground">
                      {r.description}
                    </span>
                    <span className="tabular shrink-0 font-mono text-[11px] text-faint-foreground">
                      {r.messages} turns · {r.sessions} sessions
                    </span>
                  </div>
                  <div className="mt-0.5 font-mono text-[10px] text-faint-foreground">
                    {r.raw_values
                      .map((v) => `${v.value} ${v.sessions}`)
                      .join(" · ")}
                  </div>
                </div>
              ))
            )}
          </div>
        </PanelCard>
      </div>

      <details className="rounded-card border border-border bg-card p-4">
        <summary className="microlabel cursor-pointer text-muted-foreground">
          Interaction-style rates — calibration required
        </summary>
        <p className="mt-2 max-w-2xl text-[12px] leading-relaxed text-faint-foreground">
          This section is intentionally unavailable until gold-label calibration and
          populated ux_observations meet the precision gate. When available, these
          rates describe steering frequency — they are not a quality score or model
          ranking.
        </p>
        <div className="mt-3 grid grid-cols-2 gap-3">
          {data.items.slice(0, 4).map((item) => (
            <div
              key={`style-${item.model}`}
              className="rounded-card border border-border-faint p-3"
            >
              <div className="mb-2 font-mono text-[12px]">{item.model}</div>
              <AggregatePanel
                title="Redirect / brake"
                cell={item.interaction_style}
              />
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}
