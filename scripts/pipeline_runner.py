"""Orchestrates Stage 0.5 (grounding) -> Stage 1-3.5 (council) -> Stage 2.75
(conditional revision) -> Stage 4 (completeness check) -> scorecard logging,
folder-scoped.

Contract: docs/specs/pipeline-runner-contract.md.
"""
from __future__ import annotations

import asyncio
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from scripts.completeness_check import check_fact_completeness
from scripts.grounding_pass import (
    Claim,
    Evidence,
    TaggedClaim,
    parse_claims,
    run_grounding_pass,
    tag_claim,
)
from scripts.audition_tracking import default_audition_path, record_session_for_all_models
from scripts.revision_round import ModelAnswer, run_revision_round, should_trigger_revision
from scripts.scorecard import ScorecardRecord, append_record, default_scorecard_path

RUBRIC_DIMENSIONS = ("accuracy", "relevance", "completeness", "conciseness", "clarity")

# 20min (1200s) default total wall-clock ceiling for a full run (Stage
# 0.5 -> 4). An explicit, always-on backstop - unlike max_cost_usd there is
# no None-by-default option, since the entire point is that nothing bounded
# runtime before this amendment. See docs/specs/pipeline-runner-contract.md,
# "Amendment (2026-08-12): timeout-aware council_fn + wall-clock ceiling".
DEFAULT_MAX_WALL_CLOCK_SECONDS = 1200.0

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
    # Always-on backstop (never None) - see DEFAULT_MAX_WALL_CLOCK_SECONDS.
    max_wall_clock_seconds: float = DEFAULT_MAX_WALL_CLOCK_SECONDS
    # Duplicates live_adapters.COMPLETENESS_CHECK_MODEL's value as a plain
    # string literal (dataclass defaults must be literals) - kept separate
    # so this module never imports live_adapters, the same testability
    # boundary the module docstring already describes for fetch_evidence/
    # council_fn/query_model.
    completeness_check_model: str = "google/gemini-3.6-flash"


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
    # Stage 4: ids of VERIFIED/CONTRADICTED facts the completeness check
    # judged NOT reflected in `synthesis`. Empty if no grounding happened,
    # the check was skipped for cost, or nothing was dropped.
    dropped_facts: list[str] = field(default_factory=list)
    completeness_check_skipped_for_cost: bool = False
    # True only when the completeness check actually ran (spent money) but
    # its response couldn't be parsed - dropped_facts==[] in that case is
    # NOT "verified clean," it's "undetermined." Never True when the check
    # didn't run at all (no grounding, or skipped for cost).
    completeness_check_parse_failed: bool = False
    # One line per stage transition, in order - what actually ran, what
    # was skipped and why, how many models/outcomes were involved. Read
    # top to bottom to see exactly what happened in a run without
    # reverse-engineering it from the other fields.
    debug_log: list[str] = field(default_factory=list)


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
    # Contract 5 (audition_tracking.py) - the full configured council model
    # list, so a model that fails to respond still gets a failure recorded
    # rather than silently vanishing from tracking. None (default) skips
    # audition tracking entirely - this stays an additive, optional feature
    # for every existing PipelineConfig/run_pipeline call site.
    council_models: Optional[list[str]] = None,
) -> PipelineResult:
    from datetime import datetime, timezone

    output_root = config.output_root if config.output_root is not None else Path.cwd() / "council-runs"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    output_dir = make_output_dir(output_root, config.topic_label, timestamp)
    _write_run_status(output_dir, "running")

    cost_so_far = 0.0
    debug_log: list[str] = []

    async def _run_stages():
        nonlocal cost_so_far
        verified_facts: list[TaggedClaim] = []
        if config.raw_claims_text.strip():
            claims = parse_claims(config.raw_claims_text)
            evidence_map = await fetch_evidence(claims)
            input_path = output_dir / "_raw_claims.txt"
            input_path.write_text(config.raw_claims_text)
            run_grounding_pass(input_path, evidence_map, output_dir)
            input_path.unlink()
            tagged = [tag_claim(c, evidence_map.get(c.id, [])) for c in claims]
            verified_facts = [tc for tc in tagged if tc.tag in ("VERIFIED", "CONTRADICTED")]
            n_verified = sum(1 for tc in tagged if tc.tag == "VERIFIED")
            n_contradicted = sum(1 for tc in tagged if tc.tag == "CONTRADICTED")
            n_unverifiable = sum(1 for tc in tagged if tc.tag == "UNVERIFIABLE")
            debug_log.append(
                f"Stage 0.5: grounding ran, {len(claims)} claim(s) "
                f"({n_verified} verified, {n_contradicted} contradicted, "
                f"{n_unverifiable} unverifiable)"
            )
        else:
            debug_log.append("Stage 0.5: skipped (no raw_claims_text)")

        stage1_results, stage2_results, stage3_result, metadata = await council_fn(config.query)

        debug_log.append(f"Stage 1-3.5: council returned {len(stage1_results)} model response(s)")
        if len(stage1_results) < 2:
            debug_log.append(
                f"WARNING: only {len(stage1_results)} model(s) participated - "
                "this is not multi-agent debate"
            )

        css = metadata["quality_metrics"]["core"]["consensus_strength"]
        aggregate_rankings = metadata["aggregate_rankings"]
        label_to_model = metadata["label_to_model"]
        usage = metadata["usage"]
        stage1to3_cost = usage["total"]["cost_usd"]
        cost_so_far = stage1to3_cost
        debug_log.append(f"Stage 2.5: CSS={css:.3f}")

        revision_triggered = False
        revision_skipped_for_cost = False
        revision_cost = 0.0
        synthesis = stage3_result["response"]

        if should_trigger_revision(css):
            if config.max_cost_usd is not None and stage1to3_cost >= config.max_cost_usd:
                revision_skipped_for_cost = True
                debug_log.append("Stage 2.75: skipped (would exceed max_cost_usd)")
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
                outcomes = await run_revision_round(
                    css, answers, verified_facts, query_model, source_document=config.query
                )
                revision_cost = sum(o.cost_usd for o in outcomes)
                cost_so_far += revision_cost
                revision_triggered = True
                n_accepted = sum(1 for o in outcomes if o.accepted)
                debug_log.append(
                    f"Stage 2.75: revision triggered, {len(outcomes)} model(s) "
                    f"responded, {n_accepted} accepted"
                )
        else:
            debug_log.append(f"Stage 2.75: skipped (CSS {css:.3f} >= threshold)")

        debug_log.append(f"Stage 3: synthesis produced by {stage3_result.get('model', 'unknown')}")

        dropped_facts: list[str] = []
        completeness_check_skipped_for_cost = False
        completeness_check_parse_failed = False
        completeness_check_cost = 0.0
        if not verified_facts:
            debug_log.append("Stage 4: skipped (no verified facts)")
        elif config.max_cost_usd is not None and cost_so_far >= config.max_cost_usd:
            completeness_check_skipped_for_cost = True
            debug_log.append("Stage 4: skipped (would exceed max_cost_usd)")
        else:
            dropped_facts, completeness_check_cost, parse_ok = await check_fact_completeness(
                verified_facts, synthesis, config.completeness_check_model, query_model
            )
            cost_so_far += completeness_check_cost
            completeness_check_parse_failed = not parse_ok
            if parse_ok:
                debug_log.append(f"Stage 4: ran, parse succeeded, {len(dropped_facts)} fact(s) dropped")
            else:
                debug_log.append(
                    "Stage 4: ran, parse FAILED - completeness is UNDETERMINED, not verified"
                )

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

        if council_models is not None:
            # Best-effort, per Contract 5: audition tracking must never
            # fail a pipeline run that otherwise succeeded. Informational
            # only - see audition_tracking.py's module docstring for why
            # this never touches which models actually get queried.
            try:
                audition_path = (
                    (config.output_root / "audition.jsonl")
                    if config.output_root is not None
                    else default_audition_path(Path.cwd())
                )
                record_session_for_all_models(
                    council_models, stage1_results, aggregate_rankings, audition_path
                )
                debug_log.append("Audition tracking: recorded")
            except Exception as e:
                debug_log.append(f"Audition tracking: failed non-fatally ({e})")

        total_cost_usd = stage1to3_cost + revision_cost + completeness_check_cost
        cost_ceiling_exceeded = (
            config.max_cost_usd is not None and total_cost_usd > config.max_cost_usd
        )

        return (
            css,
            revision_triggered,
            revision_skipped_for_cost,
            total_cost_usd,
            synthesis,
            cost_ceiling_exceeded,
            dropped_facts,
            completeness_check_skipped_for_cost,
            completeness_check_parse_failed,
        )

    try:
        (
            css,
            revision_triggered,
            revision_skipped_for_cost,
            total_cost_usd,
            synthesis,
            cost_ceiling_exceeded,
            dropped_facts,
            completeness_check_skipped_for_cost,
            completeness_check_parse_failed,
        ) = await asyncio.wait_for(_run_stages(), timeout=config.max_wall_clock_seconds)
    except asyncio.TimeoutError:
        # asyncio.TimeoutError is TimeoutError itself on Python 3.11+ (the
        # two names are aliased) - main() catches plain TimeoutError to map
        # this to its own exit code (4), distinct from the generic exit(1).
        error_msg = f"exceeded max_wall_clock_seconds ({config.max_wall_clock_seconds}s)"
        _write_run_status(output_dir, "failed", error=error_msg, cost_so_far_usd=cost_so_far)
        raise TimeoutError(error_msg) from None
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
        dropped_facts=dropped_facts,
        completeness_check_skipped_for_cost=completeness_check_skipped_for_cost,
        completeness_check_parse_failed=completeness_check_parse_failed,
        debug_log=debug_log,
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
        description="Run the full grounded council pipeline: Stage 0.5 -> 1-3.5 -> [2.75] -> 4 -> scorecard.",
    )
    parser.add_argument("--topic-label", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--claims-file", type=Path, default=None)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument(
        "--max-wall-clock-seconds", type=float, default=DEFAULT_MAX_WALL_CLOCK_SECONDS
    )
    return parser


