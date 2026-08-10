/**
 * Shared SVG gradient defs for Recharts (§3.4.1): soft vertical fills from
 * series color → residual tint. Fill with `chartGradient(key)` and a solid
 * stroke for stack edges. Gradients are lighting, not decoration.
 */

const SERIES_KEYS = [
  "codex",
  "claude",
  "cursor",
  "warp",
  "t3code",
  "other",
] as const;

/** Recharts <Tooltip> chrome, shared so every chart reads the same. */
export const CHART_TOOLTIP = {
  contentStyle: {
    background: "var(--popover)",
    border: "1px solid var(--border)",
    borderRadius: 6,
    fontSize: 12,
    padding: "6px 10px",
  },
  labelStyle: { color: "var(--muted-foreground)" },
  cursor: { fill: "rgba(255,255,255,0.03)" },
};

export function chartGradient(key: string): string {
  const k = SERIES_KEYS.includes(key as (typeof SERIES_KEYS)[number])
    ? key
    : "other";
  return `url(#chart-grad-${k})`;
}

export function ChartDefs() {
  return (
    <defs>
      {SERIES_KEYS.map((k) => (
        <linearGradient
          key={k}
          id={`chart-grad-${k}`}
          x1="0"
          y1="0"
          x2="0"
          y2="1"
        >
          <stop offset="0%" stopColor={`var(--harness-${k})`} stopOpacity={0.72} />
          <stop offset="55%" stopColor={`var(--harness-${k})`} stopOpacity={0.38} />
          <stop offset="100%" stopColor={`var(--harness-${k})`} stopOpacity={0.14} />
        </linearGradient>
      ))}
      <linearGradient id="chart-grad-neutral" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stopColor="var(--muted-foreground)" stopOpacity={0.55} />
        <stop offset="100%" stopColor="var(--muted-foreground)" stopOpacity={0.12} />
      </linearGradient>
    </defs>
  );
}
