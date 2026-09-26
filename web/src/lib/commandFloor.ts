import type { LiveSession } from "./api.ts";

export type CrewSpecialty =
  | "coordinator"
  | "researcher"
  | "builder"
  | "tester"
  | "reviewer"
  | "systems"
  | "operator";

export type SpecialtyEvidence = "observed" | "inferred" | "unknown";

export type FamiliarKind =
  | "scout-hound"
  | "archive-owl"
  | "builder-beetle"
  | "test-spider"
  | "review-raven"
  | "ops-crab";

export type Workstation =
  | "command-deck"
  | "archive-console"
  | "fabrication-bay"
  | "diagnostics-bay"
  | "review-console"
  | "server-rack"
  | "human-gate"
  | "standby";

export type CommandActorState =
  | {
      kind: "working";
      mode: "planning" | "reading" | "writing" | "testing" | "tool";
      label: string;
      tool: string | null;
    }
  | { kind: "orchestrating"; workerCount: number; label: string }
  | { kind: "waiting"; reason: "human-input"; label: string }
  | { kind: "idle"; reason: "unknown"; label: string };

export type ActorLineage =
  | { kind: "root" }
  | { kind: "attached"; parentId: string }
  | { kind: "unattached"; parentHint: string | null };

type ActorCommon = {
  id: string;
  sessionId: string | null;
  externalId: string;
  harness: string;
  harnessDisplay: string;
  project: string;
  roomId: string;
  callsign: string;
  displayName: string;
  specialty: CrewSpecialty;
  specialtyEvidence: SpecialtyEvidence;
  workstation: Workstation;
  state: CommandActorState;
  sourceSnapshotStatus: "stable" | "pending" | "unknown";
  ageSeconds: number;
  childIds: string[];
};

export type CrewOperator = ActorCommon & {
  kind: "operator";
  lineage: { kind: "root" };
};

export type CrewFamiliar = ActorCommon & {
  kind: "familiar";
  familiar: FamiliarKind;
  lineage: Extract<ActorLineage, { kind: "attached" | "unattached" }>;
};

export type CommandFloorActor = CrewOperator | CrewFamiliar;

export type CommandFloorRoom = {
  id: string;
  project: string;
  actorIds: string[];
  operatorIds: string[];
  familiarIds: string[];
};

export type CommandFloorProjection = {
  actors: CommandFloorActor[];
  rooms: CommandFloorRoom[];
  byId: Map<string, CommandFloorActor>;
  operatorCount: number;
  familiarCount: number;
  workingCount: number;
  waitingCount: number;
  unattachedFamiliarCount: number;
};

type Specialty = {
  specialty: CrewSpecialty;
  evidence: SpecialtyEvidence;
};

const READ_PATTERN = /\b(read|search|grep|glob|find|browse|inspect|scan|query)\b/i;
const BUILD_PATTERN = /\b(write|edit|patch|create|generate|build|implement|refactor)\b/i;
const TEST_PATTERN = /\b(tests?|pytest|jest|vitest|lint|typecheck|check|diagnos|reproduc)\b/i;
const REVIEW_PATTERN = /\b(review|evidence|adjudicat|approve|proposal|audit|assess)\b/i;
const SYSTEMS_PATTERN = /\b(deploy|server|monitor|docker|container|infra|database|migrat|recover)\b/i;

function actorId(session: LiveSession): string {
  return session.session_id || `${session.harness}:${session.external_id}`;
}

function isWorker(session: LiveSession): boolean {
  if (session.role) return session.role === "worker";
  return session.source_path.includes("/subagents/") || session.source_path.includes("\\subagents\\");
}

function slug(value: string): string {
  const normalized = value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return normalized || "unassigned";
}

function hash(value: string): number {
  let result = 2166136261;
  for (const char of value) {
    result ^= char.charCodeAt(0);
    result = Math.imul(result, 16777619);
  }
  return result >>> 0;
}

function callsignFor(id: string, worker: boolean): string {
  const number = (hash(id) % 99) + 1;
  return `${worker ? "F" : "A"}-${String(number).padStart(2, "0")}`;
}

function currentText(session: LiveSession): string {
  return [session.tool, session.step, session.activity, session.task, session.label, session.title]
    .filter((value): value is string => Boolean(value))
    .join(" ");
}

function specialtyFromText(text: string, evidence: SpecialtyEvidence): Specialty | null {
  const words = text.replace(/[_-]+/g, " ");
  if (TEST_PATTERN.test(words)) return { specialty: "tester", evidence };
  if (REVIEW_PATTERN.test(words)) return { specialty: "reviewer", evidence };
  if (BUILD_PATTERN.test(words)) return { specialty: "builder", evidence };
  if (READ_PATTERN.test(words)) return { specialty: "researcher", evidence };
  if (SYSTEMS_PATTERN.test(words)) return { specialty: "systems", evidence };
  return null;
}

function specialtyFor(session: LiveSession): Specialty {
  if (session.state === "orchestrating" || (session.worker_count ?? 0) > 0) {
    return { specialty: "coordinator", evidence: "observed" };
  }
  if (session.tool) {
    const observed = specialtyFromText(session.tool, "observed");
    if (observed) return observed;
  }
  const inferred = specialtyFromText(currentText(session), "inferred");
  return inferred ?? { specialty: "operator", evidence: "unknown" };
}

function familiarFor(specialty: CrewSpecialty): FamiliarKind {
  switch (specialty) {
    case "coordinator":
      return "scout-hound";
    case "researcher":
      return "archive-owl";
    case "builder":
      return "builder-beetle";
    case "tester":
      return "test-spider";
    case "reviewer":
      return "review-raven";
    case "systems":
    case "operator":
      return "ops-crab";
  }
}

