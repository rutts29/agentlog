import { harnessColor, shortModel, cn } from "@/lib/utils";
import type { TranscriptSourceStatus, TranscriptStorage } from "@/lib/api";

export function harnessDisplayName(harness: string): string {
  switch (harness.toLowerCase()) {
    case "t3code":
      return "T3 Code";
    case "codex":
      return "Codex";
    case "claude":
      return "Claude Code";
    case "cursor":
      return "Cursor";
    case "warp":
      return "Warp";
    case "hermes":
      return "Hermes";
    default:
      return harness || "other";
  }
}

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
      {harnessDisplayName(harness)}
    </span>
  );
}

export function RuntimeHarnessLabel({
  logicalHarness,
  runtimeHarness,
  className,
}: {
  logicalHarness: string;
  runtimeHarness: string;
  className?: string;
}) {
  if (!runtimeHarness || logicalHarness.toLowerCase() === runtimeHarness.toLowerCase()) {
    return null;
  }
  return (
    <span className={cn("text-[10px] text-faint-foreground", className)}>
      runs on {harnessDisplayName(runtimeHarness)}
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

export function TranscriptStorageBadge({
  storage,
  sourceStatus,
  className,
}: {
  storage?: TranscriptStorage | null;
  sourceStatus?: TranscriptSourceStatus | null;
  className?: string;
}) {
  const sourceBacked =
    storage === "source_backed" ||
    (sourceStatus != null && sourceStatus !== "legacy");
  const label = sourceBacked ? "Source-backed" : "Legacy transcript";
  const tint = sourceBacked ? "var(--status-info)" : "var(--faint-foreground)";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-[4px] border px-1.5 py-[2px] text-[10px] leading-none",
        className,
      )}
      style={{
        borderColor: `color-mix(in srgb, ${tint} 35%, var(--border))`,
        color: tint,
      }}
      title={sourceStatus && sourceStatus !== "ready" ? `Source status: ${sourceStatus}` : undefined}
    >
      <span aria-hidden className="inline-block h-[5px] w-[5px] rounded-full" style={{ background: tint }} />
      {label}
    </span>
  );
}
