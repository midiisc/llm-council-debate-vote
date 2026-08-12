"""ADR-029 audition-state adoption (Contract 5). Tracks each configured
council model's SHADOW/PROBATION/EVALUATION/FULL/QUARANTINE lifecycle state
using `llm-council-core==0.40.1`'s own already-shipped ADR-029 primitives
(`llm_council.audition.types.evaluate_state_transition`/
`record_session_result`, pure functions, confirmed by direct source read —
no I/O, no global state) instead of building a parallel confidence-tier
system by hand.

This module never wires audition state to actual model selection,
exclusion, or council composition — `evaluate_state_transition`'s output
(a *proposed* transition) is surfaced as one more line in the `scorecard`
CLI report, exactly as non-prescriptive as Contract 3's own design. A
proposed transition is never auto-applied; the persisted state on disk
only ever changes via `record_session_result`'s own session-count/failure
bookkeeping, never via the proposed-transition value itself.

Complementary to, not a replacement for, `scorecard.py`'s existing
`confidence_tier` (a pure session-count bucket) — this adds a *new*
`audition.jsonl` log with consecutive-failure tracking, quarantine, and
quality-percentile gating, none of which Contract 3 already covers.

Contract: docs/specs/custom-scripts-contracts.md, Contract 5.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from llm_council.audition.store import append_audition_record, read_audition_records
from llm_council.audition.types import (
    AuditionCriteria,
    AuditionState,
    AuditionStatus,
    evaluate_state_transition,
    record_session_result,
)

# ADR-029's own defaults (shadow_min_sessions=10, shadow_min_days=3,
# quarantine_cooldown_hours=24, ...) — this project has no reason to
# override them; adopting the package's own criteria is the whole point of
# using its primitives directly instead of re-deriving thresholds by hand.
_DEFAULT_CRITERIA = AuditionCriteria()

_FORBIDDEN_LANGUAGE_HEADER = (
    "Audition status (informational only - state changes are a human "
    "decision, never auto-applied):"
)


def default_audition_path(cwd: Path) -> Path:
    """cwd / "council-runs" / "audition.jsonl" - never ~/.llm-council/,
    matching Contract 3's `default_scorecard_path`'s own guarantee."""
    return cwd / "council-runs" / "audition.jsonl"


def get_or_init_status(model_id: str, path: Path) -> AuditionStatus:
    """Most recent record for model_id per `read_audition_records`'s own
    "most recent per model" contract, or a fresh SHADOW status with
    session_count=0 if none exists yet. Never raises for a first-ever
    model or a not-yet-existent file (`read_audition_records` already
    returns [] for a missing path)."""
    records = read_audition_records(str(path), model_id=model_id)
    if records:
        return records[0]
    return AuditionStatus(model_id=model_id, state=AuditionState.SHADOW, session_count=0)


def quality_percentile_from_rankings(
    model_id: str, aggregate_rankings: list[dict]
) -> Optional[float]:
    """Percentile rank (0.0-1.0) of model_id's borda_score among all models
    present in aggregate_rankings, or None if model_id isn't present (e.g.
    it failed to respond this session).

    Implemented as a cumulative-distribution-style percentile: the fraction
    of entries (including itself) whose borda_score is <= the model's own
    score. This is monotonic in borda_score (a strictly higher score can
    never map to a strictly lower percentile), always lands in (0.0, 1.0]
    for a present model, and gives tied scores an identical percentile -
    the only universal invariants the contract specifies; no single exact
    formula is mandated beyond those.
    """
    if not aggregate_rankings:
        return None

    scores_by_model = {r["model"]: r.get("borda_score", 0.0) for r in aggregate_rankings}
    if model_id not in scores_by_model:
        return None

    target_score = scores_by_model[model_id]
    total = len(scores_by_model)
    at_or_below = sum(1 for score in scores_by_model.values() if score <= target_score)
    return at_or_below / total


@dataclass
class AuditionUpdate:
    status: AuditionStatus  # the new, persisted status
    proposed_transition: Optional[AuditionState]  # what evaluate_state_transition suggested, if anything


def record_session_for_model(
    model_id: str,
    participated: bool,
    aggregate_rankings: list[dict],
    path: Path,
) -> AuditionUpdate:
    """Reads the model's prior status, applies one session's outcome via
    the package's own `record_session_result` (session_count +1 always;
    consecutive_failures resets to 0 on participation, +1 otherwise),
    derives a fresh quality_percentile only when the model actually
    participated (no ranking data exists for a model that didn't
    respond), persists the result, and surfaces (never applies) any
    proposed state transition."""
    current = get_or_init_status(model_id, path)
    updated = record_session_result(current, success=participated)

    if participated:
        updated.quality_percentile = quality_percentile_from_rankings(
            model_id, aggregate_rankings
        )

    proposed_transition = evaluate_state_transition(updated, _DEFAULT_CRITERIA)

    # `updated.state` was never touched by record_session_result or the
    # evaluate_state_transition call above - only the session/failure
    # bookkeeping changed, so persisting `updated` here can never write a
    # proposed-but-not-applied transition to disk.
    append_audition_record(updated, str(path))

    return AuditionUpdate(status=updated, proposed_transition=proposed_transition)


def record_session_for_all_models(
    council_models: list[str],
    stage1_results: list[dict],
    aggregate_rankings: list[dict],
    path: Path,
) -> list[AuditionUpdate]:
    """One `record_session_for_model` call per configured council model -
    a model that failed to respond still gets a failure-recorded entry,
    never silently skipped."""
    participated_models = {r["model"] for r in stage1_results}
    updates = []
    for model_id in council_models:
        participated = model_id in participated_models
        updates.append(
            record_session_for_model(
                model_id=model_id,
                participated=participated,
                aggregate_rankings=aggregate_rankings if participated else [],
                path=path,
            )
        )
    return updates


def render_audition_section(updates_or_statuses: list) -> str:
    """Plain-language block for scorecard's CLI report: one line per model,
    state + session_count + (if a transition was proposed this session)
    "would move to <STATE> next session" - never "should"/"recommend".
    Accepts either `AuditionStatus` entries or `AuditionUpdate` entries
    (duck-typed on the presence of `.proposed_transition`) so callers can
    pass either the raw statuses or the richer updates from
    `record_session_for_all_models` directly."""
    if not updates_or_statuses:
        return "No audition data recorded yet."

    lines = [_FORBIDDEN_LANGUAGE_HEADER]
    for item in updates_or_statuses:
        if isinstance(item, AuditionUpdate):
            status = item.status
            transition = item.proposed_transition
        else:
            status = item
            transition = None

        line = (
            f"  {status.model_id}: {status.state.value} "
            f"(sessions={status.session_count}, consecutive_failures={status.consecutive_failures})"
        )
        if transition is not None:
            line += f" - would move to {transition.value} next session"
        lines.append(line)

    return "\n".join(lines)
