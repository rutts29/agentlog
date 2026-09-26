import type {
  LiveSession,
  TimelineItem,
  TimelineMessage,
  ToolEvent,
  fetchSessionDetail,
} from "./api.ts";
import {
  projectCommandFloor,
  type CommandFloorActor,
  type CommandFloorProjection,
} from "./commandFloor.ts";
import { classifySpeaker } from "./speaker.ts";

export type ReplayFrameKind = "human" | "agent" | "tool" | "context" | "system" | "complete";
export type ReplayOutcome = "success" | "failure" | "unknown";

export type ReplayFrame = {
  id: string;
  index: number;
  kind: ReplayFrameKind;
  timestamp: string | null;
  title: string;
  detail: string;
  tool: string | null;
  outcome: ReplayOutcome;
  messageId: string | null;
  presenceState: "thinking" | "streaming" | "tool_running" | "unknown";
};

export type CommandReplay = {
  sessionId: string;
  project: string;
  frames: ReplayFrame[];
};

type SessionDetailPayload = Awaited<ReturnType<typeof fetchSessionDetail>>;

function shortText(text: string, limit = 120): string {
  const oneLine = text.replace(/\s+/g, " ").trim();
  return oneLine.length > limit ? `${oneLine.slice(0, limit - 1)}…` : oneLine;
}

function toolOutcome(tool: ToolEvent): ReplayOutcome {
  if (tool.success === 1) return "success";
  if (tool.success === 0) return "failure";
  return "unknown";
}

function toolFrame(tool: ToolEvent, timestamp: string | null, messageId: string | null): Omit<ReplayFrame, "index"> {
  const duration = tool.duration_ms == null
    ? "duration unavailable"
    : tool.duration_ms >= 1000
      ? `${(tool.duration_ms / 1000).toFixed(1)}s`
      : `${tool.duration_ms}ms`;
  return {
    id: `tool:${tool.id}`,
    kind: "tool",
    timestamp,
    title: tool.tool_name,
    detail: [tool.action, duration].filter(Boolean).join(" · "),
    tool: tool.tool_name,
    outcome: toolOutcome(tool),
    messageId,
    presenceState: "tool_running",
  };
}

function messageFrame(message: TimelineMessage, isChildSession: boolean): Omit<ReplayFrame, "index"> {
  const speaker = classifySpeaker(message, { isChildSession });
  const detail = shortText(speaker.extractedQuery ?? message.text ?? "") || "No text captured";
  switch (speaker.kind) {
    case "human":
      return {
        id: `message:${message.id}`,
        kind: "human",
        timestamp: message.timestamp,
        title: "Human command",
        detail,
        tool: null,
        outcome: "unknown",
        messageId: message.id,
        presenceState: "thinking",
      };
    case "assistant":
      return {
        id: `message:${message.id}`,
        kind: "agent",
        timestamp: message.timestamp,
        title: "Agent response",
        detail,
        tool: null,
        outcome: "unknown",
        messageId: message.id,
        presenceState: "streaming",
      };
    case "worker_brief":
    case "synthetic":
    case "skill":
      return {
        id: `message:${message.id}`,
        kind: "context",
        timestamp: message.timestamp,
        title: speaker.label,
        detail,
        tool: null,
        outcome: "unknown",
        messageId: message.id,
        presenceState: "unknown",
      };
    case "system":
    case "tool":
      return {
        id: `message:${message.id}`,
        kind: "system",
        timestamp: message.timestamp,
        title: speaker.label,
        detail,
        tool: null,
        outcome: "unknown",
        messageId: message.id,
        presenceState: "unknown",
      };
  }
}

function framesFromTimeline(timeline: TimelineItem[], isChildSession: boolean): Array<Omit<ReplayFrame, "index">> {
  const frames: Array<Omit<ReplayFrame, "index">> = [];
  for (const item of timeline) {
    if (item.kind === "tool") {
      frames.push(toolFrame({
        id: item.id,
        message_id: null,
        seq: item.seq,
        tool_name: item.tool_name,
        action: item.action,
        success: item.success,
        duration_ms: item.duration_ms,
      }, null, null));
      continue;
    }
    frames.push(messageFrame(item, isChildSession));
    for (const tool of item.tool_events) {
      frames.push(toolFrame(tool, item.timestamp, item.id));
    }
  }
  return frames;
}

export function buildCommandReplay(detail: SessionDetailPayload): CommandReplay {
  const frames = framesFromTimeline(detail.timeline, Boolean(detail.session.parent_session_id)).map(
    (frame, index): ReplayFrame => ({ ...frame, index }),
  );
  frames.push({
    id: `complete:${detail.session.id}`,
    index: frames.length,
    kind: "complete",
    timestamp: detail.session.ended_at,
    title: "Session complete",
    detail: `${detail.anatomy.message_count} messages · ${detail.anatomy.tool_count} tool calls`,
    tool: null,
    outcome: "success",
    messageId: null,
    presenceState: "unknown",
  });
  return {
    sessionId: detail.session.id,
    project: detail.session.project,
    frames,
  };
}

function replayLiveSession(detail: SessionDetailPayload, frame: ReplayFrame): LiveSession {
  const source = detail.session.source ?? detail.transcript?.source;
  const sourceStable = source?.status === "ready" || source?.status === "legacy";
  return {
    harness: detail.session.harness,
    harness_display: detail.session.logical_harness || detail.session.harness,
    external_id: detail.session.external_id,
    session_id: detail.session.id,
    logical_session_id: detail.session.orchestrator_session_id || detail.session.id,
    logical_harness: detail.session.logical_harness,
    source_path: detail.session.artifact_path || "",
    state: frame.presenceState,
    last_activity_at: frame.timestamp,
    age_seconds: 0,
    pending_ingest: false,
    source_snapshot_status: sourceStable ? "stable" : "pending",
    title: detail.session.id,
    repo: detail.session.repo,
    parent_session_id: detail.session.parent_session_id,
    role: detail.session.parent_session_id ? "worker" : "session",
    label: frame.title,
    task: frame.detail,
    step: frame.title,
    tool: frame.tool,
    activity: frame.detail,
    project: detail.session.project,
    working: frame.presenceState !== "unknown",
    worker_count: 0,
  };
}

export function projectReplayFrame(
  detail: SessionDetailPayload,
  frame: ReplayFrame,
): CommandFloorProjection {
  const projection = projectCommandFloor([replayLiveSession(detail, frame)]);
  const actor = projection.actors[0];
  if (!actor || frame.kind === "tool") return projection;

  const nextActor: CommandFloorActor = frame.kind === "human"
    ? {
        ...actor,
        specialty: "coordinator",
        specialtyEvidence: "inferred",
        workstation: "command-deck",
      }
    : {
        ...actor,
        specialty: "operator",
        specialtyEvidence: "unknown",
        workstation: frame.kind === "agent" ? "command-deck" : "standby",
      };
  return {
    ...projection,
    actors: [nextActor],
    byId: new Map([[nextActor.id, nextActor]]),
  };
}
