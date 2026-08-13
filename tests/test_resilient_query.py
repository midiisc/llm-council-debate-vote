"""Blind acceptance tests for `docs/specs/debate-resilience-contract.md`
("Debate resilience contract (Pillar 2 -- spec before code)") -- the new
`scripts/resilient_query.py` module and its `query_models_resilient`
coroutine (AC1-10).

Authored WITHOUT sight of any implementation, design reasoning, or other
agent's work -- ONLY the contract markdown above was read. As of this
writing `scripts/resilient_query.py` does not exist, so this whole file is
expected to fail at collection/import time (RED) until the module lands.
This is the correct, expected blind-TDV state (see
`docs/anti-test-hacking.md` / CLAUDE.md Pillar 3): a missing module is a
feature-missing failure, not a typo/import-path bug in the test itself --
the import path below (`scripts.resilient_query`, with a bare
`resilient_query` fallback) mirrors the exact pattern every other test file
in this repo already uses (see `tests/test_council_adapter.py`,
`tests/test_pipeline_runner.py`) for resolving `scripts.<module>` package
members, so a collection failure here is unambiguously "module doesn't
exist yet", not an authoring mistake.

DOCUMENTED ASSUMPTIONS (the contract pins the exact dataclass shapes and
async signature verbatim, but a few wiring/construction details are left
to a reasonable, standard default -- called out here rather than silently
baked in):

  1. **Dataclass construction.** `RetryPolicy`, `ModelAttempt`,
     `SubstitutionEvent`, and `ResilientQueryResult` are plain
     `@dataclass`-decorated classes per the contract's literal code block.
     Tests construct `RetryPolicy` via keyword args only (matches the
     contract's own field defaults) and read result fields via plain
     attribute access (`result.responses`, `result.attempts`, ...) -- the
     contract shows no alternate accessor, so attribute access is the only
     defensible reading.

  2. **`query_fn` call signature.** The contract pins
     `QueryFn = Callable[[str, list[dict], float], Awaitable[dict]]` and
     explicitly cross-references
     `llm_council.openrouter.query_model_with_status(model, messages,
     timeout)` as the shape it matches -- so every fake `query_fn` below is
     invoked positionally as `query_fn(model, messages, timeout)` and
     records those three positional args per call. AC9 is tested by
     identity (`is`) on the `messages` object specifically, since the
     contract says "never a mutated copy" -- a same-value-but-different-
     object list would violate the spirit of AC9's "exact same ... object"
     wording. Tests build a genuinely mutable list of dicts for `messages`
     (not an immutable/frozen structure) so that any implementation which
     *did* copy would still be a same-valued list, forcing the assertion
     to rely on identity, not equality, to actually catch that mutation.

  3. **`sleep_fn` call signature.** The contract's `SleepFn = Callable[
     [float], Awaitable[None]]` with "default: asyncio.sleep" is read as
     "called with exactly one positional float argument, the number of
     seconds" -- matching `asyncio.sleep(delay)`'s own signature. Tests
     inject a fake `sleep_fn` that never actually sleeps (records the
     requested duration and returns immediately) to keep the suite
     hermetic and fast, per the "no real ... clock" requirement.

  4. **What counts as "the first unused entry of `backup_models`".**
     AC3, AC5, and AC6 together pin backup assignment to be positional,
     in `backup_models` list order, first-come-first-served across
     primaries, and "used for at most one slot per call" -- tests
     construct scenarios with 1-3 backups and assert only on this
     ordering/uniqueness invariant, never on an unstated tie-break rule
     the contract doesn't specify (e.g. which primary "wins" when two
     primaries fail at the exact same moment -- AC10 explicitly disclaims
     testing cross-model timing).

  5. **Response dict keying is by the actual successful model, not the
     original slot.** `responses` is `dict[str, dict]` with the field
     comment "model -> query_fn's own 'ok' response dict". AC3's wording
     resolves an otherwise-real ambiguity here: it says a terminally-failed
     primary is "not present in `responses`" -- stated with NO condition on
     whether its assigned backup later succeeds. That sentence is only
     true in every case (including when the backup succeeds) if `responses`
     keys are the literal model that actually returned `status="ok"`, never
     the original primary/"slot" name it substituted for -- under a
     slot-keyed reading, a successful backup would still leave the
     *primary's* name as a live key in `responses`, contradicting AC3.
     `SubstitutionEvent.slot_model` already carries the slot-to-primary
     mapping for any caller that needs to reconstruct "who filled which
     seat", so `responses` itself doesn't need to duplicate that -- it's a
     literal per-model outcome map. Tests assert `==` on the whole response
     dict where pinned (a `query_fn` fake's `"ok"` responses always include
     a `status` key plus a distinguishing marker per model, so accidental
     structural coincidence between fixtures is not a risk), and assert
     `<failed_model> not in responses` / `<succeeding_model> in responses`
     by actual-model identity everywhere a substitution occurs.

Hermetic: no real network, no real `asyncio.sleep` (a fake `sleep_fn` is
always injected), no real clock/timer dependency, no filesystem I/O. All
`query_fn` fakes are pure Python closures driven by a scripted queue of
statuses per model.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _import(name: str):
    try:
        return importlib.import_module(f"scripts.{name}")
    except ModuleNotFoundError:
        return importlib.import_module(name)


rq = _import("resilient_query")


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _make_scripted_query_fn(scripts: dict[str, list[str]], ok_marker_prefix: str = "resp"):
    """Build a fake QueryFn.

    `scripts` maps model name -> list of statuses to return on successive
    calls to that model, in order. Extra calls beyond the scripted list
    raise AssertionError (catches unexpectedly-many retries). Records every
    call's (model, messages, timeout) triple for AC9-style assertions.
    """
    call_log: list[tuple[str, list, float]] = []
    remaining = {model: list(statuses) for model, statuses in scripts.items()}

    async def query_fn(model: str, messages: list, timeout: float) -> dict:
        call_log.append((model, messages, timeout))
        queue = remaining.setdefault(model, [])
        if not queue:
            raise AssertionError(
                f"query_fn called for {model!r} more times than scripted"
            )
        status = queue.pop(0)
        if status == "ok":
            return {"status": "ok", "text": f"{ok_marker_prefix}-{model}"}
        return {"status": status}

    return query_fn, call_log


def _make_sleep_fn():
    sleep_log: list[float] = []

    async def sleep_fn(seconds: float) -> None:
        sleep_log.append(seconds)
        # deliberately does NOT actually sleep -- hermetic, fast, no real clock

    return sleep_fn, sleep_log


DEFAULT_MESSAGES = [{"role": "user", "content": "hello"}]
DEFAULT_TIMEOUT = 30.0


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# AC1 -- all primaries succeed first try
# ---------------------------------------------------------------------------


def test_ac1_all_primaries_ok_first_attempt_no_retries_no_substitutions():
    query_fn, call_log = _make_scripted_query_fn(
        {"model-a": ["ok"], "model-b": ["ok"], "model-c": ["ok"]}
    )
    sleep_fn, sleep_log = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a", "model-b", "model-c"],
            backup_models=["backup-x"],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            minimum_council_size=3,
            sleep_fn=sleep_fn,
        )
    )

    assert set(result.responses.keys()) == {"model-a", "model-b", "model-c"}
    for model in ("model-a", "model-b", "model-c"):
        assert result.responses[model] == {"status": "ok", "text": f"resp-{model}"}

    assert len(result.attempts) == 3
    for attempt in result.attempts:
        assert attempt.attempt_number == 1
        assert attempt.model in {"model-a", "model-b", "model-c"}

    assert result.substitutions == []
    assert result.unreachable_models == []
    assert result.shortfall_warning is None
    assert sleep_log == []  # no retries -> no backoff sleeps


def test_ac1_shortfall_warning_none_iff_primary_count_meets_minimum():
    # len(primary_models) == minimum_council_size, all succeed
    query_fn, _ = _make_scripted_query_fn({"m1": ["ok"], "m2": ["ok"]})
    sleep_fn, _ = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["m1", "m2"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            minimum_council_size=2,
            sleep_fn=sleep_fn,
        )
    )
    assert result.shortfall_warning is None


# ---------------------------------------------------------------------------
# AC2 -- retryable status then success; correct backoff; no backup used
# ---------------------------------------------------------------------------


def test_ac2_retry_then_success_uses_first_backoff_and_no_backup():
    query_fn, call_log = _make_scripted_query_fn({"model-a": ["timeout", "ok"]})
    sleep_fn, sleep_log = _make_sleep_fn()
    policy = rq.RetryPolicy(max_attempts=3, backoff_seconds=(5.0, 15.0))

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a"],
            backup_models=["backup-x"],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=1,
            sleep_fn=sleep_fn,
        )
    )

    assert result.responses == {"model-a": {"status": "ok", "text": "resp-model-a"}}

    model_a_attempts = [a for a in result.attempts if a.model == "model-a"]
    assert [a.attempt_number for a in model_a_attempts] == [1, 2]
    assert [a.status for a in model_a_attempts] == ["timeout", "ok"]

    assert sleep_log == [5.0]
    assert result.substitutions == []
    # backup was never invoked for this slot
    assert all(call[0] != "backup-x" for call in call_log)


# ---------------------------------------------------------------------------
# AC3 -- terminal (non-retryable) status stops immediately, triggers backup
# ---------------------------------------------------------------------------


def test_ac3_terminal_status_no_retry_and_backup_substituted():
    query_fn, call_log = _make_scripted_query_fn(
        {"model-a": ["auth_error"], "backup-x": ["ok"]}
    )
    sleep_fn, sleep_log = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a"],
            backup_models=["backup-x", "backup-y"],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            minimum_council_size=1,
            sleep_fn=sleep_fn,
        )
    )

    model_a_attempts = [a for a in result.attempts if a.model == "model-a"]
    assert len(model_a_attempts) == 1
    assert model_a_attempts[0].status == "auth_error"

    assert "model-a" not in result.responses

    assert len(result.substitutions) == 1
    sub = result.substitutions[0]
    assert sub.slot_model == "model-a"
    assert sub.backup_model == "backup-x"
    # reason must actually reflect the attempt count and last status, not
    # just be present -- see dataclass comment: 'e.g. "unreachable after 3
    # attempts (last status=timeout)"'.
    assert sub.reason == "unreachable after 1 attempts (last status=auth_error)"

    assert sleep_log == []  # no retry -> no backoff for the terminal attempt


# ---------------------------------------------------------------------------
# AC4 -- retryable status exhausts max_attempts, correct backoff sequence
# ---------------------------------------------------------------------------


def test_ac4_retryable_exhausts_max_attempts_full_backoff_sequence_and_substitution():
    query_fn, _ = _make_scripted_query_fn(
        {"model-a": ["timeout", "rate_limited", "error"], "backup-x": ["ok"]}
    )
    sleep_fn, sleep_log = _make_sleep_fn()
    policy = rq.RetryPolicy(max_attempts=3, backoff_seconds=(5.0, 15.0))

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a"],
            backup_models=["backup-x"],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=1,
            sleep_fn=sleep_fn,
        )
    )

    model_a_attempts = [a for a in result.attempts if a.model == "model-a"]
    assert len(model_a_attempts) == 3
    assert [a.attempt_number for a in model_a_attempts] == [1, 2, 3]
    assert [a.status for a in model_a_attempts] == ["timeout", "rate_limited", "error"]

    assert sleep_log == [5.0, 15.0]
    assert "model-a" not in result.responses
    assert len(result.substitutions) == 1
    assert result.substitutions[0].slot_model == "model-a"
    assert result.substitutions[0].reason == (
        "unreachable after 3 attempts (last status=error)"
    )


# ---------------------------------------------------------------------------
# AC5 -- backup also unreachable -> next unused backup tried for same slot
# ---------------------------------------------------------------------------


def test_ac5_primary_and_first_backup_both_unreachable_second_backup_tried():
    policy = rq.RetryPolicy(max_attempts=2, backoff_seconds=(1.0,))
    query_fn, call_log = _make_scripted_query_fn(
        {
            "model-a": ["timeout", "timeout"],
            "backup-x": ["timeout", "timeout"],
            "backup-y": ["ok"],
        }
    )
    sleep_fn, sleep_log = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a"],
            backup_models=["backup-x", "backup-y"],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=1,
            sleep_fn=sleep_fn,
        )
    )

    # Keyed by the actual model that produced the "ok" (backup-y), NOT the
    # original slot/primary name -- see DOCUMENTED ASSUMPTIONS point 5 above,
    # grounded in AC3's "not present in responses" wording holding even when
    # a backup later succeeds.
    assert result.responses == {"backup-y": {"status": "ok", "text": "resp-backup-y"}}
    assert "model-a" not in result.responses
    assert "backup-x" not in result.responses  # backup-x itself never got "ok"

    assert len(result.substitutions) == 2
    first, second = result.substitutions
    assert first.slot_model == "model-a"
    assert first.backup_model == "backup-x"
    assert first.reason == "unreachable after 2 attempts (last status=timeout)"
    assert second.slot_model == "model-a"
    assert second.backup_model == "backup-y"
    assert second.reason == "unreachable after 2 attempts (last status=timeout)"

    assert "model-a" in result.unreachable_models
    assert "backup-x" in result.unreachable_models
    assert "backup-y" not in result.unreachable_models  # it succeeded


def test_ac5_no_backup_remains_slot_stays_empty():
    policy = rq.RetryPolicy(max_attempts=1, backoff_seconds=())
    query_fn, _ = _make_scripted_query_fn(
        {"model-a": ["auth_error"], "backup-x": ["auth_error"]}
    )
    sleep_fn, _ = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a"],
            backup_models=["backup-x"],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=1,
            sleep_fn=sleep_fn,
        )
    )

    assert "model-a" not in result.responses
    assert len(result.responses) == 0
    assert len(result.substitutions) == 1  # only one backup existed to try
    assert result.substitutions[0].backup_model == "backup-x"


# ---------------------------------------------------------------------------
# AC6 -- a backup, once consumed, is never attempted for a second slot
# ---------------------------------------------------------------------------


def test_ac6_consumed_backup_not_reused_for_a_different_slot():
    policy = rq.RetryPolicy(max_attempts=1, backoff_seconds=())
    query_fn, call_log = _make_scripted_query_fn(
        {
            "model-a": ["auth_error"],
            "model-b": ["auth_error"],
            "backup-x": ["ok"],
            "backup-y": ["ok"],
        }
    )
    sleep_fn, _ = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a", "model-b"],
            backup_models=["backup-x", "backup-y"],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=2,
            sleep_fn=sleep_fn,
        )
    )

    used_backups = {sub.backup_model for sub in result.substitutions}
    assert used_backups == {"backup-x", "backup-y"}
    assert len(result.substitutions) == 2  # each backup used exactly once total

    # each backup was called exactly once across the whole run
    backup_x_calls = [c for c in call_log if c[0] == "backup-x"]
    backup_y_calls = [c for c in call_log if c[0] == "backup-y"]
    assert len(backup_x_calls) == 1
    assert len(backup_y_calls) == 1


# ---------------------------------------------------------------------------
# AC7 -- shortfall_warning content and None/non-None gating
# ---------------------------------------------------------------------------


def test_ac7_shortfall_warning_present_and_names_count_minimum_and_unreachable():
    policy = rq.RetryPolicy(max_attempts=1, backoff_seconds=())
    query_fn, _ = _make_scripted_query_fn(
        {"model-a": ["ok"], "model-b": ["auth_error"]}
    )
    sleep_fn, _ = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a", "model-b"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=2,
            sleep_fn=sleep_fn,
        )
    )

    assert len(result.responses) == 1
    assert result.shortfall_warning is not None
    msg = result.shortfall_warning
    assert "1" in msg  # exact final live count
    assert "2" in msg  # minimum_council_size
    assert "model-b" in msg  # every model in unreachable_models


def test_ac7_shortfall_warning_lists_multiple_unreachable_models_comma_space_joined():
    # Two unreachable models pins the exact join separator (', ') -- a
    # single-unreachable-model scenario can't distinguish ', '.join from any
    # other separator, since there's nothing to join.
    policy = rq.RetryPolicy(max_attempts=1, backoff_seconds=())
    query_fn, _ = _make_scripted_query_fn(
        {"model-a": ["auth_error"], "model-b": ["auth_error"], "model-c": ["ok"]}
    )
    sleep_fn, _ = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a", "model-b", "model-c"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=3,
            sleep_fn=sleep_fn,
        )
    )

    assert result.shortfall_warning is not None
    assert "model-a, model-b" in result.shortfall_warning


def test_default_minimum_council_size_is_four():
    # The contract's signature pins `minimum_council_size: int = 4` as the
    # default -- exercise it by omitting the argument entirely, since every
    # other test passes it explicitly and would miss a drift in the default.
    policy = rq.RetryPolicy(max_attempts=1, backoff_seconds=())

    # Exactly 3 successes: below the default minimum of 4 -> warning.
    query_fn_short, _ = _make_scripted_query_fn(
        {"model-a": ["ok"], "model-b": ["ok"], "model-c": ["ok"]}
    )
    sleep_fn_short, _ = _make_sleep_fn()
    result_short = _run(
        rq.query_models_resilient(
            primary_models=["model-a", "model-b", "model-c"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn_short,
            retry_policy=policy,
            sleep_fn=sleep_fn_short,
            # minimum_council_size omitted -> must default to 4
        )
    )
    assert result_short.shortfall_warning is not None
    assert "4" in result_short.shortfall_warning

    # Exactly 4 successes: meets the default minimum -> no warning.
    query_fn_full, _ = _make_scripted_query_fn(
        {"model-a": ["ok"], "model-b": ["ok"], "model-c": ["ok"], "model-d": ["ok"]}
    )
    sleep_fn_full, _ = _make_sleep_fn()
    result_full = _run(
        rq.query_models_resilient(
            primary_models=["model-a", "model-b", "model-c", "model-d"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn_full,
            retry_policy=policy,
            sleep_fn=sleep_fn_full,
            # minimum_council_size omitted -> must default to 4
        )
    )
    assert result_full.shortfall_warning is None


def test_ac7_shortfall_warning_none_when_final_count_meets_minimum_despite_failures():
    policy = rq.RetryPolicy(max_attempts=1, backoff_seconds=())
    query_fn, _ = _make_scripted_query_fn(
        {
            "model-a": ["ok"],
            "model-b": ["ok"],
            "model-c": ["auth_error"],
            "backup-x": ["ok"],
        }
    )
    sleep_fn, _ = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a", "model-b", "model-c"],
            backup_models=["backup-x"],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=3,
            sleep_fn=sleep_fn,
        )
    )
    assert len(result.responses) == 3
    assert result.shortfall_warning is None


# ---------------------------------------------------------------------------
# AC8 -- unreachable_models never includes an unused backup
# ---------------------------------------------------------------------------


def test_ac8_unused_backup_excluded_from_unreachable_models():
    query_fn, call_log = _make_scripted_query_fn(
        {"model-a": ["ok"], "model-b": ["ok"]}
    )
    sleep_fn, _ = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a", "model-b"],
            backup_models=["backup-never-called"],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            minimum_council_size=2,
            sleep_fn=sleep_fn,
        )
    )

    assert "backup-never-called" not in result.unreachable_models
    assert all(call[0] != "backup-never-called" for call in call_log)


def test_ac8_unreachable_models_contains_only_attempted_never_ok_models():
    policy = rq.RetryPolicy(max_attempts=1, backoff_seconds=())
    query_fn, _ = _make_scripted_query_fn(
        {"model-a": ["auth_error"], "model-b": ["ok"], "backup-x": ["timeout"]}
    )
    sleep_fn, _ = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a", "model-b"],
            backup_models=["backup-x"],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=2,
            sleep_fn=sleep_fn,
        )
    )

    assert set(result.unreachable_models) == {"model-a", "backup-x"}
    assert "model-b" not in result.unreachable_models


# ---------------------------------------------------------------------------
# AC9 -- every query_fn call gets the exact same messages object and timeout
# ---------------------------------------------------------------------------


def test_ac9_every_call_receives_identical_messages_object_and_timeout():
    policy = rq.RetryPolicy(max_attempts=2, backoff_seconds=(1.0,))
    query_fn, call_log = _make_scripted_query_fn(
        {"model-a": ["timeout", "auth_error"], "backup-x": ["ok"]}
    )
    sleep_fn, _ = _make_sleep_fn()

    messages = [{"role": "user", "content": "identity check"}]
    timeout = 42.5

    _run(
        rq.query_models_resilient(
            primary_models=["model-a"],
            backup_models=["backup-x"],
            messages=messages,
            timeout=timeout,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=1,
            sleep_fn=sleep_fn,
        )
    )

    assert len(call_log) >= 2  # model-a x2 (retry then terminal) + backup-x
    for _model, called_messages, called_timeout in call_log:
        assert called_messages is messages  # identity, not equality
        assert called_timeout == timeout


# ---------------------------------------------------------------------------
# AC10 -- independence of each model's retry/backup resolution
# ---------------------------------------------------------------------------


def test_ac10_two_models_retry_independently_matching_single_model_scenarios():
    policy = rq.RetryPolicy(max_attempts=3, backoff_seconds=(2.0, 4.0))

    # Multi-model run: model-a needs 2 tries, model-b needs all 3.
    multi_query_fn, _ = _make_scripted_query_fn(
        {"model-a": ["timeout", "ok"], "model-b": ["timeout", "timeout", "ok"]}
    )
    multi_sleep_fn, multi_sleep_log = _make_sleep_fn()

    multi_result = _run(
        rq.query_models_resilient(
            primary_models=["model-a", "model-b"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=multi_query_fn,
            retry_policy=policy,
            minimum_council_size=2,
            sleep_fn=multi_sleep_fn,
        )
    )

    # Single-model baseline scenarios, run in isolation with the SAME
    # scripted statuses and SAME policy.
    solo_a_query_fn, _ = _make_scripted_query_fn({"model-a": ["timeout", "ok"]})
    solo_a_sleep_fn, solo_a_sleep_log = _make_sleep_fn()
    solo_a_result = _run(
        rq.query_models_resilient(
            primary_models=["model-a"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=solo_a_query_fn,
            retry_policy=policy,
            minimum_council_size=1,
            sleep_fn=solo_a_sleep_fn,
        )
    )

    solo_b_query_fn, _ = _make_scripted_query_fn(
        {"model-b": ["timeout", "timeout", "ok"]}
    )
    solo_b_sleep_fn, solo_b_sleep_log = _make_sleep_fn()
    solo_b_result = _run(
        rq.query_models_resilient(
            primary_models=["model-b"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=solo_b_query_fn,
            retry_policy=policy,
            minimum_council_size=1,
            sleep_fn=solo_b_sleep_fn,
        )
    )

    multi_a_attempts = [
        (a.attempt_number, a.status) for a in multi_result.attempts if a.model == "model-a"
    ]
    multi_b_attempts = [
        (a.attempt_number, a.status) for a in multi_result.attempts if a.model == "model-b"
    ]
    solo_a_attempts = [
        (a.attempt_number, a.status) for a in solo_a_result.attempts if a.model == "model-a"
    ]
    solo_b_attempts = [
        (a.attempt_number, a.status) for a in solo_b_result.attempts if a.model == "model-b"
    ]

    assert multi_a_attempts == solo_a_attempts
    assert multi_b_attempts == solo_b_attempts

    assert multi_result.responses["model-a"] == solo_a_result.responses["model-a"]
    assert multi_result.responses["model-b"] == solo_b_result.responses["model-b"]

    # Each model's own backoff schedule matches its solo run (order-
    # independent since AC10 explicitly disclaims cross-model interleaving
    # timing -- only checking each model contributed the SAME multiset of
    # backoff values it would contribute alone).
    assert sorted(multi_sleep_log) == sorted(solo_a_sleep_log + solo_b_sleep_log)


# ---------------------------------------------------------------------------
# Property-based tests (hypothesis) -- laws implied by the contract
# ---------------------------------------------------------------------------

# AC1/AC7 law: shortfall_warning is None IFF len(responses) >= minimum_council_size.
# This is a shape/type invariant that must hold across an arbitrary mix of
# always-ok and never-ok primaries with no backups in play (isolates the
# gating logic from substitution logic).


@st.composite
def _all_ok_or_all_fail_primary_plan(draw):
    n_models = draw(st.integers(min_value=1, max_value=6))
    models = [f"model-{i}" for i in range(n_models)]
    # each model is independently scripted to always succeed or always
    # terminally fail (auth_error, non-retryable) on its single attempt
    outcomes = draw(
        st.lists(st.booleans(), min_size=n_models, max_size=n_models)
    )
    minimum = draw(st.integers(min_value=0, max_value=n_models + 2))
    return models, outcomes, minimum


@given(plan=_all_ok_or_all_fail_primary_plan())
@settings(max_examples=50, derandomize=True, deadline=2000)
def test_property_shortfall_warning_iff_final_count_below_minimum(plan):
    """Encodes AC7 as a general law: for ANY mix of always-succeeding and
    always-terminally-failing primaries (no backups available, so the
    final response count is exactly the count of "ok" primaries),
    shortfall_warning is None if and only if that count meets the
    configured minimum -- never the inverse, never inconsistent.
    """
    models, outcomes, minimum = plan
    scripts = {
        m: (["ok"] if ok else ["auth_error"]) for m, ok in zip(models, outcomes)
    }
    query_fn, _ = _make_scripted_query_fn(scripts)
    sleep_fn, _ = _make_sleep_fn()
    policy = rq.RetryPolicy(max_attempts=1, backoff_seconds=())

    result = _run(
        rq.query_models_resilient(
            primary_models=models,
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=minimum,
            sleep_fn=sleep_fn,
        )
    )

    final_count = len(result.responses)
    if final_count >= minimum:
        assert result.shortfall_warning is None
    else:
        assert result.shortfall_warning is not None


@given(plan=_all_ok_or_all_fail_primary_plan())
@settings(max_examples=50, derandomize=True, deadline=2000)
def test_property_responses_and_unreachable_partition_attempted_models(plan):
    """Encodes AC1 + AC8 as a general shape law: every primary model ends
    up in EXACTLY ONE of `responses` (succeeded) or `unreachable_models`
    (attempted, never ok) -- never both, never neither, for any mix of
    always-ok / always-terminally-failing primaries.
    """
    models, outcomes, minimum = plan
    scripts = {
        m: (["ok"] if ok else ["auth_error"]) for m, ok in zip(models, outcomes)
    }
    query_fn, _ = _make_scripted_query_fn(scripts)
    sleep_fn, _ = _make_sleep_fn()
    policy = rq.RetryPolicy(max_attempts=1, backoff_seconds=())

    result = _run(
        rq.query_models_resilient(
            primary_models=models,
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=minimum,
            sleep_fn=sleep_fn,
        )
    )

    responded = set(result.responses.keys())
    unreachable = set(result.unreachable_models)

    assert responded.isdisjoint(unreachable)
    assert responded | unreachable == set(models)
    for i, model in enumerate(models):
        if outcomes[i]:
            assert model in responded
        else:
            assert model in unreachable


# AC4 law: for any max_attempts N and any all-retryable-status run, the
# number of attempts recorded for that model is exactly N and the number
# of backoff sleeps is exactly N-1, using the first N-1 backoff values in
# order -- a monotonic/ordering invariant over N.


@given(
    max_attempts=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=50, derandomize=True, deadline=2000)
def test_property_attempt_and_backoff_counts_scale_with_max_attempts(max_attempts):
    """Encodes AC4 as a general law over max_attempts: attempts recorded
    == max_attempts, sleeps recorded == max_attempts - 1, and the sleep
    values equal backoff_seconds[0 : max_attempts - 1] in order, for any
    valid max_attempts when every attempt returns a retryable status.
    """
    backoff = tuple(float(i + 1) for i in range(max(max_attempts - 1, 0)))
    policy = rq.RetryPolicy(max_attempts=max_attempts, backoff_seconds=backoff)
    query_fn, _ = _make_scripted_query_fn({"model-a": ["timeout"] * max_attempts})
    sleep_fn, sleep_log = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=1,
            sleep_fn=sleep_fn,
        )
    )

    model_a_attempts = [a for a in result.attempts if a.model == "model-a"]
    assert len(model_a_attempts) == max_attempts
    assert [a.attempt_number for a in model_a_attempts] == list(
        range(1, max_attempts + 1)
    )
    assert len(sleep_log) == max_attempts - 1
    assert sleep_log == list(backoff)


# ---------------------------------------------------------------------------
# Wall-clock deadline (docs/specs/wallclock-cost-budget-contract.md,
# Contract 1) -- an absolute time_fn()-based cutoff after which no further
# retry/backup attempts are issued for any unresolved slot, so Stage 1 can
# no longer alone exhaust the overall wall-clock ceiling
# (architecture-stress-test-2026-08-13.md, Critical #3). Uses a fake,
# hand-advanced clock (never real time.monotonic) so these tests are
# hermetic and deterministic.
# ---------------------------------------------------------------------------


def _make_clock(start: float = 0.0):
    """A fake monotonic clock: time_fn() returns `now`; call clock.advance(n)
    to move it forward deterministically from within a test or a fake
    query_fn/sleep_fn."""

    class _Clock:
        def __init__(self):
            self.now = start

        def __call__(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds

    return _Clock()


def test_deadline_none_behaves_identically_to_no_deadline():
    query_fn, call_log = _make_scripted_query_fn({"model-a": ["ok"], "model-b": ["ok"]})
    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a", "model-b"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            minimum_council_size=2,
            deadline=None,
        )
    )
    assert set(result.responses) == {"model-a", "model-b"}
    assert result.shortfall_warning is None
    assert len(call_log) == 2


def test_deadline_far_in_the_future_behaves_identically_to_no_deadline():
    clock = _make_clock(start=0.0)
    query_fn, call_log = _make_scripted_query_fn({"model-a": ["ok"]})
    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            minimum_council_size=1,
            deadline=10_000.0,
            time_fn=clock,
        )
    )
    assert set(result.responses) == {"model-a"}
    assert len(call_log) == 1


def test_deadline_exactly_equal_to_now_does_not_consume_a_backup():
    # Boundary case for _resolve_slot's own check specifically: at exactly
    # time_fn() == deadline, _attempt_with_retries's own (separate) check
    # would ALSO stop the primary's first attempt either way, so the two
    # checks are only distinguishable by whether a backup gets consumed
    # afterward - _resolve_slot's check must return before ever reaching
    # the backup-substitution logic. Traced by hand from a real mutmut
    # survivor (>= mutated to > in _resolve_slot).
    clock = _make_clock(start=50.0)
    query_fn, call_log = _make_scripted_query_fn({"model-a": ["ok"], "backup-1": ["ok"]})

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a"],
            backup_models=["backup-1"],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            minimum_council_size=1,
            deadline=50.0,  # exactly equal to clock.now, not past it
            time_fn=clock,
        )
    )

    assert call_log == []
    assert result.substitutions == []  # backup-1 was never even attempted
    assert "model-a" in result.unreachable_models
    assert "backup-1" not in result.unreachable_models  # never attempted at all


def test_deadline_exactly_equal_to_now_mid_retry_skips_the_next_attempt():
    # Boundary case for _attempt_with_retries's own check specifically:
    # the deadline is reached (not yet passed) exactly as attempt 1
    # finishes - attempt 2 must still be skipped. Traced by hand from a
    # real mutmut survivor (>= mutated to > in _attempt_with_retries).
    clock = _make_clock(start=0.0)

    async def query_fn(model, messages, timeout):
        clock.advance(60.0)  # lands EXACTLY on the deadline, not past it
        return {"status": "timeout"}

    policy = rq.RetryPolicy(max_attempts=3, backoff_seconds=(5.0, 15.0))
    sleep_fn, sleep_log = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=1,
            sleep_fn=sleep_fn,
            deadline=60.0,
            time_fn=clock,
        )
    )

    model_a_attempts = [a for a in result.attempts if a.model == "model-a"]
    assert len(model_a_attempts) == 1  # only attempt 1 - attempt 2 skipped at the exact boundary
    assert result.responses == {}


def test_deadline_already_passed_before_first_attempt_gets_zero_attempts():
    clock = _make_clock(start=100.0)  # already past the deadline of 50.0
    query_fn, call_log = _make_scripted_query_fn({"model-a": ["ok"]})  # would succeed if ever called
    sleep_fn, sleep_log = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            minimum_council_size=1,
            sleep_fn=sleep_fn,
            deadline=50.0,
            time_fn=clock,
        )
    )

    assert call_log == []  # zero attempts -- never even called query_fn once
    assert sleep_log == []
    assert result.responses == {}
    assert "model-a" in result.unreachable_models
    assert result.shortfall_warning is not None


def test_deadline_already_passed_does_not_consume_a_backup():
    clock = _make_clock(start=100.0)
    query_fn, call_log = _make_scripted_query_fn({"model-a": ["ok"], "backup-1": ["ok"]})

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a"],
            backup_models=["backup-1"],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            minimum_council_size=1,
            deadline=50.0,
            time_fn=clock,
        )
    )

    assert call_log == []  # neither the primary nor the backup was ever attempted
    assert result.substitutions == []  # no substitution event for an attempt that never happened
    assert result.responses == {}


def test_deadline_passes_between_attempt_1_and_attempt_2_skips_attempt_2():
    clock = _make_clock(start=0.0)
    call_log: list[str] = []

    async def query_fn(model, messages, timeout):
        call_log.append(model)
        clock.advance(60.0)  # this call itself consumes enough time to blow the deadline
        return {"status": "timeout"}

    policy = rq.RetryPolicy(max_attempts=3, backoff_seconds=(5.0, 15.0))
    sleep_fn, sleep_log = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=1,
            sleep_fn=sleep_fn,
            deadline=50.0,  # first call advances clock past this
            time_fn=clock,
        )
    )

    assert call_log == ["model-a"]  # only attempt 1 happened, attempt 2 was skipped
    assert result.responses == {}
    assert "model-a" in result.unreachable_models


def test_deadline_triggered_shortfall_warning_is_exactly_as_loud_as_backups_exhausted():
    clock = _make_clock(start=100.0)
    query_fn, _ = _make_scripted_query_fn({"model-a": ["ok"], "model-b": ["ok"]})

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a", "model-b"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            minimum_council_size=2,
            deadline=50.0,  # already passed -- neither model gets attempted
            time_fn=clock,
        )
    )

    assert result.shortfall_warning is not None
    assert "0 of the required minimum 2" in result.shortfall_warning
    assert "model-a" in result.shortfall_warning
    assert "model-b" in result.shortfall_warning


def test_retry_policy_raises_at_construction_if_backoff_seconds_too_short():
    # architecture-stress-test-2026-08-13.md, High finding: max_attempts=4
    # needs 3 backoff entries (between each pair of attempts) but only 2
    # are given here - must fail loudly and immediately at construction,
    # not with a mid-debate IndexError deep in the retry loop.
    with pytest.raises(ValueError):
        rq.RetryPolicy(max_attempts=4, backoff_seconds=(5.0, 15.0))


def test_retry_policy_accepts_exactly_matching_backoff_length():
    # max_attempts=3 needs exactly 2 backoff entries - the boundary case,
    # must NOT raise.
    policy = rq.RetryPolicy(max_attempts=3, backoff_seconds=(5.0, 15.0))
    assert policy.max_attempts == 3


def test_retry_policy_accepts_more_backoff_entries_than_strictly_needed():
    # Extra, unused backoff entries are harmless - only too FEW is an error.
    policy = rq.RetryPolicy(max_attempts=2, backoff_seconds=(5.0, 15.0, 25.0))
    assert policy.max_attempts == 2


def test_retry_policy_max_attempts_one_needs_zero_backoff_entries():
    policy = rq.RetryPolicy(max_attempts=1, backoff_seconds=())
    assert policy.max_attempts == 1


def test_backup_model_that_duplicates_a_primary_is_never_attempted_as_a_backup():
    # architecture-stress-test-2026-08-13.md, Low finding: if a
    # debate_resilience.backup_models entry is also a primary model, using
    # it as a backup for a DIFFERENT slot doesn't add real resilience (if
    # that model is down, it's down for both roles) and risks a silent
    # responses-dict collision keyed by bare model name.
    query_fn, call_log = _make_scripted_query_fn(
        {"model-a": ["timeout", "timeout", "timeout"], "model-b": ["ok"]}
    )
    policy = rq.RetryPolicy(max_attempts=3, backoff_seconds=(5.0, 15.0))
    sleep_fn, _ = _make_sleep_fn()

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a", "model-b"],
            backup_models=["model-b"],  # duplicates the OTHER primary
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            retry_policy=policy,
            minimum_council_size=1,
            sleep_fn=sleep_fn,
        )
    )

    # model-b was never attempted a second time as model-a's backup - the
    # duplicate entry is filtered out of the backup pool entirely.
    model_b_calls = [c for c in call_log if c[0] == "model-b"]
    assert len(model_b_calls) == 1
    assert result.substitutions == []
    assert "model-a" in result.unreachable_models


def test_deadline_lets_some_slots_succeed_before_it_passes_and_returns_partial_results():
    clock = _make_clock(start=0.0)

    async def query_fn(model, messages, timeout):
        if model == "model-a":
            return {"status": "ok", "text": "resp-a"}
        # model-b's very first attempt burns past the deadline
        clock.advance(1000.0)
        return {"status": "timeout"}

    result = _run(
        rq.query_models_resilient(
            primary_models=["model-a", "model-b"],
            backup_models=[],
            messages=DEFAULT_MESSAGES,
            timeout=DEFAULT_TIMEOUT,
            query_fn=query_fn,
            minimum_council_size=2,
            deadline=500.0,
            time_fn=clock,
        )
    )

    assert result.responses == {"model-a": {"status": "ok", "text": "resp-a"}}
    assert "model-b" in result.unreachable_models
    assert result.shortfall_warning is not None
