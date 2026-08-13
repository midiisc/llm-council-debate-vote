# Stage 0.5 Evidence-Weighting Clause Additions — Decision (2026-08-13)

An epistemology sweep surfaced **23 candidate additions** to the Stage 0.5
evidence-fetch prompt (`_EVIDENCE_WEIGHTING_INSTRUCTION_BLOCK`,
`scripts/live_adapters.py:153-179`), which today carries four domain-neutral
clauses (specific-over-vague with inverted polarity, aggregated/surveyed
evidence, revealed action over stated opinion, cross-domain corroboration).
**17 were dropped during consolidation** as redundant with those four clauses
or out of architectural scope, before ever reaching adversarial judging. The
remaining **6 went to adversarial review** (one judge per candidate,
attacking on evidentiary grounding, domain-neutrality, single-call
feasibility, and fabrication risk); all 6 came back `adopt-with-modification`.
This memo is the tie-breaking synthesis over those six verdicts, and it
narrows further: **4 are adopted** for implementation (Diagnosticity,
Cost-to-Fake/Against-Interest Weighting, Proxy Validity, Production-Method
Diversity) and **2 are declined** despite their individual
adopt-with-modification verdicts (Base-Rate/Reference-Class Anchoring,
Absence-of-Expected-Signal) — not because the underlying techniques are
unreal, but because a cross-candidate reading shows both reopen exactly the
fabrication-risk profile clause 1 already exists to manage, for a
signal that fires rarely and can never move a verdict on its own. Stacking
them on top of four already-adopted new clauses fails this project's own
lightweight/no-bloat bar once weighed against the stronger four.

The four adoptions become clauses 5-8 of
`_EVIDENCE_WEIGHTING_INSTRUCTION_BLOCK`, in the order below. Wording is taken
verbatim from each judge's tightened `domain_neutral_wording`, since that
tightening is itself part of the adjudicated result, not a draft to
re-litigate.

## Adopted

### 1. Diagnosticity (discriminating power vs. rival explanations)

> **5. DISCRIMINATING EVIDENCE OVER MERELY CONSISTENT EVIDENCE:** judge a
> found source not by how strongly it seems to support the claim on its own,
> but by whether it would be unlikely to exist if the claim were false —
> specifically, whether it is unlikely under the claim's own negation or the
> specific rival option the claim names. A finding that is equally
> compatible with the opposite conclusion adds little value even when it is
> well-sourced and specific. This also covers a distinct failure mode: a
> real, dated, cited source that turns out to address a different,
> similar-sounding claim rather than the one actually being evaluated
> contributes nothing here. Guard: only compare against the alternative the
> claim itself implies (its plain negation, or a rival it explicitly names)
> — never invent a new alternative to test against, and never assert that a
> source distinguishes the claim from its alternative unless the source's
> own stated content actually does so (an inferred or assumed distinction
> does not count); if no source addresses the actual claim, as opposed to a
> look-alike neighbor, default to unverifiable.

**Guardrail**: the diagnostic judgment must rest on what the retrieved
source explicitly states, never on an inference the model draws on the
source's behalf. Without this, "does this source rule out the alternative"
becomes the easiest new fabrication surface in the set — the citation is
real, but the characterization of what it shows is smuggled in. Default to
unverifiable whenever the retrieved source addresses a look-alike neighbor
claim rather than the actual one.

**Why it earns a slot**: all four shipped clauses score a piece of evidence's
support for the claim in isolation (how specific, how aggregated, what type
of action, how many spheres corroborate it). None asks whether the evidence
would look different if the claim were false. This closes that gap and, as a
side effect, catches a real-, dated-, cited-source-about-the-wrong-claim
failure mode none of the four catch today.

### 2. Cost-to-fake / incentive-weighted credibility

