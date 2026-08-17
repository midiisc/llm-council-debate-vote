# Core-seat swap contract: GLM-5.2 → Kimi K3 (Pillar 2 — spec before code)

Status: **spec only, not implemented, not gated-clear yet.** This is a
config-level change (`llm_council.yaml` + two doc tables), not new code —
but per Pillar 2 it still gets a spec because it changes council
composition, which this repo treats as a first-class decision (see
`pipeline-architecture-spec.md` §2's explicit "a proven 3 beats a padded,
unproven 4 — the user's call, not code's").

## Why a swap, not an add (scope correction from the original ask)

The user's original request was "spec a 5th core seat." That request
directly conflicts with a **unanimous** 2026-08-13 4-judge panel finding,
independently reaffirmed 2026-08-14:

- `docs/agent-model-reasoning-config.md` §1: *"Never add a 5th standing
  seat."* Confirmed unanimous — 4→5 seats pushes Stage 2's O(N²)
  review-pair count from 6→10 (+67%) for a benchmark-literature marginal
  gain of ~1 accuracy point at that point on the diminishing-returns
  curve.
- `docs/specs/stage2-3-debate-resilience-contract.md`, Non-goals: *"No
  change to the 4-model roster size — separately rejected by the same
  panel."*

Surfaced to the user 2026-08-17; user chose **swap instead of add**
(replace the still-provisional 4th seat rather than growing to 5). This
spec implements that choice. It does not reopen or override the panel's
5-seat rejection — it stays at exactly 4 core seats, so the O(N²) Stage 2
cost the panel rejected never triggers.

## Why GLM-5.2 is the seat being replaced

`council.models`' 4th seat (`z-ai/glm-5.2`) was never a fully graduated
member — `pipeline-architecture-spec.md` §2: *"GLM-5.2 still needs its
20-session ADR-029 graduation bar."* Live-checked this session:
`council-runs/scorecard.jsonl` has **3 total logged sessions** across the
project's whole lifetime, nowhere near 20. GLM-5.2 is provisional, not
proven — swapping a provisional seat carries materially less risk than
swapping a graduated one, and `pipeline-architecture-spec.md` §2 already
named this exact scenario as the trigger for reconsidering the seat:
*"if it doesn't clear it, this panel's finding — that Kimi K3 is the
strongest diversity-maximizing alternative — is the grounded starting
point for that follow-up decision, not a fresh unguided search."*

## Why Kimi K3, not Qwen3.8-Max or Grok-4.6

The 2026-08-12 diversity panel (`pipeline-architecture-spec.md` §2, "4th-seat
diversity panel") already ranked all three backup-pool candidates on the
one axis this swap is *for* — training-methodology diversity against the 3
incumbent RLHF-based core seats:

| Candidate | Panel's diversity finding | Verdict for this swap |
|---|---|---|
| **Kimi K3** | Only candidate with a genuinely different post-training topology — self-critique rubric-reward loop, no RLHF, 9 task-expert models distilled into one. | **Only real diversity win available.** |
| Qwen3.8-Max | "Methodologically redundant with GLM (both RLVR-primary + late human-preference stage)." | Swapping GLM→Qwen changes nothing on the axis this swap exists for — same methodology cluster in, same cluster out. |
| Grok-4.6 | Ranked *last* on diversity — RLHF/RLAIF pipeline, "structurally the same paradigm as the 3 incumbent seats." | Would **reduce** diversity relative to keeping GLM (RLVR-primary) — wrong direction for this swap's purpose. |

Kimi K3 is therefore the only candidate that makes this swap worth doing at
all. This is a reproduction of already-grounded panel reasoning, not fresh
model shopping — consistent with the "grounded starting point, not a fresh
unguided search" instruction above.

## Blocking precondition — reasonably satisfied 2026-08-17 (with a caveat)

Originally logged live 2026-08-17 in `docs/upstream-deltas.md`
("Model-provider operational finding"): Kimi K3 was reported
**upstream capacity-constrained at Moonshot AI** — OpenRouter showed a
live capacity warning ("limited capacity, slower responses") with
intermittent 429s. That finding is why Kimi sat in the backup pool rather
than already being a core seat; moving it to a core (always-invoked,
every single run) seat while that held would import exactly the
reliability risk this repo's Resource & Stability Gate exists to prevent.

**Resolution, same day:** two follow-up WebFetch re-checks of the
OpenRouter page came back inconclusive (client-side-rendered
capacity/uptime data not visible to a static fetch — documented in
`upstream-deltas.md`). User then confirmed a real, cheap, low-stakes live
API test call: direct execution of `llm_council.openrouter.
query_model_with_status(model="moonshotai/kimi-k3", ...)` — the same
function the live pipeline uses — returned `status: "ok"`, `latency_ms:
5612`, no 429, cost `$0.001005`. This is direct-execution evidence (this
repo's own gold-standard verification method) that Kimi K3 is reachable
and responsive right now.

**Caveat carried forward, not glossed over:** that was one low-load
sample, not a sustained/concurrent load test — a real pipeline run queries
4 models together with longer debate prompts, which this single short
call didn't reproduce. The precondition is reasonably satisfied for
proceeding, but the Pillar 6 dry run in Rollout step 4 is the next real
signal on under-load behavior, not a redundant formality.

## Contract — config surface touched

**Objective:** given the swap is cleared per the precondition above, every
place `z-ai/glm-5.2` appears in the active (non-inert) config surface is
replaced with `moonshotai/kimi-k3`, and every place GLM-5.2's provisional
status is documented is updated to describe Kimi K3's status instead — no
stale reference left claiming GLM-5.2 holds the seat.

**Files and exact locations (grounded by direct read this session):**

1. `llm_council.yaml`:
   - `council.council.models` (top-level flat list, the one
     `council_health_check` reads) — swap the 4th entry.
   - `council.tiers.pools.high.models` — swap.
   - `council.tiers.pools.balanced.models` — swap.
   - `council.tiers.pools.reasoning.models` — swap (this is
     `tiers.default`, the pool actually resolved by `consult_council()` —
     the one that matters most, per the two `load_config()`/tier-contract
     bugs already documented in `upstream-deltas.md`).
   - `debate_resilience.backup_models` — remove `moonshotai/kimi-k3` from
     rank 1 (now a core seat, no longer backup-eligible for itself); list
     becomes `[qwen/qwen3.8-max, x-ai/grok-4.6]`, ranks renumbered 1/2.
     `minimum_council_size` stays `4` — the floor is about live model
     count, not about which 4.
   - `chairman: anthropic/claude-opus-4.8` — **unchanged.** Kimi K3
     inherits GLM-5.2's exact restriction: Stage 1 + Stage 2 only, never
     chairman, never Stage 3.75 critic (`pipeline-architecture-spec.md`
     §2's rule was never GLM-specific — it's "the 4th seat," and this spec
     doesn't touch that rule, only who occupies the seat).

5. **`scripts/council_adapter.py`** (real code surface, found while
   implementing — not in the original spec draft, added per Fix-on-Sight):
   - `_STAGE1_REASONING_EFFORT` dict (line ~630): the `"z-ai/glm-5.2":
     "medium"` entry is replaced with `"moonshotai/kimi-k3": "low"` — per
     config surface #3's resolved grounding, `"medium"` isn't in Kimi's
     `supported_efforts` at all.
   - `_STAGE1_WEB_SEARCH_ENABLED_MODELS` (line ~643): no set-membership
     change needed — Kimi K3 was never a candidate for this set any more
     than GLM-5.2 was. But the exclusion **reason** must transfer
     correctly: `stage1-web-search-contract.md` excludes GLM-5.2 because
     live `/api/v1/models` pricing has no `web_search` field for it (no
     native search engine). **Live-checked this session**: `moonshotai/
     kimi-k3`'s pricing block also has no `web_search` field (`{"prompt":
     ..., "completion": ..., "input_cache_read": ...}` — same 3 keys as
     GLM-5.2, missing the `web_search` key present on all 3 of the other
     core seats). Same technical limitation, same exclusion, correctly
     inherited — but the code comment naming "z-ai/glm-5.2" explicitly as
     the permanently-excluded model must be updated so it doesn't read as
     a stale reference to an absent model once the swap lands; it should
     state the exclusion is per-model-capability (no native `web_search`
     pricing), re-verified for whichever model holds the 4th seat, not a
     GLM-specific carve-out.

2. `docs/agent-model-reasoning-config.md` §1 (roster table): replace the
   GLM-5.2 row with Kimi K3's live-verified slug/context/pricing (already
   grounded, §2 of that same file: `moonshotai/kimi-k3`, 1,048,576 ctx,
   $3.00/$15.00 per M). Role column: `"Stage 1 + Stage 2 only. Never
   chairman, never Stage 3.75 critic. Fresh seat — 0 ADR-029 sessions,
   starts SHADOW, same 20-session bar GLM-5.2 never cleared."` §2's backup
   table drops Kimi K3, promoting Qwen3.8-Max to rank 1 and Grok-4.6 to
   rank 2.

3. `docs/agent-model-reasoning-config.md` §3 (reasoning-effort wiring):
   **resolved, live-verified 2026-08-17** (`docs/upstream-deltas.md`,
   "reasoning-effort grounding item resolved" entry). `moonshotai/kimi-k3`
   DOES support `reasoning_effort`, but its scale is `["max", "high",
   "low"]` — **no `"medium"` tier**, unlike GLM-5.2's `medium` wiring
   (line 102). Its default, if left unset, is `"max"` — the most
   expensive/slowest tier, contradicting this project's deliberate
   cost-tier posture for a non-graduated seat. Contract 4's per-round
   table must set this seat's entry to **`"low"`** explicitly (nearest
   available match to GLM's cost-conscious intent) — never leave it
   unset, or it silently defaults to `max`.

4. `scripts/audition_tracking.py` (Contract 5, already-shipped machinery,
   no new code needed): `record_session_for_all_models` is driven by
   `council_models` from live config — once `llm_council.yaml` lists
   `moonshotai/kimi-k3` instead of `z-ai/glm-5.2`, `get_or_init_status`
   naturally starts it at a fresh `AuditionState.SHADOW` with 0 sessions
   (same cold-start GLM-5.2 got on 2026-08-12) the next time a real
   council run executes. Per Contract 5's explicit non-goal, this state is
   observational only — it does not auto-promote or auto-revert the swap.

## Acceptance criteria (Given/When/Then)

1. Given the blocking precondition has cleared (fresh live capacity
   re-check, dated/sourced in `upstream-deltas.md`), When the config edit
   is applied, Then `moonshotai/kimi-k3` appears in exactly the 4 config
   locations listed above and `z-ai/glm-5.2` appears in none of them.

2. Given the edit is applied, When `council_health_check` runs, Then
   `council_size` still reports `4` and `ready: true` — this is a
   same-size substitution, never a size change (the O(N²)-cost concern
   the panel rejected must never regress via this path).

3. Given the edit is applied, When `consult_council()` resolves
   `TierContract.allowed_models` for the `reasoning` tier (the active
   default per `tiers.default`), Then the resolved list contains
   `moonshotai/kimi-k3` and not `z-ai/glm-5.2` — verified by direct
   execution of `create_tier_contract('reasoning')`, the same verification
   method already used to catch the two `load_config()` bugs in
   `upstream-deltas.md` (never trust the flat `council.models` list alone
   to prove the swap took effect — that was the exact silent-failure mode
   those bugs produced).

4. Given a real council run executes post-swap, When Stage 3 synthesizes,
   Then `_get_chairman_model()` still resolves to `anthropic/claude-opus-4.8`
   — the swap must not touch chairman resolution (regression check against
   `pipeline-architecture-spec.md` §2's chairman-exclusion rule, which this
   spec explicitly carries forward for the new occupant).

5. Given a real council run executes post-swap, When
   `record_session_for_all_models` runs, Then `moonshotai/kimi-k3` gets an
   `audition.jsonl` entry with `state=SHADOW` (or its natural successor
   state after `evaluate_state_transition`) and `z-ai/glm-5.2` gets no new
   entries — its historical 3 sessions remain in `scorecard.jsonl`/
   `audition.jsonl` untouched (this spec never deletes history, only stops
   adding to it).

6. **Resolved 2026-08-17** (was open at spec-authoring time — see
   `upstream-deltas.md`'s "reasoning-effort grounding item resolved"
   entry). Given Stage 1 builds its per-model request for
   `moonshotai/kimi-k3`, When the reasoning-effort parameter is set, Then
   it is explicitly `"low"` — never left unset (which would silently
   default to `"max"`, live-confirmed) and never `"medium"` (not in this
   model's `supported_efforts: ["max", "high", "low"]`, unlike GLM-5.2's
   wiring). Contract 4's per-round table must carry this exact value.

## Non-goals

- No change to council size (stays 4) — this is the entire point of
  choosing swap over add; re-litigating the panel's 5-seat rejection is
  explicitly out of scope here.
- No change to `chairman`, `chairman_disabled`, `synthesis_mode`,
  `exclude_self_votes`, or `style_normalization` — only the 4th seat's
  occupant changes.
- No automated capacity-monitoring code for Kimi K3 (e.g. a scheduled
  check that gates the swap automatically). The blocking precondition
  above is a **manual, dated, Pillar-1-grounded re-verification** — the
  same pattern this repo already uses for the Pillar 6 Real-Money dry-run
  gate — not a new automated system. Building automated capacity-polling
  is a materially bigger, separately-scoped feature and isn't justified by
  this one swap.
- No retroactive edit to GLM-5.2's historical `scorecard.jsonl`/
  `audition.jsonl` records — those stay as an accurate record that GLM-5.2
  held the seat for 3 sessions and never graduated, not erased.
- No change to `debate_resilience.minimum_council_size` (stays `4`) —
  unrelated to which models fill the 4 seats.
- Does not resolve whether Qwen3.8-Max or Grok-4.6 should replace anything
  — both stay in the backup pool, re-ranked but otherwise untouched.

## Rollout — complete, 2026-08-17

1. ✅ Live capacity re-check of `moonshotai/kimi-k3` — two inconclusive
   WebFetch passes, then a real user-confirmed API test call
   (`status=ok`, `latency_ms=5612`, `$0.001`) settled it.
2. ✅ Reasoning-effort grounding item resolved (`"low"`, live-verified via
   raw `/api/v1/models` JSON).
3. ✅ Applied: 4 `llm_council.yaml` edits (`council.models`,
   `tiers.pools.high/balanced/reasoning`, `debate_resilience.backup_models`
   reordered), 2 doc tables (`agent-model-reasoning-config.md` §1/§2), plus
   a real code surface found while implementing —
   `scripts/council_adapter.py`'s `_STAGE1_REASONING_EFFORT` dict (now
   `"moonshotai/kimi-k3": "low"`) and the `_STAGE1_WEB_SEARCH_ENABLED_MODELS`
   exclusion comment (re-verified: Kimi K3 has no native `web_search`
   pricing either, same as GLM-5.2 — exclusion transfers correctly). 4
   pinned tests updated to match (`test_config_integrity.py`,
   `test_council_adapter_resilient_stage1.py`,
   `test_reasoning_effort_stage1_contract.py`,
   `test_web_search_stage1_contract.py`) — full suite: 871 passed. Not yet
   committed to git (working tree only, per this repo's "only commit when
   asked" norm).
4. ✅ Pillar 6 Real-Money gate: two real dry runs against the same
   low-stakes test decision used in prior Pillar 6 dry runs. First attempt
   ($0.515) hit a self-inflicted 300s `--max-wall-clock-seconds` ceiling
   (my own parameter, not a Kimi finding) after Stage 1+2 completed
   successfully with all 4 models responding. Second attempt, correct
   defaults: **complete success, $0.6028 total cost**, CSS=0.685, Stage 3
   synthesis produced by the chairman, no shortfall, no substitution
   needed. Kimi K3 was ranked last by peer review on this particular query
   but the chairman explicitly validated its dissenting position and its
   most concrete technical contribution (a package-discovery risk check)
   in the final synthesis — real evidence the debate architecture's
   "minority report" design intent works with this seat. This closes the
   "one low-load sample, not a load test" caveat from the capacity
   re-check: Stage 1 exercised all 4 models concurrently, for real,
   successfully.
5. ✅ Recorded in `docs/upstream-deltas.md` — see the 2026-08-17 entries
   (capacity re-check, reasoning-effort resolution, full dry-run cost
   summary).
