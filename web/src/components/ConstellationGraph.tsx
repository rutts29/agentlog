import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import ForceGraph2D, {
  type ForceGraphMethods,
  type LinkObject,
  type NodeObject,
} from "react-force-graph-2d";
import { forceCollide, forceX, forceY } from "d3-force-3d";
import type {
  GraphPayload,
  GraphSessionNode,
  LiveSession,
  PresenceState,
} from "@/lib/api";
import { logicalHarness, runtimeHarness } from "@/lib/api";
import {
  sessionPresenceKey,
  type PresenceSnapshot,
} from "@/lib/useLivePresence";
import { prefersReducedMotion } from "@/lib/useCountUp";
import {
  formatCount,
  formatDay,
  formatDayTime,
  formatDuration,
  shortModel,
  truncLabel,
} from "@/lib/utils";
import { LiveOrb, orbIsWorking } from "@/components/LiveOrb";

/* ── data shapes ──────────────────────────────────────────────────────── */

type GNode = NodeObject & {
  id: string;
  kind: "session" | "repo";
  harness?: string;
  logical_harness?: string | null;
  runtime_harness?: string | null;
  model?: string | null;
  repo?: string;
  started_at?: string | null;
  ended_at?: string | null;
  duration_seconds?: number | null;
  messages?: number;
  tools?: number;
  children?: number;
  parent_id?: string | null;
  label?: string;
  sessions?: number;
  harnesses?: Array<{ harness: string; sessions: number }>;
  models?: Array<{ model: string; messages: number }>;
  efforts?: Array<{ effort: string; messages: number }>;
  first_at?: string | null;
  last_at?: string | null;
  r: number;
  /** 0–1 recency weight for daily-use opacity (1 = recent). */
  recency?: number;
};

type GLink = LinkObject & {
  kind: "orchestration" | "membership";
  harness?: string | null;
};

type Anim = { t0: number };

const HOVER_FADE_MS = 120;
const SPAWN_MS = 350;
const RING_MS = 600;
const EDGE_DRAW_MS = 250;
const PULSE_MS = 1200;
/** §2.5 settle on genuine arrivals: hold a low alpha floor, then decay. */
const SETTLE_ALPHA = 0.12;
const SETTLE_MS = 800;
/** Sessions older than this fade toward the floor opacity. */
const RECENCY_HALF_LIFE_MS = 14 * 24 * 60 * 60 * 1000;
const RECENCY_FLOOR = 0.42;

/* Repos at or below this many sessions are periphery "minor" projects. */
const MINOR_SESSIONS = 2;

const STABLE_HARNESS = ["claude", "codex", "cursor", "warp", "t3code"] as const;

function harnessRank(h: string): number {
  const i = STABLE_HARNESS.indexOf(
    h.toLowerCase() as (typeof STABLE_HARNESS)[number],
  );
  return i >= 0 ? i : 50;
}

function sortHarnessNames(names: string[]): string[] {
  return [...names].sort((a, b) => {
    const d = harnessRank(a) - harnessRank(b);
    return d !== 0 ? d : a.localeCompare(b);
  });
}

function sortHarnessComp<T extends { harness: string }>(items: T[]): T[] {
  return [...items].sort((a, b) => {
    const d = harnessRank(a.harness) - harnessRank(b.harness);
    return d !== 0 ? d : a.harness.localeCompare(b.harness);
  });
}

function sessionRadius(messages: number): number {
  return Math.min(14, Math.max(4, 4 + 3 * Math.log10(1 + messages)));
}

/* Envelope radius for a project cluster with nested harness lobes. */
function clusterRadius(n: number, harnessCount = 1): number {
  const packed = 16 + 7 * Math.sqrt(n);
  if (harnessCount <= 1) return packed;
  const subR = harnessSubRadius(n, harnessCount);
  const lobe = 10 + 4.5 * Math.sqrt(n / harnessCount);
  return subR + lobe + 6;
}

/* Distance from project anchor to a harness sub-centroid. */
function harnessSubRadius(sessionCount: number, harnessCount: number): number {
  if (harnessCount <= 1) return 0;
  return Math.min(24, 9 + 3.2 * Math.sqrt(sessionCount / harnessCount));
}

function cssColor(name: string): string {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
}

