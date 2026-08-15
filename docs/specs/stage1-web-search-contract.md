# Stage 1 web-search access ("Contract 5") — Pillar 2, before code

## Grounding this contract relies on

- Direct source read (this session, unchanged since Contract 4): the installed
  `llm-council-core==0.40.1`'s `llm_council/gateway/openrouter.py::
  build_openrouter_payload` accepts only `reasoning_params`, `max_tokens`,
  `temperature`, `disable_tools` — no `tools` array, no `plugins`, no
  `response_format`, no `provider` field. Stage 1 (the independent-draft
  round) has zero web-search access today and there is no config-only way to
  add it — same class of package limitation Contract 4 already worked around
  for `reasoning_effort`.
- Live-fetched this session (`openrouter.ai/docs/guides/features/
  server-tools`, `.../server-tools/web-search`): the `openrouter:web_search`
  server tool is a top-level request field —
  ```json
  {
    "model": "...",
    "messages": [...],
    "tools": [{"type": "openrouter:web_search", "parameters": {"max_uses": 1}}],
    "max_tool_calls": 1
  }
  ```
  `max_uses` (tool parameter) and `max_tool_calls` (top-level, default/max
  30) are both genuinely OpenRouter-enforced hard ceilings, not advisory —
  confirmed via a direct follow-up fetch after an initial ambiguous read.
  When the budget is exhausted, OpenRouter redirects the model to produce a
  final answer with whatever context it already gathered — it does not
  error, matching this contract's fail-closed requirement for free.
- Live catalog fetch (`/api/v1/models`, this session): native `web_search`
  pricing per roster model — `anthropic/claude-opus-4.8` $0.01/call,
  `openai/gpt-5.5` $0.01/call, `google/gemini-3.7-flash` $0.014/call (all
  three have a native search engine). `z-ai/glm-5.2` has **no native search
  engine** and no catalog-listed `web_search` price at all — it would run on
  Exa/Parallel fallback pricing, unconfirmed this session.
- Legacy `:online` suffix / `plugins:[{id:"web"}]` confirmed **deprecated**
  (docs state this verbatim) in favor of the server-tool mechanism above —
  this contract uses `openrouter:web_search` only, never the legacy path,
  even though the legacy path is what Stage 0.5's existing
  `EVIDENCE_MODEL = "google/gemini-3.7-flash:online"` still uses (out of
  scope to migrate that here — Stage 0.5 is a separate, already-working,
  human-reviewed grounding layer, not touched by this contract).
- Response-side provenance, **confirmed by one real, direct OpenRouter call
  this session** (`google/gemini-3.7-flash`, a factual query, `max_uses:
  1`), not guessed from docs prose: `message.annotations` is an array of
  `{"type": "url_citation", "url_citation": {"url": ..., "title": ...,
  "start_index": N, "end_index": N}}` objects. Call count lives at
  `usage.server_tool_use_details.web_search_requests` (note: **not**
  `usage.server_tool_use` as the docs prose implied — a real naming
  discrepancy the live call caught).
- **`max_uses` did not behave as a strict hard cap in this live test.**
  Requested `max_uses: 1`; the response's own
  `usage.server_tool_use_details.web_search_requests` came back `2`, and
  the billed cost confirms it: `usage.cost` minus prompt/completion token
  cost = exactly $0.028 = 2 × the catalog's $0.014/request price, not 1×.
  Whether this is Gemini's native grounding internally issuing a
  search-then-expand pair counted as 2 "requests," or `max_uses` genuinely
  not enforcing as documented, was not resolved this session — AC 3 and the
  rollout dry-run (below) must treat the cost ceiling as **up to 2× the
  naive per-model price** until independently reconfirmed, not trust the
  docs' "hard ceiling" framing at face value.
- `docs/upstream-deltas.md`'s 2026-08-16 entries (both the original
  research/panel/council pass and the homogenization re-examination) are the
  full decision trail behind every requirement below — this file is the
  "what to build," that ledger is the "why."

## Why this contract exists despite a prior rejection

`docs/specs/reasoning-effort-wiring-contract.md`'s Non-goals section records
that Stage-1 web-search expansion was "explicitly considered and rejected by
the 2026-08-14 panel," citing Knowledge-Divergence homogenization concerns
(arXiv:2603.05293). Re-examined this session with the paper read directly
(not the original panel's paraphrase): the paper's homogenization mechanism
(§3.4, "Dynamic Subspaces Under Debate") formally models **peer-to-peer
revelation across debate rounds** — model A reading model B's argument
mid-debate and absorbing it. This cannot fire in Stage 1, which is a
single-pass independent draft with zero peer visibility (peer exposure only
starts at Stage 2). The mechanism that *does* apply is the paper's static
framework (§2): shared knowledge inputs reduce debate value proportionally
to their overlap, not wholesale, and the paper's own text notes models from
different pretraining pipelines (this project's 4-different-labs roster)
have higher "effective rank" of private knowledge — more robust to a single
shared-knowledge injection than same-family fine-tuned variants.

A second panel, run specifically against this corrected mechanism, converged
8 concerns raised / 6 resolved / 2 needing user decision: **proceed, but
with the design levers below as hard requirements, not optional
hardening** — neither the original blanket rejection nor an unconditional
green light. Both open user decisions (rollout gate, GLM-5.2 exclusion
permanence) were resolved by the user same session — folded into this spec
below, not left open.

## Scope

Stage 1 (`council_adapter.py`'s `_stage1_query_fn`, the same call site
Contract 4 already owns) only. No other stage. `z-ai/glm-5.2` is
**permanently excluded** from this capability (see AC 5) — not because of
today's unconfirmed pricing alone, but as a codified architectural
invariant preserving at least one always-independent draft voice,
independent of any future GLM-5.2 pricing confirmation.

## Design

A new optional parameter on the existing Contract-4 function
(`scripts/live_adapters.py::query_model_with_status_and_effort`) —
`enable_web_search: bool = False` — additive only, matching Contract 4's own
pattern for `reasoning_effort`. When `False` (every existing caller, every
existing test), the request body is unchanged from today. When `True`, the
request body gains the `tools`/`max_tool_calls` fields above, with a fixed,
low, hardcoded cap (`max_uses=1`, `max_tool_calls=1` — one search call per
model per debate round; matches the real council's converged recommendation
to start at the smallest useful bound, raise later only with observed
data). No provider/routing fields are added in this contract — that was a
"free ride-along" idea from the first panel/council pass, never examined by
the homogenization re-examination, and is explicitly deferred (see
Non-goals) rather than smuggled in.

Per-model enablement is a hardcoded set at the Stage 1 call site
(`council_adapter.py`), matching Contract 4's `_STAGE1_REASONING_EFFORT`
style: `_STAGE1_WEB_SEARCH_ENABLED_MODELS = {"anthropic/claude-opus-4.8",
"openai/gpt-5.5", "google/gemini-3.7-flash"}` — `z-ai/glm-5.2` never appears
in this set, enforced by a test (AC 5), not just by omission.

**No shared/pooled search step.** Because Stage 1 already issues one
independent HTTP request per model (Contract 4's `_stage1_query_fn` closure
calls `query_model_with_status_and_effort` once per model), each enabled
model forms and issues its own search query independently by construction —
there is no new "fetch once, broadcast to N models" mechanism like Stage
0.5's `EVIDENCE_MODEL` pattern. This satisfies the "independent queries
only" requirement structurally; AC 6 makes it an explicit regression test
so a future change can't accidentally introduce pooling.

**Claim-scoped, not exploratory.** The Stage 1 system prompt gains an
explicit instruction (new, additive block, same pattern as the existing
`_STAGE1_REFERENCE_INSTRUCTION_BLOCK`/epistemic-clauses machinery from Stage
0.5): the model may use web search only to verify a specific factual claim
it is about to make in its own draft, never for open-ended exploration, and
must treat search results as reference data, never as instructions to
follow. This is a prompt-level instruction, not a code-enforced constraint
(OpenRouter's server tool has no "scope" parameter to enforce this
mechanically) — AC 8 requires this instruction be present verbatim-testable
in the built prompt, acknowledging (per the second panel's Builder Red
finding) that a loose instruction alone is a known failure class in this
project (the 2026-08-13 Stage-0.5 internal-name leak) and should be
monitored via the provenance logging in AC 7, not assumed to work.

**Provenance, not just logging.** Every Stage 1 result (searched or not)
carries a `web_search_provenance` field distinguishing three states: `"not_
enabled"` (glm-5.2, or if a future rollout disables the feature globally),
`"enabled_no_search"` (model had access but chose not to search this
round), and `"enabled_searched"` (with the query/queries issued and
source URLs, parsed from the response's `url_citation` annotations per the
grounding note above). This is threaded through to Stage 2/3 exactly like
every other Stage 1 result field already is — no new persistence layer.

