"""Orchestrates Stage 0.5 (grounding) -> Stage 1-3.5 (council) -> Stage 2.75
(conditional revision) -> Stage 4 (completeness check) -> scorecard logging,
folder-scoped.

Contract: docs/specs/pipeline-runner-contract.md.
"""
from __future__ import annotations

import asyncio
import re
import statistics
import time
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
from scripts.critique_round import run_critique_round, should_trigger_critique
from scripts.reasoning_graph import build_reasoning_graph, write_reasoning_graph_files
from scripts.revision_round import ModelAnswer, run_revision_round, should_trigger_revision
from scripts.scorecard import ScorecardRecord, append_record, default_scorecard_path
from scripts.transcript_writer import (
    write_revision_outcomes,
    write_stage1_transcripts,
    write_stage2_summary,
    write_synthesis,
)

RUBRIC_DIMENSIONS = ("accuracy", "relevance", "completeness", "conciseness", "clarity")

# 20min (1200s) default total wall-clock ceiling for a full run (Stage
# 0.5 -> 4). An explicit, always-on backstop - unlike max_cost_usd there is
# no None-by-default option, since the entire point is that nothing bounded
# runtime before this amendment. See docs/specs/pipeline-runner-contract.md,
# "Amendment (2026-08-12): timeout-aware council_fn + wall-clock ceiling".
DEFAULT_MAX_WALL_CLOCK_SECONDS = 1200.0

FetchEvidenceFn = Callable[[list[Claim]], Awaitable[dict[str, list[Evidence]]]]
# (user_query, verified_facts) -> (stage1_results, stage2_results, stage3_result,
# metadata) - Proposal A Contract 3 (docs/specs/proposal-a-reference-grounding-
# contract.md): verified_facts is threaded through so Stage 3's synthesis call
# can see Stage 0.5's already-verified facts, reusing the grounding-pass
# result computed earlier in _run_stages(), no re-computation.
CouncilFn = Callable[[str, list[TaggedClaim]], Awaitable[tuple[list, list, dict, dict]]]
QueryModelFn = Callable[[str, str], Awaitable[tuple[str, float]]]
# (model, prompt, effort) -> (text, cost) - docs/specs/reasoning-effort-
# wiring-contract.md, Contract 2. Strictly additive/optional: None keeps
# every existing call site and test byte-identical to before this contract.
ReasoningQueryModelFn = Callable[[str, str, str], Awaitable[tuple[str, float]]]


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
    completeness_check_model: str = "google/gemini-3.7-flash"


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
    # Stage 3.75 (docs/specs/stage-3-75-critique-contract.md).
    critique_triggered: bool = False
    critique_text: Optional[str] = None
    critique_skipped_for_cost: bool = False
    # Stage 5 (docs/specs/reasoning-graph-contract.md). None iff the stage
    # was skipped for any reason (reasoning_graph_skipped_reason then names why).
    reasoning_graph_path: Optional[Path] = None
    reasoning_graph_skipped_reason: Optional[str] = None
    reasoning_graph_dropped_count: Optional[dict] = None
    # One line per stage transition, in order - what actually ran, what
    # was skipped and why, how many models/outcomes were involved. Read
    # top to bottom to see exactly what happened in a run without
    # reverse-engineering it from the other fields.
    debug_log: list[str] = field(default_factory=list)


def slugify(topic_label: str) -> str:
    lowered = topic_label.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    # Mutation-testing note (2026-08-13): `strip("-")` vs `strip("XX-XX")`
    # (i.e. stripping the char set {'X', '-'}) is a true equivalent mutant -
    # `slug` can never contain an uppercase 'X' at this point: `.lower()`
    # above removes all uppercase, and the regex substitution above it
    # replaces every character outside [a-z0-9] with '-'. Verified by
    # direct execution (mutmut run, 1 survivor, traced by hand).
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


def _label_for_model(model: str, label_to_model: dict) -> Optional[str]:
    for label, entry in label_to_model.items():
        if entry["model"] == model:
            return label
    return None


def _rubric_scores_for_model(
    model: str, stage2_results: list[dict], label_to_model: dict
) -> dict[str, list[float]]:
    """All reviewers' per-dimension scores for `model`'s response."""
    label_for_model = _label_for_model(model, label_to_model)
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