> **6. COST-TO-FAKE / AGAINST-INTEREST WEIGHTING:** when a source explicitly
> discloses that making a statement or taking an action was costly, risky,
> or worked against the stating party's own apparent interest (an explicit
> penalty, a disclosed conflict of interest, a stated resource commitment,
> or a concession that undercuts the party's own position), weight that
> finding more heavily than an equivalent statement or action with no such
> disclosed cost — a low-cost, self-serving announcement is easy to make
> regardless of whether it's true, and this can outweigh the default
> action-over-opinion ranking above. Guard: apply this only when the cost,
> risk, or against-interest nature is explicitly stated in the source
> itself. Never estimate a cost, infer risk, or guess at a party's "true
> incentive" from general knowledge of how such situations usually work — if
> the source does not disclose it, this factor does not apply, and the
> finding is scored on the other criteria alone.

**Guardrail**: binary, not a continuum in practice — the clause fires only
when the source states the cost/risk/against-interest fact in terms a reader
could quote back (a named penalty, a named conflict of interest, a named
resource commitment). Never when the model has to infer or estimate it.
Absent that explicit disclosure, the clause is inert and the finding falls
back to being scored by clauses 1-4/7/8 alone — no path from
absence-of-evidence to invented evidence.

**Why it earns a slot**: genuinely orthogonal to the existing revealed-action
clause. Clause 3 (now the third of the original four) ranks action over
opinion categorically; this clause can *invert* that ranking within a single
finding — a low-cost, self-serving action can be weaker evidence than a
high-cost, against-interest statement. Clause 3 has no mechanism to say that.

### 3. Leading-indicator / proxy-validity check

> **7. PROXY VALIDITY OVER PROXY PRECISION:** when a finding offers a
> continuously-observable stand-in measurement (a count, index, volume, or
> rate) as evidence for a separate, not-yet-confirmed outcome, a precise and
> well-sourced number for that stand-in does NOT by itself establish that it
> predicts the outcome. Trust the link between the two only if the source
> itself states, or cites, an established relationship between that specific
> measurement and that specific outcome. Never invent a predictive
> relationship, correlation, or lead-time that the source does not state.
> Absent that grounding, treat the finding as unverified for the outcome it
> is cited to support — even though the underlying number is itself real and
> dated.

**Guardrail**: the model may only report a predictive link the source itself
states or cites; it must never construct or infer one from the proxy's
precision, recency, or intuitive plausibility. Silence in the source forces
the same "unverified for the claim it's cited to support" fallback the other
seven clauses already use — no new fail-open path.

**Why it earns a slot**: a construct-validity gap, not a sourcing gap.
Clause 1 tests whether the *source* is real/dated/specific; this tests
whether a real/dated/specific number actually bears on the *different*
outcome it's cited to support. A proxy figure can pass clause 1 cleanly
(real, dated, exact) while carrying no established relationship to the
claim — the nowcasting/search-volume-index literature documents this gap
repeatedly, and none of the four shipped clauses have a mechanism to catch
it. The candidate's original bundled forward-/backward-looking timing
sub-feature is dropped — it substantially overlaps the existing
revealed-action-vs-stated-opinion clause and would make this clause
non-single-purpose.

### 4. Evidence-production independence — method-diversity half only

> **8. PRODUCTION-METHOD DIVERSITY:** when more than one real, cited source
> agrees on a direction, treat agreement between sources that were produced
> by genuinely different methods or processes (for example: a recorded
> transaction, an independent survey, a firsthand account, a direct
> measurement) as stronger evidence than agreement between sources produced
> the same way or that turn out to be restatements of one original report
> carried by multiple outlets. Apply this only when each source's production
> method is actually stated or evident from the source itself — never infer,
> assume, or guess a method that isn't shown, and never treat two copies or
> reprints of the same underlying report as independent methods just because
> they appear on different pages. If the sources' methods can't be verified
> as both real and different, give no diversity bonus — fall back to
> ordinary clause-1 treatment of each source on its own merits.

