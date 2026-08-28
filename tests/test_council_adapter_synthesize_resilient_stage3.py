"""Blind acceptance tests for `scripts.council_adapter._synthesize_resilient`
(Stage 3 chairman-synthesis retry-with-backoff, NO model substitution).

Authored from ONLY the contract handed to the isolated Verifier:

  SIGNATURE (new function, scripts/council_adapter.py):
    async def _synthesize_resilient(
        stage3_query: str,
        chairman_model: str,
        timeout: float,
        retry_policy: RetryPolicy,
        query_fn,
        sleep_fn=asyncio.sleep,
    ) -> tuple[dict, dict[str, int], bool]
    # raises ChairmanUnreachableError(chairman_model, attempts, last_status)

  class ChairmanUnreachableError(Exception):
      def __init__(self, chairman_model: str, attempts: int, last_status: str): ...

  ENVIRONMENT (given, not re-derived):
    - `scripts/resilient_query.py` exports `RetryPolicy` (fields:
      max_attempts: int, backoff_seconds: tuple[float, ...],
      retryable_statuses: frozenset[str] -- any status NOT in
      retryable_statuses is terminal, no further retry).
    - query_fn signature: async def query_fn(model, messages_or_prompt,
      timeout) -> dict returning {"status": "ok"|"timeout"|"rate_limited"|
      "auth_error"|"error", ...}; on status=="ok" the dict also carries the
      response content and a "usage" sub-dict.
    - The chairman role may ONLY ever be filled by `chairman_model` itself
      -- no backup/fallback model pool for this role, ever.

  ACCEPTANCE CRITERIA (verbatim numbering from the contract):
    AC7  -- immediate "ok" on attempt 1: returns immediately, chairman_
            degraded=False, sleep_fn never awaited, usage from that single
            response.
    AC8  -- retryable status on attempt 1, "ok" on attempt 2: returns the
            attempt-2 response, sleep_fn awaited exactly once with
            backoff_seconds[0], chairman_degraded=False.
    AC9  -- retryable status on every attempt up to max_attempts: query_fn
            called exactly max_attempts times (never more/fewer), sleep_fn
            awaited exactly max_attempts-1 times with backoff_seconds[0],
            [1], ... in order, raises ChairmanUnreachableError(chairman_
            model, attempts=max_attempts, last_status=<last status>) -- no
            partial/empty/default response, never queries any model other
            than chairman_model.
    AC10 -- terminal (non-retryable) status on attempt 1 (e.g.
            "auth_error"): exactly one call to query_fn (no retry
            attempted), ChairmanUnreachableError raised immediately with
            attempts=1.

  NON-GOALS (from the contract, respected here): do not assert anything
  about the exact prompt/messages construction passed as the second
  positional argument to query_fn (that's explicitly out of scope); do not
  assert any fallback/substitute-chairman behavior exists -- its absence
  IS asserted (every recorded query_fn call must use chairman_model, never
  any other model name), per the contract's explicit "a test asserting no
  other model is ever queried is in scope" instruction.

Authored WITHOUT sight of any implementation, design notes, or other
agent's reasoning. Confirmed before authoring (2026-08-14): `scripts/
council_adapter.py` currently defines no `_synthesize_resilient` and no
`ChairmanUnreachableError` -- every test in this file is expected to fail
at import/collection time (RED) until the feature lands. `RetryPolicy` is
imported from `scripts.resilient_query`, which the contract states is
already implemented/tested elsewhere and is treated here strictly as a
given building block (its own `__post_init__` requires
`len(backoff_seconds) >= max_attempts - 1`, which every RetryPolicy built
below satisfies).
"""
from __future__ import annotations

import asyncio

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from scripts.resilient_query import RetryPolicy

try:
    from scripts.council_adapter import ChairmanUnreachableError, _synthesize_resilient
except ImportError:  # expected RED until the feature lands
    _synthesize_resilient = None
    ChairmanUnreachableError = None


CHAIRMAN = "openai/gpt-5.1-chairman"
STAGE3_QUERY = "synthesize the council's final answer"
TIMEOUT = 42.0

RETRYABLE = frozenset({"timeout", "rate_limited", "error"})
TERMINAL_EXAMPLE = "auth_error"


def _run(coro):
    return asyncio.run(coro)


