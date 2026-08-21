# Stage 1.5 normalizer timeout, parallelism, and failure-visibility contract

Pillar 2 spec, written before code. Fixes the root cause identified and
grounded in `docs/upstream-deltas.md`'s 2026-08-21 entry (this session's
"5th seat always times out" investigation).

## Objective

`llm_council.council_stages.stage1_5_normalize_styles` — the style-
normalization pass this repo's pipeline calls at two sites inside
`scripts/council_adapter.py::run_council_with_timeouts` — hardcodes a
60-second per-call timeout with no override, runs its per-response calls
sequentially in a plain `for` loop, and treats a timed-out/failed call as a
silent fallback to un-normalized text (no exception, no signal). This is
the same 60s ceiling this repo already proved too tight for these 4
frontier models (`docs/upstream-deltas.md`, "Timeout architecture fix,
2026-08-12"), and it is not covered by any of `run_council_with_timeouts`'s
existing `stage1_timeout`/`stage2_timeout`/`stage3_timeout` knobs.

Fix: a local wrapper — mirroring the existing "wrap the real function,
don't patch installed vendor code" pattern already used throughout
`council_adapter.py` (`_stage1_query_fn`, `_stage3_query_fn`) — that gives
this call a configurable timeout, real per-response parallelism, and a
surfaced warning when a normalization call falls back, instead of a silent
per-response degradation.

## Non-goals

- Does not patch the installed `llm-council-core` package.
- Does not change the rewrite prompt text, the `style_normalization`
  config gate semantics (`False`/`True`/`"auto"`), or which model does the
  rewriting (`_get_normalizer_model()`) — those are unchanged, byte-for-byte
  faithful to the existing package behavior when normalization succeeds.
- Does not change Stage 1/2/3's own timeout wiring.
- Does not change `_add_cost_to_usage`'s existing (possibly surprising)
  cost-attribution convention of tagging normalizer spend under the
  *original* response's model slug, not the normalizer's own slug — that
  is pre-existing upstream behavior, out of scope for this contract, and
  changing it silently would be a second, undiscussed behavior change
  riding along with the timeout fix.
- Does not touch the MCP tool path (`consult_council` /
  `run_council_with_fallback`) — this module has never covered that path,
  per its own header docstring.

## Design

### New function: `_normalize_responses_with_timeout`

```python
async def _normalize_responses_with_timeout(
    entries: List[Dict[str, Any]],
    timeout: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], List[str]]:
```

- `entries`: each dict has at least `"model"` and `"response"` keys (same
  shape `stage1_5_normalize_styles` already accepts).
- Returns `(normalized_entries, usage, failed_models)`:
  - `normalized_entries`: same shape/order as
    `stage1_5_normalize_styles` returns today — one
    `{"model", "response", "original_response"}` dict per input entry, in
    input order.
  - `usage`: same accumulator shape as today
    (`{"prompt_tokens", "completion_tokens", "total_tokens", ...cost
    keys added by `_add_cost_to_usage`}`).
  - `failed_models` (new): list of `entries[i]["model"]` for every entry
    whose normalization call did not return `status == STATUS_OK`
    (timeout, error, rate-limited, etc.) and therefore fell back to its
    original, un-normalized `"response"`. Empty when every call
    succeeded, and empty (not populated) when normalization was skipped
    entirely by the config gate (a skip is a deliberate no-op, not a
    failure) — matches `stage1_5_normalize_styles`'s own early-return
    behavior for `False`/non-triggered-`"auto"`.

Behavior:
1. Read `_get_style_normalization()` first, before touching `entries` —
   preserves the exact existing "config gate short-circuits before any
   list access" behavior both current call sites already depend on for
   the empty-list/single-model-degraded-mode case.
   - `False` → return `(entries unchanged, zeroed usage, [])` with no
     model calls at all.
   - `"auto"` → call `should_normalize_styles([e["response"] for e in
     entries])`; if it returns `False`, same unchanged/zeroed/`[]` return.
   - Otherwise (config `True`, or `"auto"` triggered): proceed to step 2.
2. Build the identical rewrite prompt per entry (copy
   `stage1_5_normalize_styles`'s exact prompt template — no wording
   changes) and issue all entries' `query_model_with_status(
   _get_normalizer_model(), messages, timeout)` calls **concurrently**
   via `asyncio.gather` (this repo's own already-imported
   `query_model_with_status`, from `gateway_adapter` — the same import
   this module already uses for Stage 1/3, not the raw `openrouter`
   module — for consistent error-detail/status handling with the rest of
   this file).
3. Per result, in original entry order:
   - `status == STATUS_OK`: `normalized_entries` gets
     `{"model": entry["model"], "response": result.get("content",
     entry["response"]), "original_response": entry["response"]}`;
     accumulate `result.get("usage", {})` into `usage` (same
     `prompt_tokens`/`completion_tokens`/`total_tokens` summing
     `stage1_5_normalize_styles` already does) and call
     `_add_cost_to_usage(usage, result_usage, model=entry["model"])` —
     preserving the existing (pre-existing, out-of-scope-to-change)
     cost-attribution convention noted above.
   - otherwise: `normalized_entries` gets `{"model": entry["model"],
     "response": entry["response"], "original_response":
     entry["response"]}` (unchanged fallback, matching today's silent
     behavior for the *text*), and `entry["model"]` is appended to
     `failed_models` (the new, previously-missing signal).
4. Return `(normalized_entries, usage, failed_models)`.

### Call site 1 — Stage 1 → 1.5 (`run_council_with_timeouts`, ~line 854)

Replace:
```python
responses_for_review, stage1_5_usage = await stage1_5_normalize_styles(stage1_results)
```
with:
```python
responses_for_review, stage1_5_usage, stage1_5_failed = await _normalize_responses_with_timeout(
    stage1_results, stage1_5_timeout
)
```
(`stage1_5_timeout` is the new parameter below.)

### Call site 2 — Stage 2 reviewer commentary (`_normalize_stage2_for_stage3`)

Add a `timeout: float` parameter to `_normalize_stage2_for_stage3` and
change its internal call from `stage1_5_normalize_styles(as_pseudo_stage1)`
to `_normalize_responses_with_timeout(as_pseudo_stage1, timeout)`. Its own
return type gains a third element, `failed_models: List[str]` (the
original-response-model names whose Stage-2-commentary normalization
fell back), threaded straight through from the wrapper — this function's
own early-return (`stage2_results == []`) also gains a matching `[]` third
element.

Its call site inside `run_council_with_timeouts` (~line 960) passes
`stage1_5_timeout` (the same budget — one config knob covers both call
sites, since they're the same underlying operation applied to two
different text sources) and captures the third return value.

### `run_council_with_timeouts` signature

New parameter: `stage1_5_timeout: float = 300.0` (same default as
`stage1_timeout`/`stage2_timeout`/`stage3_timeout`, for consistency — not
tied to the package's old 60s default, which is exactly the value already
proven insufficient).

### Metadata / failure surfacing

Mirror the existing `metadata["shortfall_warning"]`/
`metadata["ungrounded_models"]` pattern (both populated only when
non-empty, both consumed in `pipeline_runner.py` as a `debug_log` WARNING
line):

```python
normalization_failures = list(stage1_5_failed) + list(stage2_normalize_failed)
if normalization_failures:
    metadata["normalization_failures"] = normalization_failures