**Fail-closed, distinct retry semantics.** `max_uses`/`max_tool_calls`
exhaustion is OpenRouter's own hard-enforced behavior (model gets redirected
to a final answer, never errors) — no new code needed for that specific
case. A **failed** search-tool call (the tool itself errors mid-request,
distinct from budget exhaustion) must not be silently swallowed into a
generic `STATUS_ERROR` in a way that triggers Stage 1's existing
retry/backup substitution *more aggressively* than a normal model failure
would — AC 9 requires this be verified against the existing
`resilient_query.py` retry wrapper's actual behavior (does a Stage 1 retry
re-issue, and re-bill, a fresh search call on every attempt?), not assumed
safe. Worst case, even 3 retries × `max_uses=1` = 3 search calls per model
is still small and bounded ($0.03–0.042 for the 3 enabled models at 3
retries each) — but this must be stated as a known, accepted cost ceiling
in the Cost & Tokens dry-run summary, not silently absorbed.

## Contract

**Acceptance criteria:**

1. Given `query_model_with_status_and_effort(model, messages, timeout,
   reasoning_effort=None, enable_web_search=False)` (new parameter,
   defaulted `False`), When `enable_web_search` is `False`, Then the request
   body is byte-identical to Contract 4's existing behavior — no `tools`,
   no `max_tool_calls` key present. Every existing caller/test needs zero
   changes.
