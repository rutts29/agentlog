import type { PresenceState } from "@/lib/api";
import { cn } from "@/lib/utils";

function statusKind(state: PresenceState | string): "working" | "waiting" | "idle" {
  if (state === "waiting") return "waiting";
  if (["streaming", "tool_running", "thinking", "orchestrating"].includes(state)) return "working";
  return "idle";
}

export function AgentStatusMark({
  state,
  worker = false,
  title,
}: {
  state: PresenceState | string;
  worker?: boolean;
  title?: string;
}) {
  const kind = statusKind(state);
  return (
    <span
      className={cn("agent-status-mark", `agent-status-mark--${kind}`, worker && "agent-status-mark--worker")}
      role="img"
      aria-label={title ?? state}
    >
      <span aria-hidden />
    </span>
  );
}

