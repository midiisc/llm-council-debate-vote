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
import scripts.live_adapters as _live_adapters_module
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
        evaluation=SimpleNamespace(
            safety=SimpleNamespace(enabled=safety_enabled),
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
    # `_build_stage2_real_ranking_prompt` (docs/specs/stage2-3-debate-
    # resilience-contract.md, Contract A) faithfully reproduces the real
    # package's position-bias-mitigating shuffle - deterministic ordering
    # was never a real contract of Stage 2, but several of these AC17-23
    # tests assert on exact stage2/aggregate_rankings order as a fixture
    # convenience. No-op it here so that convenience keeps working without
    # weakening what's actually being tested.
    monkeypatch.setattr(ca.random, "shuffle", lambda seq: None, raising=False)

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


def _is_stage2_call(messages) -> bool:
    """Stage 2's real (rubric-aware) ranking prompt (docs/specs/stage2-3-
    debate-resilience-contract.md, Contract A) always contains this marker;
    Stage 1's `build_stage1_prompt` never does. Needed because Stage 2 now
    reuses the same `query_models_resilient` engine these AC17-23 fakes
    patch, so a fake written only to model Stage 1 must not also swallow
    Stage 2's independent call in the same debate."""
    return "<responses_to_evaluate>" in messages[0]["content"]


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
        if _is_stage2_call(messages):
            return ResilientQueryResult(
                responses={m: _ok_response(m) for m in primary_models},
                attempts=[],
                substitutions=[],
                unreachable_models=[],
                shortfall_warning=None,
            )
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
        if _is_stage2_call(messages):
            return ResilientQueryResult(
                responses={m: _ok_response(m) for m in primary_models},
                attempts=[],
                substitutions=[],
                unreachable_models=[],
                shortfall_warning=None,
            )
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
        if _is_stage2_call(messages):
            return ResilientQueryResult(
                responses={m: _ok_response(m) for m in primary_models},
                attempts=[],
                substitutions=[],
                unreachable_models=[],
                shortfall_warning=None,
            )
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

    stage1_calls = []
    stage2_calls = []
    stage1_reasoning_effort_kwargs = {}

    async def fake_query_model_with_status(model, messages, timeout, *a, **kw):
        if _is_stage2_call(messages):
            stage2_calls.append(model)
            # Stage 2 still goes through the old query_model_with_status
            # signature - it must NOT receive reasoning_effort (Contract 4
            # is Stage-1-only, AC19's "every other argument unchanged").
            assert "reasoning_effort" not in kw
        else:
            stage1_calls.append(model)
            # Contract 4, AC19: Stage 1's query_fn is now
            # query_model_with_status_and_effort, which _stage1_query_fn
            # ALWAYS calls with an explicit reasoning_effort= kwarg (None
            # for an unmapped model, per its fallback) - a plain
            # query_model_with_status(model, messages, timeout) call from
            # the OLD wiring can never produce this kwarg at all, so its
            # presence is a real discriminator between old and new wiring,
            # not just a shape check. Regression-caught 2026-08-14: the
            # original **kw-tolerant fake here silently passed even with
            # the Contract 4 implementation files reverted to pre-feature
            # HEAD, because it never inspected kw - watch-RED could not be
            # established. This assertion is what makes that revert fail.
            assert "reasoning_effort" in kw
            stage1_reasoning_effort_kwargs[model] = kw["reasoning_effort"]
            assert kw["reasoning_effort"] == ca._STAGE1_REASONING_EFFORT.get(model)
        return {"status": "ok", "content": f"answer-from-{model}", "usage": {}}

    _patch(
        monkeypatch,
        [_gateway_adapter_module, _openrouter_module],
        "query_model_with_status",
        fake_query_model_with_status,
    )
    # Stage 1's query_fn is now query_model_with_status_and_effort
    # (docs/specs/reasoning-effort-wiring-contract.md, Contract 4) - same
    # (model, messages, timeout, **kw) shape, so the existing fake above
    # (already **kw-tolerant) doubles as its fake too.
    monkeypatch.setattr(
        _live_adapters_module, "query_model_with_status_and_effort", fake_query_model_with_status, raising=False
    )
    monkeypatch.setattr(ca, "query_model_with_status_and_effort", fake_query_model_with_status, raising=False)

    stage1_results, _, _, metadata = _run(ca.run_council_with_timeouts("some query"))

    # AC23 is a Stage 1 contract - Stage 2 now legitimately reuses the same
    # real query_models_resilient engine (docs/specs/stage2-3-debate-
    # resilience-contract.md, Contract A) and makes its own independent
    # once-per-reviewer call set, asserted separately so it can't mask a
    # Stage 1 regression by coincidentally matching call counts.
    assert sorted(stage1_calls) == sorted(models)
    assert len(stage1_calls) == len(models)
    assert "unused-backup" not in stage1_calls
    assert sorted(stage2_calls) == sorted(models)
    assert "unused-backup" not in stage2_calls
    assert "substitutions" not in metadata
    assert "shortfall_warning" not in metadata
    assert {r["model"] for r in stage1_results} == set(models)
    # Every Stage 1 call went through the new effort-aware path with the
    # correct per-model lookup (all 4 test models are unmapped in
    # _STAGE1_REASONING_EFFORT, so None is the contractually-correct value
    # here - the map's real-slug values are covered directly in
    # tests/test_reasoning_effort_stage1_contract.py).
    assert set(stage1_reasoning_effort_kwargs) == set(models)
    assert all(v is None for v in stage1_reasoning_effort_kwargs.values())


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
        if _is_stage2_call(messages):
            return ResilientQueryResult(
                responses={m: _ok_response(m) for m in primary_models},
                attempts=[],
                substitutions=[],
                unreachable_models=[],
                shortfall_warning=None,
            )
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

    # `_add_cost_to_usage` is now invoked once per Stage 1 draft AND once
    # per Stage 2 reviewer (docs/specs/stage2-3-debate-resilience-
    # contract.md, Contract A - Stage 2 now does its own real cost
    # accounting too, same models list) - exactly twice per model, not
    # zero/once/thrice, keeps this AC's "no double-counting, no dropped
    # accounting" intent while reflecting the real two-stage call shape.
    assert sorted(cost_calls) == sorted(models + models)
    for m in models:
        assert cost_calls.count(m) == 2


def test_query_models_resilient_receives_correct_messages_and_timeout(monkeypatch):
    models = ["model-a", "model-b"]
    _install_normal_flow_fakes(monkeypatch, models)

    captured = {}

    async def fake_query_models_resilient(*, primary_models, backup_models, messages, timeout, query_fn, retry_policy, minimum_council_size, **kw):
        if not _is_stage2_call(messages):
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
    # Stage 3 chairman anonymization (docs/specs/stage3-chairman-
    # anonymization-contract.md): the chairman's own call receives the
    # Stage 2 Response-label, never the real model slug - shuffle is
    # no-op'd by _install_normal_flow_fakes above, so labels follow
    # `models`' own order.
    assert captured["aggregate_rankings"] == [
        {"model": f"Response {chr(65 + i)}", "borda_score": 1.0, "rank": i + 1}
        for i, m in enumerate(models)
    ]


def test_stage3_receives_style_normalized_stage1_text_not_raw_draft(monkeypatch):
    """Stage 3 chairman anonymization (docs/specs/stage3-chairman-
    anonymization-contract.md) closes the explicit "Model: X" identity
    leak, but Stage 1.5 (style_normalization: true,
    docs/upstream-deltas.md) exists specifically to scrub stylistic
    fingerprinting from Stage 1 drafts BEFORE Stage 2 peer review sees
    them - a label swap alone is not real anonymization if the chairman
    still reads the raw, un-normalized draft text underneath each label.
    The chairman must see the SAME style-normalized text Stage 2 reviewers
    already see, never stage1_results' original text."""
    models = ["model-a", "model-b", "model-c"]
    _install_normal_flow_fakes(monkeypatch, models)

    async def fake_query_models_resilient(*, primary_models, backup_models, messages, timeout, query_fn, retry_policy, minimum_council_size, **kw):
        return ResilientQueryResult(
            responses={m: _ok_response(m) for m in primary_models},
            attempts=[], substitutions=[], unreachable_models=[], shortfall_warning=None,
        )

    _patch(monkeypatch, [__import__("scripts.resilient_query", fromlist=["x"])], "query_models_resilient", fake_query_models_resilient)

    # Overrides _install_normal_flow_fakes' identity-passthrough fake for
    # stage1_5_normalize_styles - an identity passthrough can't distinguish
    # "chairman received the raw draft" from "chairman received the
    # normalized draft" since they're equal either way. This fake actually
    # rewrites the text so the two cases are observably different.
    async def fake_stage1_5_actually_rewrites(stage1_results):
        normalized = [
            {"model": r["model"], "response": f"NORMALIZED::{r['response']}"}
            for r in stage1_results
        ]
        return normalized, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    _patch(monkeypatch, [_council_module], "stage1_5_normalize_styles", fake_stage1_5_actually_rewrites)

    captured = {}

    async def fake_stage3_synthesize_final(user_query, stage1_results, stage2_results, aggregate_rankings=None, **kw):
        captured["stage1_results"] = stage1_results
        return {"model": "chairman-x", "response": "final synthesis"}, {}, None

    _patch(monkeypatch, [_council_module], "stage3_synthesize_final", fake_stage3_synthesize_final)

    _run(ca.run_council_with_timeouts("some query"))

    assert captured["stage1_results"] is not None
    assert all(
        entry["response"].startswith("NORMALIZED::") for entry in captured["stage1_results"]
    )


def test_stage3_receives_style_normalized_stage2_ranking_text_not_raw_critique(monkeypatch):
    """Extends the draft-normalization test above to Stage 2 reviewer
    commentary (docs/upstream-deltas.md, "Known residual limitation" entry,
    2026-08-14 fix): a reviewer's own critique/ranking prose is as much an
    identity-adjacent stylistic signal as a drafter's, and was left
    un-normalized even after stage1_for_stage3 closed the same gap for
    drafts. The chairman must see the SAME style-normalized text for
    reviewer commentary, never stage2_results' original "ranking" text."""
    models = ["model-a", "model-b", "model-c"]
    _install_normal_flow_fakes(monkeypatch, models)

    async def fake_query_models_resilient(*, primary_models, backup_models, messages, timeout, query_fn, retry_policy, minimum_council_size, **kw):
        return ResilientQueryResult(
            responses={m: _ok_response(m) for m in primary_models},
            attempts=[], substitutions=[], unreachable_models=[], shortfall_warning=None,
        )

    _patch(monkeypatch, [__import__("scripts.resilient_query", fromlist=["x"])], "query_models_resilient", fake_query_models_resilient)

    # Same differentiating fake as the draft-normalization test above -
    # rewrites whatever "response" text it receives, regardless of which
    # stage's content is routed through it via the ranking<->response
    # mapping in _normalize_stage2_for_stage3.
    async def fake_stage1_5_actually_rewrites(stage1_results):
        normalized = [
            {"model": r["model"], "response": f"NORMALIZED::{r['response']}"}
            for r in stage1_results
        ]
        return normalized, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    _patch(monkeypatch, [_council_module], "stage1_5_normalize_styles", fake_stage1_5_actually_rewrites)

    captured = {}

    async def fake_stage3_synthesize_final(user_query, stage1_results, stage2_results, aggregate_rankings=None, **kw):
        captured["stage2_results"] = stage2_results
        return {"model": "chairman-x", "response": "final synthesis"}, {}, None

    _patch(monkeypatch, [_council_module], "stage3_synthesize_final", fake_stage3_synthesize_final)

    _run(ca.run_council_with_timeouts("some query"))

    assert captured["stage2_results"] is not None
    assert len(captured["stage2_results"]) == len(models)
    assert all(
        entry["ranking"].startswith("NORMALIZED::") for entry in captured["stage2_results"]
    )


def test_normalize_stage2_for_stage3_empty_input_returns_zeroed_usage_no_config_touch(monkeypatch):
    """Direct unit test for `_normalize_stage2_for_stage3`'s empty-input
    early return (docs/upstream-deltas.md, "Known residual limitation"
    entry, 2026-08-14 fix). Found by scoped mutmut: no existing
    integration test pins the EXACT usage dict this returns for
    `stage2_results = []` (single-model degraded mode) - a wrong key name
    or a nonzero value here would silently corrupt
    `metadata["usage"]["total"]` for every single-model run, undetected."""
    result, usage = _run(ca._normalize_stage2_for_stage3([]))
    assert result == []
    assert usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_normalize_stage2_for_stage3_rewrites_ranking_preserves_other_keys(monkeypatch):
    """Direct unit test (non-empty path): only "ranking" is replaced by the
    normalizer's output, every other key ("model", "parsed_ranking", ...)
    passes through unchanged, and the real `stage2_results` argument is
    never mutated."""
    async def fake_stage1_5_actually_rewrites(stage1_results):
        normalized = [
            {"model": r["model"], "response": f"NORMALIZED::{r['response']}"}
            for r in stage1_results
        ]
        return normalized, {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9}

    _patch(monkeypatch, [_council_module], "stage1_5_normalize_styles", fake_stage1_5_actually_rewrites)

    stage2_results = [
        {"model": "model-a", "ranking": "raw critique a", "parsed_ranking": {"scores": {"Response A": 8}}},
        {"model": "model-b", "ranking": "raw critique b", "parsed_ranking": {"scores": {"Response A": 6}}},
    ]

    result, usage = _run(ca._normalize_stage2_for_stage3(stage2_results))

    assert result == [
        {"model": "model-a", "ranking": "NORMALIZED::raw critique a", "parsed_ranking": {"scores": {"Response A": 8}}},
        {"model": "model-b", "ranking": "NORMALIZED::raw critique b", "parsed_ranking": {"scores": {"Response A": 6}}},
    ]
    assert usage == {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9}
    # the real argument is untouched
    assert stage2_results[0]["ranking"] == "raw critique a"
    assert stage2_results[1]["ranking"] == "raw critique b"
