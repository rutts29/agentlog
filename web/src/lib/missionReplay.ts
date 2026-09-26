import type {
  LiveSession,
  MissionReplayActor,
  MissionReplayEvent,
  MissionReplayMessageEvent,
  MissionReplayPayload,
} from "./api.ts";
import { projectCommandFloor, type CommandFloorProjection } from "./commandFloor.ts";
import { classifySpeaker } from "./speaker.ts";

export type MissionFrameKind =
  | "spawn"
  | "human"
  | "agent"
  | "tool"
  | "context"
  | "system"
  | "complete";

export type MissionFrameOutcome = "success" | "failure" | "unknown";

export type MissionFrame = {
  id: string;
  index: number;
  actorId: string;
  kind: MissionFrameKind;
  timestamp: string | null;
  title: string;
  detail: string;
  tool: string | null;
  action: string | null;
  context: string | null;
  durationMs: number | null;
  operationKind: string | null;
  outcome: MissionFrameOutcome;
  messageId: string | null;
  timeEvidence: "recorded" | "message" | "session" | "unknown";
};

type ActorLifecycle = {
  spawnIndex: number;
  completeIndex: number | null;
};

export type MissionReplay = {
  rootId: string;
  actors: MissionReplayActor[];
  actorById: Map<string, MissionReplayActor>;
  frames: MissionFrame[];
  tracks: Map<string, number[]>;
  lifecycleByActor: Map<string, ActorLifecycle>;
  recommendedFrameIndex: number;
  counts: MissionReplayPayload["counts"];
  bounds: MissionReplayPayload["bounds"];
};