def _ok_response(tag: str) -> dict:
    return {
        "status": "ok",
        "content": f"final-synthesis-{tag}",
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


def _fail_response(status: str) -> dict:
    return {"status": status}


def _make_query_fn(responses: list[dict]):
    """Returns (fake_query_fn, calls) -- calls records (model, timeout) for
    every invocation, in order. Pops one response per call; raises if more
    calls are made than responses were supplied (guards against runaway
    over-calling)."""
    calls: list[dict] = []
    queue = list(responses)

    async def fake(model, messages_or_prompt=None, timeout=None, **kwargs):
        if not queue:
            raise AssertionError(
                f"query_fn called more times than expected (already called "
                f"{len(calls)} times); this call used model={model!r}"
            )
        calls.append({"model": model, "timeout": timeout if timeout is not None else kwargs.get("timeout")})
        return queue.pop(0)

    return fake, calls


def _make_sleep_fn():
    calls: list[float] = []

    async def fake_sleep(seconds):
        calls.append(seconds)

    return fake_sleep, calls


def _require_impl():
    if _synthesize_resilient is None:
        pytest.fail(
            "scripts.council_adapter._synthesize_resilient / "
            "ChairmanUnreachableError not importable yet -- RED as expected "
            "until the feature lands"
        )


# ---------------------------------------------------------------------------
# AC7: immediate success on attempt 1.
# ---------------------------------------------------------------------------


def test_ac7_immediate_ok_returns_first_response_no_retry_no_sleep():
    _require_impl()
    ok = _ok_response("attempt1")
    query_fn, calls = _make_query_fn([ok])
    sleep_fn, sleep_calls = _make_sleep_fn()
    policy = RetryPolicy(max_attempts=3, backoff_seconds=(5.0, 15.0), retryable_statuses=RETRYABLE)

    response, usage, degraded = _run(
        _synthesize_resilient(STAGE3_QUERY, CHAIRMAN, TIMEOUT, policy, query_fn, sleep_fn)
    )

    assert response == ok
    assert usage == ok["usage"]
    assert degraded is False
    assert len(calls) == 1
    assert calls[0]["model"] == CHAIRMAN
    assert sleep_calls == []


# ---------------------------------------------------------------------------
# AC8: one retryable status, then ok on attempt 2.
# ---------------------------------------------------------------------------


def test_ac8_retryable_then_ok_returns_second_response_sleeps_once_with_first_backoff():
    _require_impl()
    fail = _fail_response("timeout")
    ok = _ok_response("attempt2")
    query_fn, calls = _make_query_fn([fail, ok])
    sleep_fn, sleep_calls = _make_sleep_fn()
    policy = RetryPolicy(max_attempts=3, backoff_seconds=(5.0, 15.0), retryable_statuses=RETRYABLE)

    response, usage, degraded = _run(
        _synthesize_resilient(STAGE3_QUERY, CHAIRMAN, TIMEOUT, policy, query_fn, sleep_fn)
    )

    assert response == ok
    assert usage == ok["usage"]
    assert degraded is False
    assert len(calls) == 2
    assert all(c["model"] == CHAIRMAN for c in calls)
    assert sleep_calls == [5.0]


# ---------------------------------------------------------------------------
# AC9: retryable status on every attempt up to max_attempts.
# ---------------------------------------------------------------------------


def test_ac9_exhausts_max_attempts_then_raises_chairman_unreachable():
    _require_impl()
    max_attempts = 3
    backoff = (5.0, 15.0)
    fails = [_fail_response("timeout"), _fail_response("rate_limited"), _fail_response("error")]
    query_fn, calls = _make_query_fn(fails)
    sleep_fn, sleep_calls = _make_sleep_fn()
    policy = RetryPolicy(max_attempts=max_attempts, backoff_seconds=backoff, retryable_statuses=RETRYABLE)

    with pytest.raises(ChairmanUnreachableError) as excinfo:
        _run(_synthesize_resilient(STAGE3_QUERY, CHAIRMAN, TIMEOUT, policy, query_fn, sleep_fn))

    assert len(calls) == max_attempts
    assert all(c["model"] == CHAIRMAN for c in calls), "chairman role must never be filled by another model"
    assert sleep_calls == list(backoff)

    err = excinfo.value
    assert err.chairman_model == CHAIRMAN
    assert err.attempts == max_attempts
    assert err.last_status == "error"  # status of the final (3rd) attempt


# ---------------------------------------------------------------------------
# AC10: terminal (non-retryable) status on attempt 1 -- no retry attempted.
# ---------------------------------------------------------------------------


def test_ac10_terminal_status_on_first_attempt_raises_immediately_no_retry():
    _require_impl()
    fail = _fail_response(TERMINAL_EXAMPLE)
    query_fn, calls = _make_query_fn([fail])
    sleep_fn, sleep_calls = _make_sleep_fn()
    policy = RetryPolicy(max_attempts=3, backoff_seconds=(5.0, 15.0), retryable_statuses=RETRYABLE)

    with pytest.raises(ChairmanUnreachableError) as excinfo:
        _run(_synthesize_resilient(STAGE3_QUERY, CHAIRMAN, TIMEOUT, policy, query_fn, sleep_fn))

    assert len(calls) == 1
    assert calls[0]["model"] == CHAIRMAN
    assert sleep_calls == []  # terminal status: no retry, so no backoff sleep at all

    err = excinfo.value
    assert err.chairman_model == CHAIRMAN
    assert err.attempts == 1
    assert err.last_status == TERMINAL_EXAMPLE


# ---------------------------------------------------------------------------
# Supporting unit test: ChairmanUnreachableError exposes the fields its own
# constructor names (contract's stub gives (chairman_model, attempts,
# last_status) as the identifying triple used by AC9/AC10's assertions).
# ---------------------------------------------------------------------------


def test_chairman_unreachable_error_exposes_constructor_fields_as_attributes():
    if ChairmanUnreachableError is None:
        pytest.fail("ChairmanUnreachableError not importable yet -- RED as expected")
    err = ChairmanUnreachableError(chairman_model="some/model", attempts=4, last_status="timeout")
    assert err.chairman_model == "some/model"
    assert err.attempts == 4
    assert err.last_status == "timeout"
    assert isinstance(err, Exception)


# ---------------------------------------------------------------------------
# MUTATION-GATE HARDENING (2026-08-14): added after the initial blind-
# authorship pass to close real gaps a scoped mutmut run surfaced on
# `_synthesize_resilient`/`ChairmanUnreachableError`. Distinct from the
# AC-derived tests above -- kept separate and explicitly labeled rather than
# folded into AC7-10, mirroring this project's own established pattern of
# documenting mutation-testing findings (see the "Mutation-testing note"
# comments in scripts/council_adapter.py itself).
# ---------------------------------------------------------------------------


def test_chairman_unreachable_error_message_includes_identifying_details():
    """A mutant collapsing the constructor's formatted message to a bare
    `None` survived because no test ever read `str(err)` -- every existing
    assertion only checked the three attributes, never the exception's own
    message."""
    if ChairmanUnreachableError is None:
        pytest.fail("ChairmanUnreachableError not importable yet -- RED as expected")
    err = ChairmanUnreachableError(chairman_model="some/model", attempts=4, last_status="timeout")
    message = str(err)
    assert "some/model" in message
    assert "4" in message
    assert "timeout" in message


def test_stage3_query_and_timeout_forwarded_unmodified_to_query_fn():
    """`_synthesize_resilient` does no transformation of `stage3_query`
    before forwarding it to `query_fn` -- unlike Stage 1's `messages`
    construction, there is no message-array wrapping here (per the
    signature, `query_fn(model, messages_or_prompt, timeout)`). Checking the
    forwarded value is byte-identical to the input is an identity-
    passthrough check, not the "exact prompt/messages construction" the
    contract's non-goal disclaims (which concerns transformation logic this
    function doesn't have). Found by the scoped mutmut gate:
    `query_fn(chairman_model, None, timeout)`,
    `query_fn(chairman_model, stage3_query, None)`, and two arg-dropping
    mutants all survived because no prior test recorded the message/timeout
    arguments at all -- only `model` was ever checked."""
    _require_impl()
    ok = _ok_response("attempt1")
    recorded: dict = {}

    async def fake(model, message, timeout):
        recorded["model"] = model
        recorded["message"] = message
        recorded["timeout"] = timeout
        return ok

    sleep_fn, _ = _make_sleep_fn()
    policy = RetryPolicy(max_attempts=1, backoff_seconds=())

    _run(_synthesize_resilient(STAGE3_QUERY, CHAIRMAN, TIMEOUT, policy, fake, sleep_fn))

    assert recorded["model"] == CHAIRMAN
    assert recorded["message"] == STAGE3_QUERY
    assert recorded["timeout"] == TIMEOUT


def test_max_attempts_zero_raises_immediately_with_no_calls_and_none_last_status():
    """`max_attempts=0` is a degenerate-but-valid `RetryPolicy` -- its own
    `__post_init__` only constrains `len(backoff_seconds) >= max_attempts -
    1`, never requires `max_attempts >= 1`. The loop body then never
    executes, so `ChairmanUnreachableError` must be raised carrying the
    loop's untouched initial `last_status` value. A mutant seeding
    `last_status` to `""` instead of `None` survived because the property
    test's own strategy only draws `max_attempts` from 1..5."""
    _require_impl()
    query_fn, calls = _make_query_fn([])
    sleep_fn, sleep_calls = _make_sleep_fn()
    policy = RetryPolicy(max_attempts=0, backoff_seconds=())

    with pytest.raises(ChairmanUnreachableError) as excinfo:
        _run(_synthesize_resilient(STAGE3_QUERY, CHAIRMAN, TIMEOUT, policy, query_fn, sleep_fn))

    assert len(calls) == 0
    assert sleep_calls == []
    err = excinfo.value
    assert err.chairman_model == CHAIRMAN
    assert err.attempts == 0
    assert err.last_status is None


def test_ok_response_without_usage_key_defaults_usage_to_empty_dict():
    """Defends the documented query_fn contract ("on status=='ok' ... also
    carries a 'usage' sub-dict") without trusting an external caller to
    always honor it -- `response.get("usage", {})`'s `{}` default survived
    under both a `None` default and a dropped default because every
    `_ok_response` fixture used elsewhere in this file always includes a
    "usage" key, so the fallback path was never exercised."""
    _require_impl()
    ok_no_usage = {"status": "ok", "content": "final-synthesis-no-usage"}
    query_fn, calls = _make_query_fn([ok_no_usage])
    sleep_fn, _ = _make_sleep_fn()
    policy = RetryPolicy(max_attempts=1, backoff_seconds=())

    response, usage, degraded = _run(
        _synthesize_resilient(STAGE3_QUERY, CHAIRMAN, TIMEOUT, policy, query_fn, sleep_fn)
    )

    assert usage == {}
    assert degraded is False
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# PROPERTY TEST (Hypothesis) -- general law unifying AC7/AC8/AC9/AC10 plus
# the hard non-goal "chairman role is NEVER filled by a different model".
#
# For ANY status sequence and ANY max_attempts, `_synthesize_resilient`'s
# observable behavior is fully determined by a simple state machine:
#   - walk the sequence attempt-by-attempt (capped at max_attempts);
#   - stop (and return) the first time status == "ok";
#   - stop (and raise) the first time status is NOT in retryable_statuses;
#   - otherwise (all attempts consumed, all retryable) stop after
#     max_attempts and raise.
# In every case: query_fn is called exactly `attempts_made` times, every
# call uses `chairman_model` (never a substitute), sleep_fn is awaited
# exactly `attempts_made - 1` times using retry_policy.backoff_seconds[0],
# [1], ... in order, and the raise/return path carries the correct
# attempts/last_status.
#
# This is the "ordering + invariant" law the anti-test-hacking doctrine
# asks property tests to target first: it subsumes AC7-AC10 as special
# cases of one state machine rather than four disconnected examples, and
# it is the strongest available guard against a hard-coded/partial
# implementation (e.g. one that special-cases "3 attempts" or silently
# swaps in a different model string).
# ---------------------------------------------------------------------------

_ALL_STATUSES = ("ok", "timeout", "rate_limited", "error", "auth_error")


@st.composite
def _scenario(draw):
    max_attempts = draw(st.integers(min_value=1, max_value=5))
    statuses = draw(
        st.lists(st.sampled_from(_ALL_STATUSES), min_size=max_attempts, max_size=max_attempts)
    )
    backoff = tuple(
        draw(st.lists(st.floats(min_value=0.1, max_value=99.0, allow_nan=False, allow_infinity=False),
                       min_size=max(max_attempts - 1, 0), max_size=max(max_attempts - 1, 0)))
    )
    return max_attempts, statuses, backoff


def _expected_outcome(statuses, max_attempts, retryable):
    """Pure reference model: returns ("ok", attempts_made, status) or
    ("fail", attempts_made, last_status)."""
    attempts_made = 0
    for status in statuses[:max_attempts]:
        attempts_made += 1
        if status == "ok":
            return "ok", attempts_made, status
        if status not in retryable:
            return "fail", attempts_made, status
    return "fail", attempts_made, statuses[max_attempts - 1]


@settings(max_examples=50, derandomize=True, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(_scenario())
def test_property_attempt_sleep_and_model_invariant_across_any_status_sequence(scenario):
    _require_impl()
    max_attempts, statuses, backoff = scenario

    responses = [
        _ok_response(f"a{i}") if s == "ok" else _fail_response(s)
        for i, s in enumerate(statuses)
    ]
    query_fn, calls = _make_query_fn(responses)
    sleep_fn, sleep_calls = _make_sleep_fn()
    policy = RetryPolicy(max_attempts=max_attempts, backoff_seconds=backoff, retryable_statuses=RETRYABLE)

    kind, attempts_made, last_status = _expected_outcome(statuses, max_attempts, RETRYABLE)

    if kind == "ok":
        response, usage, degraded = _run(
            _synthesize_resilient(STAGE3_QUERY, CHAIRMAN, TIMEOUT, policy, query_fn, sleep_fn)
        )
        assert response["status"] == "ok"
        assert usage == response["usage"]
        assert degraded is False
    else:
        with pytest.raises(ChairmanUnreachableError) as excinfo:
            _run(_synthesize_resilient(STAGE3_QUERY, CHAIRMAN, TIMEOUT, policy, query_fn, sleep_fn))
        err = excinfo.value
        assert err.chairman_model == CHAIRMAN
        assert err.attempts == attempts_made
        assert err.last_status == last_status

    # Invariant (non-goal: no fallback chairman, ever): every call, in
    # every branch of the state machine, used chairman_model.
    assert len(calls) == attempts_made
    assert all(c["model"] == CHAIRMAN for c in calls)

    # Backoff ordering invariant: sleeps happen only between consumed
    # attempts, using backoff_seconds[0..attempts_made-2] in order.
    assert sleep_calls == list(backoff[: max(attempts_made - 1, 0)])


# ---------------------------------------------------------------------------
# 2026-08-28 addition -- _synthesize_resilient must honor a server-supplied
# retry_after over the fixed backoff schedule too, same as
# _attempt_with_retries (see test_resilient_query.py). Both wrap the same
# query_model_with_status-shaped response and must not drift independently.
# ---------------------------------------------------------------------------


def test_synthesize_resilient_honors_retry_after_not_fixed_backoff():
    _require_impl()
    rate_limited = {"status": "rate_limited", "retry_after": 9}
    ok = _ok_response("after-retry-after")
    query_fn, calls = _make_query_fn([rate_limited, ok])
    sleep_fn, sleep_calls = _make_sleep_fn()
    policy = RetryPolicy(max_attempts=3, backoff_seconds=(5.0, 15.0), retryable_statuses=RETRYABLE)

    response, usage, degraded = _run(
        _synthesize_resilient(STAGE3_QUERY, CHAIRMAN, TIMEOUT, policy, query_fn, sleep_fn)
    )

    assert response == ok
    assert degraded is False
    assert len(calls) == 2
    assert sleep_calls == [9.0]  # NOT [5.0] -- must use the server's own signal


def test_synthesize_resilient_falls_back_to_backoff_without_retry_after():
    _require_impl()
    query_fn, calls = _make_query_fn([_fail_response("timeout"), _ok_response("after-backoff")])
    sleep_fn, sleep_calls = _make_sleep_fn()
    policy = RetryPolicy(max_attempts=3, backoff_seconds=(5.0, 15.0), retryable_statuses=RETRYABLE)

    _run(_synthesize_resilient(STAGE3_QUERY, CHAIRMAN, TIMEOUT, policy, query_fn, sleep_fn))

    assert sleep_calls == [5.0]  # unchanged behavior when no retry_after is present
