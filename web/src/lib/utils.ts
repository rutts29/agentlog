import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
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

export function harnessColor(harness: string): string {
  const key = harness.toLowerCase();
  if (key === "codex") return "var(--harness-codex)";
  if (key === "claude") return "var(--harness-claude)";
  if (key === "cursor") return "var(--harness-cursor)";
  if (key === "warp") return "var(--harness-warp)";
  return "var(--harness-other)";
}
