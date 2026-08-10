import { useCallback, useEffect, useRef, useState } from "react";
import { useOutletContext, Link, useSearchParams } from "react-router-dom";
import { useQueries, useQueryClient } from "@tanstack/react-query";
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
  fetchAttention,
  fetchDistributions,
  fetchGraph,
  fetchHeatmap,
  fetchModelMix,
  fetchProjects,
  fetchRecent,
  fetchRequestKinds,
  fetchSummary,
  fetchTimeseries,
  fetchTools,
  type PresenceEvent,
} from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { ConstellationGraph } from "@/components/ConstellationGraph";
import { Card, CardTitle } from "@/components/ui/card";
import { KpiPartial, KpiTile, KpiUnavailable } from "@/components/ui/kpi";
import { HarnessTag, StatusDot } from "@/components/ui/badges";
import { Sparkline, Meter } from "@/components/ui/spark";
import { CHART_TOOLTIP, ChartDefs, chartGradient } from "@/components/ui/chartdefs";
import { useIngestStream } from "@/lib/useIngestStream";
import { sessionPresenceKey, useLivePresence } from "@/lib/useLivePresence";
import {
  dayTickFormatter,
  formatDuration,
  formatCount,
  formatDayTime,
  formatClock,
  harnessColor,
  truncLabel,
} from "@/lib/utils";
import { LiveOrb } from "@/components/LiveOrb";
import type { AttentionItem, LiveSession } from "@/lib/api";

type Ctx = { range: string };

const HARNESSES = ["codex", "claude", "cursor", "warp"] as const;

/* Cool density ramp: stage → teal → accent-live (readable on true black). */
function heatColor(t: number): string {
  if (t <= 0) return "#0a0a0a";
  const lerp = (a: number, b: number, p: number) => Math.round(a + (b - a) * p);
  const mix = (c1: number[], c2: number[], p: number) =>
    `rgb(${lerp(c1[0], c2[0], p)},${lerp(c1[1], c2[1], p)},${lerp(c1[2], c2[2], p)})`;
  const dark = [12, 22, 28];
  const mid = [36, 110, 130];
  const light = [34, 211, 238];
  return t < 0.5 ? mix(dark, mid, t * 2) : mix(mid, light, (t - 0.5) * 2);
}

/* Column headers in the ref-1 "instrument label" register. */
function ColumnLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="microlabel border-b border-border-faint pb-1.5 font-mono text-[10px] text-faint-foreground">
      {children}
    </div>
  );
}

function attentionIdentity(item: AttentionItem): string {
  if (item.repo) return truncLabel(item.repo, 20);
  const id = item.session_id || "";
  const tail = id.includes("/")
    ? id.slice(id.lastIndexOf("/") + 1)
    : id.includes(":")
      ? id.slice(id.indexOf(":") + 1)
      : id;
  return truncLabel(tail || id, 16);
}

function attentionBlurb(item: AttentionItem): string {
  let reason = (item.reason || item.state || "").trim();
  const cut = reason.search(/\bNot urgent\b/i);
  if (cut > 0) reason = reason.slice(0, cut).trim().replace(/[.\s]+$/, "");
  reason = reason.replace(/^Live session is waiting on you\b[^.]*\.?\s*/i, "");
  if (reason.length > 64) reason = `${reason.slice(0, 63)}\u2026`;
  return reason || item.state;
}