function shortText(value: string, limit = 150): string {
  const text = value.replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function messageFrame(event: MissionReplayMessageEvent): Omit<MissionFrame, "index"> {
  const speaker = classifySpeaker({
    role: event.role,
    text: event.text,
    is_tool_plumbing: event.is_tool_plumbing,
    authored_by_agent: event.authored_by_agent,
  }, { isChildSession: true });
  const detail = shortText(speaker.extractedQuery ?? event.text) || "No text captured";
  switch (speaker.kind) {
    case "human":
      return {
        id: event.id,
        actorId: event.actor_id,
        kind: "human",
        timestamp: event.timestamp,
        title: "Human command",
        detail,
        tool: null,
        action: null,
        context: null,
        durationMs: null,
        operationKind: null,
        outcome: "unknown",
        messageId: event.source_message_id,
        timeEvidence: event.timestamp ? "recorded" : "unknown",
      };
    case "assistant":
      return {
        id: event.id,
        actorId: event.actor_id,
        kind: "agent",
        timestamp: event.timestamp,
        title: "Agent response",
        detail,
        tool: null,
        action: null,
        context: null,
        durationMs: null,
        operationKind: null,
        outcome: "unknown",
        messageId: event.source_message_id,
        timeEvidence: event.timestamp ? "recorded" : "unknown",
      };
    case "worker_brief":
    case "synthetic":
    case "skill":
      return {
        id: event.id,
        actorId: event.actor_id,
        kind: "context",
        timestamp: event.timestamp,
        title: speaker.label,
        detail,
        tool: null,
        action: null,
        context: null,
        durationMs: null,
        operationKind: null,
        outcome: "unknown",
        messageId: event.source_message_id,
        timeEvidence: event.timestamp ? "recorded" : "unknown",
      };
    case "system":
    case "tool":
      return {
        id: event.id,
        actorId: event.actor_id,
        kind: "system",
        timestamp: event.timestamp,
        title: speaker.label,
        detail,
        tool: null,
        action: null,
        context: null,
        durationMs: null,
        operationKind: null,
        outcome: "unknown",
        messageId: event.source_message_id,
        timeEvidence: event.timestamp ? "recorded" : "unknown",
      };
  }
}

function eventFrame(event: MissionReplayEvent): Omit<MissionFrame, "index"> {
  if (event.kind === "message") return messageFrame(event);
  if (event.kind === "tool") {
    return {
      id: event.id,
      actorId: event.actor_id,
      kind: "tool",
      timestamp: event.timestamp,
      title: event.tool_name,
      detail: shortText(event.context || event.action) || "Action unavailable",
      tool: event.tool_name,
      action: event.action || null,
      context: event.context || null,
      durationMs: event.duration_ms,
      operationKind: event.operation_kind,
      outcome: event.success === 1 ? "success" : event.success === 0 ? "failure" : "unknown",
      messageId: event.source_message_id,
      timeEvidence: event.time_evidence,
    };
  }
  return {
    id: event.id,
    actorId: event.actor_id,
    kind: event.phase === "spawn" ? "spawn" : "complete",
    timestamp: event.timestamp,
    title: event.phase === "spawn" ? "Agent deployed" : "Agent returned",
    detail: event.phase === "spawn" ? "Entered the mission topology" : "Execution window closed",
    tool: null,
    action: null,
    context: null,
    durationMs: null,
    operationKind: null,
    outcome: event.phase === "complete" ? "success" : "unknown",
    messageId: null,
    timeEvidence: event.timestamp ? "recorded" : "unknown",
  };
}

function recommendedFrame(frames: MissionFrame[], actorCount: number): number {
  if (actorCount <= 1) {
    const firstHuman = frames.findIndex((frame) => frame.kind === "human");
    return firstHuman >= 0 ? firstHuman : 0;
  }
  const timed = frames.filter((frame) => {
    const value = frame.timestamp ? Date.parse(frame.timestamp) : Number.NaN;
    return Number.isFinite(value) && frame.kind !== "spawn" && frame.kind !== "complete";
  });
  if (timed.length === 0) return 0;

  const actorCounts = new Map<string, number>();
  const activeAtFrame = new Map<number, number>();
  let active = 0;
  for (const frame of frames) {
    if (frame.kind === "spawn") active += 1;
    if (frame.kind === "complete") active = Math.max(0, active - 1);
    activeAtFrame.set(frame.index, active);
  }
  let left = 0;
  let toolCount = 0;
  let bestLeft = 0;
  let bestRight = 0;
  let bestScore = -1;
  const windowMs = 30 * 60 * 1_000;
  for (let right = 0; right < timed.length; right += 1) {
    const current = timed[right];
    actorCounts.set(current.actorId, (actorCounts.get(current.actorId) ?? 0) + 1);
    if (current.kind === "tool") toolCount += 1;
    const currentAt = Date.parse(current.timestamp ?? "");
    while (left < right && currentAt - Date.parse(timed[left].timestamp ?? "") > windowMs) {
      const leaving = timed[left];
      const count = (actorCounts.get(leaving.actorId) ?? 1) - 1;
      if (count === 0) actorCounts.delete(leaving.actorId);
      else actorCounts.set(leaving.actorId, count);
      if (leaving.kind === "tool") toolCount -= 1;
      left += 1;
    }
    const score = actorCounts.size * 24 + (activeAtFrame.get(current.index) ?? 0) * 8 + toolCount * 3 + (right - left + 1);
    if (score > bestScore) {
      bestScore = score;
      bestLeft = left;
      bestRight = right;
    }
  }
  const burst = timed.slice(bestLeft, bestRight + 1);
  const failedTool = burst.find((frame) => frame.kind === "tool" && frame.outcome === "failure");
  const middle = burst[Math.floor(burst.length / 2)];
  const nearbyTool = burst
    .filter((frame) => frame.kind === "tool")
    .sort((leftFrame, rightFrame) => Math.abs(leftFrame.index - middle.index) - Math.abs(rightFrame.index - middle.index))[0];
  return (failedTool ?? nearbyTool ?? middle).index;
}

export function buildMissionReplay(payload: MissionReplayPayload): MissionReplay {
  const frames = payload.events.map((event, index): MissionFrame => ({
    ...eventFrame(event),
    index,
  }));
  const tracks = new Map<string, number[]>();
  const lifecycleByActor = new Map<string, ActorLifecycle>();
  for (const actor of payload.actors) {
    tracks.set(actor.id, []);
    lifecycleByActor.set(actor.id, { spawnIndex: 0, completeIndex: null });
  }
  for (const frame of frames) {
    tracks.get(frame.actorId)?.push(frame.index);
    const lifecycle = lifecycleByActor.get(frame.actorId);
    if (!lifecycle) continue;
    if (frame.kind === "spawn") lifecycle.spawnIndex = frame.index;
    if (frame.kind === "complete") lifecycle.completeIndex = frame.index;
  }
  return {
    rootId: payload.root_id,
    actors: payload.actors,
    actorById: new Map(payload.actors.map((actor) => [actor.id, actor])),
    frames,
    tracks,
    lifecycleByActor,
    recommendedFrameIndex: recommendedFrame(frames, payload.actors.length),
    counts: payload.counts,
    bounds: payload.bounds,
  };
}

function actorFrameAt(replay: MissionReplay, actorId: string, frameIndex: number): MissionFrame | null {
  const indexes = replay.tracks.get(actorId) ?? [];
  for (let index = indexes.length - 1; index >= 0; index -= 1) {
    const frame = replay.frames[indexes[index]];
    if (frame.index <= frameIndex && frame.kind !== "spawn" && frame.kind !== "complete") {
      return frame;
    }
  }
  return null;
}

export function latestMissionFrameForActor(
  replay: MissionReplay,
  actorId: string,
  frameIndex: number,
): MissionFrame | null {
  return actorFrameAt(replay, actorId, frameIndex);
}

function actorName(actor: MissionReplayActor): string {
  if (actor.depth === 0) return `${actor.project} coordinator`;
  if (actor.workflow_group_label) return actor.workflow_group_label;
  if (actor.thread_source === "subagent") return "Subagent familiar";
  if (actor.relationship === "agent_worker") return "Agent worker";
  return `${actor.model || actor.logical_harness || actor.harness} familiar`;
}

function actorState(frame: MissionFrame | null, activeChildren: number): LiveSession["state"] {
  if (!frame) return activeChildren > 0 ? "orchestrating" : "thinking";
  if (frame.kind === "human") return "thinking";
  if (frame.kind === "agent") return "streaming";
  if (frame.kind === "tool") return "tool_running";
  return activeChildren > 0 ? "orchestrating" : "unknown";
}

function activeActorIds(replay: MissionReplay, frameIndex: number): Set<string> {
  const active = new Set<string>();
  for (const actor of replay.actors) {
    const lifecycle = replay.lifecycleByActor.get(actor.id);
    if (!lifecycle || frameIndex < lifecycle.spawnIndex) continue;
    if (lifecycle.completeIndex != null && frameIndex > lifecycle.completeIndex) continue;
    active.add(actor.id);
  }
  return active;
}

export function projectMissionReplayFrame(
  replay: MissionReplay,
  frameIndex: number,
): CommandFloorProjection {
  const active = activeActorIds(replay, frameIndex);
  const activeChildren = new Map<string, number>();
  for (const actor of replay.actors) {
    if (!active.has(actor.id) || !actor.parent_navigation_id) continue;
    activeChildren.set(
      actor.parent_navigation_id,
      (activeChildren.get(actor.parent_navigation_id) ?? 0) + 1,
    );
  }
  const sessions: LiveSession[] = replay.actors
    .filter((actor) => active.has(actor.id))
    .map((actor) => {
      const frame = actorFrameAt(replay, actor.id, frameIndex);
      const workerCount = activeChildren.get(actor.id) ?? 0;
      const name = actorName(actor);
      const activity = frame?.detail || (workerCount > 0 ? `${workerCount} linked workers active` : "Awaiting recorded activity");
      return {
        harness: actor.harness,
        harness_display: actor.logical_harness || actor.harness,
        external_id: actor.id.split(":").slice(1).join(":"),
        session_id: actor.id,
        logical_session_id: actor.id,
        logical_harness: actor.logical_harness,
        source_path: "",
        state: actorState(frame, workerCount),
        last_activity_at: frame?.timestamp ?? actor.started_at,
        age_seconds: 0,
        pending_ingest: false,
        source_snapshot_status: "stable",
        title: name,
        repo: actor.project,
        parent_session_id: actor.parent_navigation_id ?? undefined,
        role: actor.depth === 0 ? "session" : "worker",
        label: name,
        task: activity,
        step: frame?.title ?? "Deployed",
        tool: frame?.tool,
        activity,
        project: actor.project,
        working: frame?.kind !== "complete",
        worker_count: workerCount,
      };
    });
  return projectCommandFloor(sessions);
}

export function findMissionFrame(
  replay: MissionReplay,
  messageId: string | null,
  actorId: string | null,
): number | null {
  if (!messageId) return null;
  const frame = replay.frames.find(
    (candidate) => candidate.messageId === messageId && (!actorId || candidate.actorId === actorId),
  );
  return frame?.index ?? null;
}