```

`pipeline_runner.py`, alongside the existing `ungrounded_models` loop:
```python
for model in metadata.get("normalization_failures") or []:
    debug_log.append(
        f"WARNING: {model}'s response could not be style-normalized in "
        "time (Stage 1.5) - Stage 3 may see un-normalized, potentially "
        "fingerprinted text for this model"
    )
```

### `debate.py` CLI

Add `--stage1-5-timeout` (`type=float, default=300.0`), threaded into
`run_council_with_timeouts` alongside the existing three `--stageN-timeout`
flags — same pattern, same help-text style.

## Acceptance criteria (Given/When/Then)

1. **AC1 — config gate off, no calls made.** Given
   `evaluation.rubric`-unrelated config `style_normalization: false`
   (or unset, matching the package default), when
   `_normalize_responses_with_timeout` is called with any non-empty
   `entries` list, then it returns `(entries verbatim as
   {"model","response","original_response"} with "original_response" ==
   "response", zeroed usage, [])` and issues zero `query_model_with_status`
   calls.

2. **AC2 — success path, parallel not sequential.** Given
   `style_normalization: true` and 4 entries whose mocked
   `query_model_with_status` each takes a distinguishable, trackable
   amount of simulated time (e.g. via an `asyncio.Event`/counter fake),
   when `_normalize_responses_with_timeout` is called, then all 4 calls
   are in flight concurrently (verifiable via a fake that records
   overlapping start times, or asserts total elapsed time approximates
   the single slowest call rather than the sum of all four) and every
   entry's `response` becomes its mocked model's normalized text,
   `original_response` preserves the pre-normalization text, and
   `failed_models == []`.

3. **AC3 — one entry times out, others succeed.** Given 4 entries where
   one's mocked query returns `status != STATUS_OK` (e.g. `"timeout"`)
   and the other three return `STATUS_OK`, when
   `_normalize_responses_with_timeout` is called, then the timed-out
   entry's `response == original_response == entries[i]["response"]`
   (fallback to original text, matching today's behavior), the other
   three are normalized, `failed_models == [that one model]`, and no
   exception propagates.

4. **AC4 — `timeout` argument is honored, not the package's hardcoded 60s.**
   Given a fake `query_model_with_status` that asserts its own received
   `timeout` argument, when `_normalize_responses_with_timeout(entries,
   timeout=123.0)` is called, then every call receives `timeout=123.0`,
   not `60.0`.

5. **AC5 — `"auto"` mode still gates on `should_normalize_styles`.** Given
   `style_normalization: "auto"` and a fake `should_normalize_styles` that
   returns `False`, when `_normalize_responses_with_timeout` is called,
   then it returns unchanged entries / zeroed usage / `[]` with zero
   `query_model_with_status` calls (same as AC1), and when the fake
   returns `True`, normalization proceeds (same as AC2/AC3).

6. **AC6 — `_normalize_stage2_for_stage3` threads timeout and failures.**
   Given `stage2_results` with one entry whose normalization call fails,
   when `_normalize_stage2_for_stage3(stage2_results, timeout=X)` is
   called, then the failing entry's `"ranking"` is left as the original
   text, the third return value is `[that model]`, and the underlying
   normalization call received `timeout=X`. Given `stage2_results == []`,
   then the function returns `([], zeroed usage, [])` with zero calls,
   matching its existing empty-list short-circuit.

7. **AC7 — `run_council_with_timeouts` surfaces failures in metadata.**
   Given a multi-model run where at least one Stage 1.5 or Stage 2
   normalization call fails, when `run_council_with_timeouts` completes,
   then its returned `metadata["normalization_failures"]` contains every
   failing model's slug (deduplication not required — a model can appear
   once per stage it failed in), and given a run with zero normalization
   failures (including config-gate-off runs), then
   `"normalization_failures"` is absent from `metadata` entirely (not an
   empty list) — matching the existing `shortfall_warning`/
   `ungrounded_models` "only present when non-empty" convention.

8. **AC8 — `stage1_5_timeout` defaults and threads correctly.** Given no
   explicit `stage1_5_timeout` argument to `run_council_with_timeouts`,
   then it defaults to `300.0`, and given an explicit value, then both
   call sites' underlying `query_model_with_status` calls receive it.

9. **AC9 — `pipeline_runner.py` surfaces the warning.** Given a
   `council_fn` result whose `metadata` contains
   `"normalization_failures": ["some/model"]`, when `run_pipeline`
   processes it, then `debug_log` contains a line starting with
   `"WARNING: some/model's response could not be style-normalized"`.

10. **AC10 — `debate.py` exposes `--stage1-5-timeout`.** Given
    `debate.py --stage1-5-timeout 45`, when its arg parser runs, then
    `args.stage1_5_timeout == 45.0` and it is passed through to
    `run_council_with_timeouts`.

## Environment for blind test authorship

- Module under change: `scripts/council_adapter.py` (new function
  `_normalize_responses_with_timeout`; edits to
  `_normalize_stage2_for_stage3` and `run_council_with_timeouts`).
- Secondary: `scripts/pipeline_runner.py` (debug_log surfacing),
  `scripts/debate.py` (CLI flag).
- Existing fakes/mocks for `query_model_with_status` and
  `_get_style_normalization`/`_get_normalizer_model`/
  `should_normalize_styles` already exist in
  `tests/test_council_adapter.py` and
  `tests/test_council_adapter_resilient_stage1.py` — reuse their
  patching style (`unittest.mock.patch` on the
  `scripts.council_adapter` module namespace, since these are imported
  by name at module load) rather than inventing a new fixture style.
- `STATUS_OK` is `"ok"`, importable from `llm_council.openrouter`.
- `query_model_with_status(model, messages, timeout)` returns
  `{"status": ..., "content": ..., "usage": {...}, ...}` on success;
  the fields consumed here are `"status"`, `"content"`, `"usage"`.
