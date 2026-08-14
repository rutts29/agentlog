import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatCount(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toLocaleString();
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return `${h}h${rem.toString().padStart(2, "0")}m`;
}

export function formatClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function formatDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function formatDayTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatFullTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Day-axis tick labels. MM-DD reads as unsorted the moment a series crosses a
 * year boundary, so multi-year ranges keep the year.
 */
export function dayTickFormatter(
  days: ReadonlyArray<{ day?: string | number } | string>,
): (d: string) => string {
  const at = (i: number) => {
    const row = days[i];
    return String((typeof row === "string" ? row : row?.day) ?? "");
  };
  const spansYears =
    days.length > 1 && at(0).slice(0, 4) !== at(days.length - 1).slice(0, 4);
  return (d: string) => (spansYears ? d.slice(2) : d.slice(5));
}

export function harnessColor(harness: string): string {
  const key = (harness || "").toLowerCase();
  if (key === "codex") return "var(--harness-codex)";
  if (key === "claude") return "var(--harness-claude)";
  if (key === "cursor") return "var(--harness-cursor)";
  if (key === "warp") return "var(--harness-warp)";
  if (key === "t3code") return "var(--harness-t3code)";
  if (key === "grok") return "var(--harness-grok)";
  return "var(--harness-other)";
}

/** Trim vendor prefixes off model ids so badges stay short: keep the tail. */
export function shortModel(model: string | null | undefined): string {
  if (!model) return "(unknown)";
  return model.length > 28 ? `…${model.slice(-27)}` : model;
}

/** Project/path labels: keep the recognizable tail, not the shared prefix. */
export function truncLabel(s: string, max = 26): string {
  if (!s) return "—";
  return s.length <= max ? s : "\u2026" + s.slice(s.length - (max - 1));
}
