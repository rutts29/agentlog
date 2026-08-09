from __future__ import annotations

from enum import Enum

EXTRACTOR_VERSION = "0.1.0"
EXTRACTOR_NAME_DET = "det_v1"
EXTRACTOR_NAME_UX = "ux_v1"
EXTRACTOR_NAME_AUTO_REVIEW = "auto_review_v1"
EXTRACTOR_NAME_WORKER = "worker_task_v1"
EXTRACTOR_NAME_SKILL = "skill_compliance_v1"

# Empty after trim only. Not a length floor for short human turns.
MIN_NONEMPTY_CHARS = 1

USER_TEXT_CAP = 4000
ASSISTANT_TEXT_CAP = 4000
NEXT_USER_TEXT_CAP = 2000
TOOL_TIMELINE_MAX_LINES = 80
SKILL_BODY_CHAR_THRESHOLD = 20_000

DEFAULT_UX_MODEL = "grok-4.5"
DEFAULT_BATCH_SIZE = 1


class TurnKind(str, Enum):
    HUMAN_TASK = "human_task"
    HUMAN_FOLLOWUP = "human_followup"
    CLARIFYING_QUESTION = "clarifying_question"
    SOFT_APPROVAL = "soft_approval"
    CORRECTION = "correction"
    REDIRECT_OR_BRAKE = "redirect_or_brake"
    DONT_ACT_YET = "dont_act_yet"
    INTER_AGENT_HANDOFF = "inter_agent_handoff"
    WORKER_BRIEF = "worker_brief"
    COORDINATOR_NUDGE = "coordinator_nudge"
    AUTO_REVIEW = "auto_review"
    HARNESS_SYNTHETIC = "harness_synthetic"
    SKILL_INVOCATION = "skill_invocation"
    SLASH_COMMAND = "slash_command"
    IMAGE_ONLY = "image_only"
    EMPTY_OR_UNPARSEABLE = "empty_or_unparseable"
    TOOL_PLUMBING = "tool_plumbing"


class UserStance(str, Enum):
    NEUTRAL = "neutral"
    APPROVING = "approving"
    CORRECTING = "correcting"
    REDIRECTING = "redirecting"
    SKEPTICAL = "skeptical"
    FRUSTRATED = "frustrated"
    CONFUSED = "confused"
    BLOCKED_WAITING_ON_USER = "blocked_waiting_on_user"
    ABSTAIN = "abstain"


class AgentStance(str, Enum):
    EXECUTING = "executing"
    INVESTIGATING = "investigating"
    NARRATING_WAIT = "narrating_wait"
    ASKING_CLARIFICATION = "asking_clarification"
    PUSHING_BACK = "pushing_back"
    HANDING_OFF = "handing_off"
    FAILING_TOOLING = "failing_tooling"
    ABSTAIN = "abstain"


class PriorOutcome(str, Enum):
    ACCEPTED_CONTINUE = "accepted_continue"
    ACCEPTED_DONE = "accepted_done"
    PARTIAL_ACCEPT = "partial_accept"
    REJECTED_REDO = "rejected_redo"
    IGNORED_BY_USER_TOPIC_SHIFT = "ignored_by_user_topic_shift"
    ABSTAIN = "abstain"


class Route(str, Enum):
    UX = "ux"
    AUTO_REVIEW = "auto_review"
    WORKER_TASK = "worker_task"
    SKILL_COMPLIANCE = "skill_compliance"
    DROP = "drop"


PROCESS_FLAGS = (
    "premature_action_called_out",
    "scope_expansion",
    "scope_narrowing",
    "multi_agent_reference",
    "instruction_violation_alleged",
    "verification_requested",
    "usage_or_api_limit",
)

# Audit gate thresholds (eval-architecture scheduled semantic suite).
AUDIT_GATES: dict[str, tuple[float, float]] = {
    "redirect_or_brake": (0.90, 0.80),
    "dont_act_yet": (0.85, 0.70),
    "correction": (0.90, 0.80),
    "pushing_back": (0.85, 0.70),
    "frustrated": (0.85, 0.70),
    "soft_approval": (0.85, 0.70),
}
