import { cn } from "@/lib/utils";

type Props = {
  title: string;
  body: string;
  missing?: string[];
  className?: string;
};

/**
 * Deliberate empty state: says what the panel is and what will populate it.
 * A faint placeholder grid stands in for the absent data — never a blank void.
 */
export function EmptyState({ title, body, missing, className }: Props) {
  return (
    <div
      className={cn(
        "gated-well relative flex h-full min-h-[120px] flex-col justify-center gap-1.5 overflow-hidden px-4 py-5",
        className,
      )}
    >
      <svg
        aria-hidden
        className="pointer-events-none absolute inset-0 h-full w-full opacity-[0.05]"
      >
        <defs>
          <pattern id="es-grid" width="14" height="14" patternUnits="userSpaceOnUse">
            <path d="M 14 0 L 0 0 0 14" fill="none" stroke="currentColor" strokeWidth="1" />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#es-grid)" />
      </svg>
      <div className="relative">
        <div className="microlabel font-mono text-faint-foreground">awaiting data</div>
        <div className="mt-1 text-[13px] font-medium text-foreground">{title}</div>
        <p className="mt-1 max-w-prose text-[12px] leading-relaxed text-muted-foreground">
          {body}
        </p>
        {missing && missing.length > 0 ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {missing.map((m) => (
              <span
                key={m}
                className="rounded-[4px] border border-border px-1.5 py-0.5 font-mono text-[10px] text-faint-foreground"
              >
                {m}
              </span>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}