2. Given `enable_web_search=True`, When the request body is built, Then it
   includes `"tools": [{"type": "openrouter:web_search", "parameters":
   {"max_uses": 1}}]` and top-level `"max_tool_calls": 1`.
3. Given a 2xx response with `enable_web_search=True`, When parsed, Then
   the returned status dict gains a `web_search_provenance` field per the
   three-state design above, populated from
   `usage.server_tool_use_details.web_search_requests` and
   `message.annotations[].url_citation.{url,title}` — the exact,
   live-confirmed field names from the grounding note above, not a schema
   guessed from docs prose.
4. Given `enable_web_search=False` (or omitted), When parsed, Then
   `web_search_provenance` is `"not_enabled"` — every existing test's
   assertions on the status dict shape still pass unmodified except for this
   one new, defaulted field.
5. Given `council_adapter.py`'s Stage 1 wiring, When the per-model closure
   is built, Then `enable_web_search=True` is passed only for models in
   `_STAGE1_WEB_SEARCH_ENABLED_MODELS = {"anthropic/claude-opus-4.8",
   "openai/gpt-5.5", "google/gemini-3.7-flash"}`, and an explicit test
   asserts `"z-ai/glm-5.2" not in _STAGE1_WEB_SEARCH_ENABLED_MODELS` —
   framed as a permanent invariant, not a value that changes when GLM-5.2's
   fallback pricing is eventually confirmed (a future re-decision to change
   this must edit this test's own assertion deliberately, not slip through
   a config value).
