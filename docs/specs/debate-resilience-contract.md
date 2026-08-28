# Debate resilience contract (Pillar 2 — spec before code)

Status: ready for blind-TDV. Domain-neutral, folder-scoped in the same sense
as `custom-scripts-contracts.md`'s three contracts (no filesystem writes at
all in this case — this module is pure async orchestration over an
injected `query_fn`).

Grounding: `docs/upstream-deltas.md`, "Debate resilience: retry/backup
design grounding (2026-08-12)". Council-composition decision:
`docs/pipeline-architecture-spec.md` §2, "Superseding decision
(2026-08-12)".

## Problem this closes

Today, `council_adapter.py`'s Stage 1 calls the package's
`query_models_parallel`, which gives every model exactly one attempt and
returns `None` for any model that doesn't succeed — no retry, no
distinction between "timed out once, would likely succeed on retry" and
"genuinely unreachable this session", and no way to substitute a backup
model to keep the live count at the configured minimum. This contract
specifies `scripts/resilient_query.py`, a drop-in replacement call that
adds retry-with-backoff and backup-model substitution while staying a pure,
dependency-injected, fully testable unit (no live network calls in the
module itself — matches the design note at the top of
`custom-scripts-contracts.md`).

## Contract — `resilient_query.py`

**Objective:** given primary models, a backup pool, and a retry policy,
resolve as many live responses as possible: retry a model on a transient
failure, substitute an unused backup only once a model is confirmed
genuinely unreachable, and surface (never hide) any shortfall against a
configured minimum live-model count.

**Signature:**
```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

QueryFn = Callable[[str, list[dict], float], Awaitable[dict]]
# matches llm_council.openrouter.query_model_with_status(model, messages, timeout)
# -> {"status": "ok"|"timeout"|"rate_limited"|"auth_error"|"error", ...}

SleepFn = Callable[[float], Awaitable[None]]  # default: asyncio.sleep

@dataclass
class RetryPolicy:
    max_attempts: int = 3                                  # includes the first try
    backoff_seconds: tuple[float, ...] = (5.0, 15.0)        # len must be max_attempts - 1
    retryable_statuses: frozenset[str] = field(
        default_factory=lambda: frozenset({"timeout", "rate_limited", "error"})
    )
    # Any status NOT in retryable_statuses (e.g. "auth_error") is terminal:
    # stop retrying that model immediately, no separate "terminal" list needed.

@dataclass
class ModelAttempt:
    model: str
    attempt_number: int   # 1-indexed
    status: str

@dataclass
class SubstitutionEvent:
    slot_model: str    # the primary model this backup replaced
    backup_model: str
    reason: str         # e.g. "unreachable after 3 attempts (last status=timeout)"

@dataclass
class ResilientQueryResult:
    responses: dict[str, dict]              # model -> query_fn's own "ok" response dict
    attempts: list[ModelAttempt]             # every attempt, every model, in call order
    substitutions: list[SubstitutionEvent]
    unreachable_models: list[str]            # every model actually attempted that never got "ok"
    shortfall_warning: Optional[str]         # None iff len(responses) >= minimum_council_size

async def query_models_resilient(
    primary_models: list[str],
    backup_models: list[str],
    messages: list[dict],
    timeout: float,
    query_fn: QueryFn,
    retry_policy: RetryPolicy = RetryPolicy(),
    minimum_council_size: int = 4,
    sleep_fn: SleepFn = asyncio.sleep,
) -> ResilientQueryResult:
    ...
```

**Acceptance criteria (Given/When/Then):**

1. Given every primary model returns `status="ok"` on its first attempt,
   When `query_models_resilient` runs, Then `responses` contains exactly
   the primary models keyed to their `query_fn` response, `attempts` has
   exactly one entry per primary model (`attempt_number=1`), `substitutions`
   is empty, `unreachable_models` is empty, and `shortfall_warning` is
   `None` whenever `len(primary_models) >= minimum_council_size`.

2. Given a primary model returns a retryable status (e.g. `"timeout"`) on
   attempt 1 and `"ok"` on attempt 2, When it runs, Then that model's final
   entry in `responses` is the successful attempt's response, `attempts`
   records both tries for that model in order, `sleep_fn` is awaited
   exactly once with `retry_policy.backoff_seconds[0]` before the second
   attempt, and no backup is ever queried for that slot.

3. Given a primary model returns a status not in `retry_policy
   .retryable_statuses` (e.g. `"auth_error"`) on its first attempt, When it
   runs, Then exactly one `ModelAttempt` is recorded for that model (no
   retry attempted), the model is not present in `responses`, and a
   `SubstitutionEvent` is recorded pairing that model with the first unused
   entry of `backup_models`.