def _rubric_notes_for_model(
    model: str, stage2_results: list[dict], label_to_model: dict
) -> list[str]:
    """docs/specs/human-debate-characteristics-contract.md, Contract 1.

    Each reviewer's free-text "notes" justification for `model`'s response,
    in stage2_results order - real critique CONTENT, distinct from
    _rubric_scores_for_model's numeric-only summary. Upstream's rubric-
    scoring prompt (llm_council.council_stages.stage2_collect_rankings,
    confirmed by direct source read) asks each reviewer for a
    "notes": "<brief justification>" string alongside the five numeric
    dimensions - this was previously read nowhere in this repo, so a real
    reviewer's actual reasoning never reached the model doing Stage 2.75
    revision, only the numbers did.
    """
    label_for_model = _label_for_model(model, label_to_model)
    if label_for_model is None:
        return []

    notes: list[str] = []
    for reviewer_entry in stage2_results:
        evaluations = reviewer_entry.get("parsed_ranking", {}).get("evaluations", {})
        scores = evaluations.get(label_for_model)
        if not scores:
            continue
        note = scores.get("notes")
        if note:
            notes.append(note)
    return notes


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
    # Mutation-testing note (2026-08-13): `default=0`'s value (0, 1, None,
    # or omitted) is a true equivalent mutant here - `if not averages:
    # return ...` above already guarantees `averages` is non-empty, and
    # `averages`'s keys come from `per_dim.items() if values` (the SAME
    # truthiness condition as this genexpr's `if v`), so at least one `v`
    # is always truthy and the genexpr is never empty - `max()`'s default
    # is unreachable dead code. Verified by direct execution (mutmut run,
    # 3 survivors on this line, traced by hand).
    n_reviewers = max((len(v) for v in per_dim.values() if v), default=0)
    summary = (
        f"Reviewers scored your response ({n_reviewers} reviewer(s)) — "
        + ", ".join(parts)
        + f". Weakest dimension: {weakest_dim} ({averages[weakest_dim]:.1f})."
    )

    notes = _rubric_notes_for_model(model, stage2_results, label_to_model)
    if notes:
        # No forced trailing period - reviewer notes are free text and may
        # already end with their own punctuation (a forced "." would double
        # up, e.g. "...inputs..").
        summary += " Reviewer notes: " + " | ".join(notes)
    return summary


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
    # Critical (architecture-stress-test-2026-08-13.md #2): the real
    # single-model degraded_mode shape from council_adapter.py has keys
    # model/rank/average_score/average_position/vote_count/note and NO
    # "borda_score" key - .get() with a 0.0 default matches the same
    # defensive pattern audition_tracking.py's quality_percentile_from_rankings
    # already uses for this exact shape.
    scores = [entry.get("borda_score", 0.0) for entry in aggregate_rankings]
    if len(scores) < 2:
        return {entry["model"]: False for entry in aggregate_rankings}
    median = statistics.median(scores)
    stdev = statistics.pstdev(scores)
    threshold = median - 1.5 * stdev
    return {
        entry["model"]: entry.get("borda_score", 0.0) < threshold
        for entry in aggregate_rankings
    }


