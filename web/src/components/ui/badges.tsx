import { harnessColor, shortModel, cn } from "@/lib/utils";

/** Harness identity: 6px dot + lowercase name in the harness hue. */
export function HarnessTag({
  harness,
  className,
  muted,
}: {
  harness: string;
  className?: string;
  muted?: boolean;
}) {
  const color = harnessColor(harness);
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-[12px] leading-none",
        className,
      )}
      style={{ color: muted ? "var(--muted-foreground)" : color }}
    >
      <span
        aria-hidden
        className="inline-block h-[6px] w-[6px] shrink-0 rounded-full"
        style={{ background: color }}
      />
      {harness || "other"}
    </span>
  );
}

/** Model badge: mono chip, hairline border tinted by owning harness. */
export function ModelBadge({
  model,
  harness,
  effort,
  className,
}: {
  model: string | null | undefined;
  harness?: string;
  effort?: string | null;
  className?: string;
}) {
  const tint = harness ? harnessColor(harness) : "var(--border)";
  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center gap-1 rounded-[4px] border px-1.5 py-[2px] font-mono text-[11px] leading-none text-muted-foreground",
        className,
      )}
      style={{ borderColor: `color-mix(in srgb, ${tint} 35%, var(--border))` }}
      title={model ?? undefined}
    >
      <span className="truncate">{shortModel(model)}</span>
      {effort ? (
        <span className="shrink-0 text-faint-foreground">/{effort}</span>
      ) : null}
    </span>
  );
}

/** Status dot + text — never a filled badge wall. */
export function StatusDot({
  tone,
  label,
  className,
}: {
  tone: "ok" | "warn" | "error" | "info" | "neutral";
  label: string;
  className?: string;
}) {
  const color =
    tone === "neutral" ? "var(--faint-foreground)" : `var(--status-${tone})`;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-[12px] text-muted-foreground",
        className,
      )}
    >
      <span
        aria-hidden
        className="inline-block h-[6px] w-[6px] rounded-full"
        style={{ background: color }}
      />
      {label}
    </span>
  );
}