**Guardrail**: method must be stated or evident in the source itself, never
inferred, with a hard fallback to plain clause-1 treatment when
unverifiable. The wording explicitly names the single most likely real-world
gaming vector — syndicated copies of one wire report counted as
independently produced — rather than leaving it implicit.

**Why it earns a slot, and why only half the candidate**: orthogonal to the
existing cross-domain-corroboration clause, which groups sources by *sphere*
of activity (research literature vs. commercial activity vs. observable
market behavior). This groups by *how* evidence was produced — two sources
in the same sphere can differ in production method, and a mechanical record
has no "sphere" at all. The candidate's second sub-check ("a disinterested
mechanical byproduct like a filing or log outranks a persuasive statement")
is **not** adopted: its own judge flagged it as a narrower restatement of
the same action-over-opinion logic already in the revealed-action clause,
and shipping it risks the model double-counting one piece of evidence under
two clause labels — its own quiet form of fabricated confidence. Only the
method-diversity sub-check ships.

## Rejected (despite individual adopt-with-modification verdicts)

### Base-rate / reference-class anchoring

Real technique (Tetlock/Gardner superforecasting; Flyvbjerg reference-class
forecasting) and phraseable domain-neutrally — its individual judge found no
fault on either front. Declined at final synthesis for two compounding
reasons its own judge already flagged but didn't weigh against the other
five candidates: (1) the only single-call-safe trigger is narrow to the
point of firing rarely — the source itself must explicitly state a
historical base rate for "this type of situation," which is an unusual thing
for a retrieval source to volunteer; and (2) even in that narrow form, it
reopens exactly the fabrication-risk profile clause 1 already exists to
manage — asking a model to weigh a claim against a "historical frequency" is
an open invitation to either invent a specific-sounding rate from memory or
over-read an incidental mention as a reference class, which is why its own
judge called this "elevated risk relative to the existing 4 clauses." A
rarely-firing clause whose main effect is to reintroduce a risk another
clause already manages is not worth an eighth-and-ninth slot in the prompt.
Not adopted; revisit only if a concrete, repeated real-world miss surfaces
that only this check would have caught.

### Absence-of-expected-signal ("the dog that didn't bark")

