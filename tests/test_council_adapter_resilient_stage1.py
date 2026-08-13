"""Blind acceptance tests for the 2026-08-12 "resilient Stage 1" amendment to
docs/specs/pipeline-runner-contract.md (AC17-21, AC23) -- the `scripts/
council_adapter.py` half of the integration between `scripts/
resilient_query.py::query_models_resilient` (already implemented,
mutation-gate clean, and separately blind-TDV-tested in
tests/test_resilient_query.py against docs/specs/debate-resilience-
contract.md) and Stage 1 of `run_council_with_timeouts`.

The `pipeline_runner.py` companion change (debug_log surfacing of
`metadata["shortfall_warning"]`/`metadata["substitutions"]`, AC22 + the
substitution-NOTE-line prose) is covered separately in
tests/test_pipeline_runner_resilient_stage1_amendment.py.

Authored WITHOUT sight of any implementation. As of this writing,
`council_adapter.py` has no `DebateResilienceConfig`, no
`_load_debate_resilience_config`, and Stage 1 still calls
`query_models_parallel` directly (confirmed by reading the current file
before authoring these tests, per the same "read the pre-feature file to
recover accurate import paths/signatures" allowance already exercised by
tests/test_council_adapter.py for the AC11-14 amendment) -- every test in
this file is expected to fail at import/collection time or with
AttributeError (RED) until the amendment lands.

DOCUMENTED ASSUMPTIONS (contract pins names/shapes but not every wiring
detail):

  1. **Patch location for `query_models_resilient`.** The contract's own
     module docstring precedent (`council_adapter.py`'s existing "Module-
     level (not function-local) imports are deliberate ... independently
     monkeypatchable by name" comment) implies `query_models_resilient` is
     imported by name into `council_adapter.py`'s module namespace. Tests
     that need to replace it patch BOTH `scripts.resilient_query
     .query_models_resilient` (covers `resilient_query.query_models_
     resilient(...)`-style access) and `council_adapter.query_models_
     resilient` if present post-import (covers a direct `from ... import`),
     via the `_patch` helper below -- same pattern
     tests/test_council_adapter.py already uses for every other patched
     dependency.
  2. **Patch location for the injected `query_fn`.** The contract names it
     as `query_model_with_status` from `llm_council.gateway_adapter`
     (pipeline-runner-contract.md's own "Decision" paragraph) while
     `resilient_query.py`'s docstring separately cites
     `llm_council.openrouter.query_model_with_status`. Both are real,
     distinct functions in the installed package (confirmed: `gateway_
     adapter.query_model_with_status` wraps a "direct" query function;
     `openrouter.query_model_with_status` is a separate top-level
     coroutine). AC23's test patches BOTH host modules' attributes plus
     `council_adapter`'s own attribute if present, so it passes regardless
     of which one the implementation actually imports.
  3. **`_load_debate_resilience_config`'s `config_path` parameter.** The
     signature exposes `config_path: Optional[Path] = None` specifically
     as an injection point for tests (mirrors this repo's established
     "expose an override to bypass search-order plumbing for hermetic
     tests" pattern, e.g. `PipelineConfig.output_root`). AC18/AC19 tests
     therefore call it with an explicit `config_path` pointing at a
     temp file, assuming that when given, the file at that exact path is
     read directly rather than triggering `_find_config_file`'s env-var/
     cwd/home search order.
  4. **Stage-1 response dict shape.** Per debate-resilience-contract.md,
     `ResilientQueryResult.responses[model]` is "query_fn's own 'ok'
     response dict" -- shape is `query_model_with_status`'s return value,
     not asserted here beyond `status`/`model`-identity fields tests
     directly need (matches tests/test_council_adapter.py's own documented
     assumption #2: assert only on the list-of-dicts-with-"model"-key
     OUTCOME, never on the intermediate content-extraction mechanism).
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
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
import llm_council.gateway_adapter as _gateway_adapter_module
import llm_council.openrouter as _openrouter_module
from scripts.resilient_query import (
    ModelAttempt,
    ResilientQueryResult,
    RetryPolicy,
    SubstitutionEvent,
    query_models_resilient as real_query_models_resilient,
)


def _patch(monkeypatch, host_modules, name, fake):
    for host in host_modules:
        monkeypatch.setattr(host, name, fake, raising=False)
    monkeypatch.setattr(ca, name, fake, raising=False)


def _make_config(safety_enabled: bool, models: list):
    return SimpleNamespace(
        evaluation=SimpleNamespace(safety=SimpleNamespace(enabled=safety_enabled)),
        council=SimpleNamespace(models=models),
    )


def _default_resilience_config(backup_models=None, minimum_council_size=4):
    ResilienceConfigCls = ca.DebateResilienceConfig
    return ResilienceConfigCls(
        backup_models=backup_models or [],
        retry_policy=RetryPolicy(),
        minimum_council_size=minimum_council_size,
    )


def _install_normal_flow_fakes(monkeypatch, models, resilience_config=None):
    """Fakes every Stage 1.5/2/3/usage/quality dependency so
    `run_council_with_timeouts` reaches its normal (non-degraded) flow for
    `len(models) >= 3` primaries, all resolving successfully. Leaves
    `query_models_resilient` and `query_model_with_status` unpatched --
    callers patch whichever of those two this specific test needs to
    control.
    """
    _patch(monkeypatch, [_council_module], "_get_council_models", lambda: list(models))
    _patch(monkeypatch, [_council_module], "get_config", lambda: _make_config(False, models))

    async def fake_stage1_5_normalize_styles(stage1_results):
        return stage1_results, {}

    async def fake_stage2_collect_rankings(user_query, responses_for_review, timeout=120.0, **kw):
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

    def fake_calculate_aggregate_rankings(stage2_results, label_to_model, **kw):
        return [
            {"model": entry["model"], "borda_score": 1.0, "rank": i + 1}
            for i, entry in enumerate(label_to_model.values())
        ]

    async def fake_stage3_synthesize_final(user_query, stage1_results, stage2_results, **kw):
        return {"model": stage1_results[0]["model"], "response": "final synthesis"}, {}, None

    _patch(monkeypatch, [_council_module], "stage1_5_normalize_styles", fake_stage1_5_normalize_styles)
    _patch(monkeypatch, [_council_module], "stage2_collect_rankings", fake_stage2_collect_rankings)
    _patch(monkeypatch, [_council_module], "calculate_aggregate_rankings", fake_calculate_aggregate_rankings)
    _patch(monkeypatch, [_council_module], "stage3_synthesize_final", fake_stage3_synthesize_final)
    _patch(monkeypatch, [_council_module], "_build_usage_summary", lambda by_stage: {"total": {"cost_usd": 0.0}, "by_model": {}})
    _patch(monkeypatch, [_council_module], "emit_usage_metrics", lambda usage, adapter=None: None)
    _patch(monkeypatch, [_council_module], "should_include_quality_metrics", lambda: False)

    resolved_resilience_config = resilience_config or _default_resilience_config()
    monkeypatch.setattr(ca, "_load_debate_resilience_config", lambda *a, **k: resolved_resilience_config, raising=False)


def _ok_response(model: str) -> dict:
    return {"status": "ok", "content": f"answer-from-{model}", "usage": {}}


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# AC17: Given all 4 primary models succeed on the first attempt, When Stage 1
# runs, Then query_models_resilient is called with primary_models equal to
# _get_council_models()'s return value, stage1_results contains exactly
# those 4 models, and neither metadata["substitutions"] nor
# metadata["shortfall_warning"] is present at all.
# ---------------------------------------------------------------------------


def test_ac17_happy_path_calls_resilient_with_council_models_no_substitutions_no_shortfall(monkeypatch):
    models = ["model-a", "model-b", "model-c", "model-d"]
    _install_normal_flow_fakes(monkeypatch, models)

    captured_kwargs = {}

    async def fake_query_models_resilient(*, primary_models, backup_models, messages, timeout, query_fn, retry_policy, minimum_council_size, **kw):
        captured_kwargs["primary_models"] = list(primary_models)
        return ResilientQueryResult(
            responses={m: _ok_response(m) for m in primary_models},
            attempts=[ModelAttempt(model=m, attempt_number=1, status="ok") for m in primary_models],
            substitutions=[],
            unreachable_models=[],
            shortfall_warning=None,
        )

    _patch(monkeypatch, [__import__("scripts.resilient_query", fromlist=["x"])], "query_models_resilient", fake_query_models_resilient)

    stage1_results, stage2_results, stage3_result, metadata = _run(
        ca.run_council_with_timeouts("some query")
    )

    assert captured_kwargs["primary_models"] == models
    assert {r["model"] for r in stage1_results} == set(models)
    assert len(stage1_results) == len(models)
    assert "substitutions" not in metadata
    assert "shortfall_warning" not in metadata


# ---------------------------------------------------------------------------
# AC18: Given _load_debate_resilience_config is called against a YAML file
# with a debate_resilience: block matching this project's own
# llm_council.yaml, When it runs, Then the returned backup_models,
# retry_policy (max_attempts/backoff_seconds/retryable_statuses), and
# minimum_council_size exactly match the file's values.
# ---------------------------------------------------------------------------


def test_ac18_loads_real_schema_debate_resilience_block_exactly(tmp_path):
    yaml_path = tmp_path / "llm_council.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "debate_resilience": {
                    "backup_models": ["vendor-a/model-1", "vendor-b/model-2"],
                    "minimum_council_size": 3,
                    "retry": {
                        "max_attempts": 4,
                        "backoff_seconds": [2, 4, 8],
                        "retryable_statuses": ["timeout", "error"],
                    },
                },
                "unrelated_top_level_key": {"foo": "bar"},
            }
        )
    )

    result = ca._load_debate_resilience_config(config_path=yaml_path)

    assert result.backup_models == ["vendor-a/model-1", "vendor-b/model-2"]
    assert result.minimum_council_size == 3
    assert result.retry_policy.max_attempts == 4
    assert tuple(result.retry_policy.backoff_seconds) == (2.0, 4.0, 8.0)
    assert set(result.retry_policy.retryable_statuses) == {"timeout", "error"}
    # deliberately excluded from the fixture's retryable_statuses -- proves
    # the value is genuinely read from the file, not defaulted underneath.
    assert "rate_limited" not in result.retry_policy.retryable_statuses


def test_ac18_loads_this_projects_actual_llm_council_yaml_debate_resilience_block():
    # Cross-check against the real, checked-in config (re-confirmed live,
    # 2026-08-13, to carry a debate_resilience: block with backup_models
    # ["moonshotai/kimi-k3", "qwen/qwen3.8-max", "x-ai/grok-4.6"]
    # (updated from the original 2-model pool after this test was first
    # authored -- moonshotai/kimi-k3-20260715 was added as the panel's
    # diversity-optimized top backup pick, then 2026-08-13 the dated slug
    # went dead on live OpenRouter and was fixed to the undated
    # moonshotai/kimi-k3 -- see docs/upstream-deltas.md, "Kimi K3 slug
    # drift" entry), minimum_council_size: 4, retry.max_attempts: 3,
    # retry.backoff_seconds: [5, 15],
    # retry.retryable_statuses: [timeout, rate_limited, error].
    real_config_path = REPO_ROOT / "llm_council.yaml"
    assert real_config_path.exists(), "expected llm_council.yaml at repo root"

    result = ca._load_debate_resilience_config(config_path=real_config_path)

    assert result.backup_models == [
        "moonshotai/kimi-k3",
        "qwen/qwen3.8-max",
        "x-ai/grok-4.6",
    ]
    assert result.minimum_council_size == 4
    assert result.retry_policy.max_attempts == 3
    assert tuple(result.retry_policy.backoff_seconds) == (5.0, 15.0)
    assert set(result.retry_policy.retryable_statuses) == {"timeout", "rate_limited", "error"}


# ---------------------------------------------------------------------------
# AC19: Given debate_resilience: is absent from the config file entirely,
# When _load_debate_resilience_config runs, Then it returns
# backup_models=[], RetryPolicy()'s own defaults, and
# minimum_council_size=4 -- never raises.
# ---------------------------------------------------------------------------


def test_ac19_missing_debate_resilience_key_returns_safe_defaults(tmp_path):
    yaml_path = tmp_path / "llm_council.yaml"
    yaml_path.write_text(yaml.safe_dump({"other_key": {"a": 1}}))

    result = ca._load_debate_resilience_config(config_path=yaml_path)

    default_policy = RetryPolicy()
    assert result.backup_models == []
    assert result.minimum_council_size == 4
    assert result.retry_policy.max_attempts == default_policy.max_attempts
    assert tuple(result.retry_policy.backoff_seconds) == tuple(default_policy.backoff_seconds)
    assert set(result.retry_policy.retryable_statuses) == set(default_policy.retryable_statuses)


def test_ac19_entirely_missing_config_file_returns_safe_defaults_never_raises(tmp_path):
    nonexistent_path = tmp_path / "does-not-exist" / "llm_council.yaml"
    assert not nonexistent_path.exists()

    result = ca._load_debate_resilience_config(config_path=nonexistent_path)

    default_policy = RetryPolicy()
    assert result.backup_models == []
    assert result.minimum_council_size == 4
    assert result.retry_policy.max_attempts == default_policy.max_attempts
    assert tuple(result.retry_policy.backoff_seconds) == tuple(default_policy.backoff_seconds)
    assert set(result.retry_policy.retryable_statuses) == set(default_policy.retryable_statuses)


# ---------------------------------------------------------------------------
# AC20: Given query_models_resilient returns a non-empty substitutions list,
# When Stage 1 finishes, Then metadata["substitutions"] is a list of plain
# dicts (JSON-serializable, not dataclass instances) with exactly the
# slot_model/backup_model/reason keys, in the same order query_models_
# resilient produced them.
# ---------------------------------------------------------------------------


def test_ac20_substitutions_surfaced_as_plain_dicts_in_order(monkeypatch):
    models = ["model-a", "model-b", "model-c", "model-d"]
    _install_normal_flow_fakes(monkeypatch, models)

    substitution_events = [
        SubstitutionEvent(slot_model="model-a", backup_model="backup-1", reason="unreachable after 3 attempts (last status=timeout)"),
        SubstitutionEvent(slot_model="model-b", backup_model="backup-2", reason="unreachable after 3 attempts (last status=auth_error)"),
    ]

    async def fake_query_models_resilient(*, primary_models, backup_models, messages, timeout, query_fn, retry_policy, minimum_council_size, **kw):
        winners = ["backup-1", "backup-2", "model-c", "model-d"]
        return ResilientQueryResult(
            responses={w: _ok_response(w) for w in winners},
            attempts=[],
            substitutions=substitution_events,
            unreachable_models=["model-a", "model-b"],
            shortfall_warning=None,
        )

    _patch(monkeypatch, [__import__("scripts.resilient_query", fromlist=["x"])], "query_models_resilient", fake_query_models_resilient)

    _, _, _, metadata = _run(ca.run_council_with_timeouts("some query"))

    assert "substitutions" in metadata
    assert metadata["substitutions"] == [asdict(e) for e in substitution_events]
    for entry in metadata["substitutions"]:
        assert type(entry) is dict
        assert set(entry.keys()) == {"slot_model", "backup_model", "reason"}


# ---------------------------------------------------------------------------
# AC21: Given query_models_resilient returns shortfall_warning=None, When
# Stage 1 finishes, Then "shortfall_warning" is not a key in metadata at all
# (not present-and-None). Complementary direction (same AC's "matches the
# degraded_mode precedent" convention): a non-None warning IS present,
# verbatim.
# ---------------------------------------------------------------------------


def test_ac21_shortfall_warning_none_means_key_absent(monkeypatch):
    models = ["model-a", "model-b", "model-c", "model-d"]
    _install_normal_flow_fakes(monkeypatch, models)

    async def fake_query_models_resilient(*, primary_models, backup_models, messages, timeout, query_fn, retry_policy, minimum_council_size, **kw):
        return ResilientQueryResult(
            responses={m: _ok_response(m) for m in primary_models},
            attempts=[],
            substitutions=[],
            unreachable_models=[],
            shortfall_warning=None,
        )

    _patch(monkeypatch, [__import__("scripts.resilient_query", fromlist=["x"])], "query_models_resilient", fake_query_models_resilient)

    _, _, _, metadata = _run(ca.run_council_with_timeouts("some query"))

    assert "shortfall_warning" not in metadata


def test_ac21_shortfall_warning_present_when_not_none(monkeypatch):
    models = ["model-a", "model-b", "model-c"]
    _install_normal_flow_fakes(monkeypatch, models)
    warning_text = "Only 3 of the required minimum 4 council models responded; unreachable: model-d"

    async def fake_query_models_resilient(*, primary_models, backup_models, messages, timeout, query_fn, retry_policy, minimum_council_size, **kw):
        return ResilientQueryResult(
            responses={m: _ok_response(m) for m in primary_models},
            attempts=[],
            substitutions=[],
            unreachable_models=["model-d"],
            shortfall_warning=warning_text,
        )

    _patch(monkeypatch, [__import__("scripts.resilient_query", fromlist=["x"])], "query_models_resilient", fake_query_models_resilient)

    _, _, _, metadata = _run(ca.run_council_with_timeouts("some query"))

    assert metadata["shortfall_warning"] == warning_text


# ---------------------------------------------------------------------------
# Property test: AC17 + AC20 + AC21 together encode one general law --
# metadata["substitutions"]/metadata["shortfall_warning"] presence and
# content is always a faithful, order-preserving mirror of what
# query_models_resilient returned (key absent iff empty/None; present iff
# non-empty/non-None; content converted to plain dicts, never dropped or
# duplicated). This is the "type/shape invariant + round-trip" pattern
# property-based tests target first per the anti-test-hacking doctrine.
# ---------------------------------------------------------------------------


@settings(max_examples=50, derandomize=True, deadline=3000, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    substitution_data=st.lists(
        st.tuples(
            st.text(alphabet="abcdefghij-", min_size=1, max_size=8),
            st.text(alphabet="abcdefghij-", min_size=1, max_size=8),
            st.text(alphabet="abcdefghijklmnop ()=", min_size=1, max_size=20),
        ),
        max_size=3,
    ),
    shortfall_text=st.one_of(st.none(), st.text(alphabet="abcdefghijklmnop 0123456789:;", min_size=1, max_size=30)),
)
def test_property_metadata_mirrors_resilient_result_substitutions_and_shortfall(monkeypatch, substitution_data, shortfall_text):
    models = ["model-a", "model-b", "model-c", "model-d"]
    _install_normal_flow_fakes(monkeypatch, models)

    substitution_events = [
        SubstitutionEvent(slot_model=s, backup_model=b, reason=r) for s, b, r in substitution_data
    ]

    async def fake_query_models_resilient(*, primary_models, backup_models, messages, timeout, query_fn, retry_policy, minimum_council_size, **kw):
        return ResilientQueryResult(
            responses={m: _ok_response(m) for m in primary_models},
            attempts=[],
            substitutions=substitution_events,
            unreachable_models=[],
            shortfall_warning=shortfall_text,
        )

    _patch(monkeypatch, [__import__("scripts.resilient_query", fromlist=["x"])], "query_models_resilient", fake_query_models_resilient)

    _, _, _, metadata = asyncio.run(ca.run_council_with_timeouts("some query"))

    if substitution_events:
        assert metadata["substitutions"] == [asdict(e) for e in substitution_events]
        assert all(type(e) is dict for e in metadata["substitutions"])
    else:
        assert "substitutions" not in metadata

    if shortfall_text is not None:
        assert metadata["shortfall_warning"] == shortfall_text
    else:
        assert "shortfall_warning" not in metadata


# ---------------------------------------------------------------------------
# AC23: Given run_council_with_timeouts is called with query_fn faked to
# return status="ok" for every primary on the first attempt, When it runs,
# Then query_fn (i.e. query_model_with_status) is invoked exactly once per
# primary model -- no retries, no backups ever touched, confirming the
# happy path costs exactly what it did before this amendment.
#
# Uses the REAL query_models_resilient (not faked) -- this is the genuine
# integration point: only the underlying network-facing query_fn is a test
# double.
# ---------------------------------------------------------------------------


def test_ac23_happy_path_calls_query_fn_exactly_once_per_primary_no_backups(monkeypatch):
    models = ["model-a", "model-b", "model-c", "model-d"]
    resilience_config = _default_resilience_config(backup_models=["unused-backup"], minimum_council_size=4)
    _install_normal_flow_fakes(monkeypatch, models, resilience_config=resilience_config)

    # Ensure the REAL query_models_resilient is what council_adapter calls --
    # only query_model_with_status is a fake, at every plausible import site.
    monkeypatch.setattr(
        __import__("scripts.resilient_query", fromlist=["x"]),
        "query_models_resilient",
        real_query_models_resilient,
        raising=False,
    )
    monkeypatch.setattr(ca, "query_models_resilient", real_query_models_resilient, raising=False)

    calls = []

    async def fake_query_model_with_status(model, messages, timeout, *a, **kw):
        calls.append(model)
        return {"status": "ok", "content": f"answer-from-{model}", "usage": {}}

    _patch(
        monkeypatch,
        [_gateway_adapter_module, _openrouter_module],
        "query_model_with_status",
        fake_query_model_with_status,
    )

    stage1_results, _, _, metadata = _run(ca.run_council_with_timeouts("some query"))

    assert sorted(calls) == sorted(models)
    assert len(calls) == len(models)
    assert "unused-backup" not in calls
    assert "substitutions" not in metadata
    assert "shortfall_warning" not in metadata
    assert {r["model"] for r in stage1_results} == set(models)


# ---------------------------------------------------------------------------
# Mutation-gate hardening (2026-08-12): the scoped mutmut run over the
# changed surface of council_adapter.py found 44 in-diff survivors -- every
# one but a single genuine equivalent mutant (`open(path, "r")` vs
# `open(path)`, identical since "r" is `open()`'s own default mode) was a
# real weak/missing assertion, not a code defect. Per the anti-test-hacking
# doctrine ("strengthen the TEST, not the code"), the four tests below close
# those gaps: none of AC17-23's own tests ever checked (a) the actual
# extracted `response.get("content", "")` value ending up in
# `stage1_results[i]["response"]`, (b) that per-model usage
# (prompt_tokens/completion_tokens/total_tokens) is genuinely SUMMED -- not
# overwritten, subtracted, or read via a wrong key/default -- into
# `total_usage["stage1"]`, (c) that `_add_cost_to_usage` is invoked once per
# model with the correct `model=` kwarg, (d) that `messages`/`timeout` are
# threaded through to `query_models_resilient` unchanged, or (e) that
# `_load_debate_resilience_config` falls back to each key's OWN default
# (not just the whole-block-absent path) when `debate_resilience:` is
# present but a specific key inside it is missing, or (f) that
# `aggregate_rankings` is actually the value received by
# `stage3_synthesize_final` (the diff's own positional-to-keyword change),
# not merely "doesn't crash when passed something".
# ---------------------------------------------------------------------------


def test_stage1_results_content_and_usage_accumulation_are_correct(monkeypatch):
    models = ["model-a", "model-b", "model-c", "model-d"]
    _install_normal_flow_fakes(monkeypatch, models)

    # model-d deliberately omits "content"/"usage" so the extraction's own
    # *default* values (not just its key names) get exercised too.
    usage_by_model = {
        "model-a": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        "model-b": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22},
        "model-c": {"prompt_tokens": 30, "completion_tokens": 3, "total_tokens": 33},
    }
    responses = {
        m: {"status": "ok", "content": f"content-from-{m}", "usage": usage_by_model[m]}
        for m in ["model-a", "model-b", "model-c"]
    }
    responses["model-d"] = {"status": "ok"}  # no "content", no "usage" key at all

    async def fake_query_models_resilient(*, primary_models, backup_models, messages, timeout, query_fn, retry_policy, minimum_council_size, **kw):
        return ResilientQueryResult(
            responses={m: responses[m] for m in primary_models},
            attempts=[],
            substitutions=[],
            unreachable_models=[],
            shortfall_warning=None,
        )

    _patch(monkeypatch, [__import__("scripts.resilient_query", fromlist=["x"])], "query_models_resilient", fake_query_models_resilient)

    captured_usage = {}

    def fake_build_usage_summary(by_stage):
        captured_usage["by_stage"] = by_stage
        return {"total": {"cost_usd": 0.0}, "by_model": {}}

    _patch(monkeypatch, [_council_module], "_build_usage_summary", fake_build_usage_summary)

    cost_calls = []

    def fake_add_cost_to_usage(bucket, usage, model=None):
        cost_calls.append(model)

    _patch(monkeypatch, [_council_module], "_add_cost_to_usage", fake_add_cost_to_usage)

    stage1_results, _, _, _ = _run(ca.run_council_with_timeouts("some query"))

    results_by_model = {r["model"]: r["response"] for r in stage1_results}
    assert results_by_model["model-a"] == "content-from-model-a"
    assert results_by_model["model-b"] == "content-from-model-b"
    assert results_by_model["model-c"] == "content-from-model-c"
    assert results_by_model["model-d"] == ""  # default fallback, no "content" key

    stage1_usage = captured_usage["by_stage"]["stage1"]
    assert stage1_usage["prompt_tokens"] == sum(u["prompt_tokens"] for u in usage_by_model.values())
    assert stage1_usage["completion_tokens"] == sum(u["completion_tokens"] for u in usage_by_model.values())
    assert stage1_usage["total_tokens"] == sum(u["total_tokens"] for u in usage_by_model.values())

    assert sorted(cost_calls) == sorted(models)


def test_query_models_resilient_receives_correct_messages_and_timeout(monkeypatch):
    models = ["model-a", "model-b"]
    _install_normal_flow_fakes(monkeypatch, models)

    captured = {}

    async def fake_query_models_resilient(*, primary_models, backup_models, messages, timeout, query_fn, retry_policy, minimum_council_size, **kw):
        captured["messages"] = messages
        captured["timeout"] = timeout
        return ResilientQueryResult(
            responses={m: _ok_response(m) for m in primary_models},
            attempts=[],
            substitutions=[],
            unreachable_models=[],
            shortfall_warning=None,
        )

    _patch(monkeypatch, [__import__("scripts.resilient_query", fromlist=["x"])], "query_models_resilient", fake_query_models_resilient)

    _run(ca.run_council_with_timeouts("the exact query text", stage1_timeout=77.0))

    # Proposal A Contract 1 (docs/specs/proposal-a-reference-grounding-contract.md):
    # messages carry the query plus the uniform reference-reporting instruction.
    assert captured["messages"] == [
        {"role": "user", "content": ca.build_stage1_prompt("the exact query text")}
    ]
    assert captured["timeout"] == 77.0


def test_ac18_partial_debate_resilience_block_falls_back_to_per_key_defaults(tmp_path):
    """AC18/19 only tested "whole block present with every key" and "whole
    block/file absent" -- never "block present but missing an individual
    key" -- so `block.get("backup_models", [])` /
    `block.get("minimum_council_size", 4)`'s own per-key default value was
    never actually exercised or asserted.
    """
    yaml_path = tmp_path / "llm_council.yaml"
    yaml_path.write_text(
        yaml.safe_dump({"debate_resilience": {"retry": {"max_attempts": 2}}})
    )

    result = ca._load_debate_resilience_config(config_path=yaml_path)

    assert result.backup_models == []
    assert result.minimum_council_size == 4
    assert result.retry_policy.max_attempts == 2


def test_stage3_synthesize_final_receives_correct_aggregate_rankings(monkeypatch):
    models = ["model-a", "model-b", "model-c", "model-d"]
    _install_normal_flow_fakes(monkeypatch, models)

    async def fake_query_models_resilient(*, primary_models, backup_models, messages, timeout, query_fn, retry_policy, minimum_council_size, **kw):
        return ResilientQueryResult(
            responses={m: _ok_response(m) for m in primary_models},
            attempts=[],
            substitutions=[],
            unreachable_models=[],
            shortfall_warning=None,
        )

    _patch(monkeypatch, [__import__("scripts.resilient_query", fromlist=["x"])], "query_models_resilient", fake_query_models_resilient)

    captured = {}

    async def fake_stage3_synthesize_final(user_query, stage1_results, stage2_results, aggregate_rankings=None, **kw):
        captured["aggregate_rankings"] = aggregate_rankings
        return {"model": stage1_results[0]["model"], "response": "final synthesis"}, {}, None

    _patch(monkeypatch, [_council_module], "stage3_synthesize_final", fake_stage3_synthesize_final)

    _run(ca.run_council_with_timeouts("some query"))

    assert captured["aggregate_rankings"] is not None
    assert captured["aggregate_rankings"] == [
        {"model": m, "borda_score": 1.0, "rank": i + 1} for i, m in enumerate(models)
    ]
