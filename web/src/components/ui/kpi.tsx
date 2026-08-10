import { Card, CardTitle } from "@/components/ui/card";
import { Sparkline } from "@/components/ui/spark";
import { cn } from "@/lib/utils";
import { useCountUp } from "@/lib/useCountUp";

function Delta({
  delta,
  deltaBasis = "vs prior window",
}: {
  delta: number | null;
  deltaBasis?: string;
}) {
  if (delta == null) {
    return <span className="text-faint-foreground">no prior period</span>;
  }
  return (
    <>
      <span
        className="tabular"
        style={{
          color:
            delta > 0
              ? "var(--status-ok)"
              : delta < 0
                ? "var(--status-error)"
                : "var(--muted-foreground)",
        }}
      >
        {delta > 0 ? "▲" : delta < 0 ? "▼" : "="}{" "}
        {Math.abs(delta * 100).toFixed(0)}%
      </span>{" "}
      <span className="text-faint-foreground">{deltaBasis}</span>
    </>
  );
}

/**
 * Stat card readable in 200ms: label, display-scale tabular value, delta vs
 * the previous equal-length window. Green/red appear only here and in dots.
 */
export function Kpi({
  title,
  value,
  valueLabel,
  suffix,
  delta,
  deltaBasis = "vs prior window",
  sub,
  className,
}: {
  title: string;
  value?: number;
  valueLabel?: string;
  suffix?: string;
  /** Ratio, e.g. 0.12 for +12%. null = no prior period. */
  delta?: number | null;
  deltaBasis?: string;
  sub?: string;
  className?: string;
}) {
  return (
    <Card className={cn("min-w-0", className)}>
      <CardTitle>{title}</CardTitle>
      <div className="display-md display-ink mt-1.5">
        {valueLabel ?? (value ?? 0).toLocaleString()}
        {suffix ? (
          <span className="ml-1 text-[13px] font-normal text-muted-foreground">
            {suffix}
          </span>
        ) : null}
      </div>
      <div className="mt-1 truncate text-[12px] text-muted-foreground">
        {delta !== undefined ? <Delta delta={delta} deltaBasis={deltaBasis} /> : sub}
      </div>
      {delta !== undefined && sub ? (
        <div className="mt-0.5 truncate text-[12px] text-faint-foreground">{sub}</div>
      ) : null}
    </Card>
  );
}

/**
 * Compact stacked telemetry tile for the Overview left column. `hero` gets
 * the single display-xl numeral on the view; everything else display-md.
 */
export function KpiTile({
  title,
  value,
  hero,
  delta,
  sub,
  spark,
  className,
}: {
  title: string;
  value: number;
  hero?: boolean;
  delta?: number | null;
  sub?: string;
  spark?: number[];
  className?: string;
}) {
  const shown = useCountUp(value);
  return (
    <div className={cn("min-w-0 border-b border-border-faint pb-3", className)}>
      <div className="microlabel text-[10px] text-faint-foreground">{title}</div>
      <div className="mt-1 flex items-end justify-between gap-2">
        <span className={cn("display-ink", hero ? "display-xl" : "display-md")}>
          {shown.toLocaleString()}
        </span>
        {spark && spark.length > 0 ? (
          <Sparkline values={spark} width={72} height={22} stroke="var(--faint-foreground)" />
        ) : null}
      </div>
      <div className="mt-1 truncate text-[11px] text-muted-foreground">
        {delta !== undefined ? <Delta delta={delta} /> : sub}
      </div>
      {delta !== undefined && sub ? (
        <div className="mt-0.5 truncate text-[11px] text-faint-foreground">{sub}</div>
      ) : null}
    </div>
  );
}

/**
 * Honest unavailable state: an em-dash value and this metric's own reason,
 * clamped in place with the server's full text on hover.
 * Gated metrics never receive accent color — recessed well, neutral ramp.
 */
export function KpiUnavailable({
  title,
  reason,
  caption,
  compact,
  className,
}: {
  title: string;
  /** Full server-side explanation; shown on hover. */
  reason: string;
  /** Short stand-in for the caption line when `reason` is a paragraph. */
  caption?: string;
  compact?: boolean;
  className?: string;
}) {
  return (
    <div
      className={cn("gated-well min-w-0", compact ? "p-3" : "p-4", className)}
      title={reason}
    >
      <div className="flex items-baseline justify-between gap-2">
        <div className="microlabel text-[10px] text-faint-foreground">{title}</div>
        <span className="microlabel shrink-0 font-mono text-[10px] text-faint-foreground">
          gated
        </span>
      </div>
      <div
        className={cn(
          "mt-1.5 font-semibold leading-[1.2] text-faint-foreground",
          compact ? "text-[20px]" : "text-[24px]",
        )}
      >
        —
      </div>
      <div className="mt-1 line-clamp-2 font-mono text-[11px] leading-[1.35] text-faint-foreground">
        {caption ?? reason}
      </div>
    </div>
  );
}

/**
 * A real measurement over an incomplete corpus. Unlike KpiUnavailable the
 * number is genuine, so it gets ink — the coverage denominator carries the
 * caveat instead of suppressing the value.
 */
export function KpiPartial({
  title,
  valueLabel,
  suffix,
  sub,
  note,
  compact,
  className,
}: {
  title: string;
  valueLabel: string;
  suffix?: string;
  /** Coverage line, e.g. "473/632 sessions". Never omit. */
  sub: string;
  note?: string;
  compact?: boolean;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "min-w-0 rounded-card border border-border-faint bg-stage",
        compact ? "p-3" : "p-4",
        className,
      )}
      title={note}
    >
      <div className="flex items-baseline justify-between gap-2">
        <div className="microlabel text-[10px] text-faint-foreground">{title}</div>
        <span className="microlabel shrink-0 font-mono text-[10px] text-faint-foreground">
          partial
        </span>
      </div>
      <div
        className={cn(
          "display-ink mt-1.5 font-semibold leading-[1.2]",
          compact ? "text-[20px]" : "text-[24px]",
        )}
      >
        {valueLabel}
        {suffix ? (
          <span className="ml-1 text-[12px] font-normal text-muted-foreground">
            {suffix}
          </span>
        ) : null}
      </div>
      <div className="mt-1 line-clamp-2 font-mono text-[11px] leading-[1.35] text-muted-foreground">
        {sub}
      </div>
    </div>
  );
}
