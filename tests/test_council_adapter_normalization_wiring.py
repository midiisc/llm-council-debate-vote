"""Blind acceptance tests for docs/specs/stage1-5-normalizer-timeout-contract.md
(AC7-AC8) -- `run_council_with_timeouts`'s new `stage1_5_timeout` parameter
and its `metadata["normalization_failures"]` surfacing.

Authored WITHOUT sight of any implementation. As of this writing,
`run_council_with_timeouts` has no `stage1_5_timeout` parameter, calls
`stage1_5_normalize_styles` directly (not `_normalize_responses_with_
timeout`), and never assembles `metadata["normalization_failures"]` --
every test here is expected to fail RED (unexpected-kwarg TypeError, or a
metadata dict missing the new key) until the contract lands.

Harness pattern (dual-location patching, `query_models_resilient`/
`ResilientQueryResult` adaptation, `_make_config` shape, `random.shuffle`
neutralization) mirrors `tests/test_council_adapter.py`'s own established
`_install_happy_path_fakes`/`_patch`/`_as_resilient` conventions (per this
contract's own "Environment for blind test authorship" instruction to
reuse existing fixture/patching style rather than inventing a new one) --
reproduced locally here (not imported cross-file) because call site 1/2 of
this contract replace `stage1_5_normalize_styles` with the new
`_normalize_responses_with_timeout`/amended `_normalize_stage2_for_stage3`,
which the existing file's harness does not fake. Patching these two new
call-site functions directly (rather than reaching all the way down to
`query_model_with_status`) is justified by the contract's own literal
call-site code given in the "Design" section -- both are bare module-level
names called directly from within `run_council_with_timeouts`, in the same
module, which `monkeypatch.setattr(ca, name, fake)` correctly intercepts.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

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
from scripts.resilient_query import ResilientQueryResult


def _patch(monkeypatch, name, fake):
    monkeypatch.setattr(_council_module, name, fake, raising=False)
    monkeypatch.setattr(ca, name, fake, raising=False)


def _as_resilient(query_models_parallel_like):
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
        raw = await query_models_parallel_like(primary_models, messages, timeout=timeout)
        return ResilientQueryResult(
            responses={m: r for m, r in raw.items() if r is not None},
            attempts=[],
            substitutions=[],
            unreachable_models=[m for m, r in raw.items() if r is None],
            shortfall_warning=None,
        )

    return fake_query_models_resilient


def _make_config(models):
    return SimpleNamespace(
        evaluation=SimpleNamespace(
            safety=SimpleNamespace(enabled=False),
            rubric=SimpleNamespace(
                enabled=True,
                weights={
                    "accuracy": 0.3,
                    "relevance": 0.25,
                    "completeness": 0.2,
                    "conciseness": 0.15,
                    "clarity": 0.1,
                },
            ),
        ),
        council=SimpleNamespace(models=models, chairman="fake-chairman-model"),
    )


def _install_normalization_wiring_harness(
    monkeypatch,
    models,
    stage1_5_failed=None,
    stage2_normalize_failed=None,
):
    calls = {"stage1_5_timeout": None, "stage2_normalize_timeout": None}

    async def default_query_models_parallel(models_arg, messages, disable_tools=False, timeout=120.0):
        if messages and "<responses_to_evaluate>" in messages[0]["content"]:
            return {
                m: {
                    "content": (
                        f"Evaluation from {m}.\n"
                        '```json\n{"ranking": ["Response A"], "scores": {"Response A": 8}}\n```'
                    )
                }
                for m in models_arg
            }
        return {m: {"content": f"answer-from-{m} [unverified]"} for m in models_arg}

    def fake_check_response_safety(response):
        return SimpleNamespace(passed=True, reason=None, flagged_patterns=[])

    async def fake_normalize_responses_with_timeout(entries, timeout):
        calls["stage1_5_timeout"] = timeout
        normalized = [
            {"model": e["model"], "response": e["response"], "original_response": e["response"]}
            for e in entries
        ]
        return normalized, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, list(
            stage1_5_failed or []
        )

    async def fake_normalize_stage2_for_stage3(stage2_results, timeout):
        calls["stage2_normalize_timeout"] = timeout
        return list(stage2_results), {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, list(
            stage2_normalize_failed or []
        )

    def fake_calculate_aggregate_rankings(stage2_results, label_to_model, voting_authorities=None, return_shadow_votes=False):
        return [
            {"model": entry["model"], "borda_score": 1.0, "rank": i + 1}
            for i, entry in enumerate(label_to_model.values())
        ]

    async def fake_stage3_synthesize_final(
        user_query, stage1_results, stage2_results, aggregate_rankings=None,
        verdict_type=None, timeout=120.0, dispositions_instruction=None,
        on_synthesis_delta=None,
    ):
        return {"model": stage1_results[0]["model"], "response": "final synthesis"}, {}, None

    def fake_build_usage_summary(by_stage):
        return {"total": {"cost_usd": 0.0}, "by_model": {}}

    def fake_emit_usage_metrics(usage, adapter=None):
        return None

    def fake_should_include_quality_metrics():
        return False

    def fake_get_config():
        return _make_config(models)

    fakes = {
        "query_models_resilient": _as_resilient(default_query_models_parallel),
        "check_response_safety": fake_check_response_safety,
        "_normalize_responses_with_timeout": fake_normalize_responses_with_timeout,
        "_normalize_stage2_for_stage3": fake_normalize_stage2_for_stage3,
        "calculate_aggregate_rankings": fake_calculate_aggregate_rankings,
        "stage3_synthesize_final": fake_stage3_synthesize_final,
        "_build_usage_summary": fake_build_usage_summary,
        "emit_usage_metrics": fake_emit_usage_metrics,
        "should_include_quality_metrics": fake_should_include_quality_metrics,
        "get_config": fake_get_config,
    }
    for name, fn in fakes.items():
        _patch(monkeypatch, name, fn)
    monkeypatch.setattr(ca.random, "shuffle", lambda seq: None, raising=False)
    return calls


MODELS = ["model-a", "model-b", "model-c", "model-d"]


# ---------------------------------------------------------------------------
# AC7 -- normalization_failures surfaced in metadata (present only when
# non-empty, exact concatenation of stage1.5 + stage2 failures).
# ---------------------------------------------------------------------------


def test_ac7_normalization_failures_present_and_concatenated_when_nonempty(monkeypatch):
    _install_normalization_wiring_harness(
        monkeypatch, MODELS, stage1_5_failed=["model-b"], stage2_normalize_failed=["model-c"]
    )

    _, _, _, metadata = asyncio.run(ca.run_council_with_timeouts("the query"))

    assert metadata["normalization_failures"] == ["model-b", "model-c"]


def test_ac7_normalization_failures_absent_from_metadata_when_no_failures(monkeypatch):
    _install_normalization_wiring_harness(
        monkeypatch, MODELS, stage1_5_failed=[], stage2_normalize_failed=[]
    )

    _, _, _, metadata = asyncio.run(ca.run_council_with_timeouts("the query"))

    assert "normalization_failures" not in metadata


# ---------------------------------------------------------------------------
# Property test: AC7's law -- metadata["normalization_failures"] is present
# iff the concatenation of (stage1.5 failures, stage2 failures) is
# non-empty, and when present it equals EXACTLY that concatenation in
# order (stage1.5 failures first, stage2 failures second) -- never
# deduplicated, reordered, or partially dropped. Capped max_examples=50,
# derandomize=True per project convention.
# ---------------------------------------------------------------------------


@settings(max_examples=50, derandomize=True, deadline=3000, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    stage1_5_failed=st.lists(st.sampled_from(MODELS), max_size=4),
    stage2_failed=st.lists(st.sampled_from(MODELS), max_size=4),
)
def test_property_normalization_failures_metadata_mirrors_concatenation_law(
    monkeypatch, stage1_5_failed, stage2_failed
):
    _install_normalization_wiring_harness(
        monkeypatch, MODELS, stage1_5_failed=stage1_5_failed, stage2_normalize_failed=stage2_failed
    )

    _, _, _, metadata = asyncio.run(ca.run_council_with_timeouts("some query"))

    expected = list(stage1_5_failed) + list(stage2_failed)
    if expected:
        assert metadata["normalization_failures"] == expected
    else:
        assert "normalization_failures" not in metadata


# ---------------------------------------------------------------------------
# AC8 -- stage1_5_timeout defaults to 300.0 and threads to both call sites;
# an explicit value threads to both call sites too.
# ---------------------------------------------------------------------------


def test_ac8_stage1_5_timeout_defaults_to_300_and_threads_to_both_call_sites(monkeypatch):
    calls = _install_normalization_wiring_harness(monkeypatch, MODELS)

    asyncio.run(ca.run_council_with_timeouts("the query"))

    assert calls["stage1_5_timeout"] == 300.0
    assert calls["stage2_normalize_timeout"] == 300.0


def test_ac8_explicit_stage1_5_timeout_threads_to_both_call_sites(monkeypatch):
    calls = _install_normalization_wiring_harness(monkeypatch, MODELS)

    asyncio.run(ca.run_council_with_timeouts("the query", stage1_5_timeout=45.0))

    assert calls["stage1_5_timeout"] == 45.0
    assert calls["stage2_normalize_timeout"] == 45.0
