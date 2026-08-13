# Wall-clock/cost budget redesign (Pillar 2 — spec before code)

Status: ready for blind-TDV. Grounding:
`docs/architecture-stress-test-2026-08-13.md`, Critical #3 ("Wall-clock
ceiling is undersized against the retry+backup engine's own worst case"),
Critical #5 ("Stage 0.5 grounding-pass spend is never tracked anywhere"),
High ("The 'always-on' wall-clock ceiling cannot actually preempt Stage 0.5
or Stage 2.75 network calls in production"), High ("Stage 0.5 evidence
fetching is fully sequential, no concurrency, no cap").

## Problem this closes

A single Stage-1 slot's worst case (per-attempt timeout × retries, then
repeated across every backup) is currently **unbounded relative to the
overall wall-clock ceiling** — up to ~3680s against a 1200s default. Under
exactly the sustained-degradation scenario the resilience layer exists to
survive, it can consume the *entire* budget by itself, discarding
Stages 1.5-4's own remaining time. Separately, Stage 0.5's grounding-pass
spend is completely untracked (`--max-cost-usd` has zero effect on it) and
its HTTP calls are synchronous, so the wall-clock ceiling can't even
preempt them if they hang.

**Deliberately out of scope for this contract** (flagged, not silently
dropped): dynamically reallocating Stage 2/Stage 3's own 300s timeouts
against however much budget Stage 1 actually consumed. This contract gives
Stage 1 a hard, independent deadline so it can never alone exhaust the
ceiling — full dynamic cross-stage budget rebalancing is a larger, separate
redesign, not needed to close the two Critical findings.

## Contract 1 — Stage 1 hard deadline (closes Critical #3)

**File**: `scripts/resilient_query.py`.

**Objective**: give `query_models_resilient` an optional absolute deadline
(`time.monotonic()`-based). Once passed, stop issuing further retry/backup
attempts for any still-unresolved slot and return immediately with whatever
succeeded — the same "proceed degraded, don't hard-fail" behavior already
used when backups run out, now also triggered by time.

**Signature change:**
```python
async def query_models_resilient(
    primary_models: list[str],
    backup_models: list[str],
    messages: list[dict],
    timeout: float,
    query_fn: QueryFn,
    retry_policy: RetryPolicy = RetryPolicy(),
    minimum_council_size: int = 4,
    sleep_fn: SleepFn = asyncio.sleep,
    deadline: Optional[float] = None,  # NEW: absolute time.monotonic() timestamp; None = no deadline (today's behavior, backward compatible)
    time_fn: Callable[[], float] = time.monotonic,  # NEW: injectable for tests
) -> ResilientQueryResult:
    ...
```

**Acceptance criteria:**
1. Given `deadline=None` (default), When it runs, Then behavior is
   byte-identical to today — confirms this is strictly additive.
2. Given a `deadline` that has already passed by the time a model's first
   attempt would start, When it runs, Then that model gets zero attempts
   (not even one) and is immediately treated as unreachable for backup
   purposes — no backoff sleep occurs after the deadline has passed.
