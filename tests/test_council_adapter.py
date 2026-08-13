"""Blind acceptance tests for the 2026-08-12 amendment to the pipeline-runner
contract (docs/specs/pipeline-runner-contract.md, "Amendment (2026-08-12):
timeout-aware council_fn + wall-clock ceiling") -- the new
`scripts/council_adapter.py` module and its `run_council_with_timeouts`
function (AC11-14). The `max_wall_clock_seconds` half of that amendment
(AC15-16) is covered separately in tests/test_pipeline_runner_wallclock_
amendment.py.

Authored WITHOUT sight of any implementation. `scripts/council_adapter.py`
does not exist yet as of this writing -- this whole file is expected to
fail at collection/import time (RED) until it lands.

DOCUMENTED ASSUMPTIONS (the contract pins the exact call sequence and
function names via `inspect.signature`-verified citations, but not every
internal wiring detail):

  1. **Patch location.** Whether `council_adapter.py` imports each
     `llm_council.council` function by name (`from llm_council.council
     import query_models_parallel`) or accesses it through a module
     reference (`council.query_models_parallel(...)`) is not specified.
     Every patched name below is therefore patched at BOTH plausible
     locations -- on the real `llm_council.council` module (covers
     module-attribute access) and on `scripts.council_adapter` itself if
     that attribute exists there post-import (covers direct-name-import),
     via the `_patch` helper below. This assumes no import aliasing to a
     *different* local name, a standard, idiomatic default.

  2. **stage1_results shape.** `query_models_parallel` returns
     `Dict[str, Optional[Dict]]` (confirmed live via `inspect.signature`
     against the installed `llm-council-core==0.40.1`) -- keyed by model,
     `None` for a failed/timed-out model. The adapter must convert this
     into the `List[Dict]` shape `stage1_5_normalize_styles` and the rest
     of the pipeline already require (each entry carrying a `"model"` key
     -- an existing, pre-amendment invariant `pipeline_runner.py` already
     depends on for every `council_fn` implementation). Tests assert only
     on this list-of-dicts-with-"model"-key OUTCOME, never on the
     intermediate conversion mechanism.

  3. **Quality-metrics serialization.** `calculate_quality_metrics(...)`
     returns a real `llm_council.quality.types.QualityMetrics` dataclass
     instance (confirmed live). `pipeline_runner.py`'s existing,
     pre-amendment code reads `metadata["quality_metrics"]["core"]
     ["consensus_strength"]` via dict subscripting, which only works if
     the dataclass is serialized (almost certainly via
     `dataclasses.asdict`) before being attached to `metadata`. Tests that
     touch this path use a real `QualityMetrics` instance (built via the
     library's own dataclass constructors) as the mock return value, so
     they pass regardless of whether the adapter serializes via
     `dataclasses.asdict`, a `.to_dict()`-style method, or embeds the
     object as-is for a caller doing attribute access -- EXCEPT the one
     test that asserts the dict-subscript path explicitly required by
     `pipeline_runner.py` (`test_ac_quality_metrics_included_and_
     reachable_via_dict_access_when_enabled`), which is a direct
     consequence of that pre-existing, unrelated contract, not a new
     assumption invented here.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _import(name: str):
    try:
        return importlib.import_module(f"scripts.{name}")
    except ModuleNotFoundError:
        return importlib.import_module(name)


ca = _import("council_adapter")

import llm_council.council as _council_module
from llm_council.quality.types import CoreMetrics, QualityMetrics, SynthesisAttribution
from llm_council.verdict import VerdictType


def _patch(monkeypatch, name, fake):
    monkeypatch.setattr(_council_module, name, fake, raising=False)
    monkeypatch.setattr(ca, name, fake, raising=False)


from scripts.resilient_query import ResilientQueryResult


def _as_resilient(query_models_parallel_like):
    """Adapts a query_models_parallel-shaped fake ((models, messages,
    disable_tools=False, timeout=120.0) -> {model: response_or_None}) into
    a query_models_resilient-shaped fake.

    Post-amendment (docs/specs/pipeline-runner-contract.md, "Amendment
    (2026-08-12): resilient Stage 1"), council_adapter.py's Stage 1 calls
    query_models_resilient instead of query_models_parallel. Every fixture
    in this file was authored against the pre-amendment dependency
    boundary and still encodes the exact intent each test needs (which
    models "respond", with what content/usage) - this adapter keeps that
    intent intact while pointing the patch at the real call
    run_council_with_timeouts makes today, instead of leaving tests
    patching a function that's no longer on the call path (which was
    silently falling through to the real, unmocked query_models_resilient
    -> query_model_with_status and making live network attempts).
    """

    async def fake_query_models_resilient(
        primary_models,
        backup_models,
        messages,
        timeout,
        query_fn,
        retry_policy=None,
        minimum_council_size=4,
        sleep_fn=None,
        deadline=None,
        time_fn=None,
    ):
        # deadline/time_fn (docs/specs/wallclock-cost-budget-contract.md,
        # Contract 1) are accepted-and-ignored here - every test funneling
        # through this adapter predates the deadline mechanism and asserts
        # on stage1 response resolution, not on deadline plumbing (that's
        # covered directly in test_council_adapter_deadline.py); this
        # signature just needs to not reject the new kwargs council_adapter
        # now always passes.
        raw = await query_models_parallel_like(primary_models, messages, timeout=timeout)
        return ResilientQueryResult(
            responses={m: r for m, r in raw.items() if r is not None},
            attempts=[],
            substitutions=[],
            unreachable_models=[m for m, r in raw.items() if r is None],
            shortfall_warning=None,
        )

    return fake_query_models_resilient


def _make_config(safety_enabled: bool, models: list | None = None):
    # `council.models` must be present (not just `evaluation`) -- the
    # contract's own "Grounded call sequence" point 1 requires
    # `run_council_with_timeouts` to resolve Stage-1's model list via
    # `_get_council_models()`, which reads `get_config().council.models`.
    # A config double lacking `.council` makes that call path crash before
    # `query_models_parallel` is ever reached, for any contract-compliant
    # implementation -- not just this one.
    return SimpleNamespace(
        evaluation=SimpleNamespace(safety=SimpleNamespace(enabled=safety_enabled)),
        council=SimpleNamespace(models=models if models is not None else []),
    )


def _real_quality_metrics(consensus_strength: float) -> QualityMetrics:
    return QualityMetrics(
        tier="test-tier",
        core=CoreMetrics(
            consensus_strength=consensus_strength,
            deliberation_depth=0.5,
            synthesis_attribution=SynthesisAttribution(
                winner_alignment=0.5,
                max_source_alignment=0.5,
                hallucination_risk=0.1,
                grounded=True,
            ),
        ),
    )


def _install_happy_path_fakes(
    monkeypatch,
    models,
    query_models_parallel_fn=None,
    safety_enabled=False,
    include_quality_metrics=False,
):
    calls = {
        "query_models_parallel_timeout": None,
        "check_response_safety": [],
        "stage2_timeout": None,
        "stage3_timeout": None,
        "quality_metrics": [],
    }

    async def default_query_models_parallel(models_arg, messages, disable_tools=False, timeout=120.0):
        calls["query_models_parallel_timeout"] = timeout
        return {m: {"response": f"answer-from-{m}"} for m in models_arg}

    def fake_check_response_safety(response):
        calls["check_response_safety"].append(response)
        # Matches the real llm_council.safety_gate.SafetyCheckResult shape
        # (passed/reason/flagged_patterns) confirmed by direct source read -
        # not the dataclass's own field names guessed differently.
        return SimpleNamespace(passed=True, reason=None, flagged_patterns=[])

    async def fake_stage1_5_normalize_styles(stage1_results):
        return stage1_results, {}

    async def fake_stage2_collect_rankings(
        user_query, responses_for_review, timeout=120.0, models=None,
        on_progress=None, on_review_event=None,
    ):
        calls["stage2_timeout"] = timeout
        label_to_model = {
            f"Response {chr(65 + i)}": {"model": r["model"]}
            for i, r in enumerate(responses_for_review)
        }
        stage2_results = [
            {
                "model": responses_for_review[0]["model"],
                "parsed_ranking": {"evaluations": {"Response A": {"accuracy": 8}}},
            }
        ]
        return stage2_results, label_to_model, {}

    def fake_calculate_aggregate_rankings(
        stage2_results, label_to_model, voting_authorities=None, return_shadow_votes=False
    ):
        return [
            {"model": entry["model"], "borda_score": 1.0, "rank": i + 1}
            for i, entry in enumerate(label_to_model.values())
        ]

    async def fake_stage3_synthesize_final(
        user_query, stage1_results, stage2_results, aggregate_rankings=None,
        verdict_type=None, timeout=120.0, dispositions_instruction=None,
        on_synthesis_delta=None,
    ):
        calls["stage3_timeout"] = timeout
        return {"model": stage1_results[0]["model"], "response": "final synthesis"}, {}, None

    def fake_build_usage_summary(by_stage):
        return {"total": {"cost_usd": 0.05}, "by_model": {}}

    def fake_emit_usage_metrics(usage, adapter=None):
        return None

    def fake_should_include_quality_metrics():
        return include_quality_metrics

    def fake_calculate_quality_metrics(*a, **k):
        calls["quality_metrics"].append((a, k))
        return _real_quality_metrics(0.42)

    def fake_get_config():
        return _make_config(safety_enabled, models)

    fakes = {
        "query_models_resilient": _as_resilient(query_models_parallel_fn or default_query_models_parallel),
        "check_response_safety": fake_check_response_safety,
        "stage1_5_normalize_styles": fake_stage1_5_normalize_styles,
        "stage2_collect_rankings": fake_stage2_collect_rankings,
        "calculate_aggregate_rankings": fake_calculate_aggregate_rankings,
        "stage3_synthesize_final": fake_stage3_synthesize_final,
        "_build_usage_summary": fake_build_usage_summary,
        "emit_usage_metrics": fake_emit_usage_metrics,
        "should_include_quality_metrics": fake_should_include_quality_metrics,
        "calculate_quality_metrics": fake_calculate_quality_metrics,
        "get_config": fake_get_config,
    }
    for name, fn in fakes.items():
        _patch(monkeypatch, name, fn)
    return calls


# ---------------------------------------------------------------------------
# AC11: Given run_council_with_timeouts is called with all 4 configured
# models healthy, When it completes, Then the returned tuple's shape (keys,
# nesting) is identical to what run_full_council would produce for the same
# inputs.
# ---------------------------------------------------------------------------


def test_ac11_happy_path_returned_shape_matches_pipeline_runners_extraction_paths(monkeypatch):
    models = ["model-a", "model-b", "model-c", "model-d"]
    _install_happy_path_fakes(monkeypatch, models, include_quality_metrics=True)

    stage1_results, stage2_results, stage3_result, metadata = asyncio.run(
        ca.run_council_with_timeouts("the query")
    )

    assert isinstance(stage1_results, list)
    assert {r["model"] for r in stage1_results} == set(models)

    assert isinstance(stage2_results, list)
    assert stage2_results[0]["parsed_ranking"]["evaluations"]["Response A"]["accuracy"] == 8

    assert isinstance(stage3_result, dict)
    assert stage3_result["response"] == "final synthesis"

    assert isinstance(metadata, dict)
    assert "aggregate_rankings" in metadata
    assert "label_to_model" in metadata
    assert metadata["usage"]["total"]["cost_usd"] == 0.05
    assert metadata["quality_metrics"]["core"]["consensus_strength"] == 0.42


# ---------------------------------------------------------------------------
# AC12: Given stage1_timeout is set lower than a (simulated slow) model's
# response time, When query_models_parallel is called, Then that model is
# excluded from stage1_results rather than the whole call raising.
# ---------------------------------------------------------------------------


def test_ac12_timed_out_model_excluded_from_stage1_results_call_does_not_raise(monkeypatch):
    # 3 healthy + 1 timed-out = >=3 successful responses, keeping this
    # scenario in the "normal flow" branch of the degraded-mode logic
    # (documented call-sequence point 3), not the 1-/2-model shortcut.
    models = ["model-a", "model-b", "model-c", "model-d"]

    async def fake_query_models_parallel(models_arg, messages, disable_tools=False, timeout=120.0):
        result = {m: {"response": f"answer-from-{m}"} for m in models_arg}
        result["model-d"] = None  # simulated timeout/failure
        return result

    _install_happy_path_fakes(monkeypatch, models, query_models_parallel_fn=fake_query_models_parallel)

    stage1_results, _, _, _ = asyncio.run(
        ca.run_council_with_timeouts("q", stage1_timeout=1.0)
    )

    result_models = {r["model"] for r in stage1_results}
    assert "model-d" not in result_models
    assert result_models == {"model-a", "model-b", "model-c"}


# ---------------------------------------------------------------------------
# AC13: Given exactly 0 successful Stage-1 responses, When
# run_council_with_timeouts runs, Then it returns the same all-models-failed
# error tuple shape run_full_council returns:
# ([], [], {"model": "error", ...}, {"usage": ...}).
# ---------------------------------------------------------------------------


def test_ac13_zero_successful_stage1_responses_returns_error_tuple_shape(monkeypatch):
    models = ["model-a", "model-b"]

    async def fake_query_models_parallel(models_arg, messages, disable_tools=False, timeout=120.0):
        return {m: None for m in models_arg}

    _patch(monkeypatch, "query_models_resilient", _as_resilient(fake_query_models_parallel))
    _patch(monkeypatch, "get_config", lambda: _make_config(safety_enabled=False, models=models))

    stage1_results, stage2_results, stage3_result, metadata = asyncio.run(
        ca.run_council_with_timeouts("q")
    )

    assert stage1_results == []
    assert stage2_results == []
    assert stage3_result.get("model") == "error"
    assert "usage" in metadata


# ---------------------------------------------------------------------------
# AC14: Given evaluation.safety.enabled is false (default before this
# project's change), When run_council_with_timeouts runs, Then
# check_response_safety is never called - config-driven, not hardcoded on.
# ---------------------------------------------------------------------------


def test_ac14_safety_disabled_never_calls_check_response_safety(monkeypatch):
    models = ["model-a", "model-b", "model-c"]
    calls = _install_happy_path_fakes(monkeypatch, models, safety_enabled=False)

    asyncio.run(ca.run_council_with_timeouts("q"))

    assert calls["check_response_safety"] == []


def test_ac14_safety_enabled_calls_check_response_safety_per_stage1_response(monkeypatch):
    # Complements AC14: proves the gate is genuinely config-driven both
    # ways, not just "never call it" by omission/dead code.
    models = ["model-a", "model-b", "model-c"]
    calls = _install_happy_path_fakes(monkeypatch, models, safety_enabled=True)

    asyncio.run(ca.run_council_with_timeouts("q"))

    assert len(calls["check_response_safety"]) == len(models)


# ---------------------------------------------------------------------------
# Mutation-gate hardening: stage1_timeout/stage2_timeout/stage3_timeout must
# each reach their OWN stage's timeout kwarg, never swapped or dropped -
# directly grounded in the contract's own "Grounded call sequence" +
# "Timeout defaults" sections.
# ---------------------------------------------------------------------------


def test_stage_timeouts_threaded_to_correct_stage_not_swapped(monkeypatch):
    models = ["model-a", "model-b", "model-c"]
    calls = _install_happy_path_fakes(monkeypatch, models)

    asyncio.run(
        ca.run_council_with_timeouts(
            "q", stage1_timeout=111.0, stage2_timeout=222.0, stage3_timeout=333.0
        )
    )

    assert calls["query_models_parallel_timeout"] == 111.0
    assert calls["stage2_timeout"] == 222.0
    assert calls["stage3_timeout"] == 333.0


def test_default_timeouts_are_300_seconds_each(monkeypatch):
    models = ["model-a", "model-b", "model-c"]
    calls = _install_happy_path_fakes(monkeypatch, models)

    asyncio.run(ca.run_council_with_timeouts("q"))

    assert calls["query_models_parallel_timeout"] == 300.0
    assert calls["stage2_timeout"] == 300.0
    assert calls["stage3_timeout"] == 300.0


# ---------------------------------------------------------------------------
# Point 9 of the grounded call sequence: quality_metrics is conditional on
# should_include_quality_metrics(), not unconditionally attached.
# ---------------------------------------------------------------------------


def test_ac_quality_metrics_included_and_reachable_via_dict_access_when_enabled(monkeypatch):
    models = ["model-a", "model-b", "model-c"]
    calls = _install_happy_path_fakes(monkeypatch, models, include_quality_metrics=True)

    _, _, _, metadata = asyncio.run(ca.run_council_with_timeouts("q"))

    assert metadata["quality_metrics"]["core"]["consensus_strength"] == 0.42
    assert len(calls["quality_metrics"]) == 1


def test_ac_quality_metrics_not_computed_when_should_include_quality_metrics_false(monkeypatch):
    models = ["model-a", "model-b", "model-c"]
    calls = _install_happy_path_fakes(monkeypatch, models, include_quality_metrics=False)

    asyncio.run(ca.run_council_with_timeouts("q"))

    assert calls["quality_metrics"] == []


# ---------------------------------------------------------------------------
# Mutation-gate hardening round 2 (2026-08-12): the tests above only ever
# assert on a handful of top-level fields, leaving every literal dict key,
# initial numeric value, "response.get(...)" default, and swapped call
# argument in `run_council_with_timeouts` unobserved. These tests pin the
# normal (>=3-model), single-model, and two-model code paths end to end,
# deliberately leaving `_build_usage_summary`/`_add_cost_to_usage`
# UNFAKED (they're pure, hermetic, network-free - confirmed by direct
# source read of `llm_council.council_usage`) so the real token/cost
# aggregation arithmetic is exercised and pinned exactly.
# ---------------------------------------------------------------------------


def test_ac_comprehensive_normal_path_exact_field_values(monkeypatch):
    models = ["model-a", "model-b", "model-c"]
    per_model_usage = {
        "model-a": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.001},
        "model-b": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28, "cost": 0.002},
        "model-c": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42, "cost": 0.003},
    }

    calls = {
        "query_models_parallel_timeout": None,
        "query_models_parallel_messages": None,
        "check_response_safety": [],
        "stage2_call": None,
        "stage3_call": None,
        "emit_usage_metrics_arg": None,
        "quality_metrics_call": None,
    }

    async def fake_query_models_parallel(models_arg, messages, disable_tools=False, timeout=120.0):
        calls["query_models_parallel_timeout"] = timeout
        calls["query_models_parallel_messages"] = messages
        return {m: {"content": f"content-from-{m}", "usage": per_model_usage[m]} for m in models_arg}

    def fake_check_response_safety(response):
        calls["check_response_safety"].append(response)
        return SimpleNamespace(passed=False, reason="flagged-reason", flagged_patterns=["p1", "p2"])

    async def fake_stage1_5_normalize_styles(stage1_results):
        return stage1_results, {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "cost_usd": 0.0}

    async def fake_stage2_collect_rankings(
        user_query, responses_for_review, timeout=120.0, models=None,
        on_progress=None, on_review_event=None,
    ):
        calls["stage2_call"] = (user_query, responses_for_review, timeout)
        label_to_model = {
            f"Response {chr(65 + i)}": {"model": r["model"]}
            for i, r in enumerate(responses_for_review)
        }
        stage2_results = [{"model": responses_for_review[0]["model"], "parsed_ranking": {"evaluations": {}}}]
        return stage2_results, label_to_model, {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9, "cost_usd": 0.01}

    def fake_calculate_aggregate_rankings(
        stage2_results, label_to_model, voting_authorities=None, return_shadow_votes=False
    ):
        return [
            {"model": entry["model"], "borda_score": 1.0, "average_position": 1.5}
            for entry in label_to_model.values()
        ]

    async def fake_stage3_synthesize_final(
        user_query, stage1_results, stage2_results, aggregate_rankings=None,
        verdict_type=None, timeout=120.0, dispositions_instruction=None,
        on_synthesis_delta=None,
    ):
        calls["stage3_call"] = (user_query, stage1_results, stage2_results, aggregate_rankings, verdict_type, timeout)
        return (
            {"model": stage1_results[0]["model"], "response": "final synthesis"},
            {"prompt_tokens": 6, "completion_tokens": 7, "total_tokens": 13, "cost_usd": 0.02},
            None,
        )

    def fake_emit_usage_metrics(usage, adapter=None):
        calls["emit_usage_metrics_arg"] = usage

    def fake_calculate_quality_metrics(**kwargs):
        calls["quality_metrics_call"] = kwargs
        return _real_quality_metrics(0.77)

    def fake_get_config():
        return _make_config(safety_enabled=True, models=models)

    fakes = {
        "query_models_resilient": _as_resilient(fake_query_models_parallel),
        "check_response_safety": fake_check_response_safety,
        "stage1_5_normalize_styles": fake_stage1_5_normalize_styles,
        "stage2_collect_rankings": fake_stage2_collect_rankings,
        "calculate_aggregate_rankings": fake_calculate_aggregate_rankings,
        "stage3_synthesize_final": fake_stage3_synthesize_final,
        "emit_usage_metrics": fake_emit_usage_metrics,
        "should_include_quality_metrics": lambda: True,
        "calculate_quality_metrics": fake_calculate_quality_metrics,
        "get_config": fake_get_config,
        # _build_usage_summary / _add_cost_to_usage deliberately NOT faked.
    }
    for name, fn in fakes.items():
        _patch(monkeypatch, name, fn)

    stage1_results, stage2_results, stage3_result, metadata = asyncio.run(
        ca.run_council_with_timeouts(
            "the exact query", stage1_timeout=11.0, stage2_timeout=22.0, stage3_timeout=33.0
        )
    )

    # --- Stage 1 call: messages shape + timeout threaded correctly ---
    # Proposal A Contract 1 (docs/specs/proposal-a-reference-grounding-contract.md):
    # Stage 1's messages carry the original query verbatim plus a uniform,
    # byte-identical reference-reporting instruction appended via
    # build_stage1_prompt - never the raw query alone.
    stage1_messages = calls["query_models_parallel_messages"]
    assert len(stage1_messages) == 1
    assert stage1_messages[0]["role"] == "user"
    assert stage1_messages[0]["content"] == ca.build_stage1_prompt("the exact query")
    assert stage1_messages[0]["content"].startswith("the exact query")
    assert calls["query_models_parallel_timeout"] == 11.0

    # --- stage1_results: exact "content" extraction + safety_check shape ---
    by_model = {r["model"]: r for r in stage1_results}
    assert set(by_model) == set(models)
    for m in models:
        assert by_model[m]["response"] == f"content-from-{m}"
        assert by_model[m]["safety_check"] == {
            "passed": False,
            "reason": "flagged-reason",
            "flagged_patterns": ["p1", "p2"],
        }
    assert set(calls["check_response_safety"]) == {f"content-from-{m}" for m in models}

    # --- stage1 usage: real accumulation, no off-by-one, no wrong operator ---
    stage1_usage = metadata["usage"]["by_stage"]["stage1"]
    assert stage1_usage["prompt_tokens"] == 60
    assert stage1_usage["completion_tokens"] == 25
    assert stage1_usage["total_tokens"] == 85
    assert stage1_usage["cost_usd"] == pytest.approx(0.006)

    # --- stage1_5/stage2/stage3 usage: straight assignment, exact keys ---
    assert metadata["usage"]["by_stage"]["stage1_5"] == {
        "prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "cost_usd": 0.0,
    }
    assert metadata["usage"]["by_stage"]["stage2"] == {
        "prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9, "cost_usd": 0.01,
    }
    assert metadata["usage"]["by_stage"]["stage3"] == {
        "prompt_tokens": 6, "completion_tokens": 7, "total_tokens": 13, "cost_usd": 0.02,
    }

    # --- grand total sums every stage ---
    assert metadata["usage"]["total"]["prompt_tokens"] == 71
    assert metadata["usage"]["total"]["completion_tokens"] == 39
    assert metadata["usage"]["total"]["total_tokens"] == 110

    # --- _add_cost_to_usage(model=model) really threads the model kwarg ---
    assert set(metadata["usage"]["by_model"]) == set(models)
    assert metadata["usage"]["by_model"]["model-a"]["prompt_tokens"] == 10

    # --- emit_usage_metrics receives the SAME object _build_usage_summary made ---
    assert calls["emit_usage_metrics_arg"] == metadata["usage"]

    # --- no degraded_mode key at all when 3+ models succeed ---
    assert "degraded_mode" not in metadata

    # --- stage2/stage3 receive the right, unswapped args ---
    stage2_user_query, stage2_responses, stage2_timeout = calls["stage2_call"]
    assert stage2_user_query == "the exact query"
    assert stage2_responses == stage1_results
    assert stage2_timeout == 22.0

    s3_query, s3_stage1, s3_stage2, s3_rankings, s3_verdict, s3_timeout = calls["stage3_call"]
    assert s3_query == "the exact query"
    assert s3_stage1 == stage1_results
    assert s3_stage2 == stage2_results
    assert s3_rankings == metadata["aggregate_rankings"]
    assert s3_verdict == VerdictType.SYNTHESIS
    assert s3_timeout == 33.0

    # --- quality metrics: exact stage1_dict / rankings_tuples construction ---
    qm_call = calls["quality_metrics_call"]
    assert qm_call["stage1_responses"] == {m: {"content": f"content-from-{m}"} for m in models}
    assert qm_call["stage2_rankings"] == stage2_results
    assert qm_call["stage3_synthesis"] == stage3_result
    assert set(qm_call["aggregate_rankings"]) == {(m, 1.5) for m in models}
    assert qm_call["label_to_model"] == metadata["label_to_model"]
    assert metadata["quality_metrics"]["core"]["consensus_strength"] == 0.77


def test_missing_content_key_in_stage1_response_defaults_to_empty_string(monkeypatch):
    # The "content" fallback default is only reachable when a Stage-1
    # response genuinely lacks a "content" key - every other test always
    # supplies one, which leaves that default's own value unobserved. Uses
    # 3 models (not 1) so this stays on the fully-faked normal path rather
    # than falling through to the real (unfaked) stage3_synthesize_final.
    models = ["model-a", "model-b", "model-c"]

    async def fake_query_models_parallel(models_arg, messages, disable_tools=False, timeout=120.0):
        result = {m: {"content": f"content-from-{m}"} for m in models_arg}
        result["model-a"] = {}  # no "content" key at all
        return result

    _install_happy_path_fakes(monkeypatch, models, query_models_parallel_fn=fake_query_models_parallel)

    stage1_results, _, _, _ = asyncio.run(ca.run_council_with_timeouts("q"))

    by_model = {r["model"]: r["response"] for r in stage1_results}
    assert by_model["model-a"] == ""
    assert by_model["model-b"] == "content-from-model-b"


def test_single_model_branch_degraded_mode_and_untouched_stage1_5_stage2_usage(monkeypatch):
    models = ["model-solo"]
    calls = {"stage3_call": None}

    async def fake_query_models_parallel(models_arg, messages, disable_tools=False, timeout=120.0):
        return {
            "model-solo": {
                "content": "solo-content",
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            }
        }

    async def fake_stage3_synthesize_final(
        user_query, stage1_results, stage2_results, aggregate_rankings=None,
        verdict_type=None, timeout=120.0, dispositions_instruction=None,
        on_synthesis_delta=None,
    ):
        calls["stage3_call"] = (stage2_results, aggregate_rankings)
        return (
            {"model": "model-solo", "response": "solo synthesis"},
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost_usd": 0.0},
            None,
        )

    async def fail_if_called(*a, **k):
        raise AssertionError("stage1_5/stage2 must be skipped in the single-model branch")

    def fail_agg(*a, **k):
        raise AssertionError("calculate_aggregate_rankings must be skipped in the single-model branch")

    _patch(monkeypatch, "query_models_resilient", _as_resilient(fake_query_models_parallel))
    _patch(monkeypatch, "stage3_synthesize_final", fake_stage3_synthesize_final)
    _patch(monkeypatch, "get_config", lambda: _make_config(safety_enabled=False, models=models))
    _patch(monkeypatch, "should_include_quality_metrics", lambda: False)
    _patch(monkeypatch, "stage1_5_normalize_styles", fail_if_called)
    _patch(monkeypatch, "stage2_collect_rankings", fail_if_called)
    _patch(monkeypatch, "calculate_aggregate_rankings", fail_agg)

    stage1_results, stage2_results, stage3_result, metadata = asyncio.run(
        ca.run_council_with_timeouts("solo query")
    )

    assert stage2_results == []
    assert metadata["degraded_mode"] == "single_model"
    assert metadata["label_to_model"] == {"Response A": {"model": "model-solo", "display_index": 0}}
    assert metadata["aggregate_rankings"] == [
        {
            "model": "model-solo",
            "rank": 1,
            "average_score": None,
            "average_position": None,
            "vote_count": 0,
            "note": "Single model - no peer review",
        }
    ]
    assert calls["stage3_call"] == ([], metadata["aggregate_rankings"])

    # stage1_5/stage2 total_usage entries retain their untouched initial
    # values in this branch (never reassigned) - pins both the initial 0
    # values and the exact "stage1_5"/"stage2" key names.
    assert metadata["usage"]["by_stage"]["stage1_5"] == {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    }
    assert metadata["usage"]["by_stage"]["stage2"] == {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    }
    assert metadata["usage"]["by_stage"]["stage1"]["prompt_tokens"] == 7
    assert metadata["usage"]["by_stage"]["stage1"]["completion_tokens"] == 3
    assert metadata["usage"]["by_stage"]["stage1"]["total_tokens"] == 10


def test_single_model_branch_still_computes_quality_metrics_when_enabled(monkeypatch):
    # `len(stage1_results) > 0` gates quality-metrics computation - since
    # stage1_results length always equals num_responses, and the ==0 case
    # already returned early, this boundary (> 0 vs > 1) is ONLY
    # distinguishable when num_responses is exactly 1. The other
    # single-model test above always disables quality metrics, which never
    # reaches this line at all.
    models = ["model-solo"]

    async def fake_query_models_parallel(models_arg, messages, disable_tools=False, timeout=120.0):
        return {"model-solo": {"content": "solo-content"}}

    async def fake_stage3_synthesize_final(
        user_query, stage1_results, stage2_results, aggregate_rankings=None,
        verdict_type=None, timeout=120.0, dispositions_instruction=None,
        on_synthesis_delta=None,
    ):
        return (
            {"model": "model-solo", "response": "solo synthesis"},
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            None,
        )

    quality_calls = []

    def fake_calculate_quality_metrics(**kwargs):
        quality_calls.append(kwargs)
        return _real_quality_metrics(0.9)

    _patch(monkeypatch, "query_models_resilient", _as_resilient(fake_query_models_parallel))
    _patch(monkeypatch, "stage3_synthesize_final", fake_stage3_synthesize_final)
    _patch(monkeypatch, "get_config", lambda: _make_config(safety_enabled=False, models=models))
    _patch(monkeypatch, "should_include_quality_metrics", lambda: True)
    _patch(monkeypatch, "calculate_quality_metrics", fake_calculate_quality_metrics)

    _, _, _, metadata = asyncio.run(ca.run_council_with_timeouts("solo query"))

    assert len(quality_calls) == 1
    assert metadata["quality_metrics"]["core"]["consensus_strength"] == 0.9


def test_two_model_branch_degraded_mode_note_and_stage1_5_stage2_are_called(monkeypatch):
    models = ["model-x", "model-y"]
    calls = _install_happy_path_fakes(monkeypatch, models, include_quality_metrics=False)

    stage1_results, stage2_results, stage3_result, metadata = asyncio.run(
        ca.run_council_with_timeouts("two-model query")
    )

    assert metadata["degraded_mode"] == "two_models"
    assert len(metadata["aggregate_rankings"]) >= 1
    for r in metadata["aggregate_rankings"]:
        assert r["note"] == "Two-model council - rankings based on single vote"
    # Unlike the single-model branch, stage1_5/stage2 ARE reached here.
    assert calls["stage2_timeout"] is not None


def test_zero_responses_error_tuple_has_exact_response_text(monkeypatch):
    models = ["model-a", "model-b"]

    async def fake_query_models_parallel(models_arg, messages, disable_tools=False, timeout=120.0):
        return {m: None for m in models_arg}

    _patch(monkeypatch, "query_models_resilient", _as_resilient(fake_query_models_parallel))
    _patch(monkeypatch, "get_config", lambda: _make_config(safety_enabled=False, models=models))

    _, _, stage3_result, _ = asyncio.run(ca.run_council_with_timeouts("q"))

    assert stage3_result == {
        "model": "error",
        "response": "All models failed to respond. Please try again.",
    }


# ---------------------------------------------------------------------------
# Mutation-gate hardening round 3 (2026-08-12): the round-2 tests never
# actually observe a few paths that only surface through specific inputs -
# the zero-response early return's full total_usage snapshot (stage3's own
# init literal is otherwise always clobbered by the unconditional `total_
# usage["stage3"] = stage3_usage` assignment on every non-early-return path,
# so it's only ever OBSERVABLE via this branch), the stage1 per-model usage
# accumulator's OWN missing-subkey defaults (round 2's fixture always
# supplies every usage subkey), the safety_check getattr fallback chain's
# SECOND rung (`.safe`/absent -> True) which a `passed`-bearing double can
# never reach, and the rankings_tuples inner fallback-to-borda_score/0.0
# chain (round 2's aggregate_rankings fixture always supplies
# "average_position", which short-circuits the outer .get before the inner
# one's RESULT is ever read - even though it's still eagerly evaluated).
# ---------------------------------------------------------------------------


def test_zero_responses_metadata_usage_is_fully_zeroed_across_all_four_stages(monkeypatch):
    models = ["model-a", "model-b"]

    async def fake_query_models_parallel(models_arg, messages, disable_tools=False, timeout=120.0):
        return {m: None for m in models_arg}

    _patch(monkeypatch, "query_models_resilient", _as_resilient(fake_query_models_parallel))
    _patch(monkeypatch, "get_config", lambda: _make_config(safety_enabled=False, models=models))

    _, _, _, metadata = asyncio.run(ca.run_council_with_timeouts("q"))

    assert metadata["usage"] == {
        "stage1": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "stage1_5": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "stage2": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "stage3": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def test_stage1_usage_missing_subkeys_default_to_zero_not_one(monkeypatch):
    # A response whose "usage" dict is missing prompt_tokens/completion_
    # tokens/total_tokens entirely (a legitimate real-world shape - not
    # every provider reports every field) must accumulate as 0 contribution
    # for the missing field, never silently invent a nonzero one.
    models = ["model-a", "model-b", "model-c"]

    async def fake_query_models_parallel(models_arg, messages, disable_tools=False, timeout=120.0):
        return {m: {"content": f"c-{m}", "usage": {}} for m in models_arg}

    _install_happy_path_fakes(monkeypatch, models, query_models_parallel_fn=fake_query_models_parallel)

    captured_by_stage = {}

    def fake_build_usage_summary(by_stage):
        captured_by_stage["value"] = by_stage
        return {"total": {"cost_usd": 0.0}, "by_model": {}}

    _patch(monkeypatch, "_build_usage_summary", fake_build_usage_summary)

    asyncio.run(ca.run_council_with_timeouts("q"))

    assert captured_by_stage["value"]["stage1"]["prompt_tokens"] == 0
    assert captured_by_stage["value"]["stage1"]["completion_tokens"] == 0
    assert captured_by_stage["value"]["stage1"]["total_tokens"] == 0


def test_safety_check_falls_back_through_safe_attribute_then_true_default(monkeypatch):
    # The contract's own getattr-fallback-chain design (module docstring:
    # "tolerate a test double that doesn't mirror SafetyCheckResult's exact
    # shape") is only exercised by a double that genuinely lacks "passed" -
    # every existing test's double already has "passed" set directly, which
    # short-circuits getattr before the ".safe"/True fallback is ever read.
    models = ["model-a", "model-b"]

    doubles_by_model = {
        "model-a": SimpleNamespace(safe=False, reason="r-a", flagged_patterns=["x"]),
        "model-b": SimpleNamespace(),  # neither "passed" nor "safe" nor "reason"/"flagged_patterns"
    }

    def fake_check_response_safety(response):
        # response text is "body::<model>" - split unambiguously on "::".
        model = response.split("::")[-1]
        return doubles_by_model[model]

    async def fake_query_models_parallel(models_arg, messages, disable_tools=False, timeout=120.0):
        return {m: {"content": f"body::{m}"} for m in models_arg}

    _install_happy_path_fakes(
        monkeypatch, models, safety_enabled=True,
        query_models_parallel_fn=fake_query_models_parallel,
    )
    _patch(monkeypatch, "check_response_safety", fake_check_response_safety)

    stage1_results, _, _, _ = asyncio.run(ca.run_council_with_timeouts("q"))

    by_model = {r["model"]: r for r in stage1_results}
    assert by_model["model-a"]["safety_check"] == {
        "passed": False, "reason": "r-a", "flagged_patterns": ["x"],
    }
    assert by_model["model-b"]["safety_check"] == {
        "passed": True, "reason": None, "flagged_patterns": [],
    }


def test_rankings_tuples_falls_back_to_borda_score_then_zero_when_average_position_absent(monkeypatch):
    # calculate_quality_metrics's rankings_tuples input reads
    # r.get("average_position", r.get("borda_score", 0.0)) - the INNER
    # r.get(...) is eagerly evaluated on every call (it's a plain
    # expression, not lazily short-circuited), but round 2's fixture always
    # supplies "average_position" so the inner value, key name, and its own
    # 0.0 default are never what actually reaches the output tuple.
    models = ["model-a", "model-b", "model-c"]
    calls = _install_happy_path_fakes(monkeypatch, models, include_quality_metrics=True)

    def fake_calculate_aggregate_rankings(stage2_results, label_to_model, **kw):
        return [
            {"model": "model-a", "borda_score": 3.3},  # no average_position -> falls back to borda_score
            {"model": "model-b"},  # neither key -> falls back all the way to 0.0
            {"model": "model-c", "average_position": 2.2, "borda_score": 9.9},  # average_position wins
        ]

    _patch(monkeypatch, "calculate_aggregate_rankings", fake_calculate_aggregate_rankings)

    asyncio.run(ca.run_council_with_timeouts("q"))

    rankings_tuples = calls["quality_metrics"][0][1]["aggregate_rankings"]
    assert set(rankings_tuples) == {("model-a", 3.3), ("model-b", 0.0), ("model-c", 2.2)}


def test_normal_branch_threads_real_stage2_results_into_calculate_aggregate_rankings(monkeypatch):
    models = ["model-a", "model-b", "model-c"]
    captured = {}

    def fake_calculate_aggregate_rankings(stage2_results, label_to_model, **kw):
        captured["stage2_results"] = stage2_results
        return [{"model": m, "borda_score": 1.0} for m in models]

    calls = _install_happy_path_fakes(monkeypatch, models)
    _patch(monkeypatch, "calculate_aggregate_rankings", fake_calculate_aggregate_rankings)

    stage1_results, stage2_results, _, _ = asyncio.run(ca.run_council_with_timeouts("q"))

    assert captured["stage2_results"] is not None
    assert captured["stage2_results"] == stage2_results


def test_verified_facts_are_threaded_into_stage3_query_via_build_facts_section(monkeypatch):
    # Proposal A Contract 3 (docs/specs/proposal-a-reference-grounding-
    # contract.md): a non-empty verified_facts list must reach Stage 3's
    # query as user_query + the REAL _build_facts_section(verified_facts)
    # rendering - not a stub, not None, and not built from a different
    # (e.g. empty) list. _build_facts_section is deliberately left
    # UNPATCHED here (it's pure/hermetic) so the composed string is pinned
    # exactly against production code, not a test double's guess.
    from scripts.grounding_pass import Claim, TaggedClaim
    from scripts.revision_round import _build_facts_section

    models = ["model-a", "model-b"]
    captured = {}

    async def fake_stage3_synthesize_final(
        user_query, stage1_results, stage2_results, aggregate_rankings=None,
        verdict_type=None, timeout=120.0, dispositions_instruction=None,
        on_synthesis_delta=None,
    ):
        captured["stage3_query"] = user_query
        return {"model": stage1_results[0]["model"], "response": "final synthesis"}, {}, None

    _install_happy_path_fakes(monkeypatch, models)
    _patch(monkeypatch, "stage3_synthesize_final", fake_stage3_synthesize_final)

    facts = [
        TaggedClaim(claim=Claim(id="1", text="the sky is blue"), tag="VERIFIED", evidence=[]),
        TaggedClaim(claim=Claim(id="2", text="water is wet"), tag="UNVERIFIABLE", evidence=[]),
    ]

    asyncio.run(ca.run_council_with_timeouts("original query", verified_facts=facts))

    expected = f"original query\n\n{_build_facts_section(facts)}"
    assert captured["stage3_query"] == expected
    # Guards specifically against a mutant that swaps the real facts list
    # for an empty/None one when building the facts section: the rendered
    # facts section must actually mention the claim text, not fall back to
    # "(no verified facts available)".
    assert "the sky is blue" in captured["stage3_query"]
    assert "(no verified facts available)" not in captured["stage3_query"]


def test_empty_verified_facts_list_leaves_stage3_query_as_plain_user_query(monkeypatch):
    # The default-empty-list / falsy branch: no facts section at all, byte-
    # identical to the plain user_query (never routed through
    # _build_facts_section for the else branch).
    models = ["model-a"]
    captured = {}

    async def fake_stage3_synthesize_final(
        user_query, stage1_results, stage2_results, aggregate_rankings=None,
        verdict_type=None, timeout=120.0, dispositions_instruction=None,
        on_synthesis_delta=None,
    ):
        captured["stage3_query"] = user_query
        return {"model": stage1_results[0]["model"], "response": "final synthesis"}, {}, None

    _install_happy_path_fakes(monkeypatch, models)
    _patch(monkeypatch, "stage3_synthesize_final", fake_stage3_synthesize_final)

    asyncio.run(ca.run_council_with_timeouts("original query", verified_facts=[]))

    assert captured["stage3_query"] == "original query"
