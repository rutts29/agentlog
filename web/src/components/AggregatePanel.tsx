import type { AggregateCell } from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { cn } from "@/lib/utils";

type Props = {
  title: string;
  cell: AggregateCell;
  className?: string;
};

export function AggregatePanel({ title, cell, className }: Props) {
  if (cell.status !== "ok") {
    return (
      <div className={cn("space-y-2", className)}>
        <div className="text-[13px] font-medium tracking-[0.02em] text-muted-foreground uppercase">
          {title}
        </div>
        <EmptyState
          title={
            cell.status === "unavailable"
              ? "Not available yet"
              : "Insufficient data"
          }
          body={
            cell.message ??
            "This aggregate does not meet the precision gate. Sessions are listed when present; no point estimate is shown."
          }
          missing={
            cell.session_ids.length
              ? [`${cell.session_ids.length} related session(s) available for drill-down`]
              : cell.flags
          }
        />
        {cell.flags.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {cell.flags.map((f) => (
              <span
                key={f}
                className="rounded-control border border-border px-1.5 py-0.5 text-[11px] text-faint-foreground"
              >
                {f}
              </span>
            ))}
          </div>
        ) : null}
      </div>
    );
  }

  const pct = cell.kind === "binary";
  const fmt = (v: number) =>
    pct ? `${(v * 100).toFixed(1)}%` : v.toFixed(2);

  return (
    <div className={cn("space-y-2", className)}>
      <div className="text-[13px] font-medium tracking-[0.02em] text-muted-foreground uppercase">
        {title}
      </div>
      <div className="tabular text-2xl font-semibold">{fmt(cell.estimate!)}</div>
      <div className="text-[12px] text-muted-foreground">
        95% interval {fmt(cell.interval.low!)} – {fmt(cell.interval.high!)}
        {cell.interval.method ? ` (${cell.interval.method})` : ""}
      </div>
      <div className="text-[12px] text-faint-foreground">
        n={cell.n_clusters} clusters
        {cell.evidence_tier ? ` · ${cell.evidence_tier.replace("_", " ")} evidence` : ""}
      </div>
      {cell.flags.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {cell.flags.map((f) => (
            <span
              key={f}
              className="rounded-control border border-border px-1.5 py-0.5 text-[11px] text-status-warn"
            >
              {f}
            </span>
          ))}
        </div>
      ) : null}
      <p className="text-[11px] text-faint-foreground">
        Descriptive interaction style when shown. Not a quality score.
      </p>
    </div>
  );
}