def _reasoning_graph_wall_clock_margin_exceeded(
    stage_start: float, max_wall_clock_seconds: float, now_fn: Callable[[], float] = time.monotonic
) -> bool:
    # docs/specs/reasoning-graph-contract.md's wall-clock soft-budget
    # self-check. Injectable now_fn (mirrors resilient_query.py's own
    # time_fn convention) so this is unit-testable without touching real
    # event-loop timing.
    return now_fn() - stage_start > max_wall_clock_seconds - 60.0


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
    # docs/specs/reasoning-effort-wiring-contract.md, Contract 2. None
    # (default) means Stage 2.75/3.75/4 call plain `query_model` exactly as
    # before this contract - every existing call site/test is unaffected.
    query_model_with_effort: Optional[ReasoningQueryModelFn] = None,
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

        def _query_model_for_effort(effort: str) -> QueryModelFn:
            # docs/specs/reasoning-effort-wiring-contract.md, Contract 2.
            # query_model_with_effort is None on every existing call site,
            # so this is a pure passthrough there - only a caller that
            # actually supplies it (main()) gets effort-tagged requests.
            if query_model_with_effort is None:
                return query_model
            effort_fn = query_model_with_effort

            async def _fn(model: str, prompt: str) -> tuple[str, float]:
                return await effort_fn(model, prompt, effort)

            return _fn

        # docs/specs/reasoning-graph-contract.md, Stage 5 wall-clock
        # soft-budget self-check.
        stage_start = time.monotonic()
        verified_facts: list[TaggedClaim] = []
        if config.raw_claims_text.strip():
            claims = parse_claims(config.raw_claims_text)
            evidence_map = await fetch_evidence(claims)
            # Contract 2 (docs/specs/wallclock-cost-budget-contract.md):
            # real_fetch_evidence returns an EvidenceMap carrying real cost
            # and a truncation flag; a plain-dict fake (every pre-existing
            # test) has neither, so this must default via getattr, never
            # assume the attributes exist.
            stage05_cost = getattr(evidence_map, "cost_usd", 0.0)
            cost_so_far += stage05_cost
            input_path = output_dir / "_raw_claims.txt"
            input_path.write_text(config.raw_claims_text)
            try:
                run_grounding_pass(input_path, evidence_map, output_dir)
            finally:
                # Medium finding (architecture-stress-test-2026-08-13.md):
                # the temp file must be cleaned up even if run_grounding_pass
                # raises, not just on the success path.
                input_path.unlink()
            tagged = [tag_claim(c, evidence_map.get(c.id, [])) for c in claims]
            verified_facts = [tc for tc in tagged if tc.tag in ("VERIFIED", "CONTRADICTED")]
            n_verified = sum(1 for tc in tagged if tc.tag == "VERIFIED")
            n_contradicted = sum(1 for tc in tagged if tc.tag == "CONTRADICTED")
            n_unverifiable = sum(1 for tc in tagged if tc.tag == "UNVERIFIABLE")
            debug_log.append(
                f"Stage 0.5: grounding ran, {len(claims)} claim(s) "
                f"({n_verified} verified, {n_contradicted} contradicted, "
                f"{n_unverifiable} unverifiable), cost=${stage05_cost:.4f}"
            )
            if getattr(evidence_map, "truncated", False):
                debug_log.append(
                    f"Stage 0.5: WARNING - claims truncated by max_claims cap, "
                    f"only the first {len(claims)} claim(s) were fetched"
                )
        else:
            debug_log.append("Stage 0.5: skipped (no raw_claims_text)")

        stage1_results, stage2_results, stage3_result, metadata = await council_fn(
            config.query, verified_facts
        )

        debug_log.append(f"Stage 1-3.5: council returned {len(stage1_results)} model response(s)")
        if len(stage1_results) == 0:
            # Critical (architecture-stress-test-2026-08-13.md #1): the real
            # all-models-failed shape from council_adapter.py has no
            # "quality_metrics" key at all - reading metadata["quality_metrics"]
            # unconditionally below would raise a bare, uninformative KeyError.
            # Short-circuit into a clean, named failure instead, reusing the
            # same failure path (_write_run_status + re-raise) every other
            # exception in this function already goes through.
            raise RuntimeError("Stage 1: no models responded - cannot proceed")
        if len(stage1_results) < 2:
            debug_log.append(
                f"WARNING: only {len(stage1_results)} model(s) participated - "
                "this is not multi-agent debate"
            )

        # architecture-stress-test-2026-08-13.md, High finding: the safety
        # gate (evaluation.safety.enabled) was computed per Stage 1 response
        # (council_adapter.py's check_response_safety) but never read
        # anywhere - a flagged response passed through completely silently.
        # Surfaced here the same way every other real signal in this
        # function is: a loud debug_log WARNING, never a silent drop. Not a
        # new blocking/scoring behavior (that would be a separate, larger
        # design decision) - closes the specific "computed but never read"
        # gap on file, nothing more.
        for r in stage1_results:
            safety_check = r.get("safety_check")
            if safety_check and not safety_check.get("passed", True):
                debug_log.append(
                    f"WARNING: {r['model']}'s Stage 1 draft failed the safety "
                    f"check ({safety_check.get('reason')})"
                )

        # Durable persistence (docs/specs/durable-persistence-contract.md) -
        # best-effort, a disk-write failure must never crash an otherwise-
        # successful pipeline run (mirrors the audition-tracking try/except
        # idiom below).
        try:
            write_stage1_transcripts(output_dir, stage1_results)
        except Exception as e:
            debug_log.append(f"Transcript write (stage1_transcripts.md): failed non-fatally ({e})")

        for sub in metadata.get("substitutions") or []:
            debug_log.append(
                f"NOTE: {sub['slot_model']} was unreachable this session, "
                f"substituted with backup {sub['backup_model']} ({sub['reason']})"
            )

        shortfall_warning = metadata.get("shortfall_warning")
        if shortfall_warning:
            debug_log.append(f"WARNING: {shortfall_warning}")

        for model in metadata.get("ungrounded_models") or []:
            debug_log.append(
                f"WARNING: {model}'s Stage 1 draft carried no grounding tags "
                "despite being instructed to tag every substantive claim"
            )

        css = metadata["quality_metrics"]["core"]["consensus_strength"]
        aggregate_rankings = metadata["aggregate_rankings"]
        label_to_model = metadata["label_to_model"]
        usage = metadata["usage"]
        stage1to3_cost = usage["total"]["cost_usd"]
        cost_so_far += stage1to3_cost
        debug_log.append(f"Stage 2.5: CSS={css:.3f}")

        # Computed here (not just at scorecard time below) so the durable
        # write below can reuse it rather than recomputing.
        is_outlier = _compute_outliers(aggregate_rankings)
        try:
            write_stage2_summary(output_dir, stage2_results, aggregate_rankings, css, is_outlier)
        except Exception as e:
            debug_log.append(f"Transcript write (stage2_summary.md): failed non-fatally ({e})")

        revision_triggered = False
        revision_skipped_for_cost = False
        revision_cost = 0.0
        synthesis = stage3_result["response"]

        # write_synthesis runs unconditionally right after Stage 3 - not
        # gated on Stage 5 (reasoning-graph, when wired in) also writing
        # synthesis.md later; an identical overwrite there is harmless.
        try:
            write_synthesis(output_dir, synthesis, stage3_result.get("model", "unknown"))
        except Exception as e:
            debug_log.append(f"Transcript write (synthesis.md): failed non-fatally ({e})")

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
                    css,
                    answers,
                    verified_facts,
                    _query_model_for_effort("high"),
                    source_document=config.query,
                )
                revision_cost = sum(o.cost_usd for o in outcomes)
                cost_so_far += revision_cost
                revision_triggered = True
                n_accepted = sum(1 for o in outcomes if o.accepted)
                debug_log.append(
                    f"Stage 2.75: revision triggered, {len(outcomes)} model(s) "
                    f"responded, {n_accepted} accepted"
                )
                try:
                    write_revision_outcomes(output_dir, outcomes)
                except Exception as e:
                    debug_log.append(
                        f"Transcript write (revision_outcomes.md): failed non-fatally ({e})"
                    )
        else:
            debug_log.append(f"Stage 2.75: skipped (CSS {css:.3f} >= threshold)")

        debug_log.append(f"Stage 3: synthesis produced by {stage3_result.get('model', 'unknown')}")

        # docs/specs/stage-3-75-critique-contract.md: devil's-advocate +
        # counterfactual critique, GPT-5.5 only, never the chairman, gated
        # on CSS<0.50 OR any outlier. Never auto-triggers re-synthesis -
        # the memo is for the still-manual Stage 4 premortem to read.
        critique_triggered = False
        critique_text: Optional[str] = None
        critique_skipped_for_cost = False
        if not should_trigger_critique(css, is_outlier):
            debug_log.append(f"Stage 3.75: skipped (CSS {css:.3f} >= threshold and no outlier)")
        elif config.max_cost_usd is not None and cost_so_far >= config.max_cost_usd:
            critique_skipped_for_cost = True
            debug_log.append("Stage 3.75: skipped (would exceed max_cost_usd)")
        else:
            try:
                critique_outcome = await run_critique_round(
                    synthesis, _query_model_for_effort("high")
                )
                cost_so_far += critique_outcome.cost_usd
                critique_triggered = True
                critique_text = critique_outcome.critique_text
                debug_log.append("Stage 3.75: critique produced by openai/gpt-5.5")
                try:
                    (output_dir / "critique_memo.md").write_text(critique_outcome.critique_text)
                except Exception as e:
                    debug_log.append(f"Transcript write (critique_memo.md): failed non-fatally ({e})")
            except Exception as e:
                debug_log.append(f"Stage 3.75: failed non-fatally ({e})")

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
                verified_facts,
                synthesis,
                config.completeness_check_model,
                _query_model_for_effort("low"),
            )
            cost_so_far += completeness_check_cost
            completeness_check_parse_failed = not parse_ok
            if parse_ok:
                debug_log.append(f"Stage 4: ran, parse succeeded, {len(dropped_facts)} fact(s) dropped")
            else:
                debug_log.append(
                    "Stage 4: ran, parse FAILED - completeness is UNDETERMINED, not verified"
                )

        # docs/specs/reasoning-graph-contract.md, Integration section: all
        # three gates must pass, else skip loudly (never a silent absence).
        # A dropped_fact_ids set feeds build_reference_nodes_and_edges so a
        # fact Stage 4 flagged as unaddressed renders distinctly from one
        # the synthesis actually covered.
        reasoning_graph_path: Optional[Path] = None
        reasoning_graph_skipped_reason: Optional[str] = None
        reasoning_graph_dropped_count: Optional[dict] = None
        if config.max_cost_usd is not None and cost_so_far >= config.max_cost_usd:
            reasoning_graph_skipped_reason = "cost_ceiling"
            debug_log.append("Stage 5: skipped (would exceed max_cost_usd)")
        elif _reasoning_graph_wall_clock_margin_exceeded(
            stage_start, config.max_wall_clock_seconds
        ):
            reasoning_graph_skipped_reason = "wall_clock_margin"
            debug_log.append("Stage 5: skipped (too little wall-clock budget remaining)")
        else:
            try:
                async def _reasoning_graph_query_fn(model: str, prompt: str, timeout: float):
                    try:
                        text, cost = await asyncio.wait_for(
                            query_model(model, prompt), timeout=timeout
                        )
                    except asyncio.TimeoutError:
                        return None
                    nonlocal cost_so_far
                    cost_so_far += cost
                    # build_reasoning_graph only ever reads response["content"] -
                    # no "usage" key needed here (unlike live_adapters.py's raw
                    # HTTP responses, which include it for other consumers).
                    return {"content": text}

                graph, extraction_skip_reason = await build_reasoning_graph(
                    run_id=timestamp,
                    synthesis_text=synthesis,
                    verified_facts=verified_facts,
                    dropped_fact_ids=set(dropped_facts),
                    model=config.completeness_check_model,
                    query_fn=_reasoning_graph_query_fn,
                    timeout=120.0,
                )
                if graph is None:
                    reasoning_graph_skipped_reason = extraction_skip_reason
                    debug_log.append(f"Stage 5: skipped ({extraction_skip_reason})")
                else:
                    json_path, _, _ = write_reasoning_graph_files(output_dir, graph, synthesis)
                    reasoning_graph_path = json_path
                    reasoning_graph_dropped_count = {
                        "nodes": graph.dropped_node_count,
                        "edges": graph.dropped_edge_count,
                    }
                    n_nodes = len(graph.nodes) - graph.dropped_node_count
                    n_edges = len(graph.edges) - graph.dropped_edge_count
                    debug_log.append(
                        f"Stage 5: graph extracted, {n_nodes} node(s) kept "
                        f"({graph.dropped_node_count} dropped), {n_edges} edge(s) "
                        f"kept ({graph.dropped_edge_count} dropped)"
                    )
            except Exception as e:
                reasoning_graph_skipped_reason = "extraction_error"
                debug_log.append(f"Stage 5: skipped (extraction_error: {e})")

        rubric_scores = extract_rubric_scores_for_scorecard(stage2_results, label_to_model)
        ranks = _compute_ranks(aggregate_rankings)
        # is_outlier was already computed above, right before write_stage2_summary -
        # reused here rather than recomputed.
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

        total_cost_usd = cost_so_far
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
            critique_triggered,
            critique_text,
            critique_skipped_for_cost,
            reasoning_graph_path,
            reasoning_graph_skipped_reason,
            reasoning_graph_dropped_count,
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
            critique_triggered,
            critique_text,
            critique_skipped_for_cost,
            reasoning_graph_path,
            reasoning_graph_skipped_reason,
            reasoning_graph_dropped_count,
        ) = await asyncio.wait_for(_run_stages(), timeout=config.max_wall_clock_seconds)
    except asyncio.TimeoutError:
        # asyncio.TimeoutError is TimeoutError itself on Python 3.11+ (the
        # two names are aliased) - main() catches plain TimeoutError to map
        # this to its own exit code (4), distinct from the generic exit(1).
        error_msg = f"exceeded max_wall_clock_seconds ({config.max_wall_clock_seconds}s)"
        _write_run_status(
            output_dir, "failed", error=error_msg, cost_so_far_usd=cost_so_far, debug_log=debug_log
        )
        raise TimeoutError(error_msg) from None
    except Exception as e:
        # High finding (architecture-stress-test-2026-08-13.md): the
        # accumulated debug_log must survive into the failure record - it's
        # the accumulated diagnostic trail this project's own "no silent
        # failing of any step" hardening pass exists to preserve, and it was
        # previously dropped on exactly the path where it matters most.
        _write_run_status(
            output_dir, "failed", error=str(e), cost_so_far_usd=cost_so_far, debug_log=debug_log
        )
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
        critique_triggered=critique_triggered,
        critique_text=critique_text,
        critique_skipped_for_cost=critique_skipped_for_cost,
        reasoning_graph_path=reasoning_graph_path,
        reasoning_graph_skipped_reason=reasoning_graph_skipped_reason,
        reasoning_graph_dropped_count=reasoning_graph_dropped_count,
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
    # Mutation-testing note (2026-08-13): `default=None` is argparse's own
    # implicit default for `add_argument`, so dropping it is a true
    # equivalent mutant on all three lines below. Verified by direct
    # execution (mutmut run, 3 survivors, traced by hand).
    parser.add_argument("--claims-file", type=Path, default=None)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument(
        "--max-wall-clock-seconds", type=float, default=DEFAULT_MAX_WALL_CLOCK_SECONDS
    )
    return parser


