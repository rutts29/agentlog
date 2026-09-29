import { cn } from "@/lib/utils";

export function DiagnosticLoader({
  label = "Reading local ledger",
  compact = false,
  rows,
}: {
  label?: string;
  compact?: boolean;
  rows?: number;
}) {
  const rowCount = rows ?? (compact ? 2 : 5);
  return (
    <div
      className={cn("diagnostic-loader", compact && "diagnostic-loader--compact")}
      role="status"
      aria-live="polite"
      aria-label={label}
    >
      <div className="diagnostic-loader__header">
        <span className="diagnostic-loader__label">{label}</span>
        <span className="diagnostic-loader__state">local read</span>
      </div>
      <div className="diagnostic-loader__rail" aria-hidden="true">
        <span className="diagnostic-loader__scan" />
      </div>
      <div className="diagnostic-loader__rows" aria-hidden="true">
        {Array.from({ length: rowCount }, (_, index) => (
          <span
            key={index}
            className="diagnostic-loader__row"
            style={{ width: `${88 - ((index * 13) % 37)}%` }}
          />
        ))}
      </div>
    </div>
  );
}
