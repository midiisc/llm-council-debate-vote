# Weighting Quantitative Evidence Higher Than Qualitative — Decision (2026-08-13)

User proposed weighting published quantitative industry data (surveys,
consulting-firm forecasts, executive statements, market data) higher than
qualitative claims throughout the debate-vote pipeline, under a hard
zero-hallucination/retrieval-only constraint. Run as a literature-research +
mechanism-research pass feeding four adversarial judges (forecasting/
epistemics, engineering feasibility, red-team/hallucination-risk,
domain-neutrality/architecture-fit). **Unanimous verdict, all four lenses
independently: `adopt-with-modification`.** The literal proposal — "quantitative
is inherently more reliable than qualitative" — does not survive contact
with either the forecasting literature or this pipeline's own fabrication
surface, and ships in a materially narrower, source-agnostic,
verification-gated form, at exactly two stages, with guardrails that must
land in the same PR, not as a follow-up.

## 1. The correctly-calibrated claim

The unqualified claim is rejected as stated — it is a category error, not
merely an overstatement. What the calibration literature (Meehl's clinical-
vs-actuarial line; Tetlock/GJP) actually supports is that **disciplined,
quantified reasoning process** (explicit probabilities, base rates,
reference-class forecasting, formal aggregation) beats **vague, unstructured
judgment**. That is an axis about *rigor of process*, not about the *surface
form* — numeric vs. narrative — of an externally cited source. Smuggling
Meehl/Tetlock in to justify "prefer statistics from consulting firms" is
itself a motivated misreading.

Independently, the track record of the exact category the proposal wants to
privilege is mixed-to-poor: Gartner faces documented pay-to-play incentive
concerns and Magic Quadrant methodology critiques (including a 2014 lawsuit);
McKinsey has no published accuracy audit for its public predictions; sell-side
analysts show systematic optimism bias driven by underwriting conflicts; and
IMF/macro forecasters hit near-chance rates on the exact "future industry
trend" genre this proposal targets (4 of 469 downturns predicted across 30
years). A citation being genuinely real and quantitative does not make the
underlying prediction accurate — a distinct failure mode from fabrication,
and the naive version of this proposal addresses neither.

**Calibrated version, to carry into the spec verbatim:**

> Quantitative evidence earns *higher* weight than qualitative evidence only
> when it is (a) session-verified against a live, dated, resolvable source —
> not merely asserted by a model — (b) from a source whose incentive
> structure and methodology are reasonably known, and (c) within that
> source's demonstrated competence (near-term/narrow claims, not long-range
> macro predictions, where even top-tier institutional forecasters perform
> near chance). Absent verification, a model-produced "statistic" defaults to
> **lower** trust than a hedged, transparent qualitative claim — not higher —
> because it carries fabrication risk on top of ordinary uncertainty.

This inverts the naive reading's default polarity and is the load-bearing
correction every downstream mechanism below must encode.

## 2. Where this is implemented, and why not elsewhere

Two stages, and two only. All four judges converged on the same call graph
reading independently.

**Adopted:**
- **Stage 0.5** — `scripts/live_adapters.py::build_evidence_prompt`
  (lines 142-159). The earliest and highest-leverage point: it shapes what
  the `:online` retrieval model looks for, not just how later text is read.
  This is also the highest-*risk* point (see §5) and must not ship without
  its guardrails in the same change.
- **Stage 2.75** — `scripts/revision_round.py::build_revision_prompt`
  (lines 130-159). Operates on evidence that has already passed the Stage
  0.5 gate, so a weighting clause here changes how already-processed facts
  are weighed against ungated Stage-1 assertions — no new fabrication
  surface of its own. This is the safer of the two and the one to lean on
  if only one insertion point is wanted.

**Explicitly NOT this round, and why:**
- **Stage 2** (`stage2_collect_rankings`, `council_adapter.py:292-294`) —
  architecturally unreachable. It is called with the raw, unwrapped
  `user_query`, not any repo-built prompt, so no instruction placed anywhere
  in this repo reaches it directly. Reaching it would require either
  forking upstream `llm_council.council_stages` internals or mutating the
  shared `user_query` every downstream stage sees — the latter is scope
  creep with its own blast radius, not a targeted fix.
