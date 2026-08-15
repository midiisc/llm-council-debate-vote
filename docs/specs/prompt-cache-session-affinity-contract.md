# Prompt-cache session-affinity activation — Pillar 2, before code

## Grounding

Direct source read this session (`llm_council/cache_context.py`,
`llm_council/gateway/openrouter.py::build_openrouter_payload`):

- The installed package already supports OpenRouter sticky-routing via a
  `session_id` field, injected into every request whenever a `CacheContext`
  has been set via `cache_context.set_cache_context()` — but this repo never
  calls it, so the feature (enabled by default,
  `LLM_COUNCIL_PROMPT_CACHING` env, default `true`) never fires for any real
  call this project makes.
- `set_cache_context()` today is called ONLY from `llm_council/verification/
  api.py` (the package's `verify()`/ADR-034 path) — it is not a general
  drop-in for `consult_council`'s Stage 1-3 debate path. Building the FULL
  feature (Anthropic `cache_control` breakpoints, which need a `segments`
  map matching the package's own internal Stage 1-3 prompt assembly) would
  require reverse-engineering that internal structure — out of scope here,
  a separate future contract if ever pursued.
- `CacheContext.matches()` (verified by direct read) returns `False`
  immediately whenever `segments` is empty (`if not self.segments or ...:
  return False`) — confirmed safe no-op for the Anthropic-specific
  `cache_control`-breakpoint branch of `build_openrouter_payload`.
- `session_id` itself is added to the payload unconditionally whenever
  `cache_ctx is not None`, regardless of whether `segments` matches —
  confirmed by direct read, this is the part that's actually reachable and
  useful today.
- `run_council_with_timeouts()` (`scripts/council_adapter.py`) is the single
  entry point for Stage 1, 2, and 3 — all three go through the installed
  package's internal call path (only Stage 1 has this project's own
  Contract-4 bypass layered on top; Stage 2/3 use the package's functions
  directly). A `ContextVar`-based `CacheContext` set before this function's
  body executes is visible to every downstream async call within it
  (`ContextVar` propagates to child tasks on the same event loop) —
  confirmed by direct read of `_cache_context: ContextVar[...]` in
  `cache_context.py`, no thread/process boundary crossed anywhere in this
  call chain.

## Scope

`scripts/council_adapter.py::run_council_with_timeouts()` only — a single
`session_id`-only `CacheContext` (no `segments`) set for the duration of one
full council run (Stage 1 through Stage 3), cleared afterward. Additive
only; no other file changes.

## Contract

**Acceptance criteria:**

1. Given `run_council_with_timeouts(...)` is called, When it begins
   executing, Then a `CacheContext(session_id=<value>)` with an empty
   `segments` list is set via `set_cache_context()` before any Stage 1/2/3
   call is issued.
2. Given the same, When the function returns (success OR raises), Then
   `clear_cache_context()` is called exactly once (a `try`/`finally`, not a
   bare call after the return line — must fire on exception paths too).
3. Given `session_id` generation, When two separate calls to
   `run_council_with_timeouts()` happen (e.g., two different pipeline runs,
   or a test calling it twice), Then each gets a distinct `session_id` — no
   cross-run affinity bleed (a fixed/hardcoded session_id would incorrectly
   pin unrelated runs' requests to the same provider-routing affinity key).
4. Given a `CacheContext` with `segments=[]` is active, When any Stage 1/2/3
   request payload is built (via `build_openrouter_payload`, the installed
   package's own function — not reimplemented here), Then the payload gains
   a `session_id` key and is otherwise byte-identical to today's payload —
   verified via an integration-style test asserting the actual payload
   dict the package would send, not just that `set_cache_context` was
   called.
5. Given this change, When every existing test in `test_council_adapter.py`
   / `test_council_adapter_resilient_stage1.py` runs, Then all pass
   unmodified — this is purely additive around the existing call, no
   existing behavior changes.

## Non-goals

- No `segments` map / Anthropic `cache_control` breakpoint wiring — that
  needs reverse-engineering the package's internal prompt assembly, a
  separate future contract, not attempted here.
- No change to Stage 0.5's or Stage 2.75/3.75/4/5's own raw-HTTP call paths
  (`scripts/live_adapters.py`) — those already build their own payloads
  directly, not through `build_openrouter_payload`, so `CacheContext` is
  irrelevant to them (a `ContextVar` only affects code that actually reads
  `get_cache_context()`, which only the package's own gateway does).

## Rollout precondition (Pillar 6)

Per the real LLM council's caveat (`docs/upstream-deltas.md`, 2026-08-16):
this is a behavior change with a cost effect, not literally zero-risk, even
though it's additive and low-surface. Before relying on it for a real
decision: run one real dry-run pair (same query, with and without this
change) and capture the actual cost/latency delta in the Cost & Tokens
summary — confirms the saving is real rather than assumed. Not required
before merging the code itself (unlike Contract 5, this doesn't change
request *content*, only routing metadata, so the existing test suite is
sufficient pre-merge verification) — required before citing an expected
cost saving as fact in any future doc.

## Test strategy

Direct implementation, test-first, hand-verified RED → GREEN — matches this
project's established practice for a small, single-call-site, low-risk
change (same tier as Contracts 1-3 in `docs/specs/reasoning-effort-wiring-
contract.md`, not Contract 4/5's blind-TDV treatment, which is reserved for
changes to the Stage 1 resilience call site itself). This change wraps that
call site without altering its resilience behavior, so full blind-TDV is
not warranted.
