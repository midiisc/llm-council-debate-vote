"""Tests for the Stage-1 wall-clock deadline computation added to
`scripts/council_adapter.py::run_council_with_timeouts`
(docs/specs/wallclock-cost-budget-contract.md, Contract 1, AC6-7) - closes
architecture-stress-test-2026-08-13.md's Critical #3 ("Wall-clock ceiling
is undersized against the retry+backup engine's own worst case").

`scripts/resilient_query.py::query_models_resilient` itself already accepts
a `deadline`/`time_fn` pair (tested directly in
tests/test_resilient_query.py) - this file only covers the NEW piece:
`run_council_with_timeouts` computing `deadline = now + overall_wall_clock_seconds
* stage1_deadline_fraction` and passing it through, given a new optional
`overall_wall_clock_seconds` parameter.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

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


def _install_minimal_fakes(monkeypatch, models, resilient_query_spy):
    _patch(monkeypatch, "_get_council_models", lambda: list(models))
    _patch(
        monkeypatch,
        "get_config",
        lambda: SimpleNamespace(
            evaluation=SimpleNamespace(safety=SimpleNamespace(enabled=False)),
            council=SimpleNamespace(models=list(models)),
        ),
    )
    monkeypatch.setattr(ca, "query_models_resilient", resilient_query_spy)

    async def fake_stage1_5(stage1_results):
        return stage1_results, {}

    async def fake_stage2(user_query, responses_for_review, timeout=120.0, **kw):
        return [], {}, {}

    def fake_aggregate(stage2_results, label_to_model, **kw):
        return []

    async def fake_stage3(user_query, stage1_results, stage2_results, **kw):
        return {"model": "m", "response": "synthesis"}, {}, None

    _patch(monkeypatch, "stage1_5_normalize_styles", fake_stage1_5)
    _patch(monkeypatch, "stage2_collect_rankings", fake_stage2)
    _patch(monkeypatch, "calculate_aggregate_rankings", fake_aggregate)
    _patch(monkeypatch, "stage3_synthesize_final", fake_stage3)


def _make_spy():
    calls = []

    async def spy(primary_models, backup_models, messages, timeout, query_fn,
                  retry_policy=None, minimum_council_size=4, sleep_fn=None,
                  deadline=None, time_fn=None):
        calls.append({"deadline": deadline, "time_fn": time_fn})
        return ResilientQueryResult(
            responses={m: {"content": "x", "usage": {}} for m in primary_models},
            attempts=[], substitutions=[], unreachable_models=[], shortfall_warning=None,
        )

    return spy, calls


def _run(coro):
    return asyncio.run(coro)


def test_deadline_is_none_when_overall_wall_clock_seconds_not_given(monkeypatch):
    spy, calls = _make_spy()
    _install_minimal_fakes(monkeypatch, ["model-a"], spy)

    _run(ca.run_council_with_timeouts("q"))

    assert calls[0]["deadline"] is None


def test_deadline_is_computed_as_half_of_overall_wall_clock_seconds_by_default(monkeypatch):
    spy, calls = _make_spy()
    _install_minimal_fakes(monkeypatch, ["model-a"], spy)

    fixed_now = 1_000_000.0
    monkeypatch.setattr(ca.time, "monotonic", lambda: fixed_now)

    _run(ca.run_council_with_timeouts("q", overall_wall_clock_seconds=1200.0))

    assert calls[0]["deadline"] == fixed_now + 600.0  # 1200.0 * 0.5 default fraction


def test_deadline_fraction_is_configurable(monkeypatch):
    spy, calls = _make_spy()
    _install_minimal_fakes(monkeypatch, ["model-a"], spy)

    fixed_now = 0.0
    monkeypatch.setattr(ca.time, "monotonic", lambda: fixed_now)

    _run(
        ca.run_council_with_timeouts(
            "q", overall_wall_clock_seconds=1000.0, stage1_deadline_fraction=0.25
        )
    )

    assert calls[0]["deadline"] == 250.0


def test_default_deadline_fraction_is_one_half():
    assert ca.DEFAULT_STAGE1_DEADLINE_FRACTION == 0.5