def main() -> None:
    import sys

    from scripts.council_adapter import run_council_with_timeouts
    from scripts.live_adapters import real_fetch_evidence, real_query_model

    from llm_council.unified_config import get_config

    args = _build_arg_parser().parse_args()

    async def council_fn(query: str):
        return await run_council_with_timeouts(query)

    council_models = get_config().council.models

    config = PipelineConfig(
        topic_label=args.topic_label,
        query=args.query,
        raw_claims_text=args.claims_file.read_text() if args.claims_file else "",
        max_cost_usd=args.max_cost_usd,
        output_root=args.output_root,
        max_wall_clock_seconds=args.max_wall_clock_seconds,
    )

    try:
        result = asyncio.run(
            run_pipeline(
                config,
                real_fetch_evidence,
                council_fn,
                real_query_model,
                council_models=council_models,
            )
        )
    except TimeoutError as e:
        # run_pipeline raises a plain TimeoutError (asyncio.TimeoutError is
        # the same class on Python 3.11+) when config.max_wall_clock_seconds
        # is exceeded - mapped to its own exit code (4), distinct from the
        # generic exit(1) below, per docs/specs/pipeline-runner-contract.md,
        # "Amendment (2026-08-12): timeout-aware council_fn + wall-clock
        # ceiling".
        print(f"Pipeline run failed: {e}", file=sys.stderr)
        sys.exit(4)
    except Exception as e:
        print(f"Pipeline run failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Output: {result.output_dir}")
    print(f"CSS: {result.css:.3f}")
    print(f"Total cost: ${result.total_cost_usd:.4f}")
    print(result.synthesis)

    print("Debug log:", file=sys.stderr)
    for line in result.debug_log:
        print(f"  {line}", file=sys.stderr)

    if result.dropped_facts:
        print(
            "WARNING: the final synthesis does not appear to address these "
            f"verified facts: {', '.join(result.dropped_facts)}",
            file=sys.stderr,
        )
    if result.completeness_check_parse_failed:
        print(
            "WARNING: the Stage 4 completeness check ran but its response "
            "could not be understood - completeness is UNDETERMINED, not "
            "verified. dropped_facts=[] here does NOT mean nothing is missing.",
            file=sys.stderr,
        )
    if result.completeness_check_skipped_for_cost:
        print(
            "WARNING: the Stage 4 completeness check was skipped because "
            f"running it would have exceeded --max-cost-usd {config.max_cost_usd}",
            file=sys.stderr,
        )

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