Real, documented technique (Grabo's indicator-and-warning analysis) and the
only candidate that scores an active non-finding rather than something
found — genuinely distinct from all four shipped clauses. Declined for an
architectural reason its own judge stated plainly: this is "the single
highest-risk addition" reviewed, because proving a negative from **one**
search call is structurally unreliable (engine coverage, recency lag,
phrasing sensitivity all mean "found nothing" mostly means "the query was
imperfect," not "the marker doesn't exist"). The judge's own required
guardrails — cap the weight at "weak, supplementary only," forbid it from
ever moving a verdict off unverifiable by itself, phrase any null result as
search-scoped rather than an ontological claim — leave a clause whose actual
operational contribution is thin, while the fabrication surface it opens
(claiming search thoroughness a single call cannot support) is the largest
of anything reviewed. That is a bad risk/benefit trade to take on inside a
prompt whose defining constraint is exactly-one-search-call-per-claim. Not
adopted; would need Stage 0.5's architecture to support more than one search
per claim before this is worth reconsidering as a real, non-trivial signal.

## What was dropped during consolidation, and why

17 candidates were cut before ever reaching a judge, showing the sweep
covered real breadth, not just the six that made it to adversarial review.
Grouped by why each collapsed:

- **Already the philosophical/technical ancestor of a shipped clause** —
  Consilience of inductions (Whewell) collapses into clause 4's
  cross-domain-corroboration logic at the level of theories rather than
  findings; Robustness analysis / Wimsatt's non-overlapping-causal-path
  framing and Diffusion-origin distinctness both collapse into clause 4's
  existing anti-double-counting caveat, just sharper or more mechanical
  wording of a warning the clause already carries.
- **Already collapses into revealed-action-over-stated-opinion (clause 3)**
  — Job-posting/hiring-velocity data, patent-filing trends, and
  regulatory-filing-pattern data are all completed, committed acts; Earnings-
  call NLP tone/sentiment stays on the stated-opinion pole regardless of
  scoring sophistication; Second-order behavioral adjustment blends clause 3
  (it's an action) with clause 4 (it's cross-domain reaction) without adding
  a third axis; Attention-allocation as revealed interest folded directly
  into the adopted cost-to-fake clause as a lower point on the same cost
  continuum, rather than needing its own entry.
- **Already collapses into aggregated/surveyed evidence (clause 2)** —
  Crowdsourced employee-review sentiment is organically-crowdsourced
  aggregation, not a structurally new category.
- **Already collapses into specific-over-vague / polarity inversion
  (clause 1)** — Precision-Method Mismatch Flag is exactly clause 1's
  existing "unverified quantitative-sounding claim is lower trust" polarity
  applied to a stated-precision/stated-method mismatch; Falsifiability of a
  prediction checked against later resolution is a temporal extension of the
  same specific-over-vague preference, too thin alone for a separate clause.
- **Not implementable inside the one-real-source-per-claim architecture** —
  Investigator triangulation (Denzin's 4th type) doesn't apply to a single
  retrieval call by one model — it's already structurally satisfied one
  layer up by the multi-agent council itself; Pre-registered, discrete
  indicator lists (Grabo) needs a process/pre-registration redesign, not a
  content instruction, and would also collide with this repo's Pillar 6
  stance that Stage 0 pre-registration stays manual by design; Necessary-
  precondition ("logistics-tail") indicators carries the same hard-to-fake
  logic as the adopted cost-to-fake clause but works best only where there's
  a literal physical supply chain, risking the domain-neutrality
  constraint; Source self-correction/retraction history is real but would
  need researching a source's own track record as a second lookup, plus its
  own sweep flagged a fabrication-asymmetry risk.
- **Out of Stage 0.5's scope entirely** — Theory triangulation belongs at
  the interpretation/debate stage per its own sweep, not the single-call
  evidence-fetch stage.
- **Compound/less crisp than the six that were judged** — Baseline-
  deviation/pattern-of-life, velocity/acceleration, temporal triangulation,
  spatial triangulation, and person/level triangulation all converge on
  "look at the evidence over time or across settings"; kept out of the final
  set as compound rather than atomic, with their one genuinely distinct
  thread (a claim's own specificity/dating) already partially covered by
  clause 1.
- **Narrower illustrative subtype, not a separate category** —
  Motivated-critic silence is a narrower instance of
  absence-of-expected-signal, itself declined above; it does not survive
  independently either.
- **Discriminant relevance / "same claim vs. neighbor claim" check** — fully
  absorbed into the adopted Diagnosticity clause (its "wrong claim" branch),
  so it needed no separate entry.

## Net effect on the prompt

`_EVIDENCE_WEIGHTING_INSTRUCTION_BLOCK` grows from 4 clauses to 8. All four
additions are domain-neutral as written, each closes a distinct epistemic
gap none of the other seven clauses cover, and each carries a guardrail that
defaults to the block's existing fallback (`unverifiable`) whenever the
required grounding isn't present in the retrieved source — no new fail-open
path is introduced anywhere in the four adoptions. The two declines are not
"these ideas are wrong" — both are real, well-evidenced techniques — they are
"the marginal signal after mandatory narrowing doesn't clear this project's
bloat/fabrication-risk bar," which is a different and narrower claim, stated
explicitly per candidate above rather than left implicit.

## Status

**Not yet implemented.** This memo is the decision surface; per this
project's Pillar 2, the four adopted clauses need a spec
(`docs/specs/`) with Given/When/Then ACs before
`_EVIDENCE_WEIGHTING_INSTRUCTION_BLOCK` is edited, and per Pillar 3 the tests
for each new clause must be authored blind against that spec before
implementation. No code change has been made in this session.
