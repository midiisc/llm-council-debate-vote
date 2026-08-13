# Stage 2/3 debate resilience contract (Pillar 2 — spec before code)

Status: ready for blind-TDV. Extends `docs/specs/debate-resilience-contract.md`
(Stage 1 only) to close the coverage gap found in this session's expert-panel
review (`docs/upstream-deltas.md`, "Debate resilience: retry/backup design
grounding" and the 2026-08-14 panel entry below) and root-caused live: a
stale MCP server process was serving package-default models (`deepseek/
deepseek-v4-pro` + two dead slugs) from a wrong cwd — now fixed at the
registration layer (`.claude/settings.local.json` local-scope MCP entry with
`LLM_COUNCIL_CONFIG` pinned). This spec addresses the separate, real gap the
panel found while investigating: Stage 2 and Stage 3 have **no** retry,
backup, or shortfall-warning coverage at all, unlike Stage 1.

Grounding (direct source read of `llm-council-core==0.40.1`, this session,
paths under `~/.local/share/uv/tools/llm-council-core/lib/python3.13/
site-packages/llm_council/`):

- `council_stages.py::stage2_collect_rankings` (line ~465): `reviewers =
  list(models) if models is not None else list(_get_council_models())`.
  `scripts/council_adapter.py` calls it with no `models=` arg, so reviewers
  are **always** the full static 4-model roster regardless of which models
  actually produced a Stage 1 draft — a model that dropped in Stage 1 is
  still asked to review in Stage 2 today (harmless; reviewing doesn't
  require having drafted). The ranking call itself goes through
  `query_models_parallel` (one attempt, no retry) — a reviewer that times
  out in Stage 2 just silently vanishes from `stage2_results`.
- `council_rankings.py::calculate_aggregate_rankings` (line ~216):
  `num_candidates = len(label_to_model)` and `max_borda = num_candidates -
  1` — **candidate-count normalization is dynamic, not fixed at N=4**
  (confirmed by the function's own docstring: "without it a 3-model council
  and 10-model council produce incomparable scores"). Per-candidate scores
  are accumulated as a list across whatever `stage2_results` actually
  arrived and presumably averaged — also count-adaptive, not assuming a
  fixed reviewer count. **This resolves the panel's flagged open question:
  a dropped Stage 1 or Stage 2 model does NOT silently corrupt the CSS/
  Borda score** — it correctly shrinks the sample, it does not miscount it.
  The real risk is reduced statistical robustness with fewer votes, and the
  total absence of a signal to a human reading the output that this
  happened — not corrupted math.
- `council_stages.py::stage3_synthesize_final` (line ~846): chairman is a
  **single** model (`_get_chairman_model()`, `anthropic/claude-opus-4.8` in
  this project's config) making one synthesis call. Unlike Stage 1/2 there
  is no "pool" to substitute from — and per this project's own composition
  rule (`pipeline-architecture-spec.md` §2: GLM-5.2 and all 3 backup-pool
  models are "never chairman, never tie-breaker"), **no backup model may
  ever stand in as chairman**. The only resilience available for Stage 3 is
  retry-with-backoff on the same chairman model; if that's exhausted, this
  contract requires a loud, explicit failure — never a silent skip of
  synthesis and never a silent substitution of a different model into the
  chairman role.

## Problem this closes

`scripts/resilient_query.py` (Contract: `docs/specs/debate-resilience-
contract.md`) already gives Stage 1 retry-with-backoff + backup-model
substitution + a non-silent `shortfall_warning`. Stage 2 and Stage 3 have
none of that — a single dropped reviewer or an unreachable chairman fails
silently (Stage 2) or fails the whole call (Stage 3), which is the
observed "one or more models don't participate and the call gets re-run by
hand" symptom for any failure that happens after Stage 1.

## Contract A — Stage 2 reviewer resilience

Reuses `query_models_resilient` (unchanged signature) as the query engine
for Stage 2's reviewer calls, replacing `council_stages.stage2_collect_
rankings`'s internal `query_models_parallel` call from the caller's side —
i.e. `scripts/council_adapter.py` builds the ranking prompt and label
mapping itself (already partially reproduced there for Stage 1) and calls
`query_models_resilient` directly for the reviewer round, the same
"reproduce, don't vendor" pattern already used for Stage 1.

**New function: `scripts/council_adapter.py::_collect_rankings_resilient`**

```python
async def _collect_rankings_resilient(
    user_query: str,
    stage1_results: list[dict],
    primary_reviewers: list[str],
    backup_models: list[str],
    timeout: float,
    retry_policy: RetryPolicy,
    minimum_reviewer_count: int,
    already_used_backups: set[str],  # backups consumed by Stage 1 substitution
) -> tuple[list[dict], dict[str, dict], dict[str, int], Optional[str], list[SubstitutionEvent]]:
    ...
```

**Acceptance criteria (Given/When/Then):**

1. Given all 4 configured reviewers return `status="ok"`, When Stage 2
   runs, Then `stage2_results` contains exactly their 4 parsed rankings and
   `shortfall_warning` is `None`.

2. Given one reviewer is confirmed unreachable (retries exhausted) and an
   unused backup exists, When Stage 2 runs, Then the backup is queried as a
   reviewer for that slot with the same ranking prompt, a `SubstitutionEvent`
   is recorded, and the backup's ranking is included in `stage2_results` on
   success.

3. **Backup exclusivity across stages:** Given a backup model already
   substituted into a Stage 1 drafting slot for this call, When Stage 2
   selects a reviewer backup, Then that already-used backup is never
   selected again for a Stage 2 reviewer slot in the same call — the
   `already_used_backups` set threads across both stages of one call, not
   just within Stage 2 (extends AC6 of the Stage 1 contract, which only
   guaranteed exclusivity within a single `query_models_resilient` call).

4. Given the final live reviewer count is strictly less than
   `minimum_reviewer_count` (default: mirrors `debate_resilience.
   minimum_council_size`), When the result is built, Then
   `shortfall_warning` is a non-`None` string naming the live count and
   every unreachable reviewer — mirroring Stage 1's AC7 exactly.

5. Given a reviewer is asked to rank Stage 1 candidates in which it has no
   response of its own (it dropped at Stage 1), When Stage 2 runs, Then
   this is **not** an error condition — the reviewer ranks whatever
   candidates exist; self-vote exclusion logic in `calculate_aggregate_
   rankings` already handles a reviewer having no candidate response to
   exclude a vote from (verified: `_get_exclude_self_votes()` only fires
   when `reviewer_model == author_model`, never on a missing draft).

6. Given `calculate_aggregate_rankings` is called with fewer than 4
   `stage2_results` entries, When aggregate rankings are computed, Then no
   new code in this contract is needed to prevent score corruption — this
   is already correctly count-normalized by the installed package
   (grounding note above) — this contract's job is only to maximize how
   many reviewers actually respond, not to patch the aggregation math.

## Contract B — Stage 3 chairman resilience

**No model substitution — retry only, then loud failure.**

**New function: `scripts/council_adapter.py::_synthesize_resilient`**

```python
async def _synthesize_resilient(
    stage3_query: str,
    stage1_results: list[dict],
    stage2_results: list[dict],
    aggregate_rankings: list[dict],
    chairman_model: str,
    timeout: float,
    retry_policy: RetryPolicy,
) -> tuple[dict, dict[str, int], Optional[VerdictResult], bool]:
    # returns (stage3_result, usage, verdict_result, chairman_degraded)
    ...
```

**Acceptance criteria (Given/When/Then):**

7. Given the chairman model returns `status="ok"` on attempt 1 or any retry
   within `retry_policy.max_attempts`, When Stage 3 runs, Then the
   synthesis proceeds exactly as `stage3_synthesize_final` does today, and
   `chairman_degraded` is `False`.

8. Given the chairman model is confirmed unreachable after exhausting
   `retry_policy`, When Stage 3 runs, Then **no backup model is ever
   substituted as chairman** (hard constraint from `pipeline-architecture-
   spec.md` §2 — GLM-5.2 and all 3 backup-pool models are permanently
   excluded from the chairman role), the pipeline raises a loud, specific
   exception naming the chairman model and the exhausted attempt count
   (never a silent fallback to "return top-ranked response" — that
   behavior is reserved for the explicit `chairman_disabled` config flag,
   a different, deliberate code path, not an implicit failure mode), and
   `debug_log` records the failure per the existing `PipelineResult.
   debug_log` pattern (`docs/upstream-deltas.md`, "No-silent-failure
   hardening").

9. **Visible degraded-mode marker (panel must-address, ws-redteam):**
   Given Stage 2 shipped with a `shortfall_warning` (fewer than
   `minimum_reviewer_count` reviewers responded), When Stage 3 builds its
   final output, Then the synthesis prompt is told about the shortfall (the
   same delimited-note pattern already used for the missing-grounding-tag
   warning, `docs/upstream-deltas.md` "Mandatory, checkable grounding
   tags") **and** `PipelineResult`'s user-facing output carries an explicit
   `degraded: true` / reviewer-count field alongside the synthesis text
   itself — not buried only in `debug_log` or `metadata` where a human
   consuming just the final answer would never see it.

## Non-goals

- No change to Stage 1's existing contract or `resilient_query.py`'s
  signature — Stage 2/3 reuse it as-is via new caller-side wiring in
  `council_adapter.py`, same "reproduce the package's orchestration
  glue with resilience added" pattern already established for Stage 1.
- No enabling of `LLM_COUNCIL_MODEL_INTELLIGENCE` / ADR-026 — out of scope,
  separately decided against by the 2026-08-14 expert panel (dynamic
  model-selection side effect conflicts with the fixed roster).
- No change to the 4-model roster size — separately rejected by the same
  panel (this pipeline's CSS/Borda + single-chairman aggregation has no
  nose-count tie-break failure mode a 5th seat would fix).
- No retroactive fix to `calculate_aggregate_rankings` — grounding above
  confirms it already handles a variable respondent/reviewer count
  correctly; this contract only maximizes how many respond.
- Idempotency/cost-accounting requirement (panel must-address, ws-warden/
  ws-os): any implementation must reuse `resilient_query.py`'s existing
  attempt-tracking so a retried call's usage is counted once, not per
  attempt — this is already how Stage 1's integration handles it
  (`total_usage["stage1"]` sums only `responses.items()`, the final
  successful attempt per model, never every attempt) and Stage 2/3 must
  follow the identical pattern, not reinvent one.
- Privacy note (panel must-address, ws-privacy, **requires explicit user
  sign-off before implementation, not silent inheritance from Stage 1's
  approval**): extending backup-pool substitution into Stage 2 means the
  full anonymized candidate set (richer than Stage 1's single draft) can
  now reach Kimi K3 / Qwen3.8-Max / Grok 4.6 on a reviewer-slot
  substitution. Confirm OpenRouter-only routing still holds for all three
  (already true per `llm_council.yaml`'s `gateways.fallback.chain:
  [openrouter]` — no separate confirmation needed there) and confirm the
  user is comfortable with this exposure before Contract A ships.
