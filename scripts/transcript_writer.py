"""Durable, folder-scoped persistence of each pipeline stage's real output,
written incrementally as soon as its stage completes - so a mid-run crash or
wall-clock timeout still leaves everything that *did* finish on disk. Today
`pipeline_runner.py`/`debate.py` only ever persist `run_status.json` (status
+ cost) plus, conditionally, `grounding.md`; the actual Stage 1 drafts,
Stage 2 peer reviews, synthesis, and revision outcomes existed only in
memory/stdout and were lost the moment the process exited.

Each function here is a small, pure, dependency-free write: given the
already-created `output_dir` (guaranteed to exist by
`pipeline_runner.py`'s `make_output_dir`) and the data one stage produced,
render a human-legible Markdown file and return the path written. No
atomic-temp-rename dance is required (unlike `run_status.json`, which is
read back mid-run) - these are write-once-per-stage, append-never files.

Contract: docs/specs/durable-persistence-contract.md.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def write_stage1_transcripts(output_dir: Path, stage1_results: list[dict]) -> Path:
    """Writes stage1_transcripts.md: one section per model, its raw
    response verbatim, in the order given. Returns the path written."""
    path = output_dir / "stage1_transcripts.md"
    lines = ["# Stage 1 Transcripts", ""]

    if not stage1_results:
        lines.append("No models responded to this query (no models responded).")
    else:
        for entry in stage1_results:
            model = entry.get("model", "unknown")
            response = entry.get("response", "")
            lines.append(f"## {model}")
            lines.append("")
            lines.append(response)
            lines.append("")

    path.write_text("\n".join(lines) + "\n")
    return path


def _ranking_score(entry: dict) -> Any:
    """Best available score field on an aggregate_rankings entry - the
    fields calculate_aggregate_rankings actually populates vary by mode
    (borda_score in the normal multi-model path, average_score in some
    degraded modes) so this tries each in order rather than assuming one."""
    for key in ("borda_score", "average_score", "score"):
        if key in entry and entry[key] is not None:
            return entry[key]
    return "N/A"


def _peer_review_notes(stage2_results: list[dict]) -> list[str]:
    """Renders each reviewer model's per-response notes, if its
    parsed_ranking carries any (ADR-016 rubric scoring's "notes" field) -
    reviewers/responses with no notes are simply omitted, never rendered as
    blank/None."""
    rendered: list[str] = []
    for entry in stage2_results:
        reviewer = entry.get("model", "unknown")
        evaluations = (entry.get("parsed_ranking") or {}).get("evaluations") or {}
        this_reviewer_notes = [
            f"- {label}: {details.get('notes')}"
            for label, details in evaluations.items()
            if isinstance(details, dict) and details.get("notes")
        ]
        if this_reviewer_notes:
            rendered.append(f"### {reviewer}")
            rendered.extend(this_reviewer_notes)
            rendered.append("")
    return rendered


def write_stage2_summary(
    output_dir: Path,
    stage2_results: list[dict],
    aggregate_rankings: list[dict],
    css: float | None,
    is_outlier: dict[str, bool],
) -> Path:
    """Writes stage2_summary.md: CSS, the ranking table (model/rank/score),
    each model's peer-review notes if present in stage2_results, and which
    models (if any) were flagged as outliers. css=None (e.g. single-model
    degraded mode) renders as 'N/A - single model, no peer review' rather
    than a blank/error. Returns the path written."""
    path = output_dir / "stage2_summary.md"
    lines = ["# Stage 2 Summary", ""]

    if css is None:
        lines.append("Consensus Strength Score (CSS): N/A - single model, no peer review")
    else:
        lines.append(f"Consensus Strength Score (CSS): {css:.3f}")
    lines.append("")

    lines.append("## Rankings")
    lines.append("")
    lines.append("| Model | Rank | Score |")
    lines.append("| --- | --- | --- |")
    for entry in aggregate_rankings:
        model = entry.get("model", "unknown")
        rank = entry.get("rank", "N/A")
        score = _ranking_score(entry)
        marker = " (OUTLIER)" if is_outlier.get(model) else ""
        lines.append(f"| {model}{marker} | {rank} | {score} |")
    lines.append("")

    outliers = [model for model, flagged in is_outlier.items() if flagged]
    if outliers:
        lines.append("## Outliers")
        lines.append("")
        lines.append(
            "The following model(s) were flagged as statistical outliers: "
            + ", ".join(outliers)
        )
        lines.append("")

    notes = _peer_review_notes(stage2_results)
    if notes:
        lines.append("## Peer Review Notes")
        lines.append("")
        lines.extend(notes)

    path.write_text("\n".join(lines) + "\n")
    return path


def write_synthesis(output_dir: Path, synthesis_text: str, chairman_model: str) -> Path:
    """Writes synthesis.md: the verbatim chairman synthesis, with the
    chairman model name as a header. Returns the path written."""
    path = output_dir / "synthesis.md"
    path.write_text(f"# Synthesis (chairman: {chairman_model})\n\n{synthesis_text}\n")
    return path


def _outcome_field(outcome: Any, name: str, default: Any = None) -> Any:
    """Duck-typed accessor - outcomes are RevisionOutcome dataclass
    instances in production (scripts/revision_round.py) but the contract
    types this list[Any], so a plain dict works too (e.g. in tests)."""
    if isinstance(outcome, dict):
        return outcome.get(name, default)
    return getattr(outcome, name, default)


def write_revision_outcomes(output_dir: Path, outcomes: list[Any]) -> Path:
    """Writes revision_outcomes.md: one section per model's revision
    outcome (revised text + cited fact id, or 'not revising'). Only ever
    called when Stage 2.75 actually ran - the caller skips calling this at
    all when Stage 2.75 didn't fire, rather than this function handling an
    empty-outcomes case that shouldn't occur. Returns the path written."""
    path = output_dir / "revision_outcomes.md"
    lines = ["# Stage 2.75 Revision Outcomes", ""]

    for outcome in outcomes:
        model = _outcome_field(outcome, "model", "unknown")
        # Mutation-testing note (2026-08-13): `default=False` vs the
        # implicit `default=None` (dropping the arg, or passing None
        # explicitly) is a true equivalent mutant - `accepted`'s only
        # consumer is the `if accepted:` truthiness check directly below,
        # and False/None are both falsy there; no other code path sees this
        # value. Verified by direct execution (mutmut run, 2 survivors,
        # traced by hand).
        accepted = _outcome_field(outcome, "accepted", False)
        lines.append(f"## {model}")
        lines.append("")
        if accepted:
            cited_fact_id = _outcome_field(outcome, "cited_fact_id")
            revised_text = _outcome_field(outcome, "revised_text", "")
            lines.append(f"Cited fact: {cited_fact_id}")
            lines.append("")
            lines.append(revised_text)
        else:
            lines.append("not revising")
        lines.append("")

    path.write_text("\n".join(lines) + "\n")
    return path
