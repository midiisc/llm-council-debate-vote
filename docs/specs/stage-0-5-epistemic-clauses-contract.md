# Stage 0.5 epistemic clause additions — spec (Pillar 2, before code)

Status: ready for implementation. Grounding:
`docs/stage-0-5-epistemic-clauses-decision-2026-08-13.md` (12-agent sweep +
adversarial panel; every adopted clause's exact wording is taken verbatim
from that memo's tightened `domain_neutral_wording`, adapted only in
*format* — from the memo's markdown blockquote style to the flowing-prose
style `_EVIDENCE_WEIGHTING_INSTRUCTION_BLOCK`'s existing 4 clauses already
use — never in *meaning*).

## Problem this closes

The four clauses shipped this session (specific-over-vague with inverted
polarity, aggregated/surveyed evidence, revealed action over stated
opinion, cross-domain corroboration) all score a piece of evidence's
*support* for a claim in isolation. None asks whether the evidence would
look different if the claim were false (diagnosticity), whether a
low-cost/self-serving statement should be trusted less than a costly/
against-interest one, whether a real proxy measurement actually bears on
the different outcome it's cited to support, or whether multiple agreeing
sources were produced by genuinely different methods versus being reprints
of one report. The panel's adversarial review surfaced these as real,
distinct epistemic gaps, not restatements of the existing four.

## Non-goals (explicit, per the decision memo)

- **Base-rate/reference-class anchoring** and **absence-of-expected-signal**
  are explicitly NOT part of this contract — both passed individual
  adversarial review but were declined at synthesis (reopen the exact
  fabrication-risk profile clause 1 already manages, for a signal that
  fires rarely inside a single-search-call architecture). Not built.
- **No new call, no new retrieval.** All four additions are prompt-text
  only, inside the existing single real-`:online`-search-call-per-claim
  architecture. No new function signature, no new field on `Evidence`.
- **No relaxation of any existing clause.** All four additions are
  strictly additive to `_EVIDENCE_WEIGHTING_INSTRUCTION_BLOCK` — the
  existing four clauses' text is unchanged, byte-for-byte.
- Production-method diversity ships **method-diversity only** — the
  candidate's second sub-check ("a mechanical byproduct like a filing or
  log outranks a persuasive statement") is explicitly dropped per the
  decision memo (narrower restatement of the existing revealed-action
  clause; shipping both risks double-counting one piece of evidence under
  two clause labels).

## Contract — four new clauses, one `build_evidence_prompt` change

**File**: `scripts/live_adapters.py`, `_EVIDENCE_WEIGHTING_INSTRUCTION_BLOCK`.

**Objective**: append four new clauses (5-8) to the existing string
constant, each carrying its own explicit anti-fabrication guardrail that
defaults to the block's existing `"unverifiable"` fallback whenever the
required grounding isn't present in the retrieved source — no new fail-open
path anywhere.

**Acceptance criteria — Clause 5, Diagnosticity:**
1. Given `build_evidence_prompt(claim)` is called, When the returned prompt
   is inspected, Then it instructs the model to judge a found source by
   whether it would be unlikely to exist if the claim were false (or under
   the claim's own negation/explicitly-named rival), not merely by whether
   it seems supportive on its own.
2. Given the same prompt, When inspected, Then it explicitly covers the
   case of a real, dated, cited source that addresses a different,
   similar-sounding claim rather than the one actually being evaluated —
   instructing that such a source contributes nothing to the claim at hand.
3. Given the same prompt, When inspected, Then it forbids inventing a new
   alternative to test against (only the claim's own negation or an
   explicitly-named rival), and forbids asserting a source distinguishes
   the claim from its alternative unless the source's own stated content
   actually does so (an inferred/assumed distinction does not count) —
   defaulting to `"unverifiable"` when no source addresses the actual claim.

**Acceptance criteria — Clause 6, Cost-to-fake / against-interest weighting:**
4. Given the prompt is inspected, Then it instructs weighting a source more
   heavily when it explicitly discloses that a statement or action was
   costly, risky, or against the stating party's own apparent interest (a
   named penalty, a disclosed conflict of interest, a stated resource
   commitment, or a self-undermining concession) than an equivalent
   statement/action with no such disclosed cost.
5. Given the same prompt, When inspected, Then it restricts this clause to
   only fire when the cost/risk/against-interest nature is **explicitly
   stated in the source itself** — forbidding the model from estimating a
   cost, inferring risk, or guessing at incentive from general knowledge;
   absent explicit disclosure, the clause is inert and the finding is
   scored on the other clauses alone.

**Acceptance criteria — Clause 7, Proxy validity:**
6. Given the prompt is inspected, Then it instructs that a precise,
   well-sourced number for a continuously-observable stand-in measurement
   (a count, index, volume, or rate) does NOT by itself establish that it
   predicts a separate, not-yet-confirmed outcome it's cited to support.
7. Given the same prompt, When inspected, Then it requires the source
   itself to state or cite an established relationship between that
   specific measurement and that specific outcome before the link is
   trusted — forbidding the model from inventing a predictive relationship,
   correlation, or lead-time the source doesn't state; absent that
   grounding, the finding is unverified for the outcome cited, even though
   the underlying number is itself real and dated.

**Acceptance criteria — Clause 8, Production-method diversity:**
8. Given the prompt is inspected, Then it instructs treating agreement
   between multiple real, cited sources as stronger when those sources were
   produced by genuinely different methods (e.g. a recorded transaction, an
   independent survey, a firsthand account, a direct measurement) than when
   they were produced the same way or turn out to be one report reprinted
   across multiple outlets.
9. Given the same prompt, When inspected, Then it restricts this clause to
   only fire when each source's production method is actually stated or
   evident from the source itself — forbidding the model from inferring,
   assuming, or guessing an unstated method, and explicitly forbidding
   treating reprints/copies of one underlying report as independent methods
   — falling back to plain per-source treatment (no diversity bonus) when
   methods can't be verified as both real and different.

**Cross-cutting acceptance criteria (apply to all four new clauses):**
10. Given the four new clauses are appended, When `build_evidence_prompt`'s
    output is inspected, Then the existing four clauses' text (specific-
    over-vague, aggregated/surveyed, revealed-action, cross-domain) is
    present unchanged, byte-for-byte, and the new clauses appear after them
    (still inside `_EVIDENCE_WEIGHTING_INSTRUCTION_BLOCK`, still outside the
    `_CLAIM_SECTION_BEGIN`/`_CLAIM_SECTION_END` delimiters).
11. Given each new clause, When its wording is inspected, Then it names no
    subject-matter category (no specific industry, company, or research
    field) — domain-neutral by construction, matching the existing four.
12. Given each new clause's guardrail, When triggered by a source lacking
    the required explicit grounding (no stated cost, no stated predictive
    link, no stated/evident production method, no source addressing the
    actual claim), Then the fallback is always `"unverifiable"` or "score on
    the other clauses alone" — never a new path to `"supports"`/
    `"contradicts"` without the grounding the guardrail requires.

## Test strategy

Direct implementation, test-first, hand-verified RED→GREEN (not isolated
blind-TDV dispatch) — same rationale as the quantitative-evidence-weighting
contract: this is a bounded, prompt-text-only change with no new control
flow, and this project's own documented experience with batch-dispatch
non-delivery makes direct implementation the more reliable choice for a
change of this shape. Every new test run and its failure/pass genuinely
observed. Mutation testing on the changed surface (`live_adapters.py`)
after implementation, scoped `only_mutate`, results hand-verified per this
project's own documented mutmut coverage-detection unreliability finding.
