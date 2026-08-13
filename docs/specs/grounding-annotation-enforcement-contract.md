# Grounding-annotation enforcement — spec (Pillar 2, before code)

Status: ready for implementation. Grounding: a real dry run
(`docs/upstream-deltas.md`, 2026-08-13 "Real dry run performed" entry)
found all four Stage 2 peer reviewers penalizing one seat's response for
"leaked internal 'Grounding note/Stage 0.5' scaffolding" — traced to two
root causes: (1) `_STAGE1_REFERENCE_INSTRUCTION_BLOCK` itself names the
internal stage number ("Stage 0.5 grounding"), which one model echoed
verbatim into its visible answer, and (2) the instruction gives no format
guidance, so models improvised — some using a lightweight inline tag
(low peer-review cost), one using a separate labeled header section (high
peer-review cost, penalized by every reviewer). User confirmed: grounding
transparency itself is what makes output trustworthy and must not be
weakened; the fix must be to the *presentation*, not the *substance*, and
the requirement should be *strict* — every model, every claim, every
time — with a missing grounding note treated as something to flag, not
silently accept.

## Non-goals

- **Cannot touch Stage 2's actual rubric/scoring prompt.** Confirmed
  multiple times this session: `stage2_collect_rankings` is called with the
  raw, unwrapped `user_query`; no repo-owned prompt reaches it. Forking
  upstream internals to fix its scoring bias directly is out of scope, as
  already decided for the same reason in
  `docs/specs/quantitative-evidence-weighting-contract.md`.
- **Not weakening the grounding requirement.** The fix is presentation-only
  (drop the internal stage-name leak, mandate a compact, exact tag format)
  — the underlying "label every substantive claim, never fabricate a
  source" rule is unchanged, in fact tightened from "note what grounds it"
  to a mandatory, exact, checkable tag.

## Contract 1 — mandatory, checkable, presentation-neutral grounding tags

**File**: `scripts/council_adapter.py`, `_STAGE1_REFERENCE_INSTRUCTION_BLOCK`.

**Acceptance criteria:**
1. Given `build_stage1_prompt`'s output is inspected, When the grounding
   instruction is read, Then it requires an exact tag immediately after
   every substantive claim, from a fixed three-value vocabulary:
   `[grounded: document]`, `[grounded: verified]`, `[unverified]` — no
   claim may be left untagged.
2. Given the same instruction, When inspected, Then it explicitly forbids
   a separate labeled section for grounding notes and forbids naming this
   process's internal stage numbers/names in the visible answer — both are
   implementation details, not part of the deliverable.
3. Given the same instruction, When inspected, Then the existing
   anti-fabrication guarantee is preserved verbatim in substance: never
   present unverified knowledge as a citable reference, never fabricate a
   source to avoid `[unverified]`.
4. Given the instruction previously named "Stage 0.5" internally, When the
   new instruction is inspected, Then no internal stage name or number
   appears in the instruction text itself (the leak's actual root cause -
   a model can't echo terminology it was never given).

## Contract 2 — detect and surface a response with no grounding tags at all

**File**: `scripts/council_adapter.py` (detection + threading into Stage 3),
`scripts/pipeline_runner.py` (debug_log surfacing).

**Objective**: "strict, and missing it should be questioned" - a Stage 1
response with zero grounding tags anywhere must never pass through
silently. Pure-Python detection (no new model call, no new cost).

**Acceptance criteria:**
5. Given a new `has_grounding_annotations(response_text) -> bool` function,
   When called on text containing at least one of the three exact tags,
   Then it returns `True`; When called on text with none, `False`.
6. Given `run_council_with_timeouts` completes Stage 1, When any
   `stage1_results` entry's response fails `has_grounding_annotations`,
   Then that model's identity is collected into `metadata["ungrounded_models"]`
   (list) - key present only when non-empty, mirroring the existing
   `substitutions`/`shortfall_warning` optional-key convention.
7. Given `ungrounded_models` is non-empty, When Stage 3's `stage3_query` is
   built, Then it includes an additional delimited section naming which
   model(s) produced no grounding tags and instructing the chairman to
   weigh that explicitly during synthesis - the chairman actually
   "questions" the missing annotation within the debate itself, not just
   in a log a human might never read.
8. Given `ungrounded_models` is empty, When `stage3_query` is built, Then
   it is unchanged from today's shape (backward compatible, additive only).
9. Given `pipeline_runner.py` reads `metadata.get("ungrounded_models")`,
   When non-empty, Then each model is appended as its own `WARNING:` line
   in `debug_log` - loud, matching how `shortfall_warning` is already
   surfaced, never silently dropped.

## Test strategy

Direct implementation, test-first, hand-verified RED->GREEN, matching this
session's established practice for prompt/detection changes of this size.
