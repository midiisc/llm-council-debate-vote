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

## Scope: reachable-today stages only

Per `docs/agent-model-reasoning-config.md` §3's "Reachable today" column,
only Stage 2.75 (revision), Stage 3.75 (critique), and Stage 4
(completeness) go through this project's own raw-HTTP call path
(`scripts/live_adapters.py::real_query_model`) where we control the request
body directly. Stage 1 (`llm_council.gateway_adapter.query_model_with_status`,
no `reasoning_params` kwarg) and Stage 2/Stage 3
(`council_stages.stage2_collect_rankings`/`stage3_synthesize_final`, no
`reasoning_params` kwarg at all) stay explicitly out of scope — both require
either a call-path swap needing its own blind-TDV pass (Stage 1) or an
upstream fix (Stage 2/3), already logged as Pillar-5 follow-up items.

Target efforts, unchanged from the already-decided table:
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

## Non-goals

- No nested `reasoning: {effort, max_tokens, exclude}` object — the
  top-level `reasoning_effort` field supersedes it for this project's
  purposes per the grounding above.
- No reasoning-effort wiring for Stage 1, Stage 2, or Stage 3 in this pass
  — both remain logged, open follow-up items for the reasons already on
  file in `docs/agent-model-reasoning-config.md`.
- No enum validation of the `effort` string in this project's own code —
  OpenRouter's own 4xx response is the validation.

## Test strategy

Direct implementation, test-first, hand-verified RED->GREEN, matching this
session's established practice for contracts of this size.
