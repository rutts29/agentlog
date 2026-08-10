/** Hand-rolled inline SVG sparklines and micro bar strips. No chart library. */
import { useId } from "react";

export function Sparkline({
  values,
  width = 64,
  height = 16,
  stroke = "var(--muted-foreground)",
}: {
  values: number[];
  width?: number;
  height?: number;
  stroke?: string;
}) {
  if (values.length === 0) return null;
  const max = Math.max(...values, 1);
  const pts = values
    .map((v, i) => {
      const x =
        values.length <= 1 ? width / 2 : (i / (values.length - 1)) * (width - 2) + 1;
      const y = height - 1.5 - (v / max) * (height - 4);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={width} height={height} className="shrink-0" aria-hidden>
      <polyline
        fill="none"
        stroke={stroke}
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
        points={pts}
      />
    </svg>
  );
}

export function MicroBars({
  values,
  width = 64,
  height = 16,
  fill = "var(--muted-foreground)",
}: {
  values: number[];
  width?: number;
  height?: number;
  fill?: string;
}) {
  const gradId = useId();
  if (values.length === 0) return null;
  const max = Math.max(...values, 1);
  const gap = 1;
  const bar = Math.max(1, (width - gap * (values.length - 1)) / values.length);
  return (
    <svg width={width} height={height} className="shrink-0" aria-hidden>
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={fill} stopOpacity={0.9} />
          <stop offset="100%" stopColor={fill} stopOpacity={0.25} />
        </linearGradient>
      </defs>
      {values.map((v, i) => {
        const h = Math.max(v > 0 ? 2 : 1, (v / max) * height);
        return (
          <rect
            key={i}
            x={i * (bar + gap)}
            y={height - h}
            width={bar}
            height={h}
            fill={`url(#${gradId})`}
            opacity={v > 0 ? 1 : 0.3}
          />
        );
      })}
    </svg>
  );
}

/** Horizontal meter used in ranked lists (tools, models, kinds). */
export function Meter({
  ratio,
  color = "var(--foreground)",
}: {
  ratio: number;
  color?: string;
}) {
  return (
    <div className="h-[3px] flex-1 overflow-hidden rounded-full bg-muted">
      <div
        className="h-full rounded-full"
        style={{
          width: `${Math.max(0, Math.min(1, ratio)) * 100}%`,
          background: color,
          opacity: 0.75,
        }}
      />
    </div>
  );
}