function workMode(session: LiveSession): Extract<CommandActorState, { kind: "working" }>["mode"] {
  const specialty = specialtyFor(session).specialty;
  if (session.state === "thinking") return "planning";
  if (specialty === "researcher") return "reading";
  if (specialty === "builder") return "writing";
  if (specialty === "tester") return "testing";
  return session.state === "streaming" ? "writing" : "tool";
}

function stateFor(session: LiveSession): CommandActorState {
  const label = session.activity || session.step || session.tool || "No recent output";
  switch (session.state) {
    case "orchestrating":
      return {
        kind: "orchestrating",
        workerCount: Math.max(1, session.worker_count ?? 0),
        label,
      };
    case "waiting":
      return { kind: "waiting", reason: "human-input", label };
    case "streaming":
    case "tool_running":
    case "thinking":
      return {
        kind: "working",
        mode: workMode(session),
        label,
        tool: session.tool ?? null,
      };
    default:
      return { kind: "idle", reason: "unknown", label };
  }
}

function workstationFor(state: CommandActorState, specialty: CrewSpecialty): Workstation {
  if (state.kind === "waiting") return "human-gate";
  if (state.kind === "orchestrating") return "command-deck";
  if (state.kind === "idle") return "standby";
  switch (specialty) {
    case "coordinator":
      return "command-deck";
    case "researcher":
      return "archive-console";
    case "builder":
      return "fabrication-bay";
    case "tester":
      return "diagnostics-bay";
    case "reviewer":
      return "review-console";
    case "systems":
    case "operator":
      return "server-rack";
  }
}

function sourceStatus(session: LiveSession): "stable" | "pending" | "unknown" {
  if (session.source_snapshot_status === "stable") return "stable";
  if (session.source_snapshot_status === "pending") return "pending";
  return "unknown";
}

function displayName(session: LiveSession): string {
  return (
    session.label ||
    session.task ||
    session.title ||
    session.project ||
    session.repo ||
    session.harness_display ||
    session.harness
  );
}

function aliasesFor(session: LiveSession): string[] {
  return [
    actorId(session),
    session.session_id,
    session.logical_session_id,
    session.external_id,
    `${session.harness}:${session.external_id}`,
  ].filter((value): value is string => Boolean(value));
}

function parentFor(session: LiveSession, aliases: Map<string, string>): string | null {
  const hints = [session.parent_session_id, session.parent_external_id].filter(
    (value): value is string => Boolean(value),
  );
  for (const hint of hints) {
    const direct = aliases.get(hint);
    if (direct) return direct;
    const qualified = aliases.get(`${session.harness}:${hint}`);
    if (qualified) return qualified;
  }
  return null;
}

export function projectCommandFloor(sessions: LiveSession[]): CommandFloorProjection {
  const aliases = new Map<string, string>();
  for (const session of sessions) {
    const id = actorId(session);
    for (const alias of aliasesFor(session)) aliases.set(alias, id);
  }

  const actors = sessions.map<CommandFloorActor>((session) => {
    const id = actorId(session);
    const worker = isWorker(session);
    const project = session.project || session.repo || "Unassigned";
    const specialty = specialtyFor(session);
    const state = stateFor(session);
    const common: ActorCommon = {
      id,
      sessionId: session.session_id,
      externalId: session.external_id,
      harness: session.harness,
      harnessDisplay: session.harness_display || session.harness,
      project,
      roomId: slug(project),
      callsign: callsignFor(id, worker),
      displayName: displayName(session),
      specialty: specialty.specialty,
      specialtyEvidence: specialty.evidence,
      workstation: workstationFor(state, specialty.specialty),
      state,
      sourceSnapshotStatus: sourceStatus(session),
      ageSeconds: session.age_seconds,
      childIds: [],
    };
    if (!worker) return { ...common, kind: "operator", lineage: { kind: "root" } };

    const parentId = parentFor(session, aliases);
    const parentHint = session.parent_session_id || session.parent_external_id || null;
    return {
      ...common,
      kind: "familiar",
      familiar: familiarFor(specialty.specialty),
      lineage: parentId
        ? { kind: "attached", parentId }
        : { kind: "unattached", parentHint },
    };
  });

  const byId = new Map(actors.map((actor) => [actor.id, actor]));
  for (const actor of actors) {
    if (actor.kind !== "familiar" || actor.lineage.kind !== "attached") continue;
    byId.get(actor.lineage.parentId)?.childIds.push(actor.id);
  }

  const roomMap = new Map<string, CommandFloorRoom>();
  for (const actor of actors) {
    const room = roomMap.get(actor.roomId) ?? {
      id: actor.roomId,
      project: actor.project,
      actorIds: [],
      operatorIds: [],
      familiarIds: [],
    };
    room.actorIds.push(actor.id);
    if (actor.kind === "operator") room.operatorIds.push(actor.id);
    else room.familiarIds.push(actor.id);
    roomMap.set(room.id, room);
  }

  const rooms = [...roomMap.values()].sort((left, right) =>
    left.project.localeCompare(right.project),
  );
  const operatorCount = actors.filter((actor) => actor.kind === "operator").length;
  const familiarCount = actors.length - operatorCount;
  const workingCount = actors.filter(
    (actor) => actor.state.kind === "working" || actor.state.kind === "orchestrating",
  ).length;
  const waitingCount = actors.filter((actor) => actor.state.kind === "waiting").length;
  const unattachedFamiliarCount = actors.filter(
    (actor) => actor.kind === "familiar" && actor.lineage.kind === "unattached",
  ).length;

  return {
    actors,
    rooms,
    byId,
    operatorCount,
    familiarCount,
    workingCount,
    waitingCount,
    unattachedFamiliarCount,
  };
}