- **Stage 1** (`council_adapter.py::build_stage1_prompt`,
  `_STAGE1_REFERENCE_INSTRUCTION_BLOCK`, lines 81-100) — a real lever
  technically, but low-leverage: Stage 2 only sees Stage 1's text passively,
  through peer-reviewed prose, never as an enforced rule, per this
  pipeline's own documented Proposal A non-goal. Not worth the added
  surface area for what it buys.
- **Stage 3** (`stage3_query` string build, `council_adapter.py:301-304`) —
  same limitation as Stage 1: can only carry text concatenated into an
  opaque call's string argument, no enforcement possible. Optional
  reinforcement only, not a primary owner.
- **Stage 0, Stage 3.75, Stage 4** — out of scope entirely; none of these
  touch evidence retrieval or evidence weighing, and Stage 0/Stage 4 stay
  manual by this project's own Pillar 6 design (pre-registration and
  premortem are deliberately not automated).

## 3. The exact mechanism

Two concrete changes, both prompt-level, one optional schema extension:

**3a. Stage 0.5 — `build_evidence_prompt` addition.** Append a clause
instructing the retrieval model to prefer sourced, dated, verifiable
specifics over vague, unsourced assertions **when a real one is actually
found** — paired immediately with the anti-fabrication clause from §5
(these two must ship as one atomic instruction block, not two separate
edits, because the preference clause alone is what creates fabrication
pressure).

**3b. Stage 2.75 — `build_revision_prompt` addition.** Extend the existing
single-source caveat ("Single-source research findings... weigh accordingly,
do not treat as infallible," lines 148-151) with a second sentence: findings
that are dated, sourced, and specific should be weighed more heavily than
vague or unsourced ones **among facts that already passed Stage 0.5's
verification gate** — i.e., this operates only on the `verified_facts` list
that has already survived retrieval, never as a blanket "numbers > words"
instruction over raw model output.

**3c. Optional, deferred: `Evidence` schema extension.** Today
`grounding_pass.py:23-27` defines `Evidence(source: str, date: str, supports:
bool)` — no field distinguishes "34% YoY, McKinsey State of Market 2026,
dated, resolvable URL" from "the market is broadly seen as growing." A
structured `is_quantitative`/specificity flag, populated by the evidence
model's own JSON response and validated by `parse_evidence_response`
(`live_adapters.py:162-188`), would let weighting operate on real structured
signal instead of delegating the judgment to the downstream reading model.
This is a genuine multi-file change (dataclass, prompt's requested JSON
keys, parser, `_build_facts_section` in `revision_round.py:70-127`) and is
**not required to ship the initial version** — the prose-only mechanism in
3a/3b is buildable today with zero schema change. If pursued, it goes
through this project's own Pillar 2/3 gate (spec + blind-TDV + mutation
gate) as its own unit, per §5's requirement that the flag carry a *higher*
evidentiary bar than a plain tag.

## 4. Mandatory guardrails against fabrication risk

None of the following exist in the codebase today (confirmed by direct
code read in the mechanism report) and **all must ship in the same PR** as
§3a/3b, not as a follow-up — shipping the weighting preference without them
makes hallucination risk go up, which is the opposite of this repo's stated
requirement.

1. **Explicit anti-fabrication clause in `build_evidence_prompt`.** Add
   language mirroring Stage 1's existing instruction at
   `council_adapter.py:89-90` ("never fabricate a source") — currently
   absent from the Stage 0.5 evidence prompt entirely. Exact behavior
   required: if the model's web search does not turn up a real, checkable
   source for a quantitative claim, it must return the claim tagged
   `unverifiable`, never invent a plausible-sounding report/survey/firm
   name to satisfy the "prefer quantitative" instruction.

2. **URL-reachability check before VERIFIED/CONTRADICTED status.** Today
   `parse_evidence_response` (`live_adapters.py:162-188`) accepts any
   non-empty string as `source` — a vague blog title and a fabricated
   "McKinsey 2026 State of the Market Report" pass identically. Add a
   resolvability check (HTTP HEAD/GET or equivalent) on the `source` field
   either inside `parse_evidence_response` or as post-processing in
   `real_fetch_evidence` (`live_adapters.py:191-230`), before `tag_claim`
   (`grounding_pass.py:53-65`) is allowed to mark a claim `supports=True`/
   `False` rather than falling through to `UNVERIFIABLE`. An unresolvable
   URL must force `UNVERIFIABLE` regardless of how specific or numeric the
   claim text reads.

