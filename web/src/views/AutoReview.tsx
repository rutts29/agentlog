import { Link, useOutletContext, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { fetchAutoReview } from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { Card, CardTitle, PanelCard } from "@/components/ui/card";
import { Kpi } from "@/components/ui/kpi";
import { HarnessTag, ModelBadge, StatusDot } from "@/components/ui/badges";
import { CHART_TOOLTIP } from "@/components/ui/chartdefs";
import { dayTickFormatter, formatDayTime, harnessColor } from "@/lib/utils";

type Ctx = { range: string };

export function AutoReview() {
  const { range } = useOutletContext<Ctx>();
  const [params] = useSearchParams();
  const q = useQuery({
    queryKey: ["auto-review", range],
    queryFn: () => fetchAutoReview(range),
  });

  if (q.isLoading) {
    return <div className="text-[13px] text-muted-foreground">Loading…</div>;
  }
  if (q.isError || !q.data) {
    return (
      <EmptyState
        title="Could not load auto-review"
        body="The auto-review endpoint failed — refresh or check agentlog serve."
      />
    );
  }

  const data = q.data;
  const maxByModel = Math.max(...data.by_model.map((m) => m.count), 1);

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h1 className="text-[18px] font-semibold tracking-tight">Auto-review</h1>
        <span className="max-w-xl text-right text-[12px] text-faint-foreground">
          excluded from interaction-style metrics — listed as observed volume
        </span>
      </div>

      <div className="grid grid-cols-3 gap-3">
        <Kpi title="Observations" value={data.total} sub="auto-review windows" />
        <Kpi title="Models involved" value={data.by_model.length} sub="distinct model ids" />
        <Kpi title="Active days" value={data.by_day.length} sub="days with auto-review traffic" />
      </div>

      <div className="grid grid-cols-5 gap-3">
        <Card className="col-span-3">
          <CardTitle>Volume by day</CardTitle>
          <div className="mt-3 h-[210px]">
            {data.by_day.length === 0 ? (
              <EmptyState
                title="No auto-reviews in range"
                body="Bars appear per day once auto_review_observations exist in this window — a re-ingest may be repopulating them now."
                missing={["auto_review_observations"]}
              />
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={data.by_day}>
                  <CartesianGrid stroke="var(--border-faint)" vertical={false} />
                  <XAxis
                    dataKey="day"
                    tick={{ fill: "var(--faint-foreground)", fontSize: 10 }}
                    tickFormatter={dayTickFormatter(q.data.by_day)}
                    axisLine={false}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{ fill: "var(--faint-foreground)", fontSize: 10 }}
                    axisLine={false}
                    tickLine={false}
                    width={26}
                  />
                  <Tooltip {...CHART_TOOLTIP} />
                  <Bar
                    dataKey="count"
                    fill="var(--speaker-synthetic)"
                    fillOpacity={0.7}
                    isAnimationActive={false}
                  />
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
        </Card>
        <Card className="col-span-2">
          <CardTitle>By model</CardTitle>
          {data.by_model.length === 0 ? (
            <div className="mt-3">
              <EmptyState
                title="No models yet"
                body="Model attribution appears alongside the daily volume."
                className="min-h-[100px]"
              />
            </div>
          ) : (
            <div className="mt-3 max-h-[210px] space-y-1.5 overflow-y-auto">
              {data.by_model.map((m) => {
                const lead = m.harnesses[0]?.harness ?? "other";
                return (
                <div
                  key={m.model}
                  className="flex items-center gap-2 text-[11px]"
                  title={m.harnesses
                    .map((h) => `${h.harness} ${h.count}`)
                    .join(" · ")}
                >
                  <span
                    aria-hidden
                    className="inline-block h-[5px] w-[5px] shrink-0 rounded-full"
                    style={{ background: harnessColor(lead) }}
                  />
                  <span className="min-w-0 flex-1 truncate font-mono text-muted-foreground">
                    {m.model}
                  </span>
                  <div className="h-[3px] w-20 shrink-0 overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full rounded-full"
                      style={{
                        width: `${(m.count / maxByModel) * 100}%`,
                        background: harnessColor(lead),
                        opacity: 0.75,
                      }}
                    />
                  </div>
                  <span className="tabular w-8 shrink-0 text-right text-faint-foreground">
                    {m.count}
                  </span>
                </div>
                );
              })}
            </div>
          )}
        </Card>
      </div>

      <PanelCard title="Recent auto-review sessions" aside={`${data.items.length} shown`}>
        {data.items.length === 0 ? (
          <div className="p-4">
            <EmptyState
              title="None in range"
              body="Rows list the most recent auto-review observations with their session, model, and route."
              missing={["auto_review_observations"]}
            />
          </div>
        ) : (
          <table className="w-full text-left text-[12px]">
            <thead>
              <tr className="microlabel border-b border-border text-[10px] text-faint-foreground">
                <th className="px-4 py-2 font-medium">When</th>
                <th className="py-2 font-medium">Harness</th>
                <th className="py-2 font-medium">Model</th>
                <th className="py-2 font-medium">Project</th>
                <th className="py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">Session</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => (
                <tr
                  key={item.id}
                  className="border-b border-border-faint last:border-0 hover:bg-muted/40"
                >
                  <td className="tabular px-4 py-1.5 text-muted-foreground">
                    {formatDayTime(item.started_at ?? item.created_at)}
                  </td>
                  <td className="py-1.5">
                    <HarnessTag harness={item.harness} />
                  </td>
                  <td className="py-1.5">
                    <ModelBadge model={item.model} harness={item.harness} effort={item.effort} />
                  </td>
                  <td className="max-w-[160px] truncate py-1.5 pr-2 text-muted-foreground">
                    {item.project}
                  </td>
                  <td className="py-1.5">
                    {item.status ? (
                      <StatusDot tone="neutral" label={item.status} />
                    ) : (
                      <span className="text-faint-foreground">—</span>
                    )}
                  </td>
                  <td className="px-4 py-1.5">
                    <Link
                      to={`/sessions/${encodeURIComponent(item.session_id)}?${params.toString()}`}
                      className="font-mono text-[11px] text-muted-foreground hover:text-foreground"
                    >
                      open →
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </PanelCard>
    </div>
  );
}