function AttentionRow({
  item,
  search,
  tone,
  quiet,
  flash,
  live,
}: {
  item: AttentionItem;
  search: string;
  tone: "ok" | "warn" | "error" | "info" | "neutral";
  quiet?: boolean;
  flash?: boolean;
  live?: LiveSession;
}) {
  return (
    <Link
      to={`/sessions/${encodeURIComponent(item.session_id)}?${search}`}
      className={
        "block rounded-[4px] px-0.5 py-0.5 hover:bg-muted/40 " +
        (flash ? "live-glow" : "")
      }
    >
      <div className="flex items-center gap-1.5">
        {live ? (
          <LiveOrb
            state={live.state}
            harnessColor={harnessColor(live.harness)}
            size={16}
            worker={live.role === "worker"}
            title={live.activity ?? live.state}
          />
        ) : (
          <span
            aria-hidden
            className="inline-block h-[6px] w-[6px] shrink-0 rounded-full"
            style={{
              background: item.harness
                ? harnessColor(item.harness)
                : "var(--faint-foreground)",
            }}
          />
        )}
        <span
          className={
            "min-w-0 truncate font-mono text-[11px] " +
            (quiet ? "text-faint-foreground" : "text-foreground")
          }
        >
          {attentionIdentity(item)}
        </span>
        {item.harness ? (
          <span className="shrink-0 font-mono text-[10px] text-faint-foreground">
            {item.harness}
          </span>
        ) : null}
      </div>
      <div className={"mt-0.5 " + (live ? "pl-[22px]" : "pl-[12px]")}>
        <StatusDot
          tone={live ? "info" : tone}
          label={live ? (live.activity ?? attentionBlurb(item)) : attentionBlurb(item)}
          className={
            "w-full text-[11px] " +
            (quiet ? "text-faint-foreground" : "hover:text-foreground")
          }
        />
      </div>
    </Link>
  );
}

function useMinWidth(px: number): boolean {
  const [matches, setMatches] = useState(
    () => window.matchMedia(`(min-width: ${px}px)`).matches,
  );
  useEffect(() => {
    const mq = window.matchMedia(`(min-width: ${px}px)`);
    const cb = (e: MediaQueryListEvent) => setMatches(e.matches);
    mq.addEventListener("change", cb);
    return () => mq.removeEventListener("change", cb);
  }, [px]);
  return matches;
}