3. **Higher evidentiary bar for any structured quantitative flag.** If §3c's
   `is_quantitative` field is ever added, it must require a resolved URL as
   a precondition — not merely the presence of digits in the claim text.
   Rationale, stated explicitly by all four judges independently: a
   false-positive "this is quantitative-sourced" tag is weighted more
   heavily downstream than a missed one, so it must be *harder* to earn
   than an ordinary supports/contradicts tag, not equally easy.

4. **No relaxation of the existing single-source caveat.** `real_fetch_evidence`
   issues exactly one `:online` call per claim (`_fetch_one`,
   `live_adapters.py:212-218`) with zero cross-source corroboration. A
   quantitative-tagged finding does not get exempted from
   `build_revision_prompt`'s existing "do not treat as infallible" caveat —
   if anything, apply it more pointedly to quantitative claims, since a
   single-source *number* is the more persuasive and harder-to-catch
   fabrication.

5. **Default-polarity inversion stated explicitly in both prompts.** Per §1,
   an unverified quantitative-sounding claim must read as *lower*
   reliability than a hedged, sourced qualitative claim — this is not
   implicit in "prefer verified over unverified," it must be written into
   both `build_evidence_prompt` and `build_revision_prompt` as its own
   sentence, since the naive/intuitive reading of "prefer quantitative"
   runs the opposite direction.

6. **Dry-run required before real use, per this repo's Real-money gate.**
   Exercise the pipeline on a low-stakes test decision specifically
   constructed so that no real quantitative source exists for at least one
   claim, and confirm the pipeline returns `UNVERIFIABLE` rather than
   fabricating one, before this ships against an actual decision. This is
   in addition to, not instead of, the Cost & Tokens summary the Real-money
   gate already requires.

## 5. Domain-neutrality confirmation

**Stays domain-neutral, confirmed.** The instruction is meta-epistemic —
about evidentiary quality (sourced, dated, specific, verifiable) — not about
any subject-matter lens, and is structurally identical in kind to the
already-shipped "cite-or-don't-write" block at `council_adapter.py:81-91`,
which draws the same kind of content-free evidentiary line and cites the
same rationale (arXiv:2603.03299 — model confidence uncorrelated with
citation correctness).

Domain-neutral wording to carry into the spec, and the one this project's
own near-miss (an early illustrative example that leaked fundraising-specific
lenses into a supposedly generic instruction, recorded in
`pipeline-architecture-spec.md:295-306`) means must be enforced strictly:

> "Weight specific, sourced, dated, independently-verified claims over vague,
> unsourced, unverifiable ones — regardless of whether the claim is numeric
> or narrative."

