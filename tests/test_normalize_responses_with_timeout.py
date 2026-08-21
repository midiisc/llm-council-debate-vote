"""Blind acceptance tests for docs/specs/stage1-5-normalizer-timeout-contract.md
(AC1-AC6) -- the new `scripts/council_adapter.py::_normalize_responses_with_
timeout` function and the amended `_normalize_stage2_for_stage3`.

Authored WITHOUT sight of any implementation -- as of this writing,
`_normalize_responses_with_timeout` does not exist and `_normalize_stage2_
for_stage3` takes only `(stage2_results)` (no `timeout`, two-element return).
Every test in this file is expected to fail at collection (AttributeError/
ImportError) or call-signature mismatch (RED) until the contract lands.

DOCUMENTED ASSUMPTIONS:

  1. **Patch locations for `_get_style_normalization`/`_get_normalizer_
     model`/`should_normalize_styles`.** The contract's own "Environment"
     section says these are "importable" dependencies the new function
     reads, mirroring how `llm_council.council_stages.stage1_5_normalize_
     styles` (confirmed live, source read) already reads them as bare
     module-level names. Whether `council_adapter.py` imports them by name
     or accesses them via a `council_stages.` module reference is not
     pinned by the contract, so every test patches BOTH plausible
     locations -- the real `llm_council.council_stages` module attribute
     AND `scripts.council_adapter`'s own attribute if present post-import
     -- via the `_patch` helper below, the same dual-location pattern
     `tests/test_council_adapter.py` already uses for its own dependency
     doubles.
  2. **Patch location for `query_model_with_status`.** The contract states
     explicitly (Design step 2) that the new function must use "this
     repo's own already-imported `query_model_with_status`, from
     `gateway_adapter`" -- i.e. `scripts.council_adapter.query_model_with_
     status`, confirmed live as an existing module-level import
     (`from llm_council.gateway_adapter import query_model_with_status`,
     `scripts/council_adapter.py` line 65). Patched at both `llm_council.
     gateway_adapter.query_model_with_status` and `scripts.council_
     adapter.query_model_with_status` for the same dual-location safety.
  3. **Prompt/entry correlation.** Since the rewrite prompt embeds the
     entry's own `"response"` text (Design step 2: "Build the identical
     rewrite prompt per entry ... copy `stage1_5_normalize_styles`'s exact
     prompt template"), and `query_model_with_status` is always called
     with the SAME normalizer-model slug for every entry (never the
     entry's own per-response model), fakes identify which entry a given
     call belongs to by checking which entry's `"response"` text is a
     substring of the outgoing prompt -- not by the `model` argument
     (which is constant across all calls in a batch).
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

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

import llm_council.council_stages as _council_stages_module
import llm_council.gateway_adapter as _gateway_adapter_module
from llm_council.openrouter import STATUS_OK


def _patch(monkeypatch, name, fake):
    monkeypatch.setattr(_council_stages_module, name, fake, raising=False)
    monkeypatch.setattr(_gateway_adapter_module, name, fake, raising=False)
    monkeypatch.setattr(ca, name, fake, raising=False)


def _entries(n: int = 4):
    return [{"model": f"model-{i}", "response": f"raw response text {i}"} for i in range(n)]


def _run(coro):
    return asyncio.run(coro)


def _fail_if_called(*a, **k):
    raise AssertionError("query_model_with_status must not be called on this path")


ZEROED_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


# ---------------------------------------------------------------------------
# AC1 -- Given style_normalization off (config gate reads False), when
# `_normalize_responses_with_timeout` is called with a non-empty entries
# list, then entries are returned verbatim (original_response == response),
# usage is zeroed, failed_models == [], and zero query_model_with_status
# calls are issued.
# ---------------------------------------------------------------------------


def test_ac1_config_gate_off_returns_entries_verbatim_with_zero_calls(monkeypatch):
    entries = _entries(3)
    _patch(monkeypatch, "_get_style_normalization", lambda: False)

    async def _fail_query(model, messages, timeout):
        _fail_if_called()

    _patch(monkeypatch, "query_model_with_status", _fail_query)

    normalized, usage, failed = _run(ca._normalize_responses_with_timeout(entries, timeout=5.0))

    assert normalized == [
        {"model": e["model"], "response": e["response"], "original_response": e["response"]}
        for e in entries
    ]
    assert usage == ZEROED_USAGE
    assert failed == []


# ---------------------------------------------------------------------------
# AC2 -- Given style_normalization on and 4 entries whose mocked
# query_model_with_status calls are all trackably concurrent, when
# `_normalize_responses_with_timeout` is called, then all 4 calls are in
# flight simultaneously (never sequential), every entry's response becomes
# its normalized text (order-preserving), original_response preserves the
# pre-normalization text, and failed_models == [].
# ---------------------------------------------------------------------------


class _ConcurrencyBarrier:
    """Deterministically proves N callers are in flight at once: each
    caller increments a counter and waits on a shared asyncio.Event that
    only gets set once all N have arrived. A strictly-sequential caller
    (e.g. `for e in entries: await query_model_with_status(...)`) can never
    get more than 1 concurrent arrival, so the event never sets and the
    bounded `asyncio.wait_for` below raises -- caught and turned into a
    clear AssertionError instead of a silent hang.
    """

    def __init__(self, n: int, timeout: float = 2.0):
        self.n = n
        self.count = 0
        self.event = asyncio.Event()
        self.timeout = timeout

    async def wait(self):
        self.count += 1
        if self.count >= self.n:
            self.event.set()
        try:
            await asyncio.wait_for(self.event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise AssertionError(
                f"query_model_with_status calls were not concurrent: only "
                f"{self.count}/{self.n} calls were ever in flight at once "
                "before the concurrency barrier timed out -- looks like a "
                "sequential for-loop rather than asyncio.gather."
            )


def test_ac2_success_path_calls_are_concurrent_and_order_preserved(monkeypatch):
    entries = _entries(4)
    _patch(monkeypatch, "_get_style_normalization", lambda: True)
    _patch(monkeypatch, "_get_normalizer_model", lambda: "normalizer-model")
    barrier = _ConcurrencyBarrier(n=4)

    async def fake_query(model, messages, timeout):
        assert model == "normalizer-model"
        await barrier.wait()
        prompt = messages[0]["content"]
        matched = next(e for e in entries if e["response"] in prompt)
        return {
            "status": STATUS_OK,
            "content": f"NORMALIZED::{matched['model']}",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    _patch(monkeypatch, "query_model_with_status", fake_query)

    normalized, usage, failed = _run(ca._normalize_responses_with_timeout(entries, timeout=5.0))

    assert len(normalized) == 4
    for i, entry in enumerate(entries):
        assert normalized[i]["model"] == entry["model"]
        assert normalized[i]["response"] == f"NORMALIZED::{entry['model']}"
        assert normalized[i]["original_response"] == entry["response"]
    assert failed == []
    assert usage["prompt_tokens"] == 4
    assert usage["completion_tokens"] == 4
    assert usage["total_tokens"] == 8


# ---------------------------------------------------------------------------
# Mutation-testing hardening (scoped gate, 2026-08-21): the design's step 2
# says the outgoing chat message must be `{"role": "user", "content": ...}`
# and step 3 says a STATUS_OK result missing "content"/"usage" must fall
# back to the documented defaults (original response text; a zeroed usage
# dict, not None) rather than crash or silently diverge -- none of AC1-AC6
# above ever inspects the outgoing message's "role" key or exercises a
# STATUS_OK result with "content"/"usage" absent, so a mutant flipping
# "role" (or its "user" value), the `.get("content", ...)`/`.get("usage",
# ...)` fallback defaults, the `result_usage.get(..., 0)` sub-key defaults,
# or the `_add_cost_to_usage(..., model=entry["model"])` argument all
# survived (scoped mutmut run, 13 survivors, traced by hand). Verified by
# direct execution: this test alone kills all 13 when run against the
# mutated variants (`mutmut show <name>` inspected per mutant).
# ---------------------------------------------------------------------------


def test_mutation_hardening_success_path_role_and_missing_field_defaults(monkeypatch):
    entries = _entries(2)
    full_entry, sparse_entry = entries
    _patch(monkeypatch, "_get_style_normalization", lambda: True)
    _patch(monkeypatch, "_get_normalizer_model", lambda: "normalizer-model")

    async def fake_query(model, messages, timeout):
        # Design step 2: message must be a proper OpenAI-style user turn.
        assert messages[0]["role"] == "user"
        prompt = messages[0]["content"]
        matched = next(e for e in entries if e["response"] in prompt)
        if matched["model"] == full_entry["model"]:
            return {
                "status": STATUS_OK,
                "content": "NORMALIZED::full",
                "usage": {
                    "cost": 0.05,
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            }
        # sparse_entry: STATUS_OK but with neither "content" nor "usage" --
        # must fall back to the entry's own response text and a zeroed
        # usage contribution, never crash and never leave "response" as
        # None.
        return {"status": STATUS_OK}

    _patch(monkeypatch, "query_model_with_status", fake_query)

    normalized, usage, failed = _run(ca._normalize_responses_with_timeout(entries, timeout=5.0))

    by_model = {n["model"]: n for n in normalized}
    assert by_model[full_entry["model"]]["response"] == "NORMALIZED::full"
    # `.get("content", entry["response"])` default -- must be the entry's
    # OWN original text, never None (mutants 68/70 replace the default with
    # None / no default).
    assert by_model[sparse_entry["model"]]["response"] == sparse_entry["response"]
    assert failed == []

    # `.get("usage", {})` default -- a missing "usage" key must not raise
    # (mutants 81/83 replace `{}` with `None`, which crashes on the very
    # next `.get()` call) and must contribute zero to every accumulator
    # (mutants 96/107/118 replace the inner `0` sub-key defaults with `1`).
    assert usage["prompt_tokens"] == 3
    assert usage["completion_tokens"] == 2
    assert usage["total_tokens"] == 5

    # `_add_cost_to_usage(..., model=entry["model"])` -- per-model cost
    # attribution must key off the RESPONSE's own model, not `None`
    # (mutants 121/124 pass `model=None`/omit it, which means no
    # `by_model` bucket is ever created for either entry).
    assert full_entry["model"] in usage.get("by_model", {})
    assert usage["by_model"][full_entry["model"]]["cost_usd"] == 0.05
    assert sparse_entry["model"] in usage.get("by_model", {})


# ---------------------------------------------------------------------------
# AC3 -- Given 4 entries where one's mocked query returns status != OK and
# the other three return OK, when `_normalize_responses_with_timeout` is
# called, then the timed-out entry falls back to its original text, the
# other three are normalized, failed_models == [that one model], and no
# exception propagates.
# ---------------------------------------------------------------------------


def test_ac3_one_entry_times_out_others_succeed_no_exception(monkeypatch):
    entries = _entries(4)
    failing_model = entries[2]["model"]
    _patch(monkeypatch, "_get_style_normalization", lambda: True)
    _patch(monkeypatch, "_get_normalizer_model", lambda: "normalizer-model")

    async def fake_query(model, messages, timeout):
        prompt = messages[0]["content"]
        matched = next(e for e in entries if e["response"] in prompt)
        if matched["model"] == failing_model:
            return {"status": "timeout"}
        return {
            "status": STATUS_OK,
            "content": f"NORMALIZED::{matched['model']}",
            "usage": {},
        }

    _patch(monkeypatch, "query_model_with_status", fake_query)

    normalized, usage, failed = _run(ca._normalize_responses_with_timeout(entries, timeout=5.0))

    by_model = {n["model"]: n for n in normalized}
    for entry in entries:
        if entry["model"] == failing_model:
            assert by_model[entry["model"]]["response"] == entry["response"]
            assert by_model[entry["model"]]["original_response"] == entry["response"]
        else:
            assert by_model[entry["model"]]["response"] == f"NORMALIZED::{entry['model']}"
    assert failed == [failing_model]


# ---------------------------------------------------------------------------
# AC4 -- Given a fake query_model_with_status that asserts its own received
# timeout argument, when `_normalize_responses_with_timeout(entries,
# timeout=123.0)` is called, then every call receives timeout=123.0, never
# the package's hardcoded 60.0.
# ---------------------------------------------------------------------------


def test_ac4_timeout_argument_is_honored_not_hardcoded_60(monkeypatch):
    entries = _entries(2)
    _patch(monkeypatch, "_get_style_normalization", lambda: True)
    _patch(monkeypatch, "_get_normalizer_model", lambda: "normalizer-model")
    received_timeouts = []

    async def fake_query(model, messages, timeout):
        received_timeouts.append(timeout)
        return {"status": STATUS_OK, "content": "x", "usage": {}}

    _patch(monkeypatch, "query_model_with_status", fake_query)

    _run(ca._normalize_responses_with_timeout(entries, timeout=123.0))

    assert received_timeouts, "expected query_model_with_status to be called at least once"
    assert all(t == 123.0 for t in received_timeouts)
    assert 60.0 not in received_timeouts


# ---------------------------------------------------------------------------
# AC5 -- "auto" mode still gates on should_normalize_styles: False -> same
# zero-call/unchanged outcome as AC1; True -> normalization proceeds.
# ---------------------------------------------------------------------------


def test_ac5_auto_mode_not_triggered_behaves_like_gate_off(monkeypatch):
    entries = _entries(3)
    _patch(monkeypatch, "_get_style_normalization", lambda: "auto")
    received_args = []

    def fake_should_normalize(responses):
        received_args.append(responses)
        return False

    _patch(monkeypatch, "should_normalize_styles", fake_should_normalize)

    async def _fail_query(model, messages, timeout):
        _fail_if_called()

    _patch(monkeypatch, "query_model_with_status", _fail_query)

    normalized, usage, failed = _run(ca._normalize_responses_with_timeout(entries, timeout=5.0))

    assert normalized == [
        {"model": e["model"], "response": e["response"], "original_response": e["response"]}
        for e in entries
    ]
    assert usage == ZEROED_USAGE
    assert failed == []
    assert received_args == [[e["response"] for e in entries]]


def test_ac5_auto_mode_triggered_proceeds_with_normalization(monkeypatch):
    entries = _entries(3)
    _patch(monkeypatch, "_get_style_normalization", lambda: "auto")
    _patch(monkeypatch, "should_normalize_styles", lambda responses: True)
    _patch(monkeypatch, "_get_normalizer_model", lambda: "normalizer-model")

    async def fake_query(model, messages, timeout):
        prompt = messages[0]["content"]
        matched = next(e for e in entries if e["response"] in prompt)
        return {"status": STATUS_OK, "content": f"NORMALIZED::{matched['model']}", "usage": {}}

    _patch(monkeypatch, "query_model_with_status", fake_query)

    normalized, usage, failed = _run(ca._normalize_responses_with_timeout(entries, timeout=5.0))

    assert failed == []
    for i, entry in enumerate(entries):
        assert normalized[i]["response"] == f"NORMALIZED::{entry['model']}"


# ---------------------------------------------------------------------------
# Property test: AC2 + AC3 encode one general law -- normalized_entries is
# ALWAYS a length-preserving, order-preserving map of `entries`: every
# output entry's "model" matches the corresponding input entry's "model"
# (never dropped, never reordered, never duplicated), "original_response"
# always equals the input's "response", and "response" is either the
# mocked normalized text (status OK) or falls back to the original
# response (status != OK) -- with `failed_models` exactly the models whose
# calls didn't return STATUS_OK, in original order and free of duplicates.
# Capped max_examples=50, derandomize=True per project convention
# (mutation testing evidence: property tests kill ~50x more mutants than
# example tests for this exact "order/shape invariant" pattern).
# ---------------------------------------------------------------------------


@settings(max_examples=50, derandomize=True, deadline=3000, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    ok_flags=st.lists(st.booleans(), min_size=1, max_size=6),
)
def test_property_order_and_shape_preserved_regardless_of_which_entries_fail(monkeypatch, ok_flags):
    entries = [
        {"model": f"model-{i}", "response": f"raw response text {i}"} for i in range(len(ok_flags))
    ]
    _patch(monkeypatch, "_get_style_normalization", lambda: True)
    _patch(monkeypatch, "_get_normalizer_model", lambda: "normalizer-model")

    async def fake_query(model, messages, timeout):
        prompt = messages[0]["content"]
        matched = next(e for e in entries if e["response"] in prompt)
        idx = entries.index(matched)
        if ok_flags[idx]:
            return {"status": STATUS_OK, "content": f"NORMALIZED::{matched['model']}", "usage": {}}
        return {"status": "error"}

    _patch(monkeypatch, "query_model_with_status", fake_query)

    normalized, usage, failed = _run(ca._normalize_responses_with_timeout(entries, timeout=5.0))

    # Length- and order-preserving invariant.
    assert len(normalized) == len(entries)
    assert [n["model"] for n in normalized] == [e["model"] for e in entries]
    assert [n["original_response"] for n in normalized] == [e["response"] for e in entries]

    for i, (entry, ok) in enumerate(zip(entries, ok_flags)):
        if ok:
            assert normalized[i]["response"] == f"NORMALIZED::{entry['model']}"
        else:
            assert normalized[i]["response"] == entry["response"]

    expected_failed = [entries[i]["model"] for i, ok in enumerate(ok_flags) if not ok]
    assert failed == expected_failed


# ---------------------------------------------------------------------------
# AC6 -- `_normalize_stage2_for_stage3` threads timeout and failures.
# ---------------------------------------------------------------------------


def test_ac6_stage2_normalize_threads_timeout_and_reports_failed_model(monkeypatch):
    stage2_results = [
        {"model": "model-a", "ranking": "ranking text a", "parsed_ranking": {"foo": 1}},
        {"model": "model-b", "ranking": "ranking text b", "parsed_ranking": {"foo": 2}},
    ]
    _patch(monkeypatch, "_get_style_normalization", lambda: True)
    _patch(monkeypatch, "_get_normalizer_model", lambda: "normalizer-model")
    received_timeouts = []

    async def fake_query(model, messages, timeout):
        received_timeouts.append(timeout)
        prompt = messages[0]["content"]
        if "ranking text a" in prompt:
            return {"status": "timeout"}
        return {"status": STATUS_OK, "content": "NORMALIZED-B", "usage": {}}

    _patch(monkeypatch, "query_model_with_status", fake_query)

    result, usage, failed = _run(ca._normalize_stage2_for_stage3(stage2_results, timeout=99.0))

    assert received_timeouts and all(t == 99.0 for t in received_timeouts)
    by_model = {r["model"]: r for r in result}
    assert by_model["model-a"]["ranking"] == "ranking text a"
    assert by_model["model-a"]["parsed_ranking"] == {"foo": 1}
    assert by_model["model-b"]["ranking"] == "NORMALIZED-B"
    assert by_model["model-b"]["parsed_ranking"] == {"foo": 2}
    assert failed == ["model-a"]


def test_ac6_empty_stage2_results_short_circuits_with_zero_calls(monkeypatch):
    async def _fail_query(model, messages, timeout):
        _fail_if_called()

    _patch(monkeypatch, "query_model_with_status", _fail_query)
    # Deliberately no _get_style_normalization/_get_normalizer_model patch:
    # the contract's own single-model-degraded-mode note requires this
    # short-circuit to happen BEFORE any config read, so a config double
    # that doesn't define these should never be touched either.

    result, usage, failed = _run(ca._normalize_stage2_for_stage3([], timeout=5.0))

    assert result == []
    assert usage == ZEROED_USAGE
    assert failed == []
