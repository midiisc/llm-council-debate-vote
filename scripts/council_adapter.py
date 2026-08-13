"""Timeout-aware replacement for `llm_council.council.run_full_council`, used
only by `pipeline_runner.py`'s own CLI orchestrator - the MCP tool path
(`consult_council`) already gets tier-based timeouts via
`create_tier_contract`/`run_council_with_fallback` and does not use this
module.

Why this exists (grounded 2026-08-12 against `llm-council-core==0.40.1`,
source read, no live calls): `run_full_council` calls
`stage1_collect_responses(user_query)` (no timeout override at all - falls
through to `query_models_parallel`'s 120s default) and its own Stage 2/3
calls each independently default to 120s too. None of that reads
`llm_council.yaml`'s `tiers:`/`timeouts:` block. `run_council_with_fallback`
(the tier-aware entry point) returns a fundamentally different ADR-012 flat
dict shape with no `aggregate_rankings`/`label_to_model`/
`parsed_ranking.evaluations`/`quality_metrics` - adopting it would force
rewriting `pipeline_runner.py`'s dependent extraction functions, a much
bigger change than "add a timeout." This module instead calls the package's
own granular stage functions directly, with explicit per-stage timeouts,
reproducing (not vendoring) `run_full_council`'s orchestration glue.

**Drift-check note (unanimous expert-panel requirement, docs/upstream-deltas.md
"Second Expert Panel round"):** this call sequence is pinned to
`llm_council/council.py::run_full_council` (source lines ~848-1163) and
`llm_council/council_stages.py::stage1_collect_responses` (lines ~113-142)
as installed in `llm-council-core==0.40.1`. On any version bump, re-read
those two functions and update this module if the stage sequence, branching,
or metadata assembly changed - the automated Pillar-5 self-update diff check
for this file is not yet built (tracked as a follow-up, not a silent gap).

Non-goals (confirmed unused by this project today): Jury Mode
(BINARY/TIE_BREAKER `verdict_type`, deadlock detection), dissent extraction,
webhooks, bias-audit. Turning any of those on project-wide needs a follow-up
amendment to this module.

Module-level (not function-local) imports are deliberate: they're what makes
each dependency independently monkeypatchable by name for tests, the same
testability boundary `pipeline_runner.py` already documents for its own
fetch_evidence/council_fn/query_model injection points.

Contract: docs/specs/pipeline-runner-contract.md, "Amendment (2026-08-12):
timeout-aware `council_fn` + wall-clock ceiling".
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from llm_council.council import _get_council_models
from llm_council.council_rankings import calculate_aggregate_rankings
from llm_council.council_stages import (
    stage1_5_normalize_styles,
    stage2_collect_rankings,
    stage3_synthesize_final,
)
from llm_council.council_usage import _add_cost_to_usage, _build_usage_summary
from llm_council.gateway_adapter import query_model_with_status
from llm_council.observability.usage_metrics import emit_usage_metrics
from llm_council.quality.integration import calculate_quality_metrics, should_include_quality_metrics
from llm_council.safety_gate import check_response_safety
from llm_council.unified_config import _find_config_file, get_config
from llm_council.verdict import VerdictType

from scripts.grounding_pass import TaggedClaim
from scripts.resilient_query import RetryPolicy, query_models_resilient
from scripts.revision_round import _build_facts_section


# Uniform, format-neutral Stage 1 reference-reporting instruction (Proposal A
# Contract 1, docs/specs/proposal-a-reference-grounding-contract.md; tag
# format + strictness per docs/specs/grounding-annotation-enforcement-contract.md,
# Contract 1). Never varies by model or by query - appended verbatim to
# every Stage 1 prompt so CSS's same-question precondition is preserved.
# Names exactly the two checkable grounding classes this pipeline can
# actually verify: the input document itself, and facts already verified
# earlier in this process. General/background knowledge may still be
# mentioned, but must be tagged unverified - model confidence is
# uncorrelated with citation correctness (arXiv:2607.11127), so this never
# instructs a model to fabricate or omit sourcing.
#
# A real dry run (docs/upstream-deltas.md, 2026-08-13) found every Stage 2
# peer reviewer penalizing one model's response for "leaked internal
# 'Grounding note/Stage 0.5' scaffolding" - traced to this block itself
# naming the internal stage number, which the model echoed verbatim into
# its visible answer, plus no format guidance letting models improvise a
# high-cost separate header instead of a lightweight inline tag. Fixed by
# removing the internal name entirely (never given, never echoed) and
# mandating an exact, compact, machine-checkable tag vocabulary - the
# grounding REQUIREMENT is unchanged (in fact tightened to mandatory), only
# the presentation that was costing peer-review score is fixed.
_STAGE1_REFERENCE_INSTRUCTION_BLOCK = (
    "\n\n---\n"
    "For each substantive claim above, you MUST append one of these exact "
    "tags immediately after it - no substantive claim may be left "
    'untagged: "[grounded: document]" if it comes from the input document '
    '/ source material provided in this query; "[grounded: verified]" if '
    "it comes from verified facts established earlier in this process; or "
    '"[unverified]" if it is general or background knowledge with no '
    "checkable source. Never present unverified knowledge as a citable "
    "reference, and never fabricate a source to avoid using "
    '"[unverified]". Keep these tags lightweight and inline - never a '
    "separate labeled section, and never a reference to this process's "
    "internal stage names or step numbers; those are implementation "
    "details, not part of your answer."
)

# docs/specs/grounding-annotation-enforcement-contract.md, Contract 2. Pure,
# deterministic - no model call. Used to catch a Stage 1 response that
# skipped the mandatory tagging above entirely, so it can be surfaced
# (never silently accepted) rather than repeating this project's own
# already-documented "computed but never read" mistake (the dead safety
# gate).
_GROUNDING_TAG_PATTERN = re.compile(r"\[grounded: (?:document|verified)\]|\[unverified\]")


def has_grounding_annotations(response_text: str) -> bool:
    return bool(_GROUNDING_TAG_PATTERN.search(response_text))

# docs/specs/human-debate-characteristics-contract.md, Contract 4. Never
# varies by model, same reasoning as the reference-instruction block above.
# Also closes a real, previously-decided-but-never-wired gap: an earlier
# session decision (docs/agent-model-reasoning-config.md section 5) adopted
# asking Stage 1 to weigh counterfactuals/weaknesses in its own reasoning,
# but that instruction was never actually added to build_stage1_prompt -
# folded in here rather than left undelivered a second time.
_STAGE1_COLLABORATIVE_FRAMING_BLOCK = (
    "\n\n---\n"
    "Other models are independently drafting answers to this same "
    "question, without seeing each other's work. The goal of this "
    "exercise is to converge on the best-supported shared answer, not to "
    "win an argument against them - as you form your answer, weigh "
    "counterfactuals and potential weaknesses in your own reasoning, and "
    "note where a well-informed peer might reasonably disagree, while "
    "staying concise."
)


def build_stage1_prompt(user_query: str) -> str:
    """Appends uniform reference-reporting and collaborative-framing
    instructions to user_query. Never varies by model. General/background-
    knowledge claims may be noted but must be labeled unverified - never
    presented as a citable reference (fabrication risk: model confidence is
    uncorrelated with citation correctness, arXiv:2607.11127)."""
    return (
        f"{user_query}{_STAGE1_REFERENCE_INSTRUCTION_BLOCK}"
        f"{_STAGE1_COLLABORATIVE_FRAMING_BLOCK}"
    )


@dataclass
class DebateResilienceConfig:
    backup_models: List[str]
    retry_policy: RetryPolicy
    minimum_council_size: int


def _load_debate_resilience_config(config_path: Optional[Path] = None) -> DebateResilienceConfig:
    """Read the `debate_resilience:` block from `llm_council.yaml` (or an
    explicit override path, for hermetic tests). Never raises - a project
    that hasn't added this block yet (or has no config file at all) simply
    gets today's behavior plus retries, via safe defaults.

    Deliberately bypasses `get_config()`/`UnifiedConfig` - see the
    config-placement rule in docs/upstream-deltas.md - and locates the file
    the same way `llm_council.unified_config._find_config_file()` does
    (env var -> ./llm_council.yaml -> ~/.config/llm-council/llm_council.yaml)
    when no explicit `config_path` is given.
    """
    defaults = DebateResilienceConfig(
        backup_models=[],
        retry_policy=RetryPolicy(),
        minimum_council_size=4,
    )

    path = config_path if config_path is not None else _find_config_file()
    if path is None:
        return defaults

    try:
        # Mutation-testing note (2026-08-13): the explicit "r" mode is
        # builtin open()'s own default - dropping it is a true equivalent
        # mutant. Verified by direct execution (mutmut run, 1 survivor,
        # traced by hand).
        with open(path, "r") as f:
            raw = yaml.safe_load(f) or {}
    except OSError:
        return defaults

    block = raw.get("debate_resilience") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        return defaults

    retry_block = block.get("retry") or {}
    retry_kwargs: Dict[str, Any] = {}
    if "max_attempts" in retry_block:
        retry_kwargs["max_attempts"] = retry_block["max_attempts"]
    if "backoff_seconds" in retry_block:
        retry_kwargs["backoff_seconds"] = tuple(retry_block["backoff_seconds"])
    if "retryable_statuses" in retry_block:
        retry_kwargs["retryable_statuses"] = frozenset(retry_block["retryable_statuses"])
    retry_policy = RetryPolicy(**retry_kwargs)

    return DebateResilienceConfig(
        backup_models=list(block.get("backup_models", [])),
        retry_policy=retry_policy,
        minimum_council_size=block.get("minimum_council_size", 4),
    )


DEFAULT_STAGE1_DEADLINE_FRACTION = 0.5


async def run_council_with_timeouts(
    user_query: str,
    verified_facts: List[TaggedClaim] = [],
    stage1_timeout: float = 300.0,
    stage2_timeout: float = 300.0,
    stage3_timeout: float = 300.0,
    overall_wall_clock_seconds: Optional[float] = None,
    stage1_deadline_fraction: float = DEFAULT_STAGE1_DEADLINE_FRACTION,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    """Same return shape as `run_full_council(user_query, models=None)` -
    `(stage1_results, stage2_results, stage3_result, metadata)` - so it can
    be dropped in as `pipeline_runner.py`'s `CouncilFn` with no downstream
    changes to how the result is read.

    `verified_facts` (Proposal A Contract 3, default empty - strictly
    additive) is threaded ONLY into Stage 3's synthesis query, never into
    Stage 1's `messages` - Stage 1 and Stage 3 stay independently
    controllable, per `docs/specs/proposal-a-reference-grounding-contract.md`.

    `overall_wall_clock_seconds` (docs/specs/wallclock-cost-budget-contract.md,
    Contract 1, default None - strictly additive) sizes Stage 1's own
    resilient-query deadline as `stage1_deadline_fraction` of the caller's
    total wall-clock budget, so Stage 1's retry+backup engine can no longer
    alone exhaust the entire ceiling (architecture-stress-test-2026-08-13.md,
    Critical #3). None (default) means no deadline is computed - Stage 1
    retries/substitutes exactly as before this contract landed.
    """
    total_usage: Dict[str, Dict[str, Any]] = {
        "stage1": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "stage1_5": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "stage2": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "stage3": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

    # Stage 1: bypasses stage1_collect_responses (no timeout override there)
    # and calls query_models_resilient directly (retry-with-backoff +
    # backup-model substitution, docs/specs/debate-resilience-contract.md),
    # reproducing query_models_parallel's aggregation on top of its result.
    messages = [{"role": "user", "content": build_stage1_prompt(user_query)}]
    resilience_config = _load_debate_resilience_config()
    stage1_deadline = (
        time.monotonic() + overall_wall_clock_seconds * stage1_deadline_fraction
        if overall_wall_clock_seconds is not None
        else None
    )
    resilient_result = await query_models_resilient(
        primary_models=_get_council_models(),
        backup_models=resilience_config.backup_models,
        messages=messages,
        timeout=stage1_timeout,
        query_fn=query_model_with_status,
        retry_policy=resilience_config.retry_policy,
        minimum_council_size=resilience_config.minimum_council_size,
        deadline=stage1_deadline,
    )
    responses = resilient_result.responses

    stage1_results: List[Dict[str, Any]] = []
    for model, response in responses.items():
        stage1_results.append({"model": model, "response": response.get("content", "")})
        usage = response.get("usage", {})
        total_usage["stage1"]["prompt_tokens"] += usage.get("prompt_tokens", 0)
        total_usage["stage1"]["completion_tokens"] += usage.get("completion_tokens", 0)
        total_usage["stage1"]["total_tokens"] += usage.get("total_tokens", 0)
        _add_cost_to_usage(total_usage["stage1"], usage, model=model)

    num_responses = len(stage1_results)

    # ADR-016 safety gate - config-driven, matches run_full_council's own
    # `if eval_config.safety.enabled:` gating (AC14). getattr fallbacks
    # tolerate a test double that doesn't mirror SafetyCheckResult's exact
    # shape - this project never reads flagged_patterns/reason back out
    # today, so a loose double is a legitimate simplification, not a gap.
    eval_config = get_config().evaluation
    if eval_config.safety.enabled:
        for result in stage1_results:
            # Mutation-testing note (2026-08-13): `.get("response", "")`'s
            # default is unreachable dead code, not a real gap - every dict
            # in stage1_results is built at line 201 with a "response" key
            # unconditionally present, so mutating the default value
            # ("", None, "XXXX") or dropping it survives mutmut but never
            # changes actual behavior. Verified by direct execution (mutmut
            # run, 3 survivors on this line, traced by hand).
            safety_check = check_response_safety(result.get("response", ""))
            result["safety_check"] = {
                "passed": getattr(safety_check, "passed", getattr(safety_check, "safe", True)),
                "reason": getattr(safety_check, "reason", None),
                "flagged_patterns": getattr(safety_check, "flagged_patterns", []),
            }

    # docs/specs/grounding-annotation-enforcement-contract.md, Contract 2:
    # a Stage 1 response with zero grounding tags must never pass through
    # silently - collected here so it can be both surfaced in metadata
    # (pipeline_runner.py's debug_log) and threaded into Stage 3 so the
    # chairman actually weighs it during synthesis, not just logged for a
    # human who might not read it.
    ungrounded_models = [
        r["model"] for r in stage1_results if not has_grounding_annotations(r.get("response", ""))
    ]

    if num_responses == 0:
        return (
            [],
            [],
            {"model": "error", "response": "All models failed to respond. Please try again."},
            {"usage": total_usage},
        )

    # Mutation-testing note (2026-08-13): `None` vs `""` here is a true
    # equivalent mutant - the only later reads of degraded_mode are a
    # truthiness check (`if degraded_mode:`, below) and an equality check
    # against the literal "two_models", and None/"" are both falsy and both
    # != "two_models", so num_responses >= 3 (the only path where this
    # initial value survives unreassigned) behaves identically either way.
    # Verified by direct execution (mutmut run, 1 survivor, traced by hand).
    degraded_mode = None
    stage2_results: List[Dict[str, Any]]
    if num_responses == 1:
        degraded_mode = "single_model"
        stage2_results = []
        label_to_model = {"Response A": {"model": stage1_results[0]["model"], "display_index": 0}}
        aggregate_rankings = [
            {
                "model": stage1_results[0]["model"],
                "rank": 1,
                "average_score": None,
                "average_position": None,
                "vote_count": 0,
                "note": "Single model - no peer review",
            }
        ]
    else:
        if num_responses == 2:
            degraded_mode = "two_models"
        responses_for_review, stage1_5_usage = await stage1_5_normalize_styles(stage1_results)
        total_usage["stage1_5"] = stage1_5_usage
        stage2_results, label_to_model, stage2_usage = await stage2_collect_rankings(
            user_query, responses_for_review, timeout=stage2_timeout
        )
        total_usage["stage2"] = stage2_usage
        aggregate_rankings = calculate_aggregate_rankings(stage2_results, label_to_model)
        if degraded_mode == "two_models":
            for r in aggregate_rankings:
                r["note"] = "Two-model council - rankings based on single vote"

    stage3_query = user_query
    if verified_facts:
        stage3_query += f"\n\n{_build_facts_section(verified_facts)}"
    if ungrounded_models:
        stage3_query += (
            "\n\n--- BEGIN GROUNDING COMPLIANCE NOTE ---\n"
            "The following model(s) did not include any grounding tags in "
            "their Stage 1 draft, despite being instructed to tag every "
            f"substantive claim: {', '.join(ungrounded_models)}. Weigh this "
            "explicitly when synthesizing - an unlabeled draft's claims "
            "cannot be distinguished from fabricated ones.\n"
            "--- END GROUNDING COMPLIANCE NOTE ---"
        )

    stage3_result, stage3_usage, _verdict_result = await stage3_synthesize_final(
        stage3_query,
        stage1_results,
        stage2_results,
        aggregate_rankings=aggregate_rankings,
        verdict_type=VerdictType.SYNTHESIS,
        timeout=stage3_timeout,
    )
    total_usage["stage3"] = stage3_usage

    usage_summary = _build_usage_summary(total_usage)
    emit_usage_metrics(usage_summary)

    metadata: Dict[str, Any] = {
        "label_to_model": label_to_model,
        "aggregate_rankings": aggregate_rankings,
        "usage": usage_summary,
    }
    if degraded_mode:
        metadata["degraded_mode"] = degraded_mode
    if ungrounded_models:
        metadata["ungrounded_models"] = ungrounded_models
    if resilient_result.substitutions:
        metadata["substitutions"] = [asdict(s) for s in resilient_result.substitutions]
    if resilient_result.shortfall_warning is not None:
        metadata["shortfall_warning"] = resilient_result.shortfall_warning

    # Mutation-testing note (2026-08-13): `len(stage1_results) > 0` vs
    # `>= 0` is a true equivalent mutant here - the `if num_responses == 0:
    # return (...)` early-return above (and stage1_results is never
    # mutated afterward) already guarantees len(stage1_results) > 0 at this
    # point, so `> 0` is always True regardless of the operator. Likewise
    # `r.get("response", "")`'s default is unreachable dead code for the
    # same reason as the safety-gate loop above - "response" is always
    # present. Verified by direct execution (mutmut run, 4 survivors on
    # these two lines, traced by hand).
    if should_include_quality_metrics() and len(stage1_results) > 0:
        stage1_dict = {r["model"]: {"content": r.get("response", "")} for r in stage1_results}
        rankings_tuples = [
            (r["model"], r.get("average_position", r.get("borda_score", 0.0)))
            for r in aggregate_rankings
        ]
        quality_metrics = calculate_quality_metrics(
            stage1_responses=stage1_dict,
            stage2_rankings=stage2_results,
            stage3_synthesis=stage3_result,
            aggregate_rankings=rankings_tuples,
            label_to_model=label_to_model,
        )
        metadata["quality_metrics"] = quality_metrics.to_dict()

    return stage1_results, stage2_results, stage3_result, metadata
