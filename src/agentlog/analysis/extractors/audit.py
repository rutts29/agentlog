from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from agentlog.analysis.extractors.models import UxObservation, WindowContext
from agentlog.analysis.extractors.taxonomy import AUDIT_GATES
from agentlog.analysis.extractors.ux_extractor import UxExtractor
from agentlog.analysis.extractors.window_context import truncate_for_ux
from agentlog.safety.write_guard import assert_writable


@dataclass
class LabelScore:
    label: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    abstain_gold_pos: int = 0

    @property
    def precision(self) -> float | None:
        denom = self.tp + self.fp
        return (self.tp / denom) if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.tp + self.fn
        return (self.tp / denom) if denom else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": self.precision,
            "recall": self.recall,
        }


@dataclass
class AuditGateResult:
    passed: bool
    per_label: dict[str, LabelScore] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    batch_disagreement_rate: float | None = None
    recommended_batch_size: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "per_label": {k: v.to_dict() for k, v in self.per_label.items()},
            "failures": self.failures,
            "batch_disagreement_rate": self.batch_disagreement_rate,
            "recommended_batch_size": self.recommended_batch_size,
        }


def stratified_sample(
    contexts: list[WindowContext],
    *,
    n: int = 100,
    seed: int = 42,
) -> list[WindowContext]:
    """Stratify by harness then month-ish from session order; seeded."""
    rng = random.Random(seed)
    by_harness: dict[str, list[WindowContext]] = defaultdict(list)
    for ctx in contexts:
        by_harness[ctx.harness].append(ctx)
    harnesses = sorted(by_harness)
    if not harnesses or n <= 0:
        return []
    # Proportional allocation with at least 1 per non-empty harness when n allows.
    total = sum(len(v) for v in by_harness.values())
    alloc: dict[str, int] = {}
    remaining = n
    for i, h in enumerate(harnesses):
        if i == len(harnesses) - 1:
            alloc[h] = remaining
        else:
            share = max(1, round(n * len(by_harness[h]) / total)) if total else 0
            share = min(share, len(by_harness[h]), remaining - (len(harnesses) - i - 1))
            alloc[h] = max(0, share)
            remaining -= alloc[h]
    picked: list[WindowContext] = []
    for h in harnesses:
        pool = list(by_harness[h])
        rng.shuffle(pool)
        picked.extend(pool[: alloc[h]])
    rng.shuffle(picked)
    return picked[:n]


