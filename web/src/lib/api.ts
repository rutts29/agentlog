export type RangeKey = "7d" | "30d" | "90d" | "all" | "custom";

export type AggregateCell = {
  status: "ok" | "abstain" | "unavailable";
  reason: string | null;
  message: string | null;
  metric: string | null;
  kind: string | null;
  estimate: number | null;
  interval: { low: number | null; high: number | null; method: string | null };
  n_clusters: number;
  n_events: number;
  availability: number | null;
  evidence_tier: "very_low" | "low" | "adequate" | null;
  flags: string[];
  session_ids: string[];
  bound_version: string;
};

async function getJson<T>(path: string, params?: Record<string, string | undefined>): Promise<T> {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v != null && v !== "") url.searchParams.set(k, v);
    }
  }
  const res = await fetch(url.toString());
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export function fetchMeta() {
  return getJson<{
    freshness: { sessions: number; last_at: string | null };
  }>("/api/meta");
}

export function fetchSummary(range: string) {
  return getJson<{
    range: string;
    kpis: {
      sessions: {
        value: number;
        previous: number | null;
        delta_ratio: number | null;
        label: string;
      };
      tokens_est: { status: string; message: string };
      cost_est: { status: string; message: string };
      interaction_style: AggregateCell;
      streak: {
        current_days: number;
        longest_days: number;
        label: string;
        note: string;
      };
    };
    flags: string[];
  }>("/api/summary", { range });
}

export function fetchTimeseries(range: string) {
  return getJson<{
    series: Array<Record<string, string | number>>;
    note: string;
  }>("/api/timeseries/sessions", { range, by: "harness" });
}

export function fetchModelMix(range: string) {
  return getJson<{
    title: string;
    subtitle: string;
    items: Array<{
      model: string;
      harness: string;
      sessions: number;
      share: number;
      interaction_style: AggregateCell;
      flags: string[];
    }>;
  }>("/api/models", { range });
}

export function fetchHeatmap(range: string) {
  return getJson<{
    weekdays: string[];
    hours: number[];
    counts: number[][];
    note: string;
  }>("/api/heatmap", { range });
}

export function fetchProjects(range: string) {
  return getJson<{
    items: Array<{ project: string; sessions: number; sparkline: number[] }>;
  }>("/api/projects", { range });
}

export function fetchRecent(range: string) {
  return getJson<{
    items: Array<{
      id: string;
      harness: string;
      model: string;
      project: string;
      started_at: string | null;
      duration_seconds: number | null;
      message_count: number;
      tool_count: number;
      tokens: number | null;
      status: string;
    }>;
  }>("/api/sessions/recent", { range });
}

export function fetchSessions(
  range: string,
  filters?: { model?: string; harness?: string; q?: string },
) {
  return getJson<{
    total: number;
    items: Array<{
      id: string;
      harness: string;
      model: string;
      project: string;
      started_at: string | null;
      message_count: number;
      tool_count: number;
      status: string;
    }>;
  }>("/api/sessions", {
    range,
    model: filters?.model,
    harness: filters?.harness,
    q: filters?.q,
  });
}

export function fetchSkills(range: string) {
  return getJson<{
    activations: number;
    distinct_fired: number;
    items: unknown[];
    note: string;
  }>("/api/skills", { range });
}

export function fetchInsights(range: string) {
  return getJson<{
    items: unknown[];
    empty: { title: string; body: string; missing: string[] };
  }>("/api/insights", { range });
}
