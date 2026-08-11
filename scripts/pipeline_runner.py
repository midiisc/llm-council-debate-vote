"""Orchestrates Stage 0.5 (grounding) -> Stage 1-3.5 (council) -> Stage 2.75
(conditional revision) -> scorecard logging, folder-scoped.

Contract: docs/specs/pipeline-runner-contract.md.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from scripts.grounding_pass import (
    Claim,
    Evidence,
    TaggedClaim,
    parse_claims,
    run_grounding_pass,
)
from scripts.revision_round import ModelAnswer, run_revision_round, should_trigger_revision
from scripts.scorecard import ScorecardRecord, append_record, default_scorecard_path

RUBRIC_DIMENSIONS = ("accuracy", "relevance", "completeness", "conciseness", "clarity")

FetchEvidenceFn = Callable[[list[Claim]], Awaitable[dict[str, list[Evidence]]]]
CouncilFn = Callable[[str], Awaitable[tuple[list, list, dict, dict]]]
QueryModelFn = Callable[[str, str], Awaitable[tuple[str, float]]]


@dataclass
class PipelineConfig:
    topic_label: str
    query: str
    raw_claims_text: str = ""
    max_cost_usd: Optional[float] = None
    output_root: Optional[Path] = None


@dataclass
class PipelineResult:
    output_dir: Path
    css: float
    revision_triggered: bool
    revision_skipped_for_cost: bool
    total_cost_usd: float
    scorecard_appended: bool
    synthesis: str
    # True if total_cost_usd (including any real revision spend) ended up
    # over max_cost_usd. The pre-revision check (revision_skipped_for_cost)
    # can still prevent a revision round from starting; this field exists
    # because that check is structurally blind to the revision round's own
    # cost, known only after it runs - always False when max_cost_usd is None.
    cost_ceiling_exceeded: bool = False


def slugify(topic_label: str) -> str:
    lowered = topic_label.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slug.strip("-")


def make_output_dir(output_root: Path, topic_label: str, timestamp: str) -> Path:
    slug = slugify(topic_label)
    out = Path(output_root) / f"{timestamp}-{slug}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_run_status(output_dir: Path, status: str, **extra) -> None:
    """Atomic write (temp file + rename) so a crash mid-write never leaves a
    half-written run_status.json - a reader always sees either the previous
    complete status or the new one, never a torn file."""
    import json

    payload = {"status": status, **extra}
    tmp_path = output_dir / "run_status.json.tmp"
    final_path = output_dir / "run_status.json"
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.rename(final_path)


def _rubric_scores_for_model(
    model: str, stage2_results: list[dict], label_to_model: dict
) -> dict[str, list[float]]:
    """All reviewers' per-dimension scores for `model`'s response."""
    label_for_model = None
    for label, entry in label_to_model.items():
        if entry["model"] == model:
            label_for_model = label
            break
    if label_for_model is None:
        return {}

    per_dim: dict[str, list[float]] = {dim: [] for dim in RUBRIC_DIMENSIONS}
    for reviewer_entry in stage2_results:
        evaluations = reviewer_entry.get("parsed_ranking", {}).get("evaluations", {})
        scores = evaluations.get(label_for_model)
        if not scores:
            continue
        for dim in RUBRIC_DIMENSIONS:
            if dim in scores:
                per_dim[dim].append(scores[dim])
    return per_dim


def build_critique_from_rubric(
    model: str, stage2_results: list[dict], label_to_model: dict
) -> str:
    per_dim = _rubric_scores_for_model(model, stage2_results, label_to_model)
    averages = {
        dim: statistics.mean(values) for dim, values in per_dim.items() if values
    }
    if not averages:
        return "No peer scores available for this response."

    weakest_dim = min(averages, key=lambda d: averages[d])
    parts = [f"{dim}: {averages[dim]:.1f}/10" for dim in RUBRIC_DIMENSIONS if dim in averages]
    n_reviewers = max((len(v) for v in per_dim.values() if v), default=0)
    return (
        f"Reviewers scored your response ({n_reviewers} reviewer(s)) — "
        + ", ".join(parts)
        + f". Weakest dimension: {weakest_dim} ({averages[weakest_dim]:.1f})."
    )


def extract_rubric_scores_for_scorecard(
    stage2_results: list[dict], label_to_model: dict
) -> dict[str, dict[str, float]]:
    scores: dict[str, dict[str, float]] = {}
    for label, entry in label_to_model.items():
        model = entry["model"]
        per_dim = _rubric_scores_for_model(model, stage2_results, label_to_model)
        scores[model] = {
            dim: statistics.mean(values) for dim, values in per_dim.items() if values
        }
    return scores


def _compute_ranks(aggregate_rankings: list[dict]) -> dict[str, int]:
    return {entry["model"]: entry["rank"] for entry in aggregate_rankings}


def _compute_outliers(aggregate_rankings: list[dict]) -> dict[str, bool]:
    scores = [entry["borda_score"] for entry in aggregate_rankings]
    if len(scores) < 2:
        return {entry["model"]: False for entry in aggregate_rankings}
    median = statistics.median(scores)
    stdev = statistics.pstdev(scores)
    threshold = median - 1.5 * stdev
    return {
        entry["model"]: entry["borda_score"] < threshold for entry in aggregate_rankings
    }


async def run_pipeline(
    config: PipelineConfig,
    fetch_evidence: FetchEvidenceFn,
    council_fn: CouncilFn,
    query_model: QueryModelFn,
) -> PipelineResult:
    from datetime import datetime, timezone

    output_root = config.output_root if config.output_root is not None else Path.cwd() / "council-runs"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    output_dir = make_output_dir(output_root, config.topic_label, timestamp)
    _write_run_status(output_dir, "running")

    cost_so_far = 0.0
    try:
        verified_facts: list[TaggedClaim] = []
        if config.raw_claims_text.strip():
            claims = parse_claims(config.raw_claims_text)
            evidence_map = await fetch_evidence(claims)
            input_path = output_dir / "_raw_claims.txt"
            input_path.write_text(config.raw_claims_text)
            run_grounding_pass(input_path, evidence_map, output_dir)
            input_path.unlink()

        stage1_results, stage2_results, stage3_result, metadata = await council_fn(config.query)

        css = metadata["quality_metrics"]["core"]["consensus_strength"]
        aggregate_rankings = metadata["aggregate_rankings"]
        label_to_model = metadata["label_to_model"]
        usage = metadata["usage"]
        stage1to3_cost = usage["total"]["cost_usd"]
        cost_so_far = stage1to3_cost

        revision_triggered = False
        revision_skipped_for_cost = False
        revision_cost = 0.0
        synthesis = stage3_result["response"]

        if should_trigger_revision(css):
            if config.max_cost_usd is not None and stage1to3_cost >= config.max_cost_usd:
                revision_skipped_for_cost = True
            else:
                answers = [
                    ModelAnswer(
                        model=s1["model"],
                        original_text=s1["response"],
                        critique=build_critique_from_rubric(
                            s1["model"], stage2_results, label_to_model
                        ),
                    )
                    for s1 in stage1_results
                ]
                outcomes = await run_revision_round(css, answers, verified_facts, query_model)
                revision_cost = sum(o.cost_usd for o in outcomes)
                cost_so_far += revision_cost
                revision_triggered = True

        rubric_scores = extract_rubric_scores_for_scorecard(stage2_results, label_to_model)
        ranks = _compute_ranks(aggregate_rankings)
        is_outlier = _compute_outliers(aggregate_rankings)
        cost_usd = {model: bucket["cost_usd"] for model, bucket in usage["by_model"].items()}

        record = ScorecardRecord(
            timestamp=timestamp,
            topic_label=config.topic_label,
            css=css,
            rubric_scores=rubric_scores,
            ranks=ranks,
            is_outlier=is_outlier,
            cost_usd=cost_usd,
        )
        scorecard_path = (
            (config.output_root / "scorecard.jsonl")
            if config.output_root is not None
            else default_scorecard_path(Path.cwd())
        )
        append_record(record, scorecard_path)

        total_cost_usd = stage1to3_cost + revision_cost
        cost_ceiling_exceeded = (
            config.max_cost_usd is not None and total_cost_usd > config.max_cost_usd
        )
    except Exception as e:
        _write_run_status(output_dir, "failed", error=str(e), cost_so_far_usd=cost_so_far)
        raise

    _write_run_status(output_dir, "complete", total_cost_usd=total_cost_usd)

    return PipelineResult(
        output_dir=output_dir,
        css=css,
        revision_triggered=revision_triggered,
        revision_skipped_for_cost=revision_skipped_for_cost,
        total_cost_usd=total_cost_usd,
        scorecard_appended=True,
        synthesis=synthesis,
        cost_ceiling_exceeded=cost_ceiling_exceeded,
    )


def exit_code_for_result(result: PipelineResult) -> int:
    """Maps a PipelineResult to the CLI's exit-code contract (AC16-20 in
    docs/specs/pipeline-runner-contract.md). Ceiling-exceeded (3) outranks
    revision-skipped-for-cost (2), which outranks plain success (0)."""
    if result.cost_ceiling_exceeded:
        return 3
    if result.revision_skipped_for_cost:
        return 2
    return 0


def _build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="llm-council-pipeline",
        description="Run the full grounded council pipeline: Stage 0.5 -> 1-3.5 -> [2.75] -> scorecard.",
    )
    parser.add_argument("--topic-label", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--claims-file", type=Path, default=None)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--output-root", type=Path, default=None)
    return parser


def main() -> None:
    import asyncio
    import sys

    from llm_council.council import run_full_council

    from scripts.live_adapters import real_fetch_evidence, real_query_model

    args = _build_arg_parser().parse_args()

    async def council_fn(query: str):
        return await run_full_council(query, models=None)

    config = PipelineConfig(
        topic_label=args.topic_label,
        query=args.query,
        raw_claims_text=args.claims_file.read_text() if args.claims_file else "",
        max_cost_usd=args.max_cost_usd,
        output_root=args.output_root,
    )

    try:
        result = asyncio.run(
            run_pipeline(config, real_fetch_evidence, council_fn, real_query_model)
        )
    except Exception as e:
        print(f"Pipeline run failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Output: {result.output_dir}")
    print(f"CSS: {result.css:.3f}")
    print(f"Total cost: ${result.total_cost_usd:.4f}")
    print(result.synthesis)

    if result.cost_ceiling_exceeded:
        print(
            f"WARNING: total cost ${result.total_cost_usd:.4f} exceeded "
            f"--max-cost-usd {config.max_cost_usd}",
            file=sys.stderr,
        )
        sys.exit(3)
    if result.revision_skipped_for_cost:
        print(
            "WARNING: revision round was skipped because starting it would "
            f"have exceeded --max-cost-usd {config.max_cost_usd}",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