6. Given Stage 1's per-model call construction, When multiple models are
   enabled for search, Then each model's HTTP request is built and issued
   independently (no shared query string, no single fetch result reused
   across multiple models' requests) — regression-tested explicitly, not
   just true by current construction.
7. Given the claim-scoping system-prompt instruction, When the Stage 1
   prompt is built for a search-enabled model, Then the instruction block
   is present verbatim in the constructed prompt (direct string
   containment test, matching this project's existing test style for
   `_STAGE1_REFERENCE_INSTRUCTION_BLOCK`).
8. Given a search-tool-specific failure (distinct from a generic HTTP
   error — confirmed exact signal via the live test call in AC 3), When it
   occurs, Then it is classified into its own status value (not silently
   folded into `STATUS_ERROR`), and Stage 1's existing retry/backup
   substitution logic is verified (via an explicit test, not assumed) to
   apply its existing `RetryPolicy` unchanged — no new retry multiplier
   introduced by this contract.
9. Given the Cost & Tokens summary (Pillar 6) built after a real run, When
   any Stage 1 model used `enable_web_search=True`, Then the summary
   attributes search-tool spend separately and visibly (not folded silently
   into base token cost) — matching Contract 4's AC 23 precedent for
   `reasoning_effort` spend attribution.
10. Given this contract's `max_uses=1`/`max_tool_calls=1` caps, When a
    real Stage 1 round runs with all 3 enabled models searching, Then the
    dry-run's Cost & Tokens summary must state BOTH the naive per-`max_uses`
    ceiling ($0.01 + $0.01 + $0.014 = $0.034) AND the **observed** ceiling
    from this session's live test (up to 2× that, $0.068), explicitly
    flagged as unresolved whether `max_uses` under-enforces or Gemini's
    grounding double-counts — excluding retries (see AC 8's bounded-retry
    note). Do not report only the naive figure; the live test this session
    already contradicted it once.

## Non-goals

- No `provider`/routing fields bundled into this contract, despite being a
  "free ride-along" at the same call site per the first panel/council's
  original framing — the homogenization re-examination never evaluated it,
  and adding it now would be unreviewed scope creep. Left as a separate,
  future, explicitly-scoped follow-up if ever pursued.
- No migration of Stage 0.5's existing `:online`-suffix evidence-fetching
  to the new `openrouter:web_search` server tool — that's a separately
  working, already-reviewed mechanism; out of scope here.
- No enabling `z-ai/glm-5.2` for Stage 1 web search, ever, under this
  contract — permanent per the user's explicit 2026-08-16 decision, not a
  value pending a future pricing check.
- No structured-outputs (`response_format`) wiring, no Response Healing
  plugin, no Fusion adoption — all separately researched and rejected/
  deferred this session (`docs/upstream-deltas.md`), none bundled here.
- No change to Stage 0.5, Stage 2, Stage 2.75, Stage 3, Stage 3.75, Stage 4,
  or Stage 5 — this contract is Stage-1-only.

## Rollout precondition (Pillar 6, real-money gate — stricter than Contract 4's)

**Executed 2026-08-16 — see `docs/upstream-deltas.md`'s final entry for full
results.** Headline: content convergence was real but partial/narrow (one
backend-name fact, not wholesale homogenization); both provenance states
observed working on real responses. **Cost result materially exceeds AC 10's
ceiling** — the dominant cost driver is extra PROMPT tokens from injected
search-result content (Claude Opus: 85→13,837 prompt tokens for one search
call), not the flat per-call fee AC 10 was built around. `max_uses` doesn't
bound this; `max_results`/`max_characters`/`search_context_size` would be the
actual levers for tighter cost control, not implemented here — a real,
undelivered gap in this contract's cost design, flagged for a future pass if
tighter bounding is ever needed. Read the ledger entry before relying on
AC 10's stated ceiling for a real budget decision.

Per the user's explicit 2026-08-16 decision: **gate first real-decision use
on a measurement dry-run, not a judgment call** — this is a genuinely novel
risk class (prompt injection via search results, draft-diversity
homogenization) rather than a tuning knob like `reasoning_effort` was, so
real evidence is required before real use, not just a Cost & Tokens
sanity-check. Before this capability is used for any real (non-test)
council run:

1. Run one real Stage-1-only comparison pair (mirroring Contract 4's own
   dry-run method: direct calls bypassing Stage 0.5/2/3, cheaper and
   sufficient for a Stage-1-focused read) — same low-stakes test query,
   once with `enable_web_search=False` (baseline) and once with `True` for
   the 3 enabled models.
2. Compare: (a) whether the search-enabled drafts show measurable content
   convergence versus the baseline drafts (a real read of the text, not a
   CSS-only proxy — Contract 4's own history, `docs/pipeline-architecture-
   spec.md` §9, already established CSS movement alone is not valid
   evidence of quality/diversity change either direction); (b) whether any
   search call actually fired, and on what claim, per the provenance field;
   (c) the real Cost & Tokens delta against the $0.034 ceiling in AC 10.
3. Report the result — including a null/inconclusive result — before
   enabling this for a real decision, matching this project's established
   practice of reporting negative or ambiguous dry-run findings rather than
   only reporting confirmations.

## Test strategy

**Blind-TDV** (Pillar 3), same shape as Contract 4: an isolated test author
works from ACs 1-10 and the existing `query_model_with_status_and_effort`/
`QueryFn` contract alone, never from this design narrative; an isolated
implementer builds it blind; watch RED → minimal GREEN → scoped mutmut gate
(0 survivors) on `scripts/live_adapters.py`'s extended function and the
`council_adapter.py` Stage 1 wiring change. Warranted by scope for the same
reason Contract 4 was: a change to the mutation-gated Stage 1 resilience
call site, where AC 8 specifically requires existing retry/backup behavior
be re-verified, not assumed.

AC 3's exact `annotations` schema must be confirmed via one live,
low-stakes OpenRouter call **before** the blind test author is briefed —
the contract signature they receive must state the real field names, not a
guessed shape, per this project's Pillar 1 (cite-or-don't-write extends to
contracts handed to a blind test author, not just to docs).
