export type RangeKey = "7d" | "30d" | "90d" | "all" | "custom";

declare global {
  interface Window {
    /** Injected into served SPA HTML; Vite proxy supplies the header instead. */
    __AGENTLOG_TOKEN__?: string;
  }
}

function apiAuthHeaders(): HeadersInit {
  const token = window.__AGENTLOG_TOKEN__;
  if (!token) return {};
  return { Authorization: `Bearer ${token}` };
}

/** EventSource cannot set headers; append ?token= when the SPA carries one. */
export function withApiToken(path: string): string {
  const token = window.__AGENTLOG_TOKEN__;
  if (!token) return path;
  const url = new URL(path, window.location.origin);
  url.searchParams.set("token", token);
  return `${url.pathname}${url.search}${url.hash}`;
}

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

export type SessionRow = {
  id: string;
  harness: string;
  model: string;
  effort: string | null;
  project: string;
  repo?: string | null;
  branch: string | null;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  message_count: number;
  tool_count: number;
  window_count?: number;
  parent_session_id?: string | null;
  status: string;
};

async function getJson<T>(
  path: string,
  params?: Record<string, string | string[] | undefined>,
): Promise<T> {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v == null || v === "") continue;
      if (Array.isArray(v)) {
        for (const item of v) url.searchParams.append(k, item);
      } else {
        url.searchParams.set(k, v);
      }
    }
  }
  const res = await fetch(url.toString(), { headers: apiAuthHeaders() });
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

export type CountKpi = {
  value: number;
  previous: number | null;
  delta_ratio: number | null;
  label: string;
};

export type CostEstimate = {
  status: "estimated" | "unavailable" | string;
  pricing_table_version?: string;
  as_of?: string;
  message: string | null;
  usd: number | null;
};

export type TokenTotals = {
  input_tokens: number | null;
  output_tokens: number | null;
  cache_creation_input_tokens: number | null;
  cache_read_input_tokens: number | null;
  cached_input_tokens: number | null;
  cache_write_input_tokens: number | null;
  reasoning_output_tokens: number | null;
  total_tokens: number | null;
  fields_present: Record<string, boolean>;
};

export type TokenCoverage = {
  sessions_total: number;
  sessions_with_usage: number;
  sessions_coverage: number | null;
  messages_total: number;
  messages_with_usage: number;
  messages_coverage: number | null;
  by_harness: Array<{
    harness: string;
    sessions: number;
    sessions_with_usage: number;
    messages: number;
    messages_with_usage: number;
    message_usage_rows: number;
    turn_usage_rows: number;
    cumulative_usage_rows: number;
  }>;
};

/** Corpus token totals with an explicit coverage denominator. */
export type TokensEstimate = {
  totals: TokenTotals;
  cache_ratios: Record<string, number | null>;
  coverage: TokenCoverage;
  note: string;
  cost: CostEstimate;
};

export function fetchSummary(range: string) {
  return getJson<{
    range: string;
    kpis: {
      sessions: CountKpi;
      messages: CountKpi;
      tool_events: CountKpi;
      windows: CountKpi;
      auto_reviews: CountKpi;
      tokens_est: TokensEstimate;
      cost_est: CostEstimate;
      interaction_style: AggregateCell;
      streak: {
        current_days: number;
        longest_days: number;
        label: string;
        note: string;
      };
    };
    ledger: Record<string, number>;
    flags: string[];
  }>("/api/summary", { range });
}

export function fetchTimeseries(range: string, by: "harness" | "model" = "harness") {
  return getJson<{
    series: Array<Record<string, string | number>>;
    note: string;
    by: string;
  }>("/api/timeseries/sessions", { range, by });
}

/** Per-harness split of a single model row — never its own chart row. */
export type HarnessSplit = { harness: string; sessions: number };

export function fetchModelMix(range: string) {
  return getJson<{
    title: string;
    subtitle: string;
    items: Array<{
      model: string;
      harnesses: HarnessSplit[];
      messages: number;
      sessions: number;
      share: number;
      interaction_style: AggregateCell;
      flags: string[];
    }>;
    unknown: {
      label: string;
      messages: number;
      sessions: number;
      reasons: Array<{
        reason: string;
        description: string;
        messages: number;
        sessions: number;
        raw_values: Array<{ value: string; sessions: number }>;
      }>;
    };
    unknown_note: string;
    profiles: Array<{
      agent_profile: string;
      harnesses: HarnessSplit[];
      sessions: number;
      share: number;
    }>;
    profiles_note: string;
  }>("/api/models", { range });
}