def main() -> None:
    import sys
    from datetime import datetime, timezone

    from scripts.council_adapter import _load_debate_resilience_config, run_council_with_timeouts
    from scripts.live_adapters import (
        real_fetch_evidence,
        real_fetch_live_model_ids,
        real_query_model,
    )
    from scripts.slug_freshness import check_slug_freshness, default_slug_freshness_cache_path

    from llm_council.unified_config import get_config

    args = _build_arg_parser().parse_args()

    council_models = get_config().council.models

    # docs/specs/pending-stage-wiring-contract.md, Contract 1: at most
    # once/day, covers every slug this project actually configures (core
    # roster + backup pool) - visibility only, never blocking, since the
    # resilience/backup mechanism already handles a dead slug at call time.
    backup_models = _load_debate_resilience_config().backup_models
    freshness_result = asyncio.run(
        check_slug_freshness(
            configured_slugs=list(council_models) + list(backup_models),
            cache_path=default_slug_freshness_cache_path(Path.cwd()),
            fetch_fn=real_fetch_live_model_ids,
            today=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
    )
    if freshness_result.warning:
        print(f"WARNING: {freshness_result.warning}", file=sys.stderr)
    if freshness_result.fetch_error:
        print(
            f"WARNING: slug freshness check could not reach OpenRouter "
            f"({freshness_result.fetch_error}) - proceeding without it",
            file=sys.stderr,
        )

    async def council_fn(query: str, verified_facts: list[TaggedClaim]):
        return await run_council_with_timeouts(
            query,
            verified_facts,
            overall_wall_clock_seconds=args.max_wall_clock_seconds,
        )

    # docs/specs/reasoning-effort-wiring-contract.md, Contract 3 - the only
    # place this project actually sends the live `reasoning_effort` field.
    async def _query_model_with_effort(model: str, prompt: str, effort: str) -> tuple[str, float]:
        return await real_query_model(model, prompt, reasoning_effort=effort)

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
                query_model_with_effort=_query_model_with_effort,
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
