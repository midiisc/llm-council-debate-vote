# Stage 3 chairman-identity anonymization contract (Pillar 2, before code)

Status: ready for implementation. Closes a real, previously-undocumented
bias/isolation gap found during a full-pipeline audit (this session,
2026-08-14) — grounded by direct source read of the installed
`llm-council-core==0.40.1` package, not inferred. See
`docs/upstream-deltas.md`, "Stage 3 chairman identity leak" entry, for the
full grounding citation.

## The gap this closes

`llm_council/council_stages.py::stage3_synthesize_final` (lines 904-922,
verified by direct read of the installed source, 2026-08-14) builds the
chairman-synthesis prompt with **full, real model identity**:
`f"Model: {result['model']}\nResponse: {result['response']}"` for every
Stage 1 draft, `f"Model: {result['model']}\nRanking: {result['ranking']}"`
for every Stage 2 reviewer, and real model slugs in the aggregate-rankings
block (`f"  #{r['rank']}. {r['model']} (avg score: ..., votes: ...)"`).
`scripts/council_adapter.py`'s `_stage3_query_fn` calls this function
directly today, inheriting the leak unmodified.

This is the same bias class Stage 2 peer review already anonymizes against
(shuffled order, opaque `Response A/B/C` labels, no real model names sent —
`council_stages.py:486-507`, reproduced in this repo's own
`_build_stage2_real_ranking_prompt`). Leaving Stage 3 unanonymized is a real
gap because **the chairman (Claude Opus 4.8) is also a Stage 1 drafter** —
it can recognize and potentially favor its own earlier draft — and because
every model's brand/reputation is otherwise visible during synthesis, the
exact halo-effect risk Stage 2's anonymization exists to prevent.

## Design decision

Anonymize Stage 3's view the same way Stage 2 already is: real model names
are replaced with the **same `Response A/B/C` labels Stage 2's own shuffle
already assigned** (via `label_to_model`, already computed before Stage 3
runs), so a label means the same model whether the chairman is reading it
as a draft author or as a reviewer. A Stage 2 reviewer with no Stage 1
draft of its own (a backup model substituted only into a reviewer slot)
gets a fresh label continuing the same sequence.

The chairman's own model call is untouched — `_get_chairman_model()`
resolution never depends on `stage1_results`/`stage2_results`/
`aggregate_rankings` content, only on config, so real chairman identity and
real cost/usage accounting are unaffected.

**Human-legibility carve-out** (matches the global human-legible-output
directive): the anonymization must only affect what the *chairman model*
sees while reasoning. The synthesis text the chairman produces (which may
itself reference `Response A`/`Response B` per the existing debate-mode
instructions) is resolved back to real model names before it becomes the
human-facing answer/transcript — a human reading the final synthesis must
never see a raw internal label.

**Non-goals:**
- Does not touch Stage 2 (already anonymized) or any other stage.
- Does not touch the *returned* `stage1_results`/`stage2_results`/
  `aggregate_rankings`/`metadata` values `run_council_with_timeouts`
  hands back to `pipeline_runner.py` — those keep real model names
  unchanged, since transcripts and metadata are human-facing and must stay
  legible. Anonymization applies **only** to the copy passed into the
  chairman's own prompt construction.
- Does not attempt to anonymize `VerdictType.BINARY`/`TIE_BREAKER` prompt
  branches — already out of scope for this project (module docstring:
  "Non-goals (confirmed unused by this project today)").
- Does not change `stage3_synthesize_final`'s retry/backup/error-status
  behavior (`_synthesize_resilient`, already covered by its own contract)
  — this contract only changes what data reaches the prompt and what
  happens to the output text, not the call/retry mechanics.

## Contract 1 — three new functions in `scripts/council_adapter.py`

### Function A: `_build_stage3_identity_map`

**Signature:**
```python
def _build_stage3_identity_map(
    stage1_results: list[dict],
    stage2_results: list[dict],
    label_to_model: dict[str, dict],
) -> dict[str, str]:
```

**Environment (given, not re-derived):**
- `stage1_results`: list of dicts, each with at least a `"model"` key
  (real model slug string, e.g. `"anthropic/claude-opus-4.8"`).
- `stage2_results`: list of dicts, each with at least a `"model"` key
  (the *reviewer's* real model slug — not the author being reviewed).
- `label_to_model`: shape `{"Response A": {"model": "<slug>", "display_index": 0}, ...}`
  — Stage 2's own label assignment (already computed before Stage 3 runs;
  same shape whether via `_build_stage2_real_ranking_prompt`'s real path or
  the single-model degraded-mode manual construction).