export function fetchModelMonthly(range: string) {
  return getJson<{
    series: Array<{
      month: string;
      total: number;
      items: Array<{
        model: string;
        harnesses: HarnessSplit[];
        sessions: number;
        share: number;
      }>;
    }>;
    note: string;
  }>("/api/models/monthly", { range });
}

export function fetchTools(range: string, limit = 30) {
  return getJson<{
    total: number;
    distinct_tools: number;
    items: Array<{
      tool: string;
      count: number;
      share: number;
      by_harness: Record<string, number>;
    }>;
    note: string;
  }>("/api/tools", { range, limit: String(limit) });
}

export function fetchRequestKinds(range: string) {
  return getJson<{
    total: number;
    items: Array<{ request_kind: string; count: number; share: number }>;
    orchestration_signals: Record<string, number>;
    note: string;
  }>("/api/request-kinds", { range });
}

export function fetchDistributions(range: string) {
  return getJson<{
    sessions: number;
    with_duration: number;
    duration_seconds: {
      p50: number | null;
      p90: number | null;
      p99: number | null;
      max: number | null;
    };
    duration_buckets: Array<{ bucket: string; count: number }>;
    message_buckets: Array<{ bucket: string; count: number }>;
    note: string;
  }>("/api/distributions", { range });
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

export function fetchRecent(range: string, limit?: number) {
  return getJson<{
    items: SessionRow[];
  }>("/api/sessions/recent", {
    range,
    limit: limit != null ? String(limit) : undefined,
  });
}

export function fetchFacets(range: string) {
  return getJson<{
    harness: Array<{ value: string; count: number }>;
    model: Array<{ value: string; count: number }>;
    effort: Array<{ value: string; count: number }>;
    branch: Array<{ value: string; count: number }>;
    project: Array<{ value: string; count: number }>;
  }>("/api/facets", { range });
}

export function fetchSessions(
  range: string,
  filters?: {
    model?: string | string[];
    harness?: string | string[];
    effort?: string | string[];
    branch?: string | string[];
    project?: string | string[];
    q?: string;
    sort?: string;
    order?: string;
    cursor?: number;
    limit?: number;
  },
) {
  return getJson<{
    total: number;
    cursor: number;
    next_cursor: number | null;
    sort: string;
    order: string;
    items: SessionRow[];
  }>("/api/sessions", {
    range,
    model: filters?.model,
    harness: filters?.harness,
    effort: filters?.effort,
    branch: filters?.branch,
    project: filters?.project,
    q: filters?.q,
    sort: filters?.sort,
    order: filters?.order,
    cursor: filters?.cursor != null ? String(filters.cursor) : undefined,
    limit: filters?.limit != null ? String(filters.limit) : undefined,
  });
}

export type ToolEvent = {
  id: string;
  message_id: string | null;
  seq: number | null;
  tool_name: string;
  action: string | null;
  success: number | null;
  duration_ms: number | null;
};

export type TimelineMessage = {
  kind: "message";
  id: string;
  seq: number | null;
  role: string;
  timestamp: string | null;
  model: string | null;
  effort: string | null;
  text: string;
  is_tool_plumbing: boolean;
  request_kind?: string | null;
  skills?: string[];
  tool_events: ToolEvent[];
};

export type TimelineOrphanTool = {
  kind: "tool";
  id: string;
  seq: number | null;
  tool_name: string;
  action: string | null;
  success: number | null;
  duration_ms: number | null;
  message_id: null;
};

export type TimelineItem = TimelineMessage | TimelineOrphanTool;

export function fetchSessionDetail(sessionId: string) {
  return getJson<{
    session: {
      id: string;
      harness: string;
      model: string | null;
      effort: string | null;
      project: string;
      repo: string | null;
      cwd: string | null;
      branch: string | null;
      commit_sha: string | null;
      started_at: string | null;
      ended_at: string | null;
      duration_seconds: number | null;
      parent_session_id: string | null;
      artifact_id: number | null;
      artifact_path: string | null;
      external_id: string;
    };
    timeline: TimelineItem[];
    skills: Array<{ skill_name: string; exposure_type: string; c: number }>;
    children: Array<{
      id: string;
      harness: string;
      model: string | null;
      effort: string | null;
      started_at: string | null;
      message_count: number;
    }>;
    anatomy: {
      message_count: number;
      tool_count: number;
      window_count: number;
      child_count: number;
    };
  }>(`/api/sessions/${encodeURIComponent(sessionId)}`);
}

export function fetchSearch(
  range: string,
  q: string,
  filters?: {
    harness?: string | string[];
    model?: string | string[];
    project?: string | string[];
    cursor?: number;
    limit?: number;
  },
) {
  return getJson<{
    q: string;
    total: number;
    next_cursor: number | null;
    items: Array<{
      message_id: string;
      session_id: string;
      seq: number;
      role: string;
      timestamp: string | null;
      snippet: string;
      harness: string;
      model: string;
      effort: string | null;
      project: string;
      started_at: string | null;
    }>;
    note: string;
    truncated?: boolean;
  }>("/api/search", {
    range,
    q,
    harness: filters?.harness,
    model: filters?.model,
    project: filters?.project,
    cursor: filters?.cursor != null ? String(filters.cursor) : undefined,
    limit: filters?.limit != null ? String(filters.limit) : undefined,
  });
}

export function fetchOrchestration(range: string) {
  return getJson<{
    supervisor_roots: number;
    child_sessions: number;
    signals: Record<string, number>;
    items: Array<{
      id: string;
      harness: string;
      model: string;
      effort: string | null;
      project: string;
      started_at: string | null;
      child_count: number;
      message_count: number;
    }>;
    note: string;
  }>("/api/orchestration", { range });
}

export function fetchSessionTree(sessionId: string) {
  return getJson<{
    root_id: string;
    requested_id: string;
    tree: TreeNode;
    note: string;
  }>(`/api/sessions/${encodeURIComponent(sessionId)}/tree`);
}

export type TreeNode = {
  id: string;
  harness: string;
  model: string;
  effort: string | null;
  project: string;
  started_at: string | null;
  ended_at: string | null;
  message_count: number;
  tool_count: number;
  children: TreeNode[];
};

export function fetchAutoReview(range: string) {
  return getJson<{
    total: number;
    by_model: Array<{
      model: string;
      harnesses: Array<{ harness: string; count: number }>;
      count: number;
    }>;
    by_day: Array<{ day: string; count: number }>;
    items: Array<{
      id: string;
      window_id: string;
      session_id: string;
      harness: string;
      model: string;
      effort: string | null;
      project: string;
      started_at: string | null;
      created_at: string | null;
      status: string | null;
      route: string | null;
      request_kind: string;
    }>;
    note: string;
  }>("/api/auto-review", { range });
}

export function fetchSkills(range: string) {
  return getJson<{
    activations: number;
    distinct_fired: number;
    items: Array<{
      skill: string;
      fires: number;
      sessions: number;
      last_fired: string | null;
      sparkline?: number[];
    }>;
    note: string;
  }>("/api/skills", { range });
}

export type GraphSessionNode = {
  id: string;
  kind: "session";
  harness: string;
  model: string | null;
  repo: string;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  messages: number;
  tools: number;
  parent_id: string | null;
  children: number;
};

export type GraphRepoNode = {
  id: string;
  kind: "repo";
  label: string;
  sessions: number;
  harnesses: Array<{ harness: string; sessions: number }>;
  models: Array<{ model: string; messages: number }>;
  efforts: Array<{ effort: string; messages: number }>;
  first_at: string | null;
  last_at: string | null;
  messages: number;
  tools: number;
};

export type GraphNodePayload = GraphSessionNode | GraphRepoNode;

export type GraphEdgePayload = {
  source: string;
  target: string;
  kind: "orchestration" | "membership";
  harness?: string | null;
};

export type GraphPayload = {
  nodes: GraphNodePayload[];
  edges: GraphEdgePayload[];
  counts: { sessions: number; repos: number; orchestration_edges: number };
  truncated: { shown: number; hidden: number; note: string } | null;
  note: string;
};

export function fetchGraph(range: string) {
  return getJson<GraphPayload>("/api/graph", { range });
}

export type PresenceState =
  | "streaming"
  | "tool_running"
  | "thinking"
  | "orchestrating"
  | "waiting"
  | "unknown";

export type LiveSession = {
  harness: string;
  harness_display?: string;
  external_id: string;
  session_id: string | null;
  source_path: string;
  state: PresenceState | string;
  last_activity_at: string | null;
  age_seconds: number;
  pending_ingest: boolean;
  title: string | null;
  repo: string | null;
  parent_session_id?: string | null;
  parent_external_id?: string | null;
  /** "session" = a chat with a human; "worker" = background subagent. */
  role?: "session" | "worker" | string;
  /** Display name: current step, else derived task, else project. */
  label?: string;
  task?: string | null;
  step?: string | null;
  tool?: string | null;
  /** Human phrase for what is happening right now. */
  activity?: string;
  project?: string | null;
  working?: boolean;
  worker_count?: number;
  /** Seconds since the harness last flushed, when it is mid-turn and silent. */
  observed_gap_seconds?: number;
};

export type LivePayload = {
  ts: string | null;
  generation: number;
  active_seconds: number;
  working_grace_seconds?: number;
  path: string;
  watcher?: {
    presence_ts: string | null;
    age_seconds: number | null;
    fresh: boolean;
  };
  counts?: {
    total: number;
    sessions: number;
    workers: number;
    working: number;
  };
  took_ms?: number;
  sessions: LiveSession[];
};

export type PresenceEvent = {
  ts: string | null;
  generation: number;
  sessions: LiveSession[];
  transitions: Array<{ action: "active" | "idle" | string; key: string }>;
};

export type HealthPayload = {
  ok: boolean;
  db?: string;
  degraded?: boolean;
  reason?: string | null;
  watcher?: {
    alive: boolean;
    presence_path?: string;
    presence_exists?: boolean;
    presence_ts?: string | null;
    presence_age_seconds?: number | null;
    presence_fresh?: boolean;
    stale_after_seconds?: number;
  };
  last_ingest_at?: string | null;
};

export function fetchLive() {
  return getJson<LivePayload>("/api/live");
}

export function fetchHealth() {
  return getJson<HealthPayload>("/api/health");
}

export type AttentionItem = {
  session_id: string;
  state: string;
  severity: string;
  reason: string;
  last_activity_at: string | null;
  harness: string | null;
  lane?: "urgent" | "resumable" | string;
  repo?: string | null;
  branch?: string | null;
};

export type AttentionPayload = {
  generated_at: string;
  count: number;
  items: AttentionItem[];
  resumable_count?: number;
  resumable?: AttentionItem[];
  stats?: Record<string, number>;
  thresholds?: Record<string, number>;
};

export function fetchAttention() {
  return getJson<AttentionPayload>("/api/attention");
}

export type IngestEvent = {
  id: number;
  ts: string;
  harness: string;
  sessions_added: number;
  sessions_updated: number;
  messages_added: number;
};

export type InsightCard = {
  id: string;
  kind: "fact" | "coach" | "usage";
  title: string;
  body: string;
  confidence: "ok" | "insufficient" | "abstain";
  sample_size: number;
  does_not_prove: string;
  theme: string | null;
  source: "claim" | "proposal";
  source_id: string;
  origin: "session" | "corpus" | "proposal";
  suggested_instruction?: string | null;
  href?: string | null;
};

export type InsightsPayload = {
  items: InsightCard[];
  empty: { title: string; body: string; missing: string[] };
};

export function fetchInsights(range: string) {
  return getJson<InsightsPayload>("/api/insights", { range });
}

export type AdjudicationLabels = {
  turn_kind: string[];
  user_stance: string | null;
  agent_stance: string | null;
  prior_outcome: string | null;
  notes?: string;
  source?: string;
  adjudicated_at?: string;
  triage?: "yes" | "no" | "unclear" | null;
};

export type TaxonomyOption = {
  value: string;
  label: string;
  key: string;
};

export type AdjudicationTurn = {
  id: string;
  role: string;
  slot: string;
  text: string;
  model?: string | null;
  authored_by_agent?: boolean;
  is_tool_plumbing?: boolean;
};

export type AdjudicationQueueItem = {
  window_id: string;
  index: number;
  position: number;
  harness: string | null;
  session_id: string | null;
  payload: {
    window_id?: string;
    session_id?: string | null;
    harness?: string | null;
    model?: string | null;
    turns?: AdjudicationTurn[];
    user?: string;
    assistant?: string;
    next_user?: string;
  };
  llm: AdjudicationLabels | null;
  adjudication: AdjudicationLabels | null;
  adjudicated: boolean;
};

export type AdjudicationRate = {
  matches?: number;
  n?: number;
  tp?: number;
  denominator?: number;
  rate: number | null;
};

export type AdjudicationReport = {
  adjudicated: number;
  with_llm?: number;
  total_queue: number;
  min_required: number;
  insufficient_data: boolean;
  fields?: Record<
    string,
    {
      exact_match: AdjudicationRate;
      llm_precision: Record<string, AdjudicationRate>;
      confusion_pairs: Array<{ human: string; llm: string; count: number }>;
    }
  >;
};

export type AdjudicationQueueResponse = {
  items: AdjudicationQueueItem[];
  progress: { done: number; total: number; remaining?: number };
  audit_pack: string;
  original_pack_eligibility?: {
    total: number;
    eligible: number;
    ineligible: number;
    ineligible_rate: number | null;
    reasons: Record<string, number>;
    min_human_chars: number;
  };
};

export function fetchAdjudicationTaxonomy() {
  return getJson<{
    human_present: TaxonomyOption[];
    turn_kind: TaxonomyOption[];
    user_stance: TaxonomyOption[];
    agent_stance: TaxonomyOption[];
    prior_outcome: TaxonomyOption[];
    vague_key: string;
  }>("/api/adjudication/taxonomy");
}

export function fetchAdjudicationQueue(rebuild = false) {
  return getJson<AdjudicationQueueResponse>("/api/adjudication/queue", {
    rebuild: rebuild ? "true" : undefined,
  });
}

export function fetchAdjudicationReport() {
  return getJson<AdjudicationReport>("/api/adjudication/report");
}

export async function postAdjudication(
  windowId: string,
  body: {
    turn_kind: string[];
    user_stance: string | null;
    agent_stance: string | null;
    prior_outcome: string | null;
    notes?: string;
    source?: string;
    triage?: "yes" | "no" | "unclear" | null;
    vague_fields?: string[];
  },
) {
  const res = await fetch(
    `/api/adjudication/${encodeURIComponent(windowId)}`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...apiAuthHeaders(),
      },
      body: JSON.stringify(body),
    },
  );
  if (!res.ok) {
    const detail = await res.text();
    let message = detail;
    try {
      const parsed = JSON.parse(detail) as { detail?: unknown };
      if (typeof parsed.detail === "string") message = parsed.detail;
    } catch {
      /* keep raw body */
    }
    throw new Error(`${res.status} ${res.statusText}: ${message}`);
  }
  return res.json() as Promise<
    AdjudicationLabels & { window_id: string; llm?: AdjudicationLabels | null }
  >;
}

