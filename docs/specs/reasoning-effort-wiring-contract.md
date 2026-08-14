# Reasoning-effort wiring — Stage 2.75 / Stage 3.75 / Stage 4 (Pillar 2, before code)

## Grounding this contract relies on

- `docs/agent-model-reasoning-config.md` §3 (existing session grounding, live
  OpenRouter doc fetches): valid `effort` values are `none, minimal, low,
  medium, high, xhigh, max`; the nested `reasoning: {effort, max_tokens,
  exclude}` object treats `effort`/`max_tokens` as **mutually exclusive**,
  and per-provider support for that nested object differs (Anthropic:
  `max_tokens` only; OpenAI/Gemini 3: `effort`).
- **New this pass**, live-fetched from `openrouter.ai/docs/api-reference/
  parameters`: OpenRouter also exposes a **separate, simpler top-level
  request field** `reasoning_effort: "none"|"minimal"|"low"|"medium"|
  "high"|"xhigh"` ("OpenAI-style reasoning effort setting"), distinct from
  the nested `reasoning` object.
- **New this pass**, live catalog fetch (`/api/v1/models`,
  `supported_parameters` per entry): all 4 models this project actually
  calls in these three stages — `anthropic/claude-opus-4.8`,
  `openai/gpt-5.5`, `google/gemini-3.6-flash`, `z-ai/glm-5.2` — list
  **both** `reasoning` and `reasoning_effort` in their live
  `supported_parameters`. OpenRouter advertising a parameter as supported
  for a specific model is the most authoritative per-model signal available
  short of an actual call.
- The installed package's own capability check
  (`llm_council.metadata.get_provider().supports_reasoning(model)`) was
  checked and found **stale/wrong** for this exact question — it reports
  `False` for `openai/gpt-5.5` and `google/gemini-3.6-flash`, contradicted
  by the live catalog fetch above. Not used for this reason.

**Decision**: use the top-level `reasoning_effort` field, not the nested
`reasoning` object. It sidesteps the mutual-exclusivity/per-provider
max_tokens-mapping complexity entirely (no `max_tokens` value to invent),
and is directly confirmed supported per-model by the live catalog for every
model this project's real-money paths actually call.

## Scope

Per `docs/agent-model-reasoning-config.md` §3's "Reachable today" column,
Stage 2.75 (revision), Stage 3.75 (critique), and Stage 4 (completeness) go
through this project's own raw-HTTP call path
(`scripts/live_adapters.py::real_query_model`) where we control the request
body directly — Contracts 1-3 below.

