/**
 * Speaker identity for transcript rendering.
 *
 * The corpus mixes six distinct voices. Classification prefers the
 * deterministic window label (window_det_classifications.request_kind,
 * surfaced per message by the API) and falls back to structural
 * heuristics when a re-ingest has not yet repopulated classifications.
 */

export type SpeakerKind =
  | "human"
  | "assistant"
  | "tool"
  | "worker_brief"
  | "synthetic"
  | "system"
  | "skill";

export type SpeakerSpec = {
  kind: SpeakerKind;
  label: string;
  /** Secondary tag shown next to the label, e.g. the request_kind. */
  detail: string | null;
  /** CSS color var for the gutter rail + label. */
  color: string;
  /** Collapse body by default — synthetic wrappers and long system dumps. */
  collapsed: boolean;
  /** For cursor-wrapped turns: the extracted human-authored core. */
  extractedQuery: string | null;
};

const SYNTHETIC_KINDS = new Set([
  "cursor_wrapped",
  "task_notification",
  "auto_review",
  "synthetic",
  "tool_result",
]);

const BRIEF_KINDS = new Set(["worker_brief", "inter_agent_handoff"]);

const WRAP_TAGS =
  /<(system_reminder|system_notification|attached_files|system-reminder|additional_context|environment_context|task_notification|user_instructions)[\s>]/;

const BRIEF_HEURISTIC =
  /^(full repository path:|you are running as a subagent|## (task|brief)\b|<task>)/i;

const NOTIFICATION_HEURISTIC =
  /^(<system_notification|\[?(task|background shell|subagent) (notification|completed)|a background (command|agent))/i;

/** Cursor emits these inside <user_query> after subagent/background completion. */
const SYNTHETIC_QUERY_HEURISTIC =
  /^(perform any necessary follow-up actions|a (background|cloud) (agent|command) (has )?(completed|finished))/i;

export function extractUserQuery(text: string): string | null {
  const m = text.match(/<user_query>\s*([\s\S]*?)\s*<\/user_query>/);
  return m ? m[1] : null;
}

function looksWrapped(text: string): boolean {
  return WRAP_TAGS.test(text) || /^<user_query>/.test(text.trimStart());
}

export function classifySpeaker(msg: {
  role: string;
  text: string;
  request_kind?: string | null;
  is_tool_plumbing?: boolean;
  skills?: string[];
}): SpeakerSpec {
  const text = msg.text || "";
  const kind = msg.request_kind || null;

  if (msg.role === "system") {
    const isSkill = (msg.skills?.length ?? 0) > 0;
    return {
      kind: isSkill ? "skill" : "system",
      label: isSkill ? "skill" : "system",
      detail: isSkill ? msg.skills!.join(", ") : null,
      color: "var(--speaker-system)",
      collapsed: text.length > 280,
      extractedQuery: null,
    };
  }

  if (msg.role !== "user") {
    if (msg.is_tool_plumbing) {
      return {
        kind: "tool",
        label: "tool io",
        detail: null,
        color: "var(--speaker-tool)",
        collapsed: text.length > 280,
        extractedQuery: null,
      };
    }
    return {
      kind: "assistant",
      label: "agent",
      detail: null,
      color: "var(--speaker-agent)",
      collapsed: false,
      extractedQuery: null,
    };
  }

  // role === user: split the human from everything pretending to be them.
  if (msg.is_tool_plumbing) {
    return {
      kind: "tool",
      label: "tool io",
      detail: null,
      color: "var(--speaker-tool)",
      collapsed: text.length > 280,
      extractedQuery: null,
    };
  }

  if (kind && BRIEF_KINDS.has(kind)) {
    return {
      kind: "worker_brief",
      label: "brief",
      detail: kind,
      color: "var(--speaker-brief)",
      collapsed: text.length > 600,
      extractedQuery: null,
    };
  }

  if (kind && SYNTHETIC_KINDS.has(kind)) {
    const q = extractUserQuery(text);
    return {
      kind: "synthetic",
      label: "harness",
      detail: kind,
      color: "var(--speaker-synthetic)",
      collapsed: true,
      extractedQuery: q,
    };
  }

  if (kind === "substantive") {
    const q = extractUserQuery(text);
    if (q !== null && SYNTHETIC_QUERY_HEURISTIC.test(q.trimStart())) {
      return {
        kind: "synthetic",
        label: "harness",
        detail: "follow-up",
        color: "var(--speaker-synthetic)",
        collapsed: true,
        extractedQuery: q,
      };
    }
    if (q !== null && looksWrapped(text)) {
      // Substantive content arriving through the harness envelope: show the
      // human core, tuck the wrapper away.
      return {
        kind: "human",
        label: "human",
        detail: "wrapped",
        color: "var(--speaker-human)",
        collapsed: false,
        extractedQuery: q,
      };
    }
    return {
      kind: "human",
      label: "human",
      detail: null,
      color: "var(--speaker-human)",
      collapsed: false,
      extractedQuery: null,
    };
  }

  // No classification available (e.g. mid re-ingest) — structural fallback.
  const q = extractUserQuery(text);
  if (BRIEF_HEURISTIC.test(text.trimStart())) {
    return {
      kind: "worker_brief",
      label: "brief",
      detail: "heuristic",
      color: "var(--speaker-brief)",
      collapsed: text.length > 600,
      extractedQuery: null,
    };
  }
  if (NOTIFICATION_HEURISTIC.test(text.trimStart())) {
    return {
      kind: "synthetic",
      label: "harness",
      detail: "notification",
      color: "var(--speaker-synthetic)",
      collapsed: true,
      extractedQuery: null,
    };
  }
  if (q !== null) {
    if (SYNTHETIC_QUERY_HEURISTIC.test(q.trimStart())) {
      return {
        kind: "synthetic",
        label: "harness",
        detail: "follow-up",
        color: "var(--speaker-synthetic)",
        collapsed: true,
        extractedQuery: q,
      };
    }
    return {
      kind: "human",
      label: "human",
      detail: "wrapped",
      color: "var(--speaker-human)",
      collapsed: false,
      extractedQuery: q,
    };
  }
  if (looksWrapped(text)) {
    return {
      kind: "synthetic",
      label: "harness",
      detail: "injected",
      color: "var(--speaker-synthetic)",
      collapsed: true,
      extractedQuery: null,
    };
  }
  return {
    kind: "human",
    label: "human",
    detail: null,
    color: "var(--speaker-human)",
    collapsed: false,
    extractedQuery: null,
  };
}

export const SPEAKER_LEGEND: Array<{
  kind: SpeakerKind;
  label: string;
  color: string;
  description: string;
}> = [
  {
    kind: "human",
    label: "human",
    color: "var(--speaker-human)",
    description: "Typed by you",
  },
  {
    kind: "assistant",
    label: "agent",
    color: "var(--speaker-agent)",
    description: "Model output",
  },
  {
    kind: "tool",
    label: "tool",
    color: "var(--speaker-tool)",
    description: "Tool calls and results",
  },
  {
    kind: "worker_brief",
    label: "brief",
    color: "var(--speaker-brief)",
    description: "Supervisor task brief to a worker",
  },
  {
    kind: "synthetic",
    label: "harness",
    color: "var(--speaker-synthetic)",
    description: "Harness-synthetic (wrapped, notifications, auto-review)",
  },
  {
    kind: "system",
    label: "system",
    color: "var(--speaker-system)",
    description: "System and skill injections",
  },
];