export type ProposalEvidence = {
  session_id: string | null;
  window_id: string | null;
  message_id: string | null;
  quote: string | null;
  timestamp: string | null;
  harness: string | null;
};

export type ProposalClaim = {
  id: string;
  kind: string;
  subject: string;
  predicate: string;
  derivation: string;
  support_status: string;
  sample_size: number;
  denominator: number | null;
  rate: number | null;
  observed_at: string;
  does_not_prove: string;
  evidence: ProposalEvidence[];
};

export type ProposalSupport = {
  tier: "ok" | "insufficient" | "abstain" | "unsupported";
  derivations: string[];
  sample_size: number;
  denominator: number | null;
  evidence_count: number;
  language: string;
};

export type ProposalTargetState = {
  exists: boolean;
  current_content_hash: string | null;
  matches_proposed: boolean;
  changed_since_proposal: boolean;
};

export type ProposalDecision = "accepted" | "rejected" | "deferred";
export type ProposalStatus = "pending" | ProposalDecision;

export type ProposalRow = {
  id: string;
  title: string;
  action: string;
  status: ProposalStatus;
  target_path: string;
  target_kind: string;
  scope_type: string;
  scope_id: string | null;
  unified_diff: string;
  proposed_content: string | null;
  rationale: string;
  derivation_summary: string;
  does_not_prove: string;
  sample_size: number;
  created_at: string;
  updated_at: string;
  decided_at: string | null;
  decision_note: string | null;
  claims: ProposalClaim[];
  support: ProposalSupport;
  target_state: ProposalTargetState;
  advisory_only: boolean;
};

export type ProposalsPayload = {
  items: ProposalRow[];
  count: number;
  counts_by_status: Record<string, number>;
  decisions: ProposalDecision[];
  advisory_only: boolean;
  note: string;
};

export function fetchProposals(status?: string) {
  return getJson<ProposalsPayload>("/api/proposals", {
    status: status && status !== "all" ? status : undefined,
  });
}

export async function postProposalDecision(
  proposalId: string,
  decision: ProposalDecision | "pending",
  note?: string,
) {
  const res = await fetch(
    `/api/proposals/${encodeURIComponent(proposalId)}/decision`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...apiAuthHeaders(),
      },
      body: JSON.stringify({ decision, note: note ?? null }),
    },
  );
  if (!res.ok) {
    const detail = await res.text();
    let message = detail;
    try {
      const parsed = JSON.parse(detail) as { detail?: unknown };
      if (typeof parsed.detail === "string") message = parsed.detail;
    } catch {
      /* keep raw body */
    }
    throw new Error(`${res.status} ${res.statusText}: ${message}`);
  }
  return res.json() as Promise<ProposalRow>;
}
