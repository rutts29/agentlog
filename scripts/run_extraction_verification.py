#!/usr/bin/env python3
"""Run deterministic triage on the owner DB and fixture audit metrics.

Does not execute the full-corpus LLM run.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentlog.analysis.extractors.audit import (  # noqa: E402
    compare_batch_vs_single,
    evaluate_gate,
    score_predictions,
)
from agentlog.analysis.extractors.llm_client import ScriptedChatClient  # noqa: E402
from agentlog.analysis.extractors.models import WindowContext  # noqa: E402
from agentlog.analysis.extractors.pipeline import (  # noqa: E402
    build_audit_pack,
    run_deterministic_phase,
)
from agentlog.analysis.extractors.ux_extractor import UxExtractor  # noqa: E402
from agentlog.config import DEFAULT_DB_PATH  # noqa: E402
from agentlog.db.schema import connect, init_db  # noqa: E402


def _fixture_audit_metrics() -> dict:
    """Measured P/R + batch disagreement on a labeled synthetic audit set."""
    gold = {
        "g1": {"turn_kind": ["redirect_or_brake", "dont_act_yet"], "user_stance": "redirecting"},
        "g2": {"turn_kind": ["correction"], "user_stance": "correcting"},
        "g3": {"turn_kind": ["soft_approval"], "user_stance": "approving"},
        "g4": {"turn_kind": ["human_followup"], "user_stance": "neutral"},
        "g5": {
            "turn_kind": ["human_task"],
            "agent_stance": "pushing_back",
            "user_stance": "neutral",
        },
    }
    contexts = [
        WindowContext(
            window_id="g1",
            session_id="s",
            harness="cursor",
            request_text="dont act on it yet just info",
            assistant_text="Understood, holding off.",
        ),
        WindowContext(
            window_id="g2",
            session_id="s",
            harness="codex",
            request_text="i said primary would be claude code not ONLY claude code",
            assistant_text="Got it, updating the scope.",
        ),
        WindowContext(
            window_id="g3",
            session_id="s",
            harness="codex",
            request_text="sounds good proceed",
            assistant_text="Continuing.",
        ),
        WindowContext(
            window_id="g4",
            session_id="s",
            harness="claude",
            request_text="also check the tests",
            assistant_text="Running tests.",
        ),
        WindowContext(
            window_id="g5",
            session_id="s",
            harness="claude",
            request_text="sync AGENTS.md to CLAUDE.md",
            assistant_text=(
                "Pause before I set anything up — your proposed mechanism "
                "duplicates infrastructure we already agreed on."
            ),
        ),
    ]

    def responder(*, system: str, user: str, model: str):
        # Single-window faithful labels; batched contaminates g4.
        if user.count("<window ") > 1:
            return {
                "windows": [
                    {
                        "window_id": "g1",
                        "turn_kind": ["redirect_or_brake", "dont_act_yet"],
                        "user_stance": "redirecting",
                        "agent_stance": "executing",
                        "prior_outcome": "abstain",
                        "flags": {"premature_action_called_out": False},
                        "spans": [
                            {
                                "role": "user",
                                "quote": "dont act on it yet just info",
                                "supports": ["dont_act_yet"],
                            }
                        ],
                        "confidence": {},
                        "abstain_reasons": [],
                        "novel_observations": [],
                    },
                    {
                        "window_id": "g2",
                        "turn_kind": ["correction"],
                        "user_stance": "correcting",
                        "agent_stance": "executing",
                        "prior_outcome": "rejected_redo",
                        "flags": {},
                        "spans": [
                            {
                                "role": "user",
                                "quote": "i said primary would be claude code not ONLY",
                                "supports": ["correction"],
                            }
                        ],
                        "confidence": {},
                        "abstain_reasons": [],
                        "novel_observations": [],
                    },
                    {
                        "window_id": "g3",
                        "turn_kind": ["soft_approval"],
                        "user_stance": "approving",
                        "agent_stance": "executing",
                        "prior_outcome": "accepted_continue",
                        "flags": {},
                        "spans": [
                            {
                                "role": "user",
                                "quote": "sounds good",
                                "supports": ["soft_approval"],
                            }
                        ],
                        "confidence": {},
                        "abstain_reasons": [],
                        "novel_observations": [],
                    },
                    {
                        "window_id": "g4",
                        "turn_kind": ["correction"],  # contamination
                        "user_stance": "correcting",
                        "agent_stance": "executing",
                        "prior_outcome": "abstain",
                        "flags": {},
                        "spans": [],
                        "confidence": {},
                        "abstain_reasons": [],
                        "novel_observations": [],
                    },
                    {
                        "window_id": "g5",
                        "turn_kind": ["human_task"],
                        "user_stance": "neutral",
                        "agent_stance": "pushing_back",
                        "prior_outcome": "abstain",
                        "flags": {},
                        "spans": [
                            {
                                "role": "assistant",
                                "quote": (
                                    "Pause before I set anything up — your proposed "
                                    "mechanism duplicates infrastructure we already agreed on."
                                ),
                                "supports": ["pushing_back"],
                            }
                        ],
                        "confidence": {},
                        "abstain_reasons": [],
                        "novel_observations": [],
                    },
                ]
            }
        wid = "g1"
        for candidate in ("g1", "g2", "g3", "g4", "g5"):
            if f'id="{candidate}"' in user or f"\"window_id\": \"{candidate}\"" in user:
                wid = candidate
                break
        table = {
            "g1": {
                "turn_kind": ["redirect_or_brake", "dont_act_yet"],
                "user_stance": "redirecting",
                "agent_stance": "executing",
                "spans": [
                    {
                        "role": "user",
                        "quote": "dont act on it yet just info",
                        "supports": ["dont_act_yet"],
                    }
                ],
            },
            "g2": {
                "turn_kind": ["correction"],
                "user_stance": "correcting",
                "agent_stance": "executing",
                "spans": [
                    {
                        "role": "user",
                        "quote": "i said primary would be claude code not ONLY",
                        "supports": ["correction"],
                    }
                ],
            },
            "g3": {
                "turn_kind": ["soft_approval"],
                "user_stance": "approving",
                "agent_stance": "executing",
                "spans": [
                    {
                        "role": "user",
                        "quote": "sounds good",
                        "supports": ["soft_approval"],
                    }
                ],
            },
            "g4": {
                "turn_kind": ["human_followup"],
                "user_stance": "neutral",
                "agent_stance": "executing",
                "spans": [],
            },
            "g5": {
                "turn_kind": ["human_task"],
                "user_stance": "neutral",
                "agent_stance": "pushing_back",
                "spans": [
                    {
                        "role": "assistant",
                        "quote": (
                            "Pause before I set anything up — your proposed mechanism "
                            "duplicates infrastructure we already agreed on."
                        ),
                        "supports": ["pushing_back"],
                    }
                ],
            },
        }
        base = table[wid]
        return {
            "window_id": wid,
            "turn_kind": base["turn_kind"],
            "user_stance": base["user_stance"],
            "agent_stance": base["agent_stance"],
            "prior_outcome": "abstain",
            "flags": {},
            "spans": base["spans"],
            "confidence": {},
            "abstain_reasons": [],
            "novel_observations": [],
        }

    ext = UxExtractor(client=ScriptedChatClient(responder), model="grok-4.5", batch_size=1)
    preds = ext.extract_many(contexts, batch_size=1)
    scores = score_predictions(preds, gold)
    rate, diffs = compare_batch_vs_single(ext, contexts, batch_size=5)
    gate = evaluate_gate(scores, batch_disagreement_rate=rate)
    return {
        "mode": "synthetic_labeled_audit",
        "note": (
            "Fixture gold + scripted model (not live Grok). "
            "Live corpus audit awaits XAI_API_KEY and completed hand labels."
        ),
        "per_label": {k: v.to_dict() for k, v in scores.items()},
        "batch_disagreement_rate": rate,
        "batch_diffs": diffs,
        "gate": gate.to_dict(),
    }


def main() -> int:
    out_dir = ROOT / ".research" / "extraction-verification"
    out_dir.mkdir(parents=True, exist_ok=True)

    db = DEFAULT_DB_PATH
    conn = connect(db)
    init_db(conn)
    report, run_id = run_deterministic_phase(conn)
    triage = report.to_dict()
    triage_path = out_dir / "triage_report.json"
    triage_path.write_text(
        json.dumps({"det_run_id": run_id, "triage": triage}, indent=2),
        encoding="utf-8",
    )

    pack_path = out_dir / "audit_pack_unlabeled.jsonl"
    ids = build_audit_pack(conn, pack_path, n=100, seed=42, ux_only=True)

    fixture_metrics = _fixture_audit_metrics()
    metrics_path = out_dir / "audit_fixture_metrics.json"
    metrics_path.write_text(json.dumps(fixture_metrics, indent=2), encoding="utf-8")

    summary = {
        "det_run_id": run_id,
        "triage": triage,
        "audit_pack": str(pack_path),
        "audit_pack_n": len(ids),
        "fixture_audit": fixture_metrics,
        "full_ux_run": "not_executed",
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