**Stage 1 (independent draft) is now in scope, added 2026-08-14 as Contract
4** — see that section for why the originally-assumed fix (swap to
`llm_council.openrouter.query_model_with_status`'s `reasoning_params`) turned
out to be unsafe and what replaces it.

Stage 2 (`council_stages.stage2_collect_rankings`) and Stage 3
(`stage3_synthesize_final`) stay explicitly out of scope — no
`reasoning_params`/`reasoning_effort` kwarg exists on either function at all,
requiring either a full reimplementation of the stage outside the package
(same scope as Stage 1's fix, not attempted here) or an upstream fix. Already
logged as a Pillar-5 follow-up item; unchanged by this contract.

Target efforts, unchanged from the already-decided table:
- Stage 1 draft: **high** (opus-4.8, gpt-5.5), **medium** (gemini-3.6-flash,
  glm-5.2) — briefly reverted to medium-for-all after a dry-run
  misinterpretation, restored same day; see "Rollout precondition" in
  Contract 4 below and `docs/pipeline-architecture-spec.md` §9.
- Stage 2.75 revision: **high**
- Stage 3.75 critique: **high**
- Stage 4 completeness check: **low**
- Stage 5 reasoning-graph extraction: not in the original table (a stage
  added after it) — left at no override, out of scope for this contract.

## Contract 1 — `scripts/live_adapters.py`

**Acceptance criteria:**
1. Given `_post_chat_completion(model, prompt, ..., reasoning_effort:
   Optional[str] = None)`, When `reasoning_effort` is `None` (the default),
   Then the request body is byte-identical to today's — no `reasoning_effort`
   key present. Existing callers/tests need zero changes.
2. Given `reasoning_effort` is a non-`None` string, When the request body is
   built, Then it includes a top-level `"reasoning_effort": <value>` key,
   unchanged/unvalidated (no enum check in this layer — an invalid value is
   OpenRouter's 4xx to report, not ours to silently coerce).
3. Given `real_query_model(model, prompt, reasoning_effort=None)` (the
   existing 2-positional-arg call shape used everywhere today), When called,
   Then behavior and return shape are unchanged from before this contract.
4. Given `real_query_model(model, prompt, reasoning_effort="high")`, When
   called, Then `reasoning_effort` reaches `_post_chat_completion` unchanged.

## Contract 2 — wired into `scripts/pipeline_runner.py`, additive only

**Acceptance criteria:**
5. Given a new type alias `ReasoningQueryModelFn = Callable[[str, str, str],
   Awaitable[tuple[str, float]]]` (`(model, prompt, effort) -> (text,
   cost)`) and a new `run_pipeline(..., query_model_with_effort:
   Optional[ReasoningQueryModelFn] = None)` parameter, When
   `query_model_with_effort` is `None` (the default — every existing
   `run_pipeline` call site, every existing test), Then Stage 2.75/3.75/4
   call `query_model` exactly as before this contract — zero behavior
   change, zero existing test needs updating.
6. Given `query_model_with_effort` is provided, When `_run_stages()` reaches
   Stage 2.75 revision, Then it calls `query_model_with_effort(model,
   prompt, "high")` instead of `query_model(model, prompt)` for every
   revision-round query.
7. Given the same, When Stage 3.75 critique runs, Then it calls
   `query_model_with_effort(model, prompt, "high")`.
8. Given the same, When Stage 4 completeness check runs, Then it calls
   `query_model_with_effort(model, prompt, "low")`.
9. Stage 5's reasoning-graph extraction is explicitly NOT changed by this
   contract (out of scope, see above) — it keeps calling the plain
   `query_model`.

## Contract 3 — wired into `main()`

**Acceptance criteria:**
10. Given `main()`, When it builds the `run_pipeline(...)` call, Then it
    passes a `query_model_with_effort` closure that calls
    `real_query_model(model, prompt, reasoning_effort=effort)` — so a real
    CLI invocation actually sends the effort-tagged requests end to end.

## Contract 4 — Stage 1 (independent draft), added 2026-08-14

Grew out of a user request for "deep research mode" (rejected — see below)
and "reasoning tier as primary" (found to already be the design intent for
every generation-shaped stage; Stage 1 was simply the one unwired gap). Full
panel deliberation: see `docs/upstream-deltas.md`'s 2026-08-14 "deep research
+ reasoning-tier-primary" entry. Deep-research-as-a-distinct-model and
web-search expansion to Stage 1/3 were both rejected there (knowledge-
divergence and prompt-injection/egress concerns) — this contract is scoped
to `reasoning_effort` only, nothing else.

### Why the obvious fix doesn't work

`docs/agent-model-reasoning-config.md` originally proposed swapping
`resilient_query.py`'s `query_fn` from `llm_council.gateway_adapter.
query_model_with_status` (no `reasoning_params` kwarg) to `llm_council.
openrouter.query_model_with_status` (does accept `reasoning_params`). Direct
read of the installed package (`llm_council/gateway/openrouter.py::
build_openrouter_payload`) found this unsafe for this project's actual
roster:

1. **Anthropic mutual-exclusivity violation.** Whenever `reasoning_params` is
   set, the payload always includes `{"effort", "max_tokens", "exclude"}`
   together (`payload["reasoning"] = {...}` unconditionally). This violates
   the already-documented (§3 above) Anthropic rule that `effort` and
   `max_tokens` are mutually exclusive — hits the opus-4.8 seat specifically,
   the one that most needs `effort="high"` here.
2. **Silent no-op for 2 of 4 seats.** Injection is gated on `provider.
   supports_reasoning(model)`, the same capability check already found stale
   elsewhere in this project (§3's 2026-08-13 correction) — it returns
   `False` for `openai/gpt-5.5` and `google/gemini-3.6-flash`, so
   `reasoning_params` would be silently dropped for those two seats, no
   error, no warning.

Both are package-internal behaviors, not configuration this project
controls, so wrapping/monkeypatching them is out (matches this project's
existing preference for "wrap the real function" over fragile internals
coupling, see `docs/upstream-deltas.md`'s 2026-08-14 Contract A entry).

### The actual fix

A new function, owned by this project, in `scripts/live_adapters.py`:

```python
async def query_model_with_status_and_effort(
    model: str,
    messages: list[dict],
    timeout: float = 120.0,
    reasoning_effort: Optional[str] = None,
) -> dict[str, Any]:
```

- Same top-level `reasoning_effort` field as Contracts 1-3 (never the nested
  `reasoning` object) — sidesteps both landmines above entirely, since it
  never goes through `build_openrouter_payload`'s reasoning injection path.
- Matches `resilient_query.py`'s own documented `QueryFn` reference shape —
  `(model, messages, timeout) -> dict`, returning the same status-dict shape
  (`status`/`content`/`latency_ms`/`usage`) as `llm_council.gateway_adapter.
  query_model_with_status` today — so it drops into `query_models_resilient`
  as a direct `query_fn` replacement, zero changes needed to
  `resilient_query.py` itself.
- Classifies failures into the exact STATUS_* taxonomy already recorded in
  `docs/upstream-deltas.md` (`STATUS_OK`/`STATUS_TIMEOUT`/
  `STATUS_RATE_LIMITED`/`STATUS_AUTH_ERROR`/`STATUS_ERROR`) — HTTP 429 →
  rate_limited, 401/403 → auth_error, timeout/`URLError`/socket timeout →
  timeout, any other exception or non-2xx → error, 2xx → ok. This is a NEW
  classification layer, not reused from `_post_chat_completion` (which
  raises on final failure instead of returning a status dict) or from
  `llm_council.openrouter.query_model_with_status` (the unsafe path above) —
  written fresh against the taxonomy table, single source of truth.
- **Exactly one attempt per call, no internal retry loop** — unlike
  `_post_chat_completion`'s own `MAX_RETRIES=3` backoff, which would double
  up with `resilient_query.py`'s injected `RetryPolicy` and corrupt Contract
  A/B's attempt-counting. This function must be a thin single-shot HTTP
  call; all retry/backoff ownership stays in `resilient_query.py`.
- Per-model effort mapping is a hardcoded dict at the Stage 1 call site in
  `council_adapter.py` (matching this project's existing style of
  hardcoding exact model→role assignments, e.g. Stage 3.75 = gpt-5.5-only),
  not a new config schema. `query_models_resilient`'s `query_fn` type is
  a fixed 3-arg callable, so this maps to a per-model closure/`functools.
  partial` built at the call site, not a 4th positional argument threaded
  through `resilient_query.py`. **Live values: `{"anthropic/claude-opus-4.8":
  "high", "openai/gpt-5.5": "high", "google/gemini-3.6-flash": "medium",
  "z-ai/glm-5.2": "medium"}`** — briefly reverted opus-4.8/gpt-5.5 to
  `medium` on 2026-08-14 after a dry-run misread (see "Rollout
  precondition" below), restored same day once the misread was caught;
  `docs/pipeline-architecture-spec.md` §9 explains why CSS movement alone
  didn't support that revert.

**Acceptance criteria:**

11. Given `query_model_with_status_and_effort(model, messages, timeout)`
    with `reasoning_effort=None` (default), When called, Then the request
    body has no `reasoning_effort` key — byte-identical to what
    `llm_council.gateway_adapter.query_model_with_status` sends today for
    the same inputs (mod the fields that function doesn't expose, e.g.
    `disable_tools` — not used by Stage 1 today, out of scope).
12. Given `reasoning_effort="high"` (or any non-None string), When called,
    Then the request body includes top-level `"reasoning_effort": "high"`,
    unvalidated (OpenRouter's own 4xx is the validation, same as Contract 1).
13. Given a 2xx response, When parsed, Then the returned dict has
    `status="ok"`, `content`, `latency_ms`, and a `usage` dict shaped
    identically to `llm_council.gateway_adapter.query_model_with_status`'s
    today (so nothing downstream of Stage 1 — cost accounting, response
    assembly — needs to change).
14. Given HTTP 429, When parsed, Then `status="rate_limited"`.
15. Given HTTP 401 or 403, When parsed, Then `status="auth_error"`.
16. Given a network/socket timeout or the caller's `timeout` elapsing, When
    parsed, Then `status="timeout"`.
17. Given HTTP 400 or any other exception not covered above, When parsed,
    Then `status="error"`.
18. Given the function raises internally for any reason NOT in 14-17 (e.g. a
    programming bug), When it happens, Then it must NOT be silently
    swallowed into `status="error"` in a way that hides a real code defect
    from this project's own test suite — only genuine request-level
    failures map to a status; unexpected exceptions during response
    parsing itself should surface as loudly as they do in the package's own
    `query_model_with_status` (i.e. mirror its behavior exactly, don't
    invent stricter or looser error handling).
19. Given `council_adapter.py`'s Stage 1 section (`run_council_with_
    timeouts`, the `query_models_resilient(...)` call currently at line
    ~649), When wired, Then `query_fn` becomes a per-model-aware closure
    over `query_model_with_status_and_effort` using the hardcoded effort
    map above, and every other argument to `query_models_resilient`
    (`primary_models`, `backup_models`, `retry_policy`,
    `minimum_council_size`, `deadline`) is unchanged.
20. Given Stage 1's existing resilience/backup-substitution behavior
    (cross-stage backup exclusivity with Stage 2, retry/backoff per
    `RetryPolicy`, shortfall warnings), When the call-path swap lands, Then
    all of it is verified unaffected by an explicit integration test — not
    assumed from Contract A/B's original coverage, since those were written
    against the OLD `query_fn`.
21. Given `effort="high"` calls plausibly run longer than the package's
    default (more reasoning tokens before a response), When Stage 1's
    `stage1_timeout`/`stage1_deadline` (computed from `tiers.default=
    reasoning`'s larger budget, see `llm_council.yaml`) is evaluated against
    this, Then a genuinely-slow-but-healthy high-effort response must not
    false-trigger a backup-model substitution within the existing timeout
    budget — explicit test with a deliberately slow-but-successful mock
    response, not just fast-mock coverage.
22. Given an invalid `reasoning_effort` value reaches OpenRouter and it
    returns a 4xx, When Stage 1 surfaces this to the CLI user (post-retry,
    if the model is confirmed unreachable), Then the resulting message is
    legible (names the model and the rejected value), not a raw traceback.
23. Given the Cost & Tokens summary (Pillar 6) is built after a real run,
    When Stage 1 used `reasoning_effort`, Then the summary correctly
    attributes the resulting spend — no silent gap between what was billed
    and what's reported.

### Rollout precondition (Pillar 6, real-money gate)

Per the user's explicit decision (2026-08-14): **gate rollout on a dry-run,
not fast-follow it.** Before Stage 1's `reasoning_effort` wiring is used for
any real (non-test) council run, execute one dry-run pair — same query,
`medium` vs `high` Stage 1 effort — and compare Stage 2's CSS/rubric-score
distributions, mirroring the discipline already applied to the Stage 1
prompt-enrichment decision (`docs/agent-model-reasoning-config.md` §5). If
`high` effort doesn't show a measurable quality gain over `medium` for the
2 seats being promoted (opus-4.8, gpt-5.5), that's a real finding to report
back, not a reason to skip the dry-run and ship anyway.

**Result (executed 2026-08-14, same day):** ran directly via
`council_adapter.run_council_with_timeouts()` (bypassing Stage 0.5
grounding — out of scope for this comparison, both runs show
`grounded: false` identically as a result, not a differentiator), same
low-stakes query, real OpenRouter. `medium`-for-all-4-seats baseline: CSS
0.721 ("moderate consensus"), cost $0.362. `high`-for-opus/gpt-5.5: CSS
0.572 ("weak consensus"), cost $0.360 — essentially identical cost.

**Correction (same day, on direct user challenge — see
`docs/pipeline-architecture-spec.md` §9):** this CSS movement was
initially, incorrectly, read as evidence `high` effort produced worse
Stage 1 output, and the default was briefly reverted to `medium` on that
basis. CSS measures cross-model *ranking agreement*, not correctness —
both 0.721 and 0.572 fall inside this pipeline's normal, fully-handled
operating range (neither crossed the <0.50 "significant disagreement"
threshold that actually triggers extra deliberation), and this pipeline
already treats lower agreement as an expected, acted-on signal, not a
failure state. The CSS drop alone was never valid evidence about answer
quality in either direction. **`_STAGE1_REASONING_EFFORT` restored to
`high` for opus-4.8/gpt-5.5** (the originally-shipped default). A genuine
quality comparison — reading actual Stage 1 draft content against the
Stage 2 rubric, not inferring from CSS — is a separate, still-open
follow-up; CSS could never have settled it either way. Full incident:
`docs/upstream-deltas.md`'s 2026-08-14 "Contract 4 dry-run" and "CSS
correction" entries.

## Non-goals

- No nested `reasoning: {effort, max_tokens, exclude}` object anywhere in
  this project's own request-building code — the top-level `reasoning_effort`
  field supersedes it for this project's purposes per the grounding above,
  now including Contract 4.
- No reasoning-effort wiring for Stage 2 or Stage 3 in this pass — both
  remain logged, open, upstream-blocked follow-up items.
- No enum validation of the `effort` string in this project's own code —
  OpenRouter's own 4xx response is the validation.
- No "deep research" model/product integration (e.g. `openai/o3-deep-
  research`) and no expansion of the existing `:online` web-search plugin
  beyond its current Stage-0.5-only role — both explicitly considered and
  rejected by the 2026-08-14 panel (see `docs/upstream-deltas.md`).
- No change to Stage 2's deliberate `reasoning_effort="none"` default, and
  no change to Stage 2.75/3.75's existing CSS/outlier gating — the panel
  explicitly rejected making high effort an unconditional blanket policy.

## Test strategy

Contracts 1-3: direct implementation, test-first, hand-verified RED->GREEN,
matching this session's established practice for contracts of this size.

Contract 4: **blind-TDV** (this project's Pillar 3) — an isolated test
author works from ACs 11-23 and the `QueryFn`/status-dict contract alone,
never from this implementation narrative, then an isolated implementer
builds it blind, watch RED -> minimal GREEN -> scoped mutmut gate (0
survivors) on `scripts/live_adapters.py`'s new function and the
`council_adapter.py` Stage 1 wiring change. Warranted by scope (new
function + a change to the mutation-gated Stage 1 resilience call site,
per AC20's requirement that existing backup/retry behavior be re-verified,
not assumed).
