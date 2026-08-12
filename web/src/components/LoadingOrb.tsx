export function LoadingOrb({
  label = "Thinking",
  compact = false,
}: {
  label?: string;
  compact?: boolean;
}) {
  return (
    <div
      className={compact ? "thinking-loader thinking-loader--compact" : "thinking-loader"}
      role="status"
      aria-live="polite"
      aria-label={label}
    >
      <div className="thinking-orb" aria-hidden="true">
        <span className="thinking-orb__signal" />
        <span className="thinking-orb__orbit thinking-orb__orbit--outer">
          <span className="thinking-orb__node" />
        </span>
        <span className="thinking-orb__orbit thinking-orb__orbit--inner">
          <span className="thinking-orb__node" />
        </span>
        <span className="thinking-orb__core" />
      </div>
      <span className="microlabel text-[10px] text-faint-foreground">{label}</span>
    </div>
  );
}
