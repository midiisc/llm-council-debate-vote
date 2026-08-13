# Stage 3.75 — devil's-advocate + counterfactual critique (Pillar 2, before code)

Status: ready for implementation. Design already fully decided and grounded
this session (`docs/agent-model-reasoning-config.md` section 6,
`docs/upstream-deltas.md`'s MAD architecture panel entry) — this contract
only turns that decision into code, no new design choices.

## Recap of the already-made decisions

- Runs once, on **Stage 3's synthesis only** (never the 4 raw Stage 1
  drafts — keeps prompt cost and injection surface down, same reasoning
  reasoning-graph-contract.md already applied to Stage 5).
- Executed by **GPT-5.5 only, never Opus-4.8 (the chairman)** — running
  self-critique on one's own synthesis is the literal self-refine pattern
  this project's own cited literature (arXiv:2607.28576) found reliably
  worse than doing nothing; this was the panel's sharpest, most-agreed-on
  finding.
- **Gated**: `CSS < 0.50 OR any model flagged is_outlier` — the outlier
  clause catches the case CSS alone misses (three models tightly agreeing,
  one genuine dissenter); `_compute_outliers` already computes this today,
  previously unused for this purpose.
- Only two techniques survive from the original eight considered:
  devil's-advocate/adversarial-critique + counterfactual/what-if, folded
  into one call.
- Output is a **labeled critique memo attached to the synthesis**, for the
  still-manual Stage 4 premortem to read. It must **never auto-trigger
  re-synthesis** — that would reopen the exact revision/re-solving failure
  mode Stage 2.75's own CSS-gating already exists to avoid.

## Contract 1 — `scripts/critique_round.py` (new module)

**Acceptance criteria:**
1. Given `should_trigger_critique(css: float, is_outlier: dict[str, bool],
   threshold: float = 0.50) -> bool`, When `css < threshold`, Then it
   returns `True` regardless of `is_outlier`'s content.
2. Given the same function, When `css >= threshold` but at least one value
   in `is_outlier` is `True`, Then it still returns `True` - the outlier
   clause is a genuine OR, not shadowed by a CSS check that passed.
3. Given `css >= threshold` and no `is_outlier` value is `True` (or
   `is_outlier` is empty), When called, Then it returns `False`.
4. Given `build_critique_prompt(synthesis_text: str) -> str`, When
   inspected, Then it instructs the model to attack the synthesis via (a)
   devil's-advocate/adversarial critique - actively arguing the strongest
   case against the conclusion - and (b) counterfactual/what-if framing -
   what would have to be true for this conclusion to be wrong - folded
   into one instruction, matching the two-of-eight-surviving-techniques
   decision.
5. Given the same prompt, When inspected, Then it explicitly instructs the
   model that this is a critique memo, not a rewrite - it must never
   produce a replacement synthesis or claim to supersede the original.
6. Given `synthesis_text`, When it's placed in the prompt, Then it is
   wrapped in fixed `--- BEGIN SYNTHESIS ---`/`--- END SYNTHESIS ---`
   delimiters, matching this repo's consistent delimiting discipline for
   any text block placed inside a prompt.
7. Given `CRITIC_MODEL`, When inspected, Then it is the literal string
   `"openai/gpt-5.5"` - a hardcoded choice for this
   role, matching how `EVIDENCE_MODEL`/`COMPLETENESS_CHECK_MODEL` are
   hardcoded in `live_adapters.py` (this project's existing convention for
   a role tied to a specific, deliberately-chosen model, not a
   config-driven seat).
8. Given `run_critique_round(synthesis_text: str, query_fn: QueryModelFn)
   -> CritiqueOutcome` (`QueryModelFn = (model, prompt) -> (text, cost)`,
   matching Stage 2.75/4's existing injected shape), When it runs, Then it
   calls `query_fn(CRITIC_MODEL, build_critique_prompt(synthesis_text))`
   exactly once and returns a `CritiqueOutcome(critique_text: str,
   cost_usd: float, model: str)` dataclass - never raises; an exception
   from `query_fn` is the caller's (`pipeline_runner.py`'s) responsibility
   to catch, matching every other conditional stage's existing division of
   labor.

## Contract 2 — wired into `pipeline_runner.py`

**Acceptance criteria:**
9. Given Stage 3 synthesis and Stage 2.5's `css`/`is_outlier` are already
   computed, When `_run_stages()` reaches the point immediately after
   Stage 3 (before Stage 4), Then it calls `should_trigger_critique(css,
   is_outlier)` and only proceeds to Contract 1's `run_critique_round` when
   it returns `True`.
10. Given the cost ceiling is already met (`config.max_cost_usd is not
    None and cost_so_far >= config.max_cost_usd`), When Stage 3.75 would
    otherwise trigger, Then it is skipped instead, `debug_log` records why,
    and no call is attempted - same idiom as Stage 2.75/4's existing
    pre-flight cost checks.
11. Given the critique round runs successfully, When it completes, Then
    its real cost is added to `cost_so_far` (never hardcoded/discarded),
    the critique memo is persisted to `output_dir/critique_memo.md`
    (best-effort, a write failure logs non-fatally, matching every other
    durable-persistence call site), and `debug_log` records that it ran.
12. Given the critique round doesn't trigger (CSS high, no outlier), When
    `_run_stages()` reaches this point, Then `debug_log` records the skip
    reason explicitly (`"CSS {css:.3f} >= threshold and no outlier"`) -
    never a silent absence.
13. Given new `PipelineResult` fields `critique_triggered: bool = False`,
    `critique_text: Optional[str] = None`,
    `critique_skipped_for_cost: bool = False`, When a run completes, Then
    they reflect what actually happened, matching the existing
    `revision_triggered`/`revision_skipped_for_cost` field pair's naming
    convention.
14. Given `query_fn` raises during the critique call, When caught, Then
    the run still completes successfully (critique failure is never fatal
    to an otherwise-complete pipeline run) - `debug_log` records the
    failure non-fatally, matching Stage 5's own exception-isolation idiom.

## Non-goals

- No auto-triggered re-synthesis from the critique's content - explicitly
  rejected by the original design decision.
- No new query_fn shape - reuses the existing `(model, prompt) -> (text,
  cost)` `query_model` parameter `run_pipeline` already threads through,
  same as Stage 2.75/Stage 4.
- No structured parsing of the critique's content (unlike Stage 2.75's
  citation-marker extraction) - the critique is free text, consumed by a
  human at the manual Stage 4 premortem, not by any downstream code.

## Test strategy

Direct implementation, test-first, hand-verified RED->GREEN, matching this
session's established practice for new-module contracts of this size.
