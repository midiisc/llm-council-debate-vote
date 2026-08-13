"""Retry-with-backoff and backup-model substitution for council model
queries. Drop-in hardening for any call site that currently uses
`llm_council.gateway_adapter.query_models_parallel` (one attempt per model,
`None` on failure, no substitution) - see
`docs/specs/debate-resilience-contract.md`.

Pure, dependency-injected orchestration: no network calls, no config file
reads, no cost/usage accounting. The caller supplies `query_fn` (matching
`llm_council.openrouter.query_model_with_status(model, messages, timeout)`),
reads `llm_council.yaml`'s `debate_resilience:` block itself, and builds the
`RetryPolicy`/`backup_models`/`minimum_council_size` it passes in.

Contract: docs/specs/debate-resilience-contract.md.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

TimeFn = Callable[[], float]  # default: time.monotonic

QueryFn = Callable[[str, list[dict], float], Awaitable[dict]]
# matches llm_council.openrouter.query_model_with_status(model, messages, timeout)
# -> {"status": "ok"|"timeout"|"rate_limited"|"auth_error"|"error", ...}

SleepFn = Callable[[float], Awaitable[None]]  # default: asyncio.sleep


@dataclass
class RetryPolicy:
    max_attempts: int = 3  # includes the first try
    backoff_seconds: tuple[float, ...] = (5.0, 15.0)
    retryable_statuses: frozenset[str] = field(
        default_factory=lambda: frozenset({"timeout", "rate_limited", "error"})
    )
    # Any status NOT in retryable_statuses (e.g. "auth_error") is terminal:
    # stop retrying that model immediately, no separate "terminal" list needed.

    def __post_init__(self) -> None:
        # A misconfigured llm_council.yaml (e.g. max_attempts raised without
        # adding a matching backoff_seconds entry) must fail loudly and
        # immediately here - not with a mid-debate IndexError deep inside
        # _attempt_with_retries's retry loop
        # (architecture-stress-test-2026-08-13.md, High finding).
        needed = self.max_attempts - 1
        if len(self.backoff_seconds) < needed:
            raise ValueError(
                f"RetryPolicy.backoff_seconds has {len(self.backoff_seconds)} "
                f"entries but max_attempts={self.max_attempts} needs at least "
                f"{needed} (one between each pair of attempts)"
            )


@dataclass
class ModelAttempt:
    model: str
    attempt_number: int  # 1-indexed
    status: str


@dataclass
class SubstitutionEvent:
    slot_model: str  # the primary model this backup replaced
    backup_model: str
    reason: str  # e.g. "unreachable after 3 attempts (last status=timeout)"


@dataclass
class ResilientQueryResult:
    responses: dict[str, dict]  # model -> query_fn's own "ok" response dict
    attempts: list[ModelAttempt]  # every attempt, every model, in call order
    substitutions: list[SubstitutionEvent]
    unreachable_models: list[str]  # every model actually attempted that never got "ok"
    shortfall_warning: Optional[str]  # None iff len(responses) >= minimum_council_size


async def _attempt_with_retries(
    candidate: str,
    messages: list[dict],
    timeout: float,
    query_fn: QueryFn,
    retry_policy: RetryPolicy,
    sleep_fn: SleepFn,
    deadline: Optional[float] = None,
    time_fn: TimeFn = time.monotonic,
) -> tuple[list[ModelAttempt], Optional[dict]]:
    """Run `candidate` through its own retry sequence (independent of any
    other candidate). Returns every ModelAttempt recorded plus the
    successful response dict, or None if it never reached status="ok".

    `deadline` (an absolute `time_fn()`-based cutoff) stops the sequence
    before any attempt whose start would be at or past it - including the
    very first attempt, and including one that would start mid-sequence
    after a backoff sleep (docs/specs/wallclock-cost-budget-contract.md,
    Contract 1, AC2/AC3). `deadline=None` (default) never stops anything -
    identical to the pre-deadline behavior.
    """
    attempts: list[ModelAttempt] = []
    for attempt_number in range(1, retry_policy.max_attempts + 1):
        if deadline is not None and time_fn() >= deadline:
            break
        response = await query_fn(candidate, messages, timeout)
        status = response.get("status")
        attempts.append(ModelAttempt(model=candidate, attempt_number=attempt_number, status=status))

        if status == "ok":
            return attempts, response

        if status not in retry_policy.retryable_statuses:
            break  # terminal status: stop retrying this candidate immediately

        if attempt_number < retry_policy.max_attempts:
            await sleep_fn(retry_policy.backoff_seconds[attempt_number - 1])

    return attempts, None


async def _resolve_slot(
    primary_model: str,
    backup_queue: list[str],
    messages: list[dict],
    timeout: float,
    query_fn: QueryFn,
    retry_policy: RetryPolicy,
    sleep_fn: SleepFn,
    deadline: Optional[float] = None,
    time_fn: TimeFn = time.monotonic,
) -> tuple[Optional[str], Optional[dict], list[ModelAttempt], list[SubstitutionEvent], list[str]]:
    """Resolve one primary model's slot: the primary itself, then as many
    unused backups (in `backup_queue` order) as needed until one succeeds or
    the backup pool is exhausted. Returns (winning_model_or_None,
    response_or_None, attempts, substitutions, unreachable_candidates).

    `deadline`: checked before each new candidate (primary or backup) is
    even tried - if already past, that candidate gets zero attempts and no
    backup is consumed for it (docs/specs/wallclock-cost-budget-contract.md,
    Contract 1, AC2/AC4) - this proceeds-degraded-not-hard-fail behavior
    mirrors what already happens when the backup pool is simply exhausted.
    """
    attempts: list[ModelAttempt] = []
    substitutions: list[SubstitutionEvent] = []
    unreachable: list[str] = []

    candidate = primary_model
    while True:
        if deadline is not None and time_fn() >= deadline:
            unreachable.append(candidate)
            return None, None, attempts, substitutions, unreachable

        candidate_attempts, response = await _attempt_with_retries(
            candidate, messages, timeout, query_fn, retry_policy, sleep_fn,
            deadline=deadline, time_fn=time_fn,
        )
        attempts.extend(candidate_attempts)

        if response is not None:
            return candidate, response, attempts, substitutions, unreachable

        unreachable.append(candidate)
        last_status = candidate_attempts[-1].status if candidate_attempts else None

        # backup_queue.pop(0) is a synchronous, non-yielding operation, so
        # concurrent slots (see query_models_resilient's asyncio.gather)
        # can never race on the same backup entry - each pop happens
        # atomically between the last and next actual suspension point.
        if not backup_queue:
            return None, None, attempts, substitutions, unreachable

        next_backup = backup_queue.pop(0)
        substitutions.append(
            SubstitutionEvent(
                slot_model=primary_model,
                backup_model=next_backup,
                reason=f"unreachable after {len(candidate_attempts)} attempts (last status={last_status})",
            )
        )
        candidate = next_backup


async def query_models_resilient(
    primary_models: list[str],
    backup_models: list[str],
    messages: list[dict],
    timeout: float,
    query_fn: QueryFn,
    retry_policy: RetryPolicy = RetryPolicy(),
    minimum_council_size: int = 4,
    sleep_fn: SleepFn = asyncio.sleep,
    deadline: Optional[float] = None,
    time_fn: TimeFn = time.monotonic,
) -> ResilientQueryResult:
    # Shared, mutable queue: each backup is consumed by at most one slot
    # across the whole call. Independent per-slot resolution runs
    # concurrently (matches query_models_parallel's "parallel" framing);
    # queue consumption itself never spans an await, so it stays race-free.
    # A backup entry that duplicates a primary model is filtered out here -
    # substituting it for a DIFFERENT slot adds no real resilience (if that
    # model is down, it's down for both roles) and would otherwise risk a
    # silent responses-dict collision keyed by bare model name
    # (architecture-stress-test-2026-08-13.md, Low finding).
    primary_set = set(primary_models)
    backup_queue = [m for m in backup_models if m not in primary_set]

    slot_results = await asyncio.gather(
        *(
            _resolve_slot(
                primary, backup_queue, messages, timeout, query_fn, retry_policy, sleep_fn,
                deadline=deadline, time_fn=time_fn,
            )
            for primary in primary_models
        )
    )

    responses: dict[str, dict] = {}
    attempts: list[ModelAttempt] = []
    substitutions: list[SubstitutionEvent] = []
    unreachable_models: list[str] = []

    for winner, response, slot_attempts, slot_substitutions, slot_unreachable in slot_results:
        attempts.extend(slot_attempts)
        substitutions.extend(slot_substitutions)
        unreachable_models.extend(slot_unreachable)
        if winner is not None:
            responses[winner] = response

    shortfall_warning: Optional[str] = None
    if len(responses) < minimum_council_size:
        shortfall_warning = (
            f"Only {len(responses)} of the required minimum {minimum_council_size} "
            f"council models responded; unreachable: {', '.join(unreachable_models)}"
        )

    return ResilientQueryResult(
        responses=responses,
        attempts=attempts,
        substitutions=substitutions,
        unreachable_models=unreachable_models,
        shortfall_warning=shortfall_warning,
    )
