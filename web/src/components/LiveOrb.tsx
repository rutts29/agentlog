import type { PresenceState } from "@/lib/api";

export type OrbKind =
  | "streaming"
  | "tool"
  | "thinking"
  | "orchestrating"
  | "waiting"
  | "unknown";

export function orbKind(state: PresenceState | string): OrbKind {
  switch (state) {
    case "streaming":
      return "streaming";
    case "tool_running":
      return "tool";
    case "thinking":
      return "thinking";
    case "orchestrating":
      return "orchestrating";
    case "waiting":
      return "waiting";
    default:
      return "unknown";
  }
}

export function orbIsWorking(state: PresenceState | string): boolean {
  const kind = orbKind(state);
  return kind !== "waiting" && kind !== "unknown";
}

/**
 * Loader for a live agent.
 *
 * Working states (streaming / tool / thinking / orchestrating) animate
 * continuously — the motion is the signal that work is in flight. waiting is a
 * settled open-C with no loop; unknown is a bare ring.
 */
export function LiveOrb({
  state,
  harnessColor,
  flash = false,
  worker = false,
  size = 30,
  title,
}: {
  state: PresenceState | string;
  harnessColor: string;
  flash?: boolean;
  worker?: boolean;
  size?: number;
  title?: string;
}) {
  const kind = orbKind(state);
  const working = orbIsWorking(state);

  return (
    <span
      className={
        "live-orb live-orb--" +
        kind +
        (worker ? " live-orb--worker" : "") +
        (flash ? " live-orb--flash" : "") +
        (working ? " live-orb--working" : "")
      }
      style={{ width: size, height: size }}
      data-state={state}
      data-working={working ? "true" : "false"}
      role="img"
      aria-label={title ?? kind}
      title={title ?? kind}
    >
      <svg viewBox="0 0 32 32" width={size} height={size} aria-hidden>
        {working ? (
          <circle
            className="live-orb__glow"
            cx="16"
            cy="16"
            r="14"
            fill="var(--accent-live)"
          />
        ) : null}

        <circle
          className="live-orb__shell"
          cx="16"
          cy="16"
          r={working ? 8.2 : 7.2}
          fill="var(--accent-live)"
        />
        <circle
          className="live-orb__core"
          cx="16"
          cy="16"
          r={working ? 4.4 : 3.8}
          fill={harnessColor}
        />

        {working ? (
          <circle
            className="live-orb__track"
            cx="16"
            cy="16"
            r="12"
            fill="none"
            stroke="var(--accent-live)"
            strokeWidth="1.2"
            opacity="0.22"
          />
        ) : null}

        {kind === "streaming" ? (
          <circle
            className="live-orb__sweep"
            cx="16"
            cy="16"
            r="12"
            fill="none"
            stroke="var(--accent-live)"
            strokeWidth="2.8"
            strokeLinecap="round"
            strokeDasharray="20 55"
          />
        ) : null}

        {kind === "tool" ? (
          <g className="live-orb__sweep">
            <circle
              cx="16"
              cy="16"
              r="12"
              fill="none"
              stroke="var(--accent-live)"
              strokeWidth="2.8"
              strokeLinecap="round"
              strokeDasharray="11 64"
            />
            <g
              stroke="var(--accent-live)"
              strokeWidth="2"
              strokeLinecap="round"
            >
              <line x1="16" y1="1.6" x2="16" y2="5" />
              <line x1="16" y1="27" x2="16" y2="30.4" />
            </g>
          </g>
        ) : null}

        {kind === "thinking" ? (
          <circle
            className="live-orb__sweep"
            cx="16"
            cy="16"
            r="12"
            fill="none"
            stroke="var(--accent-live)"
            strokeWidth="2.6"
            strokeLinecap="round"
            strokeDasharray="6 69"
          />
        ) : null}

        {kind === "orchestrating" ? (
          <g className="live-orb__sweep">
            <circle cx="16" cy="4" r="2.1" fill="var(--accent-live)" />
            <circle cx="26.4" cy="22" r="2.1" fill="var(--accent-live)" />
            <circle cx="5.6" cy="22" r="2.1" fill="var(--accent-live)" />
          </g>
        ) : null}

        {kind === "waiting" ? (
          <g>
            {/* Settled open C — needs you; not a spinner. */}
            <path
              d="M 9.6 8.2 A 10.2 10.2 0 1 0 22.4 8.2"
              fill="none"
              stroke="var(--accent-live)"
              strokeWidth="2"
              strokeLinecap="round"
              opacity="0.85"
            />
            <circle cx="16" cy="5" r="1.2" fill="var(--accent-live)" />
          </g>
        ) : null}

        {kind === "unknown" ? (
          <circle
            cx="16"
            cy="16"
            r="10.5"
            fill="none"
            stroke="var(--accent-live)"
            strokeWidth="1.3"
            opacity="0.5"
          />
        ) : null}
      </svg>
    </span>
  );
}