function withAlpha(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  if (h.length !== 6) return hex;
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function linkEndId(end: string | number | NodeObject | undefined): string {
  if (end == null) return "";
  return typeof end === "object" ? String(end.id) : String(end);
}

function recencyWeight(startedAt: string | null | undefined): number {
  if (!startedAt) return RECENCY_FLOOR;
  const age = Date.now() - new Date(startedAt).getTime();
  if (!Number.isFinite(age) || age <= 0) return 1;
  const w = Math.exp(-age / RECENCY_HALF_LIFE_MS);
  return RECENCY_FLOOR + (1 - RECENCY_FLOOR) * w;
}

function stateLabel(state: string): string {
  if (state === "streaming") return "writing";
  if (state === "tool_running") return "tool";
  if (state === "thinking") return "thinking";
  if (state === "orchestrating") return "orchestrating";
  if (state === "waiting") return "waiting";
  return state || "unknown";
}

/** Everything below is served by /api/live; the UI never re-derives it. */
export function liveRailTitle(s: LiveSession): string {
  return s.label || s.project || s.external_id.slice(-14);
}

export function liveRailStateLabel(s: LiveSession): string {
  return s.activity || stateLabel(s.state);
}

/** "no output for 4m" — an honest note when the harness has gone quiet. */
export function liveGapNote(s: LiveSession): string | null {
  const gap = s.observed_gap_seconds ?? 0;
  if (gap < 60) return null;
  return `quiet ${Math.round(gap / 60)}m`;
}

/* ── component ────────────────────────────────────────────────────────── */

export function ConstellationGraph({
  data,
  range,
  streamConnected,
  focusId,
  presence,
}: {
  data: GraphPayload;
  /** Identifies which window `data` describes, so a range swap is not read
      as an ingest. */
  range: string;
  streamConnected: boolean;
  focusId?: string | null;
  presence: PresenceSnapshot;
}) {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const fgRef = useRef<ForceGraphMethods | undefined>(undefined);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const reduced = useMemo(() => prefersReducedMotion(), []);

  const [selected, setSelected] = useState<GNode | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [settleAlpha, setSettleAlpha] = useState(0);
  const rangeRef = useRef(range);
  const settleTimer = useRef<number | null>(null);
  const toastTimer = useRef<number | null>(null);
  const hoverRef = useRef<GNode | null>(null);
  const [hoverTip, setHoverTip] = useState<{
    node: GNode;
    x: number;
    y: number;
    harnessGroup?: string | null;
  } | null>(null);
  const selectedRef = useRef<GNode | null>(null);
  selectedRef.current = selected;

  /* Highlight dim level, eased toward target per frame (§2.3, 120ms). */
  const dimRef = useRef({ value: 0, last: 0 });
  const egoRef = useRef<Set<string>>(new Set());

  /* Animation registries — glow paints stay capped at ≤~20 nodes. */
  const spawnRef = useRef<Map<string, Anim>>(new Map());
  const ringRef = useRef<Map<string, Anim & { color: string }>>(new Map());
  const pulseRef = useRef<Map<string, Anim>>(new Map());
  const edgeDrawRef = useRef<Map<string, Anim>>(new Map());
  const [paintLive, setPaintLive] = useState(false);
  const paintLiveTimer = useRef<number | null>(null);

  const colors = useMemo(
    () => ({
      harness: {
        codex: cssColor("--harness-codex") || "#5b9dff",
        claude: cssColor("--harness-claude") || "#f5a623",
        cursor: cssColor("--harness-cursor") || "#2dd4bf",
        warp: cssColor("--harness-warp") || "#e45cc3",
        t3code: cssColor("--harness-t3code") || "#a78bfa",
        other: cssColor("--harness-other") || "#66718a",
      } as Record<string, string>,
      anchor: cssColor("--graph-anchor") || "#3a3a3a",
      live: cssColor("--accent-live") || "#22d3ee",
      foreground: cssColor("--foreground") || "#ececec",
    }),
    [],
  );
  const harnessHex = useCallback(
    (harness: string | undefined | null) =>
      colors.harness[(harness || "").toLowerCase()] ?? colors.harness.other,
    [colors],
  );

  /* Live presence lookup by session id. */
  const liveById = useMemo(() => {
    const map = new Map<string, LiveSession>();
    for (const s of presence.sessions) {
      if (s.session_id) map.set(s.session_id, s);
    }
    return map;
  }, [presence.sessions]);

  /* Rail membership comes straight off the shared presence payload, so a
     session that is live here is live in every other panel too. */
  const railConversations = presence.conversations;
  const railWorkers = presence.workers;

  /* Per-repo harness lists (stable order) for sub-centroids + rings. */
  const repoHarnesses = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const n of data.nodes) {
      if (n.kind !== "repo") continue;
      const names = sortHarnessNames(
        (n.harnesses ?? []).map((h) => h.harness),
      );
      map.set(n.id, names);
      map.set(n.label, names);
    }
    return map;
  }, [data]);

  /* Anchor seeds: largest project at center, remaining significant projects
     on a size-aware ring around it, 1–2 session repos pushed to a periphery
     ring so meaningful projects dominate the stage. */
  const seeds = useMemo(() => {
    const repos = data.nodes
      .filter((n) => n.kind === "repo")
      .map((n) => n as GNode)
      .sort((a, b) => (b.sessions ?? 0) - (a.sessions ?? 0));
    const map = new Map<string, { x: number; y: number }>();
    if (repos.length === 0) return map;
    const aspect =
      size.w > 0 && size.h > 0
        ? Math.min(1.8, Math.max(1, size.w / size.h))
        : 1.4;

    const center = repos[0];
    map.set(center.id, { x: 0, y: 0 });
    const rest = repos.slice(1);
    const ring = rest.filter((r) => (r.sessions ?? 0) > MINOR_SESSIONS);
    const minors = rest.filter((r) => (r.sessions ?? 0) <= MINOR_SESSIONS);
    const est = (r: GNode) =>
      clusterRadius(
        r.sessions ?? 1,
        (repoHarnesses.get(r.id) ?? []).length || 1,
      );
    const ringEsts = ring.map(est);
    const maxRingEst = Math.max(0, ...ringEsts);
    const arcNeed =
      ring.reduce((acc, r) => acc + 2 * est(r) + 28, 0) / (2 * Math.PI);
    const ringR = Math.max(est(center) + maxRingEst + 40, arcNeed);
    ring.forEach((repo, i) => {
      const a = -Math.PI / 2 + (2 * Math.PI * i) / ring.length;
      map.set(repo.id, {
        x: ringR * aspect * Math.cos(a),
        y: ringR * Math.sin(a),
      });
    });

    const minorR = ringR + maxRingEst + 60;
    minors.forEach((repo, i) => {
      const a =
        -Math.PI / 2 +
        Math.PI / Math.max(1, minors.length) +
        (2 * Math.PI * i) / Math.max(1, minors.length);
      map.set(repo.id, {
        x: minorR * aspect * Math.cos(a),
        y: minorR * Math.sin(a),
      });
    });
    return map;
  }, [data, repoHarnesses, size]);

  /* Nested harness sub-centroids: invisible points on a small circle around
     each project seed. Same harness occupies the same angular slot order
     across projects; single-harness projects sit on the project seed. */
  const harnessSeeds = useMemo(() => {
    const map = new Map<string, { x: number; y: number }>();
    for (const n of data.nodes) {
      if (n.kind !== "repo") continue;
      const repo = n as GNode;
      const origin = seeds.get(repo.id) ?? { x: 0, y: 0 };
      const names = repoHarnesses.get(repo.id) ?? [];
      const H = names.length;
      const subR = harnessSubRadius(repo.sessions ?? 1, H);
      names.forEach((harness, i) => {
        const key = `${repo.id}|${harness}`;
        const pos =
          H <= 1
            ? { x: origin.x, y: origin.y }
            : {
                x:
                  origin.x +
                  subR * Math.cos(-Math.PI / 2 + (2 * Math.PI * i) / H),
                y:
                  origin.y +
                  subR * Math.sin(-Math.PI / 2 + (2 * Math.PI * i) / H),
              };
        map.set(key, pos);
        map.set(`${repo.id}|${harness.toLowerCase()}`, pos);
      });
    }
    return map;
  }, [data, repoHarnesses, seeds]);

  const seedOfSession = useCallback(
    (repo: string | undefined, harness: string | undefined) => {
      const anchorId = `repo:${repo ?? ""}`;
      const h = harness || "other";
      return (
        harnessSeeds.get(`${anchorId}|${h}`) ??
        harnessSeeds.get(`${anchorId}|${h.toLowerCase()}`) ??
        seeds.get(anchorId) ?? { x: 0, y: 0 }
      );
    },
    [harnessSeeds, seeds],
  );

  /* Keep the canvas ticker alive briefly for one-shot cues after the sim cools. */
  const kickPaint = useCallback(
    (ms: number) => {
      if (reduced) return;
      setPaintLive(true);
      if (paintLiveTimer.current) window.clearTimeout(paintLiveTimer.current);
      paintLiveTimer.current = window.setTimeout(() => {
        setPaintLive(false);
        paintLiveTimer.current = null;
      }, ms);
    },
    [reduced],
  );

  /* One-shot rings on presence arrival / state change — never looping. */
  useEffect(() => {
    if (reduced) return;
    const now = performance.now();
    let any = false;
    for (const key of presence.arrivals) {
      const sid =
        presence.sessions.find((s) => sessionPresenceKey(s) === key)
          ?.session_id ?? key;
      if (!nodeMapRef.current.has(sid)) continue;
      ringRef.current.set(sid, { t0: now, color: colors.live });
      any = true;
    }
    for (const t of presence.transitions) {
      const sid =
        presence.sessions.find((s) => sessionPresenceKey(s) === t.key)
          ?.session_id ?? t.key;
      if (!nodeMapRef.current.has(sid)) continue;
      ringRef.current.set(sid, { t0: now, color: colors.live });
      any = true;
    }
    if (any) kickPaint(RING_MS + 40);
  }, [
    colors.live,
    kickPaint,
    presence.arrivals,
    presence.sessions,
    presence.transitions,
    reduced,
  ]);

  /* Build sim objects, restoring positions across refetches so live updates
     never re-scramble a settled layout. */
  const nodeMapRef = useRef<Map<string, GNode>>(new Map());
  const prevStatsRef = useRef<Map<string, number> | null>(null);

  const graphData = useMemo(() => {
    const maxRepo = Math.max(
      1,
      ...data.nodes
        .filter((n) => n.kind === "repo")
        .map((n) => (n as { sessions: number }).sessions),
    );
    const prev = nodeMapRef.current;
    const nodes: GNode[] = data.nodes.map((raw) => {
      const r =
        raw.kind === "repo"
          ? (raw as { sessions: number }).sessions <= MINOR_SESSIONS
            ? 9
            : 16 + 6 * ((raw as { sessions: number }).sessions / maxRepo)
          : sessionRadius((raw as GraphSessionNode).messages);
      const node: GNode = {
        ...raw,
        r,
        recency:
          raw.kind === "session"
            ? recencyWeight((raw as GraphSessionNode).started_at)
            : 1,
      };
      const old = prev.get(raw.id);
      if (old) {
        node.x = old.x;
        node.y = old.y;
        node.vx = old.vx;
        node.vy = old.vy;
      } else if (raw.kind === "repo") {
        const seed = seeds.get(raw.id) ?? { x: 0, y: 0 };
        node.x = seed.x;
        node.y = seed.y;
      } else {
        const sess = raw as GraphSessionNode;
        const logical = logicalHarness(sess);
        const seed = seedOfSession(sess.repo, logical);
        const names = repoHarnesses.get(`repo:${sess.repo}`) ?? [logical];
        const lobe =
          names.length <= 1
            ? 18 + Math.random() * 28
            : 8 + Math.random() * 13;
        const theta = Math.random() * Math.PI * 2;
        node.x = seed.x + lobe * Math.cos(theta);
        node.y = seed.y + lobe * Math.sin(theta);
      }
      return node;
    });
    nodeMapRef.current = new Map(nodes.map((n) => [n.id, n]));
    const links: GLink[] = data.edges.map((e) => ({ ...e }));
    return { nodes, links };
  }, [data, repoHarnesses, seedOfSession, seeds]);

  /* Adjacency for ego highlight + keyboard walk (payload ids, not sim refs). */
  const adjacency = useMemo(() => {
    const neighbors = new Map<string, Set<string>>();
    const childrenOf = new Map<string, string[]>();
    const add = (a: string, b: string) => {
      if (!neighbors.has(a)) neighbors.set(a, new Set());
      neighbors.get(a)!.add(b);
    };
    for (const e of data.edges) {
      add(e.source, e.target);
      add(e.target, e.source);
      if (e.kind === "orchestration") {
        if (!childrenOf.has(e.source)) childrenOf.set(e.source, []);
        childrenOf.get(e.source)!.push(e.target);
      }
    }
    return { neighbors, childrenOf };
  }, [data]);

  const hubs = useMemo(
    () =>
      data.nodes
        .filter((n) => n.kind === "session" && (n as GraphSessionNode).children > 0)
        .sort(
          (a, b) =>
            (b as GraphSessionNode).children - (a as GraphSessionNode).children,
        )
        .map((n) => n.id),
    [data],
  );

  /* Diff refetches into spawn/pulse animations (§2.5). */
  useEffect(() => {
    /* Swapping to a range React Query already has cached replaces the whole
       node set without anything being ingested. Diffing across that boundary
       would report every id in the new range as a fresh arrival — a false
       ingest toast and a relayout for pure navigation. */
    const rangeChanged = rangeRef.current !== range;
    rangeRef.current = range;

    const prev = rangeChanged ? null : prevStatsRef.current;
    const stats = new Map<string, number>();
    for (const n of data.nodes) {
      if (n.kind === "session")
        stats.set(n.id, (n as GraphSessionNode).messages);
    }
    prevStatsRef.current = stats;
    if (!prev) return;

    const added: string[] = [];
    const pulsed: string[] = [];
    for (const [id, messages] of stats) {
      if (!prev.has(id)) added.push(id);
      else if ((prev.get(id) ?? 0) < messages) pulsed.push(id);
    }
    if (added.length === 0 && pulsed.length === 0) return;

    const animate =
      !reduced && !document.hidden && added.length + pulsed.length <= 25;
    const now = performance.now();
    if (animate) {
      added.forEach((id, i) => {
        const node = nodeMapRef.current.get(id);
        const t0 = now + i * 60;
        spawnRef.current.set(id, { t0 });
        ringRef.current.set(id, {
          t0,
          color: harnessHex(node ? logicalHarness(node) : undefined),
        });
        const parent = (node as GraphSessionNode | undefined)?.parent_id;
        if (parent) edgeDrawRef.current.set(`${parent}→${id}`, { t0 });
      });
      pulsed.slice(0, 20).forEach((id) => pulseRef.current.set(id, { t0: now }));
      kickPaint(Math.max(SPAWN_MS, PULSE_MS) + added.length * 60);
    } else if (added.length > 25) {
      setToast(`${added.length} sessions ingested`);
      if (toastTimer.current) window.clearTimeout(toastTimer.current);
      toastTimer.current = window.setTimeout(() => setToast(null), 4000);
    }
    /* A graphData change already restarts the engine, so the settle only needs
       a floor under alpha, not a reset to 1.0 (§2.5). */
    if (added.length > 0 && !reduced) {
      setSettleAlpha(SETTLE_ALPHA);
      if (settleTimer.current) window.clearTimeout(settleTimer.current);
      settleTimer.current = window.setTimeout(
        () => setSettleAlpha(0),
        SETTLE_MS,
      );
    }
  }, [data, harnessHex, kickPaint, range, reduced]);

  useEffect(
    () => () => {
      if (settleTimer.current) window.clearTimeout(settleTimer.current);
      if (toastTimer.current) window.clearTimeout(toastTimer.current);
    },
    [],
  );

  /* Cluster physics — project membership + nested harness gravity. */
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    /* Slightly stronger charge than the prior -26: lobes need air without
       scattering whole projects. */
    fg.d3Force("charge")?.strength(-32);
    const link = fg.d3Force("link") as
      | {
          distance: (fn: (l: GLink) => number) => void;
          strength: (fn: (l: GLink) => number) => void;
        }
      | undefined;
    link?.distance((l) => (l.kind === "orchestration" ? 22 : 40));
    /* Orchestration hugs supervisors. Membership edges stay drawn but do not
       pull in the sim — otherwise they collapse harness lobes back onto the
       project anchor. X/Y gravity to the harness sub-centroid owns layout. */
    link?.strength((l) => (l.kind === "orchestration" ? 1.0 : 0));
    fg.d3Force(
      "collide",
      forceCollide()
        .radius((n: GNode) => n.r + 2)
        .iterations(2),
    );
    const targetOf = (n: GNode) => {
      if (n.kind === "repo") return seeds.get(n.id) ?? { x: 0, y: 0 };
      return seedOfSession(n.repo, logicalHarness(n));
    };
    /* Anchors near-pinned; multi-harness sessions pull harder to their lobe. */
    const gravity = (n: GNode) => {
      if (n.kind === "repo") return 0.78;
      const names = repoHarnesses.get(`repo:${n.repo}`) ?? [];
      return names.length > 1 ? 0.16 : 0.1;
    };
    fg.d3Force("x", forceX((n: GNode) => targetOf(n).x).strength(gravity));
    fg.d3Force("y", forceY((n: GNode) => targetOf(n).y).strength(gravity));
  }, [graphData, repoHarnesses, seedOfSession, seeds]);

  /* Visibility guard: hidden tab pauses the render/sim loop entirely. */
  useEffect(() => {
    const onVis = () => {
      if (document.hidden) fgRef.current?.pauseAnimation();
      else fgRef.current?.resumeAnimation();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  useEffect(() => {
    return () => {
      if (paintLiveTimer.current) window.clearTimeout(paintLiveTimer.current);
    };
  }, []);

  /* Fit the constellation once the first simulation settles. */
  const fittedRef = useRef(false);
  const onEngineStop = useCallback(() => {
    if (fittedRef.current) return;
    fittedRef.current = true;
    fgRef.current?.zoomToFit(reduced ? 0 : 400, 24);
  }, [reduced]);

  /* Stage sizing. */
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const rect = entries[0].contentRect;
      setSize({ w: rect.width, h: rect.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  /* Orchestration → graph deep link: select + center the requested hub. */
  const focusDone = useRef(false);
  useEffect(() => {
    if (!focusId || focusDone.current) return;
    const node = nodeMapRef.current.get(focusId);
    if (!node) return;
    focusDone.current = true;
    setSelected(node);
    window.setTimeout(() => {
      const n = nodeMapRef.current.get(focusId);
      if (n && n.x != null && n.y != null)
        fgRef.current?.centerAt(n.x, n.y, reduced ? 0 : 500);
    }, 600);
  }, [focusId, graphData, reduced]);

  /* Ego set for the current highlight center. */
  const setHighlight = useCallback(
    (node: GNode | null) => {
      const ego = new Set<string>();
      if (node) {
        ego.add(node.id);
        for (const n of adjacency.neighbors.get(node.id) ?? []) ego.add(n);
        /* Selecting/hovering a repo also lifts its harness-sibling sessions
           so the nested group is readable without permanent labels. */
        if (node.kind === "repo") {
          for (const n of nodeMapRef.current.values()) {
            if (n.kind === "session" && n.repo === node.label) ego.add(n.id);
          }
        } else if (node.kind === "session" && node.repo) {
          const logical = logicalHarness(node);
          for (const n of nodeMapRef.current.values()) {
            if (
              n.kind === "session" &&
              n.repo === node.repo &&
              logicalHarness(n) === logical
            )
              ego.add(n.id);
          }
        }
      }
      egoRef.current = ego;
    },
    [adjacency],
  );

  useEffect(() => {
    setHighlight(hoverRef.current ?? selected);
  }, [selected, setHighlight]);

  /* ── painting ─────────────────────────────────────────────────────── */

  const paintLiveOrb = useCallback(
    (
      ctx: CanvasRenderingContext2D,
      x: number,
      y: number,
      r: number,
      scale: number,
      state: PresenceState | string,
      harnessHexColor: string,
      nowMs: number,
    ) => {
      const live = colors.live;
      const working = orbIsWorking(state);
      /* Wide halo — findable after rail click. Loader spin is rail-primary;
         graph may echo a sweep while work is in flight. */
      const glowR = r + 22 / scale;
      const grad = ctx.createRadialGradient(x, y, r * 0.15, x, y, glowR);
      grad.addColorStop(0, withAlpha(live, working ? 0.55 : 0.38));
      grad.addColorStop(0.4, withAlpha(live, working ? 0.22 : 0.12));
      grad.addColorStop(1, withAlpha(live, 0));
      ctx.beginPath();
      ctx.arc(x, y, glowR, 0, 2 * Math.PI);
      ctx.fillStyle = grad;
      ctx.fill();

      ctx.beginPath();
      ctx.arc(x, y, r + 7 / scale, 0, 2 * Math.PI);
      ctx.strokeStyle = withAlpha(live, working ? 0.4 : 0.28);
      ctx.lineWidth = 5 / scale;
      ctx.stroke();

      ctx.beginPath();
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.fillStyle = withAlpha(live, working ? 0.92 : 0.7);
      ctx.fill();
      ctx.beginPath();
      ctx.arc(x, y, Math.max(2.4 / scale, r * 0.52), 0, 2 * Math.PI);
      ctx.fillStyle = withAlpha(harnessHexColor, 0.95);
      ctx.fill();

      if (state === "streaming" || state === "thinking" || state === "orchestrating") {
        const ring = r + 4.2 / scale;
        ctx.beginPath();
        ctx.arc(x, y, ring, 0, 2 * Math.PI);
        ctx.strokeStyle = withAlpha(live, 0.35);
        ctx.lineWidth = 2.2 / scale;
        ctx.stroke();
        if (!reduced) {
          const a = (nowMs / 1000) * 2.2;
          ctx.beginPath();
          ctx.arc(x, y, ring, a, a + Math.PI * 0.55);
          ctx.strokeStyle = withAlpha(live, 1);
          ctx.lineWidth = 3 / scale;
          ctx.lineCap = "round";
          ctx.stroke();
          ctx.lineCap = "butt";
        } else {
          ctx.beginPath();
          ctx.arc(x, y, ring, 0, 2 * Math.PI);
          ctx.strokeStyle = withAlpha(live, 1);
          ctx.lineWidth = 3 / scale;
          ctx.stroke();
        }
      } else if (state === "tool_running") {
        ctx.beginPath();
        ctx.arc(x, y, r + 2.8 / scale, 0, 2 * Math.PI);
        ctx.strokeStyle = withAlpha(live, 0.95);
        ctx.lineWidth = 1.9 / scale;
        ctx.stroke();
        ctx.save();
        ctx.translate(x, y);
        if (!reduced) ctx.rotate((nowMs / 1000) * 1.5);
        ctx.beginPath();
        ctx.arc(0, 0, r + 5.8 / scale, 0, 2 * Math.PI);
        ctx.strokeStyle = withAlpha(live, 0.75);
        ctx.lineWidth = 1.25 / scale;
        ctx.setLineDash([4 / scale, 2.5 / scale]);
        ctx.stroke();
        ctx.setLineDash([]);
        const tick = 7.5 / scale;
        ctx.strokeStyle = withAlpha(live, 1);
        ctx.lineWidth = 1.9 / scale;
        ctx.beginPath();
        ctx.moveTo(0, -r - tick);
        ctx.lineTo(0, -r - 1.5 / scale);
        ctx.moveTo(0, r + 1.5 / scale);
        ctx.lineTo(0, r + tick);
        ctx.moveTo(-r - tick, 0);
        ctx.lineTo(-r - 1.5 / scale, 0);
        ctx.moveTo(r + 1.5 / scale, 0);
        ctx.lineTo(r + tick, 0);
        ctx.stroke();
        ctx.restore();
      } else if (state === "waiting") {
        ctx.beginPath();
        ctx.arc(x, y, r + 4.2 / scale, -Math.PI * 0.68, Math.PI * 0.68);
        ctx.strokeStyle = withAlpha(live, 0.85);
        ctx.lineWidth = 2.4 / scale;
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(x, y - r - 4.2 / scale, 1.5 / scale, 0, 2 * Math.PI);
        ctx.fillStyle = withAlpha(live, 0.85);
        ctx.fill();
      } else {
        ctx.beginPath();
        ctx.arc(x, y, r + 3.5 / scale, 0, 2 * Math.PI);
        ctx.strokeStyle = withAlpha(live, 0.55);
        ctx.lineWidth = 1.6 / scale;
        ctx.stroke();
      }
    },
    [colors.live, reduced],
  );

  const paintNode = useCallback(
    (nodeObj: NodeObject, ctx: CanvasRenderingContext2D, scale: number) => {
      const node = nodeObj as GNode;
      const now = performance.now();
      const x = node.x ?? 0;
      const y = node.y ?? 0;

      const target = egoRef.current.size > 0 ? 1 : 0;
      const dt = dimRef.current.last ? now - dimRef.current.last : 16;
      dimRef.current.last = now;
      dimRef.current.value +=
        (target - dimRef.current.value) *
        Math.min(1, reduced ? 1 : dt / HOVER_FADE_MS);
      const dimmed =
        egoRef.current.size > 0 && !egoRef.current.has(node.id);
      const recency = node.kind === "session" ? (node.recency ?? 1) : 1;
      const alpha = (dimmed ? 1 - 0.82 * dimRef.current.value : 1) * recency;

      const live = node.kind === "session" ? liveById.get(node.id) : undefined;
      let r = node.r;
      if (live) r = Math.max(r, 11);
      const spawn = spawnRef.current.get(node.id);
      if (spawn) {
        const p = (now - spawn.t0) / SPAWN_MS;
        if (p >= 1) spawnRef.current.delete(node.id);
        else if (p <= 0) r = 0.01;
        else r = r * (1 - (1 - p) * (1 - p));
      }

      /* Live nodes stay full-bright — presence is the glance signal. */
      ctx.globalAlpha = live ? Math.max(alpha, 0.95) : alpha;

      if (node.kind === "repo") {
        const minor = (node.sessions ?? 0) <= MINOR_SESSIONS;
        ctx.beginPath();
        ctx.arc(x, y, r, 0, 2 * Math.PI);
        ctx.strokeStyle = withAlpha(colors.anchor, minor ? 0.55 : 0.9);
        ctx.lineWidth = 1 / scale;
        ctx.stroke();

        /* Harness ring in stable angular order — matches spatial lobes. */
        const comp = sortHarnessComp(node.harnesses ?? []);
        const total = comp.reduce((acc, h) => acc + h.sessions, 0);
        if (total > 0) {
          const gap = comp.length > 1 ? 0.09 : 0;
          let a0 = -Math.PI / 2;
          const ringR = r + 2.5 / scale;
          ctx.lineWidth = (minor ? 1.5 : 2.5) / scale;
          for (const h of comp) {
            const span = (2 * Math.PI * h.sessions) / total;
            ctx.beginPath();
            ctx.arc(x, y, ringR, a0 + gap / 2, a0 + span - gap / 2);
            ctx.strokeStyle = withAlpha(
              harnessHex(h.harness),
              minor ? 0.6 : 0.95,
            );
            ctx.stroke();
            a0 += span;
          }
        }

        const labelled =
          !minor ||
          hoverRef.current?.id === node.id ||
          selectedRef.current?.id === node.id ||
          scale > 0.9;
        if (labelled) {
          const label = node.label ? truncLabel(node.label) : "";
          ctx.font = `${11 / scale}px "JetBrains Mono", monospace`;
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          const above = (node.label ?? "").length % 2 === 1;
          const ty = above ? y - r - 17 / scale : y + r + 6 / scale;
          const w = ctx.measureText(label).width;
          ctx.fillStyle = "rgba(7,10,15,0.7)";
          ctx.fillRect(
            x - w / 2 - 3 / scale,
            ty - 2 / scale,
            w + 6 / scale,
            15 / scale,
          );
          ctx.fillStyle = withAlpha(colors.foreground, minor ? 0.6 : 0.85);
          ctx.fillText(label, x, ty);
        }
        ctx.globalAlpha = 1;
        return;
      }

      const hex = harnessHex(logicalHarness(node));
      const isHover = hoverRef.current?.id === node.id;
      const isSelected = selectedRef.current?.id === node.id;
      const pulse = pulseRef.current.get(node.id);
      let pulseP = 0;
      if (pulse) {
        pulseP = (now - pulse.t0) / PULSE_MS;
        if (pulseP >= 1) {
          pulseRef.current.delete(node.id);
          pulseP = 0;
        }
      }
      const isLive = Boolean(live);

      /* Glow: hovered/selected/one-shot pulse only — live orbs paint their own. */
      if ((isHover || isSelected || pulseP > 0) && !isLive) {
        let glowAlpha = 0.25;
        if (pulseP > 0) glowAlpha = 0.3 * (1 - pulseP);
        const glowR = r + 6 / scale;
        const grad = ctx.createRadialGradient(x, y, r * 0.5, x, y, glowR);
        const glowColor = pulseP > 0 ? colors.live : hex;
        grad.addColorStop(0, withAlpha(glowColor, glowAlpha));
        grad.addColorStop(1, withAlpha(glowColor, 0));
        ctx.beginPath();
        ctx.arc(x, y, glowR, 0, 2 * Math.PI);
        ctx.fillStyle = grad;
        ctx.fill();
      }

      /* One expanding spawn / presence ring (§2.5.1) — one-shot only. */
      const ring = ringRef.current.get(node.id);
      if (ring) {
        const p = (now - ring.t0) / RING_MS;
        if (p >= 1) ringRef.current.delete(node.id);
        else if (p > 0) {
          ctx.beginPath();
          ctx.arc(x, y, r + (40 / scale) * p, 0, 2 * Math.PI);
          ctx.strokeStyle = withAlpha(ring.color, 0.8 * (1 - p));
          ctx.lineWidth = 1.5 / scale;
          ctx.stroke();
        }
      }

      if (isLive && live) {
        paintLiveOrb(ctx, x, y, r, scale, live.state, hex, now);
      } else {
        ctx.beginPath();
        ctx.arc(x, y, r, 0, 2 * Math.PI);
        ctx.fillStyle = withAlpha(hex, 0.9);
        ctx.fill();
      }

      if (isSelected) {
        ctx.beginPath();
        ctx.arc(x, y, r + (isLive ? 6.5 : 3) / scale, 0, 2 * Math.PI);
        ctx.strokeStyle = withAlpha(colors.foreground, 0.9);
        ctx.lineWidth = 1.25 / scale;
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    },
    [colors, harnessHex, liveById, paintLiveOrb, reduced],
  );

  const paintPointerArea = useCallback(
    (nodeObj: NodeObject, color: string, ctx: CanvasRenderingContext2D) => {
      const node = nodeObj as GNode;
      ctx.beginPath();
      ctx.arc(node.x ?? 0, node.y ?? 0, node.r + 3, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
    },
    [],
  );

  const linkMode = useCallback(
    (l: LinkObject) =>
      (l as GLink).kind === "orchestration" ? ("replace" as const) : undefined,
    [],
  );

  const paintLink = useCallback(
    (linkObj: LinkObject, ctx: CanvasRenderingContext2D, scale: number) => {
      const link = linkObj as GLink;
      const src = link.source as GNode;
      const dst = link.target as GNode;
      if (src?.x == null || dst?.x == null) return;
      const sid = linkEndId(link.source);
      const tid = linkEndId(link.target);
      const dimmedOut =
        egoRef.current.size > 0 &&
        !(egoRef.current.has(sid) && egoRef.current.has(tid));
      const base = 0.55 * (dimmedOut ? 1 - 0.82 * dimRef.current.value : 1);

      const key = `${sid}→${tid}`;
      const draw = edgeDrawRef.current.get(key);
      let frac = 1;
      if (draw) {
        const p = (performance.now() - draw.t0) / EDGE_DRAW_MS;
        if (p >= 1) edgeDrawRef.current.delete(key);
        else frac = Math.max(0, p);
      }

      ctx.beginPath();
      ctx.moveTo(src.x!, src.y!);
      ctx.lineTo(
        src.x! + (dst.x! - src.x!) * frac,
        src.y! + (dst.y! - src.y!) * frac,
      );
      ctx.strokeStyle = withAlpha(harnessHex(link.harness), base);
      ctx.lineWidth = 1.25 / scale;
      ctx.stroke();
    },
    [harnessHex],
  );

  const membershipColor = useCallback((linkObj: LinkObject) => {
    const sid = linkEndId(linkObj.source);
    const tid = linkEndId(linkObj.target);
    const dimmedOut =
      egoRef.current.size > 0 &&
      !(egoRef.current.has(sid) && egoRef.current.has(tid));
    return `rgba(35,42,53,${
      0.35 * (dimmedOut ? 1 - 0.82 * dimRef.current.value : 1)
    })`;
  }, []);

  /* ── interactions ─────────────────────────────────────────────────── */

  const openSession = useCallback(
    (id: string) => {
      navigate(`/sessions/${encodeURIComponent(id)}?${params.toString()}`);
    },
    [navigate, params],
  );

  const filterRepo = useCallback(
    (repo: string) => {
      const p = new URLSearchParams(params);
      p.set("project", repo);
      navigate({ pathname: "/sessions", search: p.toString() });
    },
    [navigate, params],
  );

  const onNodeHover = useCallback(
    (nodeObj: NodeObject | null) => {
      const node = nodeObj as GNode | null;
      if (node && !pointerInRef.current) return;
      hoverRef.current = node;
      setHighlight(node ?? selectedRef.current);
      if (node && node.x != null && node.y != null && fgRef.current) {
        const pt = fgRef.current.graph2ScreenCoords(node.x, node.y);
        setHoverTip({
          node,
          x: pt.x,
          y: pt.y,
          harnessGroup:
            node.kind === "session" ? logicalHarness(node) : null,
        });
      } else {
        setHoverTip(null);
      }
    },
    [setHighlight],
  );

  const lastClickRef = useRef<{ id: string; t: number } | null>(null);
  const onNodeClick = useCallback(
    (nodeObj: NodeObject) => {
      const node = nodeObj as GNode;
      wrapRef.current?.focus();
      const now = performance.now();
      const last = lastClickRef.current;
      lastClickRef.current = { id: node.id, t: now };
      if (last && last.id === node.id && now - last.t < 300) {
        if (node.kind === "session") {
          openSession(node.id);
          return;
        }
      }
      setSelected(node);
    },
    [openSession],
  );

  const pointerInRef = useRef(true);
  const clearHover = useCallback(() => {
    if (hoverRef.current) {
      hoverRef.current = null;
      setHighlight(selectedRef.current);
      setHoverTip(null);
    }
  }, [setHighlight]);
  const onPointerEnter = useCallback(() => {
    pointerInRef.current = true;
  }, []);
  const onPointerLeave = useCallback(() => {
    pointerInRef.current = false;
    clearHover();
  }, [clearHover]);

  const selectId = useCallback((id: string | null | undefined) => {
    if (!id) return;
    const node = nodeMapRef.current.get(id);
    if (node) setSelected(node);
  }, []);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      const sel = selectedRef.current;
      const fg = fgRef.current;
      if (e.key === "Escape") {
        setSelected(null);
        return;
      }
      if (e.key === "f") {
        fg?.zoomToFit(reduced ? 0 : 400, 40);
        return;
      }
      if (e.key === "+" || e.key === "=") {
        fg?.zoom((fg.zoom() ?? 1) * 1.4, reduced ? 0 : 200);
        return;
      }
      if (e.key === "-") {
        fg?.zoom((fg.zoom() ?? 1) / 1.4, reduced ? 0 : 200);
        return;
      }
      /* Tab only cycles hubs once a node is selected. Claiming it
         unconditionally made the stage a focus trap with no keyboard exit;
         Esc deselects and hands Tab back to normal focus traversal. */
      if (e.key === "Tab" && sel && hubs.length > 0) {
        e.preventDefault();
        const idx = hubs.indexOf(sel.id);
        const step = e.shiftKey ? -1 : 1;
        selectId(hubs[(idx + step + hubs.length) % hubs.length]);
        return;
      }
      if (!sel) {
        if (e.key === "Enter" || e.key.startsWith("Arrow")) {
          e.preventDefault();
          selectId(hubs[0] ?? data.nodes.find((n) => n.kind === "session")?.id);
        }
        return;
      }
      if (e.key === "Enter") {
        if (sel.kind === "session") openSession(sel.id);
        return;
      }
      if (e.key === "c") {
        const repo = sel.kind === "repo" ? sel.label : sel.repo;
        if (repo) filterRepo(repo);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        selectId(sel.parent_id);
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        selectId(adjacency.childrenOf.get(sel.id)?.[0]);
        return;
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        const parentId = sel.parent_id;
        const siblings = parentId
          ? (adjacency.childrenOf.get(parentId) ?? [])
          : [];
        if (siblings.length < 2) return;
        const parent = nodeMapRef.current.get(parentId!);
        const angleOf = (id: string) => {
          const n = nodeMapRef.current.get(id);
          if (!n || !parent) return 0;
          return Math.atan2(
            (n.y ?? 0) - (parent.y ?? 0),
            (n.x ?? 0) - (parent.x ?? 0),
          );
        };
        const ordered = [...siblings].sort((a, b) => angleOf(a) - angleOf(b));
        const idx = ordered.indexOf(sel.id);
        const step = e.key === "ArrowRight" ? 1 : -1;
        selectId(ordered[(idx + step + ordered.length) % ordered.length]);
      }
    },
    [adjacency, data, filterRepo, hubs, openSession, reduced, selectId],
  );

  const onLiveRailClick = useCallback(
    (s: LiveSession) => {
      if (s.session_id && nodeMapRef.current.has(s.session_id)) {
        const node = nodeMapRef.current.get(s.session_id)!;
        setSelected(node);
        wrapRef.current?.focus();
        if (node.x != null && node.y != null) {
          const ms = reduced ? 0 : 450;
          fgRef.current?.centerAt(node.x, node.y, ms);
          const z = fgRef.current?.zoom?.() ?? 1;
          if (z < 2.2) fgRef.current?.zoom(2.2, ms);
        }
        return;
      }
      if (s.session_id) openSession(s.session_id);
    },
    [openSession, reduced],
  );

  /* One-shot rail orb flash on arrival / state change. */
  const [railFlash, setRailFlash] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    const keys = [
      ...presence.arrivals,
      ...presence.transitions.map((t) => t.key),
    ];
    if (keys.length === 0) return;
    setRailFlash((prev) => {
      const next = new Set(prev);
      for (const k of keys) next.add(k);
      return next;
    });
    const t = window.setTimeout(() => {
      setRailFlash((prev) => {
        const next = new Set(prev);
        for (const k of keys) next.delete(k);
        return next;
      });
    }, 1400);
    return () => window.clearTimeout(t);
  }, [presence.arrivals, presence.transitions]);

  /* ── render ───────────────────────────────────────────────────────── */

  const sel = selected;
  const selSession = sel && sel.kind === "session" ? (sel as GNode) : null;
  const selLive = selSession ? liveById.get(selSession.id) : undefined;
  const railSessions = [...railConversations, ...railWorkers];
  const watcherStale = presence.watcherFresh === false;
  const workInFlight = presence.sessions.some((s) => orbIsWorking(s.state));

  const renderRailRow = (s: LiveSession, worker: boolean) => {
    const key = sessionPresenceKey(s);
    const gap = liveGapNote(s);
    return (
      <button
        key={key}
        type="button"
        onClick={() => onLiveRailClick(s)}
        className={
          "flex items-center gap-2 rounded-control border px-2 py-1.5 text-left " +
          (worker
            ? "border-border-faint opacity-90 hover:border-[color-mix(in_srgb,var(--accent-live)_35%,var(--border))]"
            : "border-border hover:border-[color-mix(in_srgb,var(--accent-live)_45%,var(--border))]")
        }
      >
        <LiveOrb
          state={s.state}
          harnessColor={harnessHex(s.harness)}
          flash={railFlash.has(key)}
          worker={worker}
          title={`${liveRailTitle(s)} — ${liveRailStateLabel(s)}`}
        />
        <span className="min-w-0 flex-1">
          <span
            className={
              "block truncate font-sans text-[11px] " +
              (worker ? "text-muted-foreground" : "text-foreground")
            }
            title={liveRailTitle(s)}
          >
            {liveRailTitle(s)}
          </span>
          <span className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 font-mono text-[10px] text-faint-foreground">
            <span className="text-accent-live">{liveRailStateLabel(s)}</span>
            {s.project ? (
              <span className="truncate">{truncLabel(s.project, 14)}</span>
            ) : null}
            <span className="truncate">
              {s.harness_display ?? s.harness}
            </span>
            {gap ? <span className="text-faint-foreground">{gap}</span> : null}
          </span>
        </span>
      </button>
    );
  };

  return (
    <div className="stage-frame stage-floor flex h-full w-full overflow-hidden rounded-card border border-border">
      <div
        ref={wrapRef}
        tabIndex={0}
        role="application"
        aria-label="Session constellation graph"
        onKeyDown={onKeyDown}
        onPointerEnter={onPointerEnter}
        onPointerLeave={onPointerLeave}
        className="relative min-w-0 flex-1 outline-none"
      >
      {size.w > 0 && size.h > 0 ? (
        <ForceGraph2D
          ref={fgRef}
          width={size.w}
          height={size.h}
          graphData={
            graphData as unknown as {
              nodes: NodeObject[];
              links: LinkObject[];
            }
          }
          backgroundColor="rgba(0,0,0,0)"
          d3AlphaDecay={0.038}
          d3AlphaMin={0.005}
          d3AlphaTarget={settleAlpha}
          warmupTicks={reduced ? 300 : 0}
          cooldownTicks={reduced ? 0 : undefined}
          nodeCanvasObject={paintNode}
          nodePointerAreaPaint={paintPointerArea}
          nodeLabel={() => ""}
          linkCanvasObjectMode={linkMode}
          linkCanvasObject={paintLink}
          linkColor={membershipColor}
          linkWidth={0.5}
          onNodeHover={onNodeHover}
          onNodeClick={onNodeClick}
          onBackgroundClick={() => setSelected(null)}
          onZoom={clearHover}
          onEngineStop={onEngineStop}
          enableNodeDrag={false}
          autoPauseRedraw={!paintLive && !(workInFlight && !reduced)}
        />
      ) : null}

      {/* Corner chrome: truncation + stream / watcher state. */}
      <div className="pointer-events-none absolute left-3 top-2.5 flex flex-col gap-1 font-mono text-[10px] text-faint-foreground">
        <span>
          {data.counts.sessions.toLocaleString()} sessions ·{" "}
          {data.counts.repos} repos · {data.counts.orchestration_edges} links
        </span>
        {data.truncated ? <span>{data.truncated.note}</span> : null}
        {!streamConnected ? (
          <span className="text-status-warn">stream offline</span>
        ) : null}
        {watcherStale ? (
          <span className="text-status-warn">
            watcher idle
            {presence.watcherReason
              ? ` · ${presence.watcherReason.split(";")[0]}`
              : ""}
          </span>
        ) : null}
      </div>

      {toast ? (
        <div className="elevated-overlay absolute bottom-14 right-3 rounded-control border border-border px-2.5 py-1.5 font-mono text-[11px] text-muted-foreground">
          {toast}
        </div>
      ) : null}

      {/* Hover tooltip — harness group identity without permanent clutter. */}
      {hoverTip && hoverTip.node.kind === "session" ? (
        <div
          className="elevated-overlay pointer-events-none absolute z-10 w-[230px] rounded-card border border-border p-2.5"
          style={{
            left: Math.min(Math.max(hoverTip.x + 14, 8), size.w - 240),
            top: Math.min(Math.max(hoverTip.y - 10, 8), size.h - 150),
          }}
        >
          <div className="flex items-center gap-1.5">
            <span
              className="inline-block h-[6px] w-[6px] shrink-0 rounded-full"
              style={{ background: harnessHex(logicalHarness(hoverTip.node)) }}
            />
            <span className="truncate font-mono text-[11px] text-foreground">
              {shortModel(hoverTip.node.model)}
            </span>
            {liveById.has(hoverTip.node.id) ? (
              <span className="ml-auto font-mono text-[10px] text-accent-live">
                {stateLabel(liveById.get(hoverTip.node.id)!.state)}
              </span>
            ) : null}
          </div>
          <div className="mt-1 truncate text-[11px] text-muted-foreground">
            {hoverTip.node.repo}
            {hoverTip.harnessGroup ? (
              <span className="text-faint-foreground">
                {" "}
                · {hoverTip.harnessGroup} group
              </span>
            ) : null}
            {runtimeHarness(hoverTip.node) !== logicalHarness(hoverTip.node) ? (
              <span className="text-faint-foreground">
                {" "}
                · runtime {runtimeHarness(hoverTip.node)}
              </span>
            ) : null}
          </div>
          <div className="tabular mt-1 font-mono text-[10px] text-faint-foreground">
            {formatDayTime(hoverTip.node.started_at)} ·{" "}
            {formatDuration(hoverTip.node.duration_seconds)}
          </div>
          <div className="tabular mt-0.5 font-mono text-[10px] text-faint-foreground">
            {hoverTip.node.messages} msgs · {hoverTip.node.tools} tools
            {(hoverTip.node.children ?? 0) > 0
              ? ` · ${hoverTip.node.children} children`
              : ""}
          </div>
        </div>
      ) : hoverTip && hoverTip.node.kind === "repo" ? (
        <div
          className="elevated-overlay pointer-events-none absolute z-10 rounded-card border border-border px-2.5 py-1.5"
          style={{
            left: Math.min(Math.max(hoverTip.x + 14, 8), size.w - 180),
            top: Math.min(Math.max(hoverTip.y - 10, 8), size.h - 60),
          }}
        >
          <div className="font-mono text-[11px] text-foreground">
            {hoverTip.node.label}
          </div>
          <div className="tabular font-mono text-[10px] text-faint-foreground">
            {hoverTip.node.sessions} sessions
          </div>
          {(hoverTip.node.harnesses ?? []).length > 0 ? (
            <div className="mt-1 flex flex-wrap items-center gap-2">
              {sortHarnessComp(hoverTip.node.harnesses ?? []).map((h) => (
                <span
                  key={h.harness}
                  className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground"
                >
                  <span
                    className="inline-block h-[5px] w-[5px] rounded-full"
                    style={{ background: harnessHex(h.harness) }}
                  />
                  {h.harness} {h.sessions}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {/* Inspector strip */}
      <div
        className={
          "elevated-overlay absolute inset-x-2 bottom-2 rounded-card border border-border px-3 py-2 transition-transform duration-150 " +
          (sel ? "translate-y-0" : "pointer-events-none translate-y-[120%]")
        }
      >
        {selSession ? (
          <div className="flex items-center gap-3 text-[11px]">
            <span
              className="inline-block h-[7px] w-[7px] shrink-0 rounded-full"
              style={{ background: harnessHex(logicalHarness(selSession)) }}
            />
            <span className="min-w-0 truncate font-mono text-[11px] text-foreground">
              {selSession.id}
            </span>
            {selLive ? (
              <span className="shrink-0 font-mono text-[10px] text-accent-live">
                {stateLabel(selLive.state)}
              </span>
            ) : null}
            <span className="shrink-0 font-mono text-muted-foreground">
              {shortModel(selSession.model)}
            </span>
            <span className="shrink-0 text-muted-foreground">
              {selSession.repo}
              <span className="text-faint-foreground">
                {" "}
                · {logicalHarness(selSession)}
                {runtimeHarness(selSession) !== logicalHarness(selSession)
                  ? ` · runtime ${runtimeHarness(selSession)}`
                  : ""}
              </span>
            </span>
            <span className="tabular shrink-0 font-mono text-faint-foreground">
              {formatDayTime(selSession.started_at)} ·{" "}
              {formatDuration(selSession.duration_seconds)} ·{" "}
              {selSession.messages} msgs
              {(selSession.children ?? 0) > 0
                ? ` · ${selSession.children} children`
                : ""}
            </span>
            <span className="ml-auto flex shrink-0 items-center gap-2 text-faint-foreground">
              <button
                type="button"
                className="hover:text-foreground"
                onClick={() => openSession(selSession.id)}
              >
                <kbd>↵</kbd> open session
              </button>
              <button
                type="button"
                className="hover:text-foreground"
                onClick={() => selSession.repo && filterRepo(selSession.repo)}
              >
                <kbd>c</kbd> filter sessions
              </button>
            </span>
          </div>
        ) : sel && sel.kind === "repo" ? (
          <div className="flex flex-col gap-1 text-[11px]">
            <div className="flex items-center gap-3">
              <span className="min-w-0 truncate font-mono text-foreground">
                {sel.label}
              </span>
              <span className="flex shrink-0 items-center gap-2">
                {sortHarnessComp(sel.harnesses ?? []).map((h) => (
                  <span
                    key={h.harness}
                    className="flex items-center gap-1 font-mono text-[10px] text-muted-foreground"
                  >
                    <span
                      className="inline-block h-[5px] w-[5px] rounded-full"
                      style={{ background: harnessHex(h.harness) }}
                    />
                    {h.harness} {h.sessions}
                  </span>
                ))}
                <span className="tabular font-mono text-[10px] text-faint-foreground">
                  / {sel.sessions} sessions
                </span>
              </span>
              <span className="tabular shrink-0 font-mono text-[10px] text-faint-foreground">
                {formatDay(sel.first_at)} – {formatDay(sel.last_at)} ·{" "}
                {formatCount(sel.messages ?? 0)} msgs ·{" "}
                {formatCount(sel.tools ?? 0)} tool events
              </span>
              <span className="ml-auto flex shrink-0 items-center gap-2 text-faint-foreground">
                <button
                  type="button"
                  className="hover:text-foreground"
                  onClick={() => sel.label && filterRepo(sel.label)}
                >
                  <kbd>c</kbd> filter sessions
                </button>
              </span>
            </div>
            {(sel.models ?? []).length > 0 || (sel.efforts ?? []).length > 0 ? (
              <div className="flex items-center gap-3 font-mono text-[10px] text-faint-foreground">
                {(sel.models ?? []).length > 0 ? (
                  <span className="tabular min-w-0 truncate">
                    models:{" "}
                    {(sel.models ?? [])
                      .slice(0, 4)
                      .map(
                        (m) =>
                          `${shortModel(m.model)} ${formatCount(m.messages)}`,
                      )
                      .join(" · ")}
                    {(sel.models ?? []).length > 4
                      ? ` · +${(sel.models ?? []).length - 4} more`
                      : ""}{" "}
                    of {formatCount(sel.messages ?? 0)} msgs
                  </span>
                ) : null}
                {(sel.efforts ?? []).length > 0 ? (
                  <span className="tabular shrink-0">
                    effort:{" "}
                    {(sel.efforts ?? [])
                      .map((e) => `${e.effort} ${formatCount(e.messages)}`)
                      .join(" · ")}
                  </span>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
      </div>

      {/* ACTIVE NOW — flanking column (never overlays the stage). */}
      {railSessions.length > 0 || watcherStale ? (
        <aside className="flex w-[220px] shrink-0 flex-col gap-1.5 overflow-y-auto border-l border-border bg-[color-mix(in_srgb,var(--stage)_88%,var(--card))] px-2 py-2.5">
          <div className="font-mono text-[10px] uppercase tracking-[0.08em] text-faint-foreground">
            {railSessions.length > 0
              ? `active now · ${railSessions.length}`
              : "active now"}
          </div>
          {railSessions.length === 0 && watcherStale ? (
            <div className="rounded-control border border-border px-2 py-1.5 font-mono text-[10px] text-status-warn">
              no live sessions · watcher not reporting
            </div>
          ) : null}
          {railConversations.map((s) => renderRailRow(s, false))}
          {railWorkers.length > 0 ? (
            <>
              <div className="mt-0.5 font-mono text-[10px] uppercase tracking-[0.08em] text-faint-foreground">
                workers · {railWorkers.length}
              </div>
              {railWorkers.map((s) => renderRailRow(s, true))}
            </>
          ) : null}
        </aside>
      ) : null}
    </div>
  );
}