Note this drops "quantitative" as the *operative* criterion and replaces it
with "verified" — per §1, this is not a weakening of the user's intent, it is
the correction that makes the intent defensible. The wording never
enumerates example subject-matter categories (no "prefer revenue/
growth-rate/market-share statistics" — that would reintroduce exactly the
hardcoded domain list the project's rule bans) and applies verbatim whether
the decision under debate is a fundraise, a hire, or a hardware purchase.

## 6. Explicitly NOT adopted from the original ask

- **"Quantitative is inherently more reliable than qualitative," unqualified
  and unconditional.** Rejected outright per §1 — replaced with a
  verification-gated, source-competence-gated claim. This is the single
  biggest departure from the literal ask and is non-negotiable per all four
  judges.
- **"Add this at multiple/all stages."** Requested broadly; only 2 of the
  8 pipeline stages (0.5, 2.75) are actually appropriate owners, per §2.
  Stage 2 is flatly unreachable without forking upstream; Stage 1 and Stage
  3 are low-leverage text-only riders not worth the added surface area for
  an initial ship. If wanted later, Stage 1's block and the Stage 3 query
  string are the only remaining legitimate (if weak) levers — Stage 2
  remains off the table permanently absent an upstream fork.
- **Treating any cited number as evidence of accuracy.** The proposal's
  implicit premise — that a real, non-fabricated quantitative citation is
  therefore a *reliable* prediction — is not adopted. §1's source-competence
  qualifier (near-term/narrow vs. long-range/macro, known-vs-opaque
  incentive structure) stays attached to the weighting rule; a real citation
  from a source with a poor track record on this class of prediction does
  not automatically outrank careful qualitative reasoning.
- **Shipping the weighting preference without the guardrails.** Not staged
  as "weighting now, guardrails later" — per §4, the guardrails ship in the
  same PR or the change does not ship at all, because the mechanism report
  confirms zero existing defenses against exactly the fabrication mode this
  proposal would incentivize.

## Status

**Implemented, 2026-08-13, same session.** Spec:
`docs/specs/quantitative-evidence-weighting-contract.md` (Contracts 1-4,
covering §3a/3b's prompt changes, §4.1-4.2's guardrails as Given/When/Then
ACs, and §4.6's dry-run as an explicit process gate). Implemented directly
(test-first, hand-verified), not dispatched. The `Evidence` schema
extension (§3c) remains deferred, as planned — not blocking this ship.

## Addendum — two mid-turn refinements (same session, same day)

Both raised by the user immediately after the decision above was reached,
before implementation started; folded directly into Contract 1's
`build_evidence_prompt` instruction rather than re-running the panel, since
neither changes the underlying risk profile (still single-source, still
gated by the same anti-fabrication + reachability guardrails) — they only
widen what counts as "a specific, dated, verifiable finding."

1. **Academic literature / research-volume signal.** The same
   weighting principle should recognize a body of independent research
   converging on a direction (a systematic review, a meta-analysis, a
   growing count of papers in one direction), not just industry surveys.
   Added: prefer a finding that aggregates/surveys many independent sources
   over one study/opinion/anecdote, when the aggregation is itself real and
   cited — the same GRADE/Cochrane-style "converging independent evidence
   beats a single study" principle, applied domain-neutrally. "Systematic
   review"/"meta-analysis"/"industry-wide survey" are evidence-*methodology*
   labels (same status as "verified facts"/"input document" in the Stage 1
   reference instruction), not subject-matter content, so this stays inside
   the domain-neutrality rule.
2. **Revealed action vs. stated opinion.** The user's own examples —
   M&A activity, competitor moves, alliances, partnerships — are real,
   high-value signals of an entity's actual direction. **Not adopted
   verbatim**: those are corporate-specific vocabulary, and hardcoding them
   into a shared template would reintroduce the exact fundraising-specific
   near-miss `pipeline-architecture-spec.md` §6 already caught once.
   **Adopted, generalized**: a dated, verifiable action (a signed agreement,
   a completed transaction, a public commitment) is often stronger evidence
   of actual direction than a stated prediction or opinion — the classic
   revealed-vs-stated-preference distinction, domain-neutral by
   construction. The user's specific examples (M&A, alliances, etc.) belong
   in a session's own Stage 0 pre-registration when the decision at hand
   calls for them, not in the shared pipeline template.
3. **Cross-domain corroboration.** A follow-up ask (publication-count
   trends, industry-vs-academic publication volume, office openings as
   strategic signals, framed by the user as "industrial/market/academia
   signals") turned out to already be covered by refinements 1-2 above — a
   real, cited "publications on X grew from N to M" is exactly the
   "specific, dated, verifiable finding" refinement's parent clause already
   prefers; an office opening is exactly the "revealed action" already
   covered. What was genuinely new: the same underlying fact, independently
   corroborated by real sources from *more than one* sphere of activity, is
   stronger signal than any single source — classic triangulation.
   **Adopted, generalized, not as a fixed taxonomy**: added a clause naming
   three illustrative spheres (research literature, commercial/industrial
   activity, observable market behavior) as *examples* of independent
   spheres, not an exhaustive named category list — paired with an explicit
   warning against the failure mode this most directly risks: citing more
   sources than actually exist, or treating repeated mentions of one
   underlying source as if they were independent. The user's own
   "industrial/market/academia" framing was deliberately not shipped
   verbatim as three named categories, for the same domain-neutrality
   reason as refinement 2.