3. Given a `deadline` that passes *between* two attempts of the same model
   (e.g., after attempt 1's backoff sleep), When the loop checks before
   attempt 2, Then attempt 2 is skipped, the model is marked unreachable,
   and (per existing behavior) a `SubstitutionEvent` is attempted for the
   next unused backup — but the backup itself is also subject to the same
   deadline check (AC2) before its own first attempt.
4. Given the deadline passes while some primary/backup slots are still
   fully unresolved, When the function returns, Then `responses` contains
   whatever succeeded before the deadline, `unreachable_models` lists
   everything that didn't get a chance to fully resolve, and
   `shortfall_warning` is set exactly as today's "ran out of backups" case
   already sets it if the final count is below `minimum_council_size` — a
   deadline-triggered shortfall must be exactly as loud as a
   backups-exhausted shortfall, never quieter.
5. Given `deadline` is set generously (never actually reached), When it
   runs, Then behavior is identical to `deadline=None` — confirms the
   deadline check only ever *removes* attempts, never adds delay or changes
   success-path behavior.

**File**: `scripts/council_adapter.py`, `run_council_with_timeouts`.

**Objective**: compute and pass a Stage-1 deadline sized as a fraction of
the overall wall-clock budget, so Stage 1 can never alone exhaust it.

**Acceptance criteria:**
6. Given `run_council_with_timeouts` gains a new optional
   `stage1_deadline_fraction: float = 0.5` parameter (of whatever the
   caller's own overall wall-clock budget is — threaded in as a new
   parameter, e.g. `overall_wall_clock_seconds: Optional[float] = None`),
   When Stage 1 starts, Then it computes
   `deadline = time.monotonic() + overall_wall_clock_seconds * stage1_deadline_fraction`
   and passes it to `query_models_resilient` — if
   `overall_wall_clock_seconds` is `None` (e.g. `debate.py`'s call site,
   which has its own independent ceiling per Contract 2 below), no deadline
   is computed (`None` passed through, backward compatible per AC1).
7. Given `pipeline_runner.py`'s `council_fn` closure, When it calls
   `run_council_with_timeouts`, Then it passes
   `overall_wall_clock_seconds=config.max_wall_clock_seconds` — confirms
   the real, configured ceiling actually reaches Stage 1's deadline
   calculation, not a hardcoded default.

## Contract 2 — Stage 0.5 cost tracking + non-blocking + concurrency (closes Critical #5, related High findings)

**File**: `scripts/live_adapters.py`.

**Objective**: (a) track and return real cost from `real_fetch_evidence`,
(b) make its HTTP calls non-blocking so `asyncio.wait_for` can actually
preempt them, (c) fetch claims concurrently instead of sequentially, (d)
cap total claims fetched with a loud, visible truncation warning (never a
silent cap, per this project's own no-silent-caps rule).

**Signature changes:**
```python
async def _post_chat_completion_async(
    model: str, prompt: str, max_tokens: int = 2000, max_retries: int = MAX_RETRIES,
) -> dict[str, Any]:
    """Runs the existing synchronous _post_chat_completion in a worker
    thread (asyncio.to_thread) so the event loop stays responsive to
    asyncio.wait_for cancellation during the call - no migration off
    urllib, no new HTTP client, minimal-diff fix for the specific
    'blocks the event loop' finding."""
    ...

async def real_fetch_evidence(
    claims: list[Claim],
    max_claims: int = 50,  # NEW - a sensible default cap, tunable
    max_concurrency: int = 5,  # NEW
) -> tuple[dict[str, list[Evidence]], float, bool]:
    # Returns (evidence, total_cost_usd, truncated) - truncated=True iff
    # len(claims) > max_claims, so the caller can surface a loud warning.
    ...
```

**Acceptance criteria:**
1. Given `real_fetch_evidence` is called, When each claim's
   `_post_chat_completion_async` call returns, Then its
   `data.get("usage", {}).get("cost") or 0.0` is accumulated into a running
   total, and the function returns `(evidence, total_cost_usd, truncated)`
   — never discarding the cost figure.
2. Given `len(claims) > max_claims`, When it runs, Then only the first
   `max_claims` claims are fetched, `truncated=True` is returned, and the
   caller (`pipeline_runner.py`) must surface this as a visible
   warning/debug_log line naming exactly how many claims were dropped —
   never a silent truncation.
3. Given multiple claims, When they're fetched, Then calls happen
   concurrently up to `max_concurrency` at a time (e.g.
   `asyncio.Semaphore(max_concurrency)` + `asyncio.gather`), not one at a
   time in a `for` loop — verify via a test with a fake
   `_post_chat_completion_async` that records call *overlap* (e.g. via
   `asyncio.Event`/timing) and asserts more than one call is in flight
   simultaneously.
4. Given `_post_chat_completion_async` wraps the existing synchronous
   function via `asyncio.to_thread`, When a test simulates a slow call
   (e.g. `time.sleep` inside a faked sync function) running under
   `asyncio.wait_for` with a short outer timeout, Then the outer
   `asyncio.wait_for` actually raises `TimeoutError` at approximately the
   configured timeout (not blocked until the slow call finishes) — this is
   the direct regression test for the "can't preempt" finding; the
   existing synchronous `_post_chat_completion` function itself is
   UNCHANGED (still used directly by `real_query_model` for Stage 2.75/
   Stage 4 — those call sites get the same `asyncio.to_thread` treatment
   too, see AC5).
5. Given `real_query_model` (used by Stage 2.75/Stage 4, already returns
   `(text, cost_usd)` correctly) also calls the synchronous
   `_post_chat_completion`, When it's updated to route through the new
   `_post_chat_completion_async` wrapper too, Then its existing behavior
   (return shape, cost extraction) is unchanged — confirms this is a
   non-breaking internal change, not a new contract for that function.

**File**: `scripts/pipeline_runner.py`, the Stage 0.5 block in
`_run_stages()`.

**Acceptance criteria:**
6. Given `real_fetch_evidence` now returns `(evidence, cost, truncated)`,
   When Stage 0.5 completes, Then its cost is added to `cost_so_far`
   immediately (using the incremental-update mechanism already landed by
   the prior hardening contract this session) — confirms `--max-cost-usd`
   now actually bounds Stage 0.5 spend for the first time.
7. Given `truncated=True`, When Stage 0.5 completes, Then a debug_log line
   explicitly states how many claims were dropped (e.g. "Stage 0.5: N of M
   claims fetched, (M-N) dropped by max_claims cap") — never silent.
8. Given `config.max_cost_usd` is already exceeded by an earlier partial
   accumulation (unlikely for Stage 0.5 specifically since it's the first
   stage, but the check must exist for defense-in-depth / future stage
   reordering), When Stage 0.5 would start, Then it's skipped with the same
   `debug_log` skip-reason convention Stage 2.75/Stage 4 already use.

## Contract 3 — `debate.py` deadline parity (small, uses Contract 1's new parameter)

**File**: `scripts/debate.py`.

**Objective**: `debate.py`'s own `--max-wall-clock-seconds` flag (landed
earlier this session) should also feed Stage 1's new deadline mechanism,
not just wrap the whole call in `asyncio.wait_for` from the outside.

**Acceptance criteria:**
1. Given `debate.py` calls `run_council_with_timeouts`, When it does so,
   Then it passes `overall_wall_clock_seconds=args.max_wall_clock_seconds`
   — Stage 1 gets the same internal deadline protection
   `pipeline_runner.py` gets, not just the outer `asyncio.wait_for` safety
   net.

## Non-goals (all contracts)

No change to Stage 2/Stage 3's own independent 300s timeouts — they are
not resized based on how much budget Stage 1 actually consumed (flagged as
a known residual, see "Problem this closes"). No migration of
`live_adapters.py` off `urllib` onto `httpx` (the two-divergent-HTTP-clients
finding stays open, tracked separately, Low severity). No change to
`RetryPolicy`'s own per-attempt `backoff_seconds`/`retryable_statuses`
semantics — this contract only adds an outer deadline check, not new retry
logic.