- Labels already present in `label_to_model` follow the pattern
  `"Response " + chr(65 + i)` for `i` in insertion order, but the function
  must not assume any particular set of letters is already used — the
  input could (in principle) already contain non-contiguous or unusual
  keys; treat `label_to_model` as an opaque `str -> {"model": str, ...}`
  mapping and only rely on its `len()` and its values' `"model"` field.

**Acceptance criteria:**
1. Given `label_to_model = {"Response A": {"model": "m1", "display_index": 0}, "Response B": {"model": "m2", "display_index": 1}}` and `stage1_results`/`stage2_results` both referencing only `"m1"`/`"m2"`, When called, Then the result is exactly `{"m1": "Response A", "m2": "Response B"}` (every Stage 1 drafter keeps the exact label `label_to_model` already assigned it — never reassigned to a different label).
2. Given the same `label_to_model` as AC1, and `stage2_results = [{"model": "m3", "ranking": "..."}]` (a reviewer with no Stage 1 draft of its own — not a key in `label_to_model`), When called, Then the result contains `"m3"` mapped to a label that is neither `"Response A"` nor `"Response B"` (a genuinely new label, continuing the sequence — e.g. `"Response C"`), and `"m1"`/`"m2"` still map to `"Response A"`/`"Response B"` unchanged.
3. Given the same inputs as AC2 but `stage2_results` also contains a second entry `{"model": "m3", "ranking": "..."}` (the same reviewer appearing twice — not a realistic shape, but must not crash), When called, Then `"m3"` still maps to exactly one label (no duplicate/second label minted for the same model).
4. Given `stage2_results = [{"model": "m1", "ranking": "..."}]` where `"m1"` is already a key in `label_to_model` (a Stage 1 drafter also reviewing, the normal case), When called, Then `"m1"` maps to its existing Stage 1 label (`"Response A"`), not a new one.
5. Given `stage2_results = []` (single-model degraded mode, no Stage 2 round), When called, Then the result is exactly the inversion of `label_to_model` (e.g. `{"m1": "Response A"}`) — no crash on an empty list.
6. Given `label_to_model = {}` and `stage2_results = []`, When called, Then the result is `{}` — no crash on fully empty inputs.
7. Given any valid inputs, When called, Then no two distinct real model names ever map to the same label (the map is injective) — every value in the returned dict is unique.
8. Given any valid inputs, When called, Then the function is pure — it does not mutate `stage1_results`, `stage2_results`, or `label_to_model` (compare each argument's identity-preserving content before/after the call).
9. Given inputs, When called twice with the same arguments, Then it returns an equal result both times (deterministic given fixed inputs — no reliance on randomness, wall-clock, or hash-order-sensitive iteration beyond each input list's own given order).

### Function B: `_anonymize_for_stage3`

**Signature:**
```python
def _anonymize_for_stage3(
    stage1_results: list[dict],
    stage2_results: list[dict],
    aggregate_rankings: list[dict] | None,
    model_to_label: dict[str, str],
) -> tuple[list[dict], list[dict], list[dict] | None]:
```

**Environment (given, not re-derived):**
- `model_to_label`: output shape of Function A (`{"<real model slug>": "Response X", ...}`).
- `stage1_results` entries carry `"model"` plus other keys (at minimum
  `"response"`, possibly `"safety_check"` — treat any non-`"model"` key as
  opaque passthrough data, never inspected or transformed).
- `stage2_results` entries carry `"model"` plus other keys (at minimum
  `"ranking"`, `"parsed_ranking"` — same opaque-passthrough rule).
- `aggregate_rankings` entries (when not `None`) carry `"model"` plus other
  keys (`"rank"`, `"average_score"`, `"vote_count"`, possibly `"note"`,
  `"borda_score"` — same opaque-passthrough rule).

**Acceptance criteria:**
10. Given `stage1_results = [{"model": "m1", "response": "text"}]` and `model_to_label = {"m1": "Response A"}`, When called, Then the returned first-tuple-element is `[{"model": "Response A", "response": "text"}]` — the `"model"` value is replaced with its label, every other key/value is preserved exactly.
11. Given the same call as AC10, When called, Then the original `stage1_results` list and its dict elements are **not mutated** — `stage1_results[0]["model"]` still equals `"m1"` after the call returns.
12. Given `stage2_results = [{"model": "m2", "ranking": "text", "parsed_ranking": {"a": 1}}]` and `model_to_label = {"m2": "Response B"}`, When called, Then the returned second-tuple-element replaces only `"model"` (to `"Response B"`), leaving `"ranking"`/`"parsed_ranking"` byte-identical, and does not mutate the input.
13. Given `aggregate_rankings = [{"model": "m1", "rank": 1, "average_score": 8.5, "vote_count": 3}]` and `model_to_label = {"m1": "Response A"}`, When called, Then the returned third-tuple-element replaces only `"model"`, preserving `"rank"`/`"average_score"`/`"vote_count"` exactly, and does not mutate the input.
14. Given `aggregate_rankings = None`, When called, Then the returned third-tuple-element is `None` (not `[]`, not a crash).
15. Given a `stage1_results` or `stage2_results` entry whose `"model"` value is **not** a key in `model_to_label` (must never happen given Function A's construction, but must not crash if it does), When called, Then that entry's `"model"` value is left as the original real model string, unchanged (safe fallback, never a `KeyError`).
16. Given `stage1_results = []`, `stage2_results = []`, `aggregate_rankings = []`, and `model_to_label = {}`, When called, Then the result is `([], [], [])` — no crash on fully empty inputs.

### Function C: `_resolve_response_labels`

**Signature:**
```python
def _resolve_response_labels(text: str, model_to_label: dict[str, str]) -> str:
```

**Environment (given, not re-derived):**
- `model_to_label`: same shape as Function A's output/Function B's input
  (`{"<real model slug>": "Response X", ...}`) — this function performs
  the **reverse** substitution (label found in `text` → real model name),
  so it must invert the mapping internally.
- `text` is the chairman's synthesis output — arbitrary natural-language
  text that may contain zero, one, or many occurrences of any label.

**Acceptance criteria:**
17. Given `model_to_label = {"real-model-x": "Response A"}` and `text = "Response A said this."`, When called, Then the result is `"real-model-x said this."` (label replaced with the real model name).
18. Given the same `model_to_label` as AC17 and `text = "Response A agrees with Response A on this point."` (the label appears twice), When called, Then **both** occurrences are replaced — the result contains `"real-model-x"` twice and no remaining occurrence of the literal string `"Response A"`.
19. Given `model_to_label = {"m1": "Response A", "m2": "Response B"}` and `text = "Response A and Response B disagree."`, When called, Then both labels are correctly resolved to their respective real model names (`"m1 and m2 disagree."`) — no cross-contamination (Response A never resolves to m2's name or vice versa).
20. Given `text` containing no occurrence of any label in `model_to_label` (e.g. plain prose with no `"Response X"` substring), When called, Then the result equals `text` unchanged (no-op, no crash).
21. Given `model_to_label = {}`, When called with any `text`, Then the result equals `text` unchanged.
22. Given `model_to_label = {"m1": "Response A", "m2": "Response AA"}` (a longer label that textually contains a shorter one as a prefix — not possible with today's single-letter scheme, but the function must handle it correctly if the label scheme ever grows), When called with `text = "Response AA said X"`, Then the result correctly resolves to `"m2 said X"` — the longer label must not be partially matched/corrupted by the shorter label's replacement running first.
23. Given a `text` and `model_to_label`, When called, Then the function does not mutate `model_to_label` and does not raise for any string input (never crashes on malformed/empty/unicode `text`).

## Wiring (NOT part of this blind contract — done after landing)

Per this repo's established pattern (see `docs/upstream-deltas.md`,
Stage 2/3 debate-resilience entries: "wired into the real call site
immediately after landing, rather than left as a tested-but-unused
function"), `_stage3_query_fn` inside `run_council_with_timeouts` will be
updated, after these three functions pass blind-TDV, to:
1. Compute `model_to_label = _build_stage3_identity_map(stage1_results, stage2_results, label_to_model)` once, before the chairman retry loop.
2. Compute `anon_stage1, anon_stage2, anon_rankings = _anonymize_for_stage3(stage1_results, stage2_results, aggregate_rankings, model_to_label)` once, alongside it.
3. Pass `anon_stage1`/`anon_stage2`/`anon_rankings` (not the real-named originals) into `stage3_synthesize_final`.
4. Apply `_resolve_response_labels(result["response"], model_to_label)` to the returned synthesis text before it becomes `stage3_result["response"]` — the value returned by `run_council_with_timeouts` and written to `synthesis.md`.

This wiring step also requires updating three existing test assertions
that currently pin the old (leaky) behavior as correct — identified during
this session's audit, not part of the blind contract itself:
`tests/test_council_adapter.py::test_ac11_happy_path_returned_shape_matches_pipeline_runners_extraction_paths`
(asserts `stage3_synthesize_final` receives `stage1_results`/
`stage2_results`/`aggregate_rankings` byte-identical to the real-named
originals — must become "receives the anonymized copies" instead),
`tests/test_council_adapter.py::test_single_model_branch_degraded_mode_and_untouched_stage1_5_stage2_usage`
(same pattern for the single-model branch), and
`tests/test_council_adapter_resilient_stage1.py::test_stage3_synthesize_final_receives_correct_aggregate_rankings`
(same pattern, aggregate_rankings only).