export function Overview() {
  const { range } = useOutletContext<Ctx>();
  const [params] = useSearchParams();
  const queryClient = useQueryClient();
  const wideEnoughForGraph = useMinWidth(900);
  const focusId = params.get("focus");

  const onPresenceRef = useRef<(data: PresenceEvent) => void>(() => {});
  const attentionDebounce = useRef<number | null>(null);
  const scheduleAttentionRefresh = useCallback(() => {
    if (attentionDebounce.current)
      window.clearTimeout(attentionDebounce.current);
    attentionDebounce.current = window.setTimeout(() => {
      queryClient.invalidateQueries({ queryKey: ["attention"] });
    }, 750);
  }, [queryClient]);

  const { connected } = useIngestStream(
    ({ events }) => {
      if (events.length === 0) return;
      for (const key of ["graph", "recent", "summary", "meta", "timeseries"]) {
        queryClient.invalidateQueries({ queryKey: [key] });
      }
      scheduleAttentionRefresh();
    },
    (data) => {
      onPresenceRef.current(data);
      /* Presence changes can clear or promote live_waiting / waiting items. */
      scheduleAttentionRefresh();
    },
  );
  const { presence, onPresenceEvent } = useLivePresence(connected);
  useEffect(() => {
    onPresenceRef.current = onPresenceEvent;
  }, [onPresenceEvent]);
  useEffect(() => {
    return () => {
      if (attentionDebounce.current)
        window.clearTimeout(attentionDebounce.current);
    };
  }, []);

  const [summary, timeseries, models, heatmap, projects, recent, tools, kinds, dist, graph, attention] =
    useQueries({
      queries: [
        { queryKey: ["summary", range], queryFn: () => fetchSummary(range) },
        { queryKey: ["timeseries", range], queryFn: () => fetchTimeseries(range) },
        { queryKey: ["models", range], queryFn: () => fetchModelMix(range) },
        { queryKey: ["heatmap", range], queryFn: () => fetchHeatmap(range) },
        { queryKey: ["projects", range], queryFn: () => fetchProjects(range) },
        { queryKey: ["recent", range], queryFn: () => fetchRecent(range) },
        { queryKey: ["tools", range], queryFn: () => fetchTools(range, 12) },
        { queryKey: ["kinds", range], queryFn: () => fetchRequestKinds(range) },
        { queryKey: ["dist", range], queryFn: () => fetchDistributions(range) },
        { queryKey: ["graph", range], queryFn: () => fetchGraph(range) },
        { queryKey: ["attention"], queryFn: fetchAttention },
      ],
    });

  /* Live-feed glow: the newest row lights up when it first appears. */
  const prevTopRef = useRef<string | null>(null);
  const [glowId, setGlowId] = useState<string | null>(null);
  const topId = recent.data?.items[0]?.id ?? null;
  useEffect(() => {
    if (topId && prevTopRef.current && topId !== prevTopRef.current) {
      setGlowId(topId);
      const t = window.setTimeout(() => setGlowId(null), 2000);
      prevTopRef.current = topId;
      return () => window.clearTimeout(t);
    }
    prevTopRef.current = topId;
  }, [topId]);

  /* One-shot flash when Attention membership changes (not on first paint). */
  const attnSeenRef = useRef(false);
  const attnPrevKeys = useRef<Set<string>>(new Set());
  const [attnFlash, setAttnFlash] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    if (!attention.data) return;
    const urgent = (attention.data.items ?? []).filter(
      (item) => !String(item.state).startsWith("live_"),
    );
    const resumable = attention.data.resumable ?? [];
    const keys = new Set(
      [...urgent, ...resumable].map(
        (item) => `${item.session_id}:${item.state}`,
      ),
    );
    if (!attnSeenRef.current) {
      attnSeenRef.current = true;
      attnPrevKeys.current = keys;
      return;
    }
    const arrived: string[] = [];
    for (const key of keys) {
      if (!attnPrevKeys.current.has(key)) arrived.push(key);
    }
    const cleared = [...attnPrevKeys.current].some((key) => !keys.has(key));
    attnPrevKeys.current = keys;
    if (arrived.length === 0 && !cleared) return;
    setAttnFlash(new Set(arrived.length > 0 ? arrived : ["__cleared__"]));
    const t = window.setTimeout(() => setAttnFlash(new Set()), 1600);
    return () => window.clearTimeout(t);
  }, [attention.data]);

  const queries = [summary, timeseries, models, heatmap, projects, recent, tools, kinds, dist, graph];
  if (queries.some((q) => q.isLoading)) {
    return <div className="text-[13px] text-muted-foreground">Loading…</div>;
  }
  if (
    !summary.data ||
    !timeseries.data ||
    !models.data ||
    !heatmap.data ||
    !projects.data ||
    !recent.data ||
    !tools.data ||
    !kinds.data ||
    !dist.data ||
    !graph.data
  ) {
    return (
      <EmptyState
        title="Could not load overview"
        body="One or more descriptive endpoints failed. Refresh, or confirm agentlog serve is running against the ledger."
      />
    );
  }

  const kpis = summary.data.kpis;
  const tokenCoverage = kpis.tokens_est.coverage;
  const tokensIn = kpis.tokens_est.totals.input_tokens ?? 0;
  const tokensOut = kpis.tokens_est.totals.output_tokens ?? 0;
  const mix = models.data.items.slice(0, 6);
  const maxShare = Math.max(...mix.map((m) => m.share), 0.001);
  const heat = heatmap.data;
  const maxHeat = Math.max(...heat.counts.flat(), 1);
  const toolItems = tools.data.items;
  const maxTool = Math.max(...toolItems.map((t) => t.count), 1);
  const kindItems = kinds.data.items.slice(0, 8);
  const maxKind = Math.max(...kindItems.map((k) => k.count), 1);
  const durationBuckets = dist.data.duration_buckets;
  const maxDurBucket = Math.max(...durationBuckets.map((b) => b.count), 1);

  /* Daily totals for the hero sparkline + harness mix from the same series.
     `total` is a server-provided sibling of the per-harness keys, so summing
     every numeric key would count each day twice. */
  const series = timeseries.data.series;
  const dailyTotals = series.map((row) =>
    typeof row.total === "number" ? row.total : 0,
  );
  /* Harness columns come from the payload so a newly ingested harness cannot
     silently vanish from the mix or the stacked chart. */
  const harnessKeys = (() => {
    const seen = new Set<string>();
    for (const row of series) {
      for (const k of Object.keys(row)) {
        if (k !== "day" && k !== "total") seen.add(k);
      }
    }
    const known = HARNESSES.filter((h) => seen.has(h));
    const extra = [...seen].filter((k) => !HARNESSES.includes(k as never)).sort();
    return [...known, ...extra];
  })();
  const harnessTotals = harnessKeys
    .map((h) => ({
      harness: h,
      count: series.reduce(
        (acc, row) => acc + (typeof row[h] === "number" ? (row[h] as number) : 0),
        0,
      ),
    }))
    .filter((h) => h.count > 0);
  const maxHarness = Math.max(...harnessTotals.map((h) => h.count), 1);

  const formatDayTick = dayTickFormatter(series);

  const linkWith = (extra: Record<string, string>) => {
    const p = new URLSearchParams(params);
    for (const [k, v] of Object.entries(extra)) p.set(k, v);
    return { pathname: "/sessions", search: p.toString() };
  };

  /* Attention keeps durable/abandoned signals; live_* rows resolve through the
     same presence payload as ACTIVE NOW so the two panels cannot disagree. */
  const liveByKey = (() => {
    const map = new Map<string, LiveSession>();
    for (const s of presence.sessions) {
      map.set(sessionPresenceKey(s), s);
      if (s.session_id) map.set(s.session_id, s);
      map.set(`${s.harness}:${s.external_id}`, s);
    }
    return map;
  })();
  const liveFor = (sessionId: string | null | undefined) =>
    sessionId ? liveByKey.get(sessionId) : undefined;
  const urgentAll = (attention.data?.items ?? []).filter((item) => {
    if (!String(item.state).startsWith("live_")) return true;
    const live = liveFor(item.session_id);
    // Presence owns "happening now": drop stale live_waiting once the agent
    // is working again, and drop live rows already visible on the rail.
    if (!live) return true;
    if (live.working || live.state !== "waiting") return false;
    return true;
  });
  const urgentItems = urgentAll.slice(0, 4);
  const urgentHidden = urgentAll.length - urgentItems.length;
  const resumableAll = attention.data?.resumable ?? [];
  const resumableItems = resumableAll.slice(0, 3);
  const resumableHidden = resumableAll.length - resumableItems.length;
  const attentionQuiet =
    urgentItems.length === 0 && resumableItems.length === 0;

  return (
    <div className="space-y-3">
      {/* Observatory: telemetry L · graph stage · telemetry R (§5). */}
      <div className="grid grid-cols-1 gap-3 min-[900px]:grid-cols-[260px_minmax(0,1fr)] min-[1200px]:grid-cols-[260px_minmax(520px,1fr)_300px]">
        {/* ── LEFT TELEMETRY ── */}
        <aside className="min-w-0 space-y-3">
          <ColumnLabel>Telemetry</ColumnLabel>
          <KpiTile
            title="Sessions"
            value={kpis.sessions.value}
            hero
            delta={kpis.sessions.delta_ratio}
            sub={`${kpis.streak.current_days}d streak · longest ${kpis.streak.longest_days}`}
            spark={dailyTotals}
          />
          <KpiTile
            title="Messages"
            value={kpis.messages.value}
            delta={kpis.messages.delta_ratio}
          />
          <KpiTile
            title="Tool calls"
            value={kpis.tool_events.value}
            delta={kpis.tool_events.delta_ratio}
            sub={`${tools.data.distinct_tools} distinct tools`}
          />
          <KpiTile
            title="Windows"
            value={kpis.windows.value}
            delta={kpis.windows.delta_ratio}
            sub={`${kpis.auto_reviews.value.toLocaleString()} auto-reviews`}
          />
          {/* Full width: the coverage denominator must never be clipped. */}
          <div className="space-y-2">
            {tokensIn > 0 || tokensOut > 0 ? (
              <KpiPartial
                title="Tokens"
                valueLabel={formatCount(tokensIn)}
                suffix="in"
                sub={`${formatCount(tokensOut)} out · usage on ${tokenCoverage.sessions_with_usage.toLocaleString()}/${tokenCoverage.sessions_total.toLocaleString()} sessions`}
                note={kpis.tokens_est.note}
              />
            ) : (
              <KpiUnavailable
                title="Tokens"
                reason={kpis.tokens_est.note}
                caption={`no usage reported by any of ${tokenCoverage.sessions_total.toLocaleString()} sessions in range`}
                compact
              />
            )}
            {kpis.cost_est.status === "estimated" && kpis.cost_est.usd != null ? (
              <KpiPartial
                title="Cost"
                valueLabel={`$${kpis.cost_est.usd.toFixed(2)}`}
                sub="estimated from native token usage"
                note={kpis.cost_est.message ?? undefined}
                compact
              />
            ) : (
              <KpiUnavailable
                title="Cost"
                reason={kpis.cost_est.message ?? "No cost estimate available."}
                caption={
                  kpis.cost_est.message?.split(";")[0] ?? "no cost estimate"
                }
                compact
              />
            )}
          </div>

          <div>
            <ColumnLabel>Harness mix</ColumnLabel>
            <div className="mt-2 space-y-1.5">
              {harnessTotals.map((h) => (
                <Link
                  key={h.harness}
                  to={linkWith({ harness: h.harness })}
                  className="flex items-center gap-2 text-[11px]"
                >
                  <HarnessTag harness={h.harness} className="w-16 shrink-0" />
                  <Meter ratio={h.count / maxHarness} color={harnessColor(h.harness)} />
                  <span className="tabular w-10 shrink-0 text-right font-mono text-faint-foreground">
                    {h.count.toLocaleString()}
                  </span>
                </Link>
              ))}
            </div>
          </div>

          <div>
            <ColumnLabel>Request kinds</ColumnLabel>
            {kindItems.length === 0 ? (
              <div className="mt-2">
                <EmptyState
                  title="Classifications pending"
                  body="Deterministic request-kind labels over exchange windows populate here after the classification pass runs on ingested windows."
                  missing={["window_det_classifications"]}
                  className="min-h-[90px]"
                />
              </div>
            ) : (
              <div className="mt-2 space-y-1.5">
                {kindItems.map((k) => (
                  <div key={k.request_kind} className="flex items-center gap-2 text-[11px]">
                    <div className="w-24 shrink-0 truncate font-mono text-muted-foreground">
                      {k.request_kind}
                    </div>
                    <Meter ratio={k.count / maxKind} color="var(--muted-foreground)" />
                    <div className="tabular w-8 shrink-0 text-right text-faint-foreground">
                      {k.count}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </aside>

        {/* ── GRAPH STAGE (the view's one glow center) ── */}
        <section className="min-h-[560px] min-w-0">
          {wideEnoughForGraph ? (
            <div className="h-[560px]">
              <ConstellationGraph
                data={graph.data}
                range={range}
                streamConnected={connected}
                focusId={focusId}
                presence={presence}
              />
            </div>
          ) : (
            <Card className="flex h-[240px] flex-col items-center justify-center gap-2">
              <div className="microlabel font-mono text-faint-foreground">
                graph stage
              </div>
              <div className="text-[12px] text-muted-foreground">
                The constellation graph needs a wider viewport.
              </div>
              <div className="font-mono text-[11px] text-faint-foreground">
                {graph.data.counts.sessions.toLocaleString()} sessions ·{" "}
                {graph.data.counts.repos} repos
              </div>
            </Card>
          )}
        </section>

        {/* ── RIGHT TELEMETRY ── */}
        <aside className="min-w-0 space-y-4 min-[900px]:col-span-2 min-[1200px]:col-span-1">
          <div>
            <ColumnLabel>Attention</ColumnLabel>
            {attentionQuiet ? (
              <div
                className={
                  "mt-2 font-mono text-[11px] text-faint-foreground " +
                  (attnFlash.has("__cleared__") ? "live-glow" : "")
                }
              >
                nothing needs you
              </div>
            ) : (
              <div className="mt-2 space-y-2.5">
                {urgentItems.length > 0 ? (
                  <div className="space-y-1.5">
                    {urgentItems.map((item) => {
                      const key = `${item.session_id}:${item.state}`;
                      return (
                        <AttentionRow
                          key={`${item.session_id}-${item.state}`}
                          item={item}
                          search={params.toString()}
                          tone={item.severity === "warn" ? "warn" : "info"}
                          flash={attnFlash.has(key)}
                          live={liveFor(item.session_id)}
                        />
                      );
                    })}
                    {urgentHidden > 0 ? (
                      <div className="pl-[12px] font-mono text-[10px] text-faint-foreground">
                        +{urgentHidden} more
                      </div>
                    ) : null}
                  </div>
                ) : null}
                {resumableItems.length > 0 ? (
                  <div>
                    <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.08em] text-faint-foreground">
                      resumable
                    </div>
                    <div className="space-y-1.5">
                      {resumableItems.map((item) => {
                        const key = `${item.session_id}:${item.state}`;
                        return (
                          <AttentionRow
                            key={`${item.session_id}-${item.state}`}
                            item={item}
                            search={params.toString()}
                            tone="neutral"
                            quiet
                            flash={attnFlash.has(key)}
                            live={liveFor(item.session_id)}
                          />
                        );
                      })}
                      {resumableHidden > 0 ? (
                        <div className="pl-[12px] font-mono text-[10px] text-faint-foreground">
                          +{resumableHidden} more
                        </div>
                      ) : null}
                    </div>
                  </div>
                ) : null}
              </div>
            )}
          </div>

          <div>
            <div className="flex items-baseline justify-between border-b border-border-faint pb-1.5">
              <span className="microlabel font-mono text-[10px] text-faint-foreground">
                Model mix
              </span>
              <Link
                to={{ pathname: "/models", search: params.toString() }}
                className="text-[10px] text-faint-foreground hover:text-foreground"
              >
                all models →
              </Link>
            </div>
            <div className="mt-2 space-y-2">
              {mix.map((m) => {
                const lead = m.harnesses[0]?.harness ?? "other";
                return (
                  <Link
                    key={m.model}
                    to={linkWith({ model: m.model })}
                    className="group block"
                  >
                    <div className="mb-0.5 flex items-baseline justify-between gap-2">
                      <span className="min-w-0 truncate font-mono text-[11px] text-muted-foreground group-hover:text-foreground">
                        {m.model}
                      </span>
                      <span className="tabular shrink-0 font-mono text-[11px] text-faint-foreground">
                        {(m.share * 100).toFixed(0)}%
                      </span>
                    </div>
                    <div className="h-[5px] overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full rounded-full"
                        style={{
                          width: `${(m.share / maxShare) * 100}%`,
                          background: `linear-gradient(180deg, ${harnessColor(lead)} 32%, color-mix(in srgb, ${harnessColor(lead)} 35%, transparent))`,
                        }}
                      />
                    </div>
                    {m.harnesses.length > 1 ? (
                      <div className="mt-0.5 font-mono text-[9px] text-faint-foreground">
                        {m.harnesses
                          .map((h) => `${h.harness} ${h.sessions}`)
                          .join(" · ")}
                      </div>
                    ) : null}
                  </Link>
                );
              })}
            </div>
          </div>

          <div>
            <div className="flex items-baseline justify-between border-b border-border-faint pb-1.5">
              <span className="microlabel font-mono text-[10px] text-faint-foreground">
                Recent
              </span>
              <Link
                to={{ pathname: "/sessions", search: params.toString() }}
                className="text-[10px] text-faint-foreground hover:text-foreground"
              >
                all sessions →
              </Link>
            </div>
            <div className="mt-1.5">
              {recent.data.items.slice(0, 8).map((s) => {
                const live = liveFor(s.id);
                return (
                <Link
                  key={s.id}
                  to={`/sessions/${encodeURIComponent(s.id)}?${params.toString()}`}
                  className={
                    "group flex items-center gap-2 rounded-[4px] border-b border-border-faint px-1 py-1.5 text-[11px] last:border-0 hover:bg-muted/40 " +
                    (glowId === s.id ? "live-glow" : "")
                  }
                >
                  <span className="tabular shrink-0 font-mono text-faint-foreground">
                    {formatClock(s.started_at)}
                  </span>
                  {live ? (
                    <LiveOrb
                      state={live.state}
                      harnessColor={harnessColor(live.harness)}
                      size={16}
                      worker={live.role === "worker"}
                      title={live.activity ?? live.state}
                    />
                  ) : (
                    <span
                      aria-hidden
                      className="inline-block h-[6px] w-[6px] shrink-0 rounded-full"
                      style={{ background: harnessColor(s.harness) }}
                    />
                  )}
                  <span
                    className={
                      "min-w-0 flex-1 truncate font-mono text-[11px] group-hover:text-foreground " +
                      (live ? "text-foreground" : "text-muted-foreground")
                    }
                    title={live?.label ?? s.project}
                  >
                    {truncLabel(s.project, 22)}
                  </span>
                  <span className="tabular shrink-0 font-mono text-faint-foreground">
                    {live
                      ? (live.activity ?? "live")
                      : formatDuration(s.duration_seconds)}
                  </span>
                </Link>
                );
              })}
            </div>
          </div>
        </aside>
      </div>

      {/* ── BELOW THE FOLD — full-width, re-skinned v1 components ── */}
      <div className="grid grid-cols-5 gap-3">
        <Card className="col-span-3">
          <div className="flex items-baseline justify-between">
            <CardTitle>Sessions by harness</CardTitle>
            <div className="flex gap-3 text-[11px] text-muted-foreground">
              {harnessKeys.map((h) => (
                <Link key={h} to={linkWith({ harness: h })}>
                  <HarnessTag harness={h} />
                </Link>
              ))}
            </div>
          </div>
          <div className="mt-3 h-[210px]">
            {timeseries.data.series.length === 0 ? (
              <EmptyState
                title="No sessions in range"
                body="Widen the time range or run an ingest; daily stacked bars appear per harness."
              />
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={timeseries.data.series} barCategoryGap={2}>
                  <ChartDefs />
                  <CartesianGrid stroke="var(--border-faint)" vertical={false} />
                  <XAxis
                    dataKey="day"
                    tick={{ fill: "var(--faint-foreground)", fontSize: 10 }}
                    tickFormatter={formatDayTick}
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
                  {harnessKeys.map((h) => (
                    <Bar
                      key={h}
                      dataKey={h}
                      stackId="a"
                      fill={chartGradient(h)}
                      stroke={harnessColor(h)}
                      strokeWidth={0.75}
                      isAnimationActive={false}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
        </Card>

        <Card className="col-span-2">
          <div className="flex items-baseline justify-between">
            <CardTitle>Activity heatmap</CardTitle>
            <span className="text-[10px] text-faint-foreground">
              session starts · hour × weekday (UTC)
            </span>
          </div>
          <div className="mt-3 overflow-x-auto">
            <div
              className="grid gap-[3px]"
              style={{ gridTemplateColumns: `34px repeat(24, 13px)` }}
            >
              <div />
              {heat.hours.map((h) => (
                <div key={h} className="tabular text-center text-[9px] text-faint-foreground">
                  {h % 4 === 0 ? h.toString().padStart(2, "0") : ""}
                </div>
              ))}
              {heat.weekdays.map((day, di) => (
                <div key={day} className="contents">
                  <div className="pr-1 text-right text-[10px] leading-[13px] text-muted-foreground">
                    {day}
                  </div>
                  {heat.counts[di].map((c, hi) => (
                    <div
                      key={`${day}-${hi}`}
                      title={`${day} ${hi.toString().padStart(2, "0")}:00 — ${c} session${c === 1 ? "" : "s"}`}
                      className="h-[13px] w-[13px] rounded-[2px]"
                      style={{ background: heatColor(c / maxHeat) }}
                    />
                  ))}
                </div>
              ))}
            </div>
            <div className="mt-2 flex items-center gap-1.5 text-[9px] text-faint-foreground">
              less
              {[0, 0.25, 0.5, 0.75, 1].map((t) => (
                <span
                  key={t}
                  className="inline-block h-[9px] w-[9px] rounded-[2px]"
                  style={{ background: heatColor(t) }}
                />
              ))}
              more
            </div>
          </div>
        </Card>
      </div>

      <div className="grid grid-cols-5 gap-3">
        <Card className="col-span-2">
          <CardTitle>Top projects</CardTitle>
          <div className="mt-2.5 space-y-1.5">
            {projects.data.items.map((p) => (
              <Link
                key={p.project}
                to={linkWith({ project: p.project })}
                className="flex items-center gap-3 text-[12px] text-muted-foreground hover:text-foreground"
              >
                <span className="min-w-0 flex-1 truncate">{p.project}</span>
                <span className="tabular shrink-0 text-faint-foreground">{p.sessions}</span>
                <Sparkline
                  values={p.sparkline}
                  stroke="color-mix(in srgb, var(--accent-live) 55%, var(--muted-foreground))"
                />
              </Link>
            ))}
          </div>
          <div className="mt-4 border-t border-border-faint pt-3">
            <div className="flex items-baseline justify-between">
              <CardTitle>Duration</CardTitle>
              <span className="tabular text-[10px] text-faint-foreground">
                p50 {formatDuration(dist.data.duration_seconds.p50)} · p90{" "}
                {formatDuration(dist.data.duration_seconds.p90)}
              </span>
            </div>
            <div className="mt-2 space-y-1">
              {durationBuckets.map((b) => (
                <div key={b.bucket} className="flex items-center gap-2 text-[11px]">
                  <div className="tabular w-12 shrink-0 text-muted-foreground">
                    {b.bucket}
                  </div>
                  <Meter
                    ratio={b.count / maxDurBucket}
                    color="color-mix(in srgb, var(--accent-live) 60%, var(--muted-foreground))"
                  />
                  <div className="tabular w-8 shrink-0 text-right text-faint-foreground">
                    {b.count}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </Card>

        <Card className="col-span-3">
          <div className="flex items-baseline justify-between">
            <CardTitle>Tool usage</CardTitle>
            <span className="tabular text-[11px] text-muted-foreground">
              {formatCount(tools.data.total)} calls
            </span>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1.5">
            {toolItems.map((t) => (
              <div key={t.tool} className="flex items-center gap-2 text-[11px]">
                <div className="w-28 shrink-0 truncate font-mono text-muted-foreground">
                  {t.tool}
                </div>
                <Meter
                  ratio={t.count / maxTool}
                  color="color-mix(in srgb, var(--status-info) 55%, var(--muted-foreground))"
                />
                <div className="tabular w-12 shrink-0 text-right text-faint-foreground">
                  {t.count.toLocaleString()}
                </div>
              </div>
            ))}
          </div>
        </Card>
      </div>

      <div className="text-[11px] text-faint-foreground">
        Last event {formatDayTime(recent.data.items[0]?.started_at)} · range{" "}
        {range}
      </div>
    </div>
  );
}