4. Given a primary model returns a retryable status on every attempt up to
   `retry_policy.max_attempts`, When it runs, Then `attempts` records
   exactly `max_attempts` tries for that model, `sleep_fn` is awaited
   exactly `max_attempts - 1` times with `retry_policy.backoff_seconds[0]`,
   `[1]`, ... in order, the model is not present in `responses`, and a
   `SubstitutionEvent` is recorded for it.

5. Given a primary model is unreachable and its assigned backup is also
   unreachable (exhausts the same retry policy), When it runs, Then the
   next unused entry of `backup_models` (if any) is tried for that same
   slot — with its own full retry sequence — and a second
   `SubstitutionEvent` is recorded for that slot (`slot_model` stays the
   original primary's name, `backup_model` updates to the new candidate).
   If no backup remains unused, the slot stays empty.

6. Given `backup_models` has already supplied a candidate to fill one slot,
   When a different primary model also needs a backup, Then that
   already-consumed backup model is never attempted again for a second slot
   — each entry in `backup_models` is used for at most one slot per call.

7. Given the final count of successful responses (`len(responses)`) is
   strictly less than `minimum_council_size`, When the result is built,
   Then `shortfall_warning` is a non-`None`, human-readable string that
   names the exact final live count, `minimum_council_size`, and every
   model in `unreachable_models`. Given the final count is
   `>= minimum_council_size`, `shortfall_warning` is `None`.

8. Given a model was never attempted (e.g. an unused backup that no slot
   needed), When `unreachable_models` is built, Then it contains only
   models that were actually attempted at least once and never reached
   `status="ok"` — never a backup that sat unused.

9. Given any call to `query_fn` (primary or backup, any attempt), When it's
   invoked, Then it always receives the exact same `messages` object and
   `timeout` value passed into `query_models_resilient` — never a mutated
   copy, never a different timeout for a backup vs. a primary.

10. Given two or more primary models each need retries, When they run,
    Then each model's retry-and-backup resolution is independent — one
    model's attempt count, statuses, and backoff sleeps are never affected
    by another model's outcome (verifiable via each model's own `attempts`
    entries matching what a single-model scenario with the same fake
    `query_fn` would produce). **Not independently tested**: whether the
    implementation resolves models concurrently (e.g. `asyncio.gather`) or
    sequentially is unspecified — both satisfy every AC above, since none
    of them assert on cross-model timing/interleaving. Concurrent
    resolution is the intended design (matches `query_models_parallel`'s
    own "parallel" framing) but is a performance property, not a
    correctness one, and is called out here as a documented non-goal of
    the test suite rather than left as an unstated assumption.

**Non-goals:** no network calls, no config file reading (the caller reads
`llm_council.yaml`'s `debate_resilience:` block and passes `RetryPolicy`/
`backup_models`/`minimum_council_size` in — see
`docs/upstream-deltas.md`'s config-placement rule for why this module never
does its own `yaml.safe_load`), no cost/usage tracking (the caller's
existing usage-aggregation in `council_adapter.py` handles that from
`responses`' own `usage` sub-dicts, unchanged), no change to
`query_model_with_status`'s own retry/timeout semantics — `timeout` here is
the same per-attempt value already threaded through `council_adapter.py`.

## Amendment 2026-08-28 — honor `retry_after` over the fixed backoff schedule

Found via a separate project's live end-to-end canary run + panel-debate that traced this
repo's own `openrouter.py`: `query_model_with_status` already parses a `rate_limited` response's
`Retry-After` header into `response["retry_after"]`, but `_attempt_with_retries` (this module)
and `_synthesize_resilient` (`council_adapter.py`, Stage 3's chairman-only retry) both ignored it
completely, always sleeping the fixed `backoff_seconds[attempt_number - 1]` regardless of what
the server actually signaled.

Fixed by adding `RetryPolicy.max_retry_after_seconds` (default 30.0 — the same ceiling
`external-llm-research/scripts/dispatch-telegram.sh` in a different project uses for the same
reason: a real `retry_after`/`Retry-After` value has been observed in the thousands of seconds
during abuse, and a fixed ceiling protects this module's own bounded-retry design goal) and a
new shared `resolve_retry_wait_seconds(response, attempt_number, retry_policy)` function, used by
both `_attempt_with_retries` and `_synthesize_resilient` instead of reading
`backoff_seconds[attempt_number - 1]` directly — one implementation, not two independently-
drifting copies of the same "should this be server-signaled or fixed?" decision.

Only honored when `response.get("status") == "rate_limited"` — a stray `retry_after` key on any
other status is never trusted, since `query_model_with_status` only ever sets it on that one
status. Verified by 8 new tests (`test_resilient_query.py`, `test_council_adapter_synthesize_
resilient_stage3.py`) plus a full re-run of the existing suite: 901 passed (893 baseline + 8 new),
zero regressions. Not a real-money change — no live API call was made to verify this, only the
existing hermetic dependency-injected test harness (`query_fn`/`sleep_fn` fakes, no network).