def emit_audit_pack(
    contexts: list[WindowContext],
    path: Path,
    *,
    n: int = 100,
    seed: int = 42,
) -> list[WindowContext]:
    """Write reviewable JSONL for hand labeling. Does not include model labels as gold."""
    sample = stratified_sample(contexts, n=n, seed=seed)
    path = assert_writable(path, purpose="audit pack")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ctx in sample:
            payload = truncate_for_ux(ctx)
            row = {
                "window_id": ctx.window_id,
                "harness": ctx.harness,
                "session_id": ctx.session_id,
                "payload": payload,
                "labels": {
                    "turn_kind": [],
                    "user_stance": None,
                    "agent_stance": None,
                    "prior_outcome": None,
                    "flags": {},
                    "notes": "",
                },
                "label_status": "unlabeled",
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return sample


def load_gold(path: Path) -> dict[str, dict[str, Any]]:
    gold: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("label_status") == "unlabeled":
                continue
            labels = row.get("labels") or row
            gold[str(row["window_id"])] = labels
    return gold


def _label_set(labels: dict[str, Any]) -> set[str]:
    kinds = set(labels.get("turn_kind") or [])
    if labels.get("agent_stance") == "pushing_back":
        kinds.add("pushing_back")
    if labels.get("user_stance") == "frustrated":
        kinds.add("frustrated")
    return {str(k) for k in kinds}


def score_predictions(
    predictions: Iterable[UxObservation],
    gold: dict[str, dict[str, Any]],
    *,
    labels: Iterable[str] | None = None,
) -> dict[str, LabelScore]:
    label_list = list(labels) if labels is not None else list(AUDIT_GATES)
    scores = {lab: LabelScore(label=lab) for lab in label_list}
    pred_by_id = {p.window_id: p for p in predictions}
    for wid, g in gold.items():
        gset = _label_set(g)
        pred = pred_by_id.get(wid)
        if pred is None:
            pset: set[str] = set()
            abstained = set()
        else:
            pset = set(pred.turn_kind)
            if pred.agent_stance == "pushing_back":
                pset.add("pushing_back")
            if pred.user_stance == "frustrated":
                pset.add("frustrated")
            abstained = set(pred.abstain_reasons)
        for lab in label_list:
            gpos = lab in gset
            # Correction/frustrated: only score non-abstained predictions for precision;
            # false negatives still count when gold is positive and model abstained/missed.
            if lab in ("correction", "frustrated"):
                pred_pos = lab in pset
                if any(lab in a for a in abstained) or (
                    pred is not None
                    and (
                        (lab == "correction" and pred.user_stance == "abstain")
                        or (lab == "frustrated" and pred.user_stance == "abstain")
                    )
                ):
                    if gpos and not pred_pos:
                        scores[lab].fn += 1
                        scores[lab].abstain_gold_pos += 1
                    continue
            else:
                pred_pos = lab in pset
            if pred_pos and gpos:
                scores[lab].tp += 1
            elif pred_pos and not gpos:
                scores[lab].fp += 1
            elif (not pred_pos) and gpos:
                scores[lab].fn += 1
    return scores


def evaluate_gate(
    scores: dict[str, LabelScore],
    *,
    batch_disagreement_rate: float | None = None,
    max_batch_disagreement: float = 0.05,
) -> AuditGateResult:
    failures: list[str] = []
    for lab, (pmin, rmin) in AUDIT_GATES.items():
        sc = scores.get(lab)
        if sc is None:
            continue
        # Skip labels with no gold support.
        if (sc.tp + sc.fn) == 0 and (sc.tp + sc.fp) == 0:
            continue
        if sc.precision is not None and sc.precision < pmin:
            failures.append(
                f"{lab} precision {sc.precision:.3f} < {pmin:.2f}"
            )
        if sc.recall is not None and sc.recall < rmin:
            failures.append(f"{lab} recall {sc.recall:.3f} < {rmin:.2f}")
    recommended = 1
    if batch_disagreement_rate is not None:
        if batch_disagreement_rate > max_batch_disagreement:
            failures.append(
                f"batch disagreement {batch_disagreement_rate:.3f} > "
                f"{max_batch_disagreement:.2f}; use batch_size=1"
            )
            recommended = 1
        else:
            recommended = 8
    return AuditGateResult(
        passed=not failures,
        per_label=scores,
        failures=failures,
        batch_disagreement_rate=batch_disagreement_rate,
        recommended_batch_size=recommended,
    )


def compare_batch_vs_single(
    extractor: UxExtractor,
    contexts: list[WindowContext],
    *,
    batch_size: int = 8,
) -> tuple[float, list[dict[str, Any]]]:
    """Return disagreement rate and per-window diffs."""
    if not contexts:
        return 0.0, []
    single = {o.window_id: o for o in extractor.extract_many(contexts, batch_size=1)}
    batched = {
        o.window_id: o for o in extractor.extract_many(contexts, batch_size=batch_size)
    }
    diffs: list[dict[str, Any]] = []
    disagree = 0
    for ctx in contexts:
        a = single[ctx.window_id]
        b = batched[ctx.window_id]
        a_key = (
            tuple(sorted(a.turn_kind)),
            a.user_stance,
            a.agent_stance,
            a.prior_outcome,
        )
        b_key = (
            tuple(sorted(b.turn_kind)),
            b.user_stance,
            b.agent_stance,
            b.prior_outcome,
        )
        if a_key != b_key:
            disagree += 1
            diffs.append(
                {
                    "window_id": ctx.window_id,
                    "single": {
                        "turn_kind": a.turn_kind,
                        "user_stance": a.user_stance,
                        "agent_stance": a.agent_stance,
                        "prior_outcome": a.prior_outcome,
                    },
                    "batched": {
                        "turn_kind": b.turn_kind,
                        "user_stance": b.user_stance,
                        "agent_stance": b.agent_stance,
                        "prior_outcome": b.prior_outcome,
                    },
                }
            )
    rate = disagree / len(contexts)
    return rate, diffs


def observation_fingerprint(obs: UxObservation) -> str:
    raw = json.dumps(
        {
            "turn_kind": sorted(obs.turn_kind),
            "user_stance": obs.user_stance,
            "agent_stance": obs.agent_stance,
            "prior_outcome": obs.prior_outcome,
            "flags": obs.flags.model_dump(),
        },
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:12]
