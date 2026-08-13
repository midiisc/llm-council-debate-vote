# Human-debate characteristics — gap analysis and spec (Pillar 2)

Status: ready for implementation. Grounding: user-supplied framework
(dialectic-vs-eristic distinction; Rapoport's Rules, Daniel Dennett,
*Intuition Pumps and Other Tools for Thinking*, 2013, crediting game
theorist Anatol Rapoport — independently re-verified via WebSearch, real
and accurately attributed; structural consensus-building characteristics:
cooperation-over-preference, egalitarianism/inclusion, iterative
modification, "addressing the gap"). Mapped directly against this
project's own architecture, read live, not assumed.

## Mapping — what's already covered, what's genuinely missing, what's in tension

| Characteristic | Status | Disposition |
|---|---|---|
| Dialectic (cooperative truth-seeking) vs eristic (competitive winning) | **Gap** | Add explicit framing to Stage 1's shared instruction. |
| Rapoport's Rules (restate fairly → list agreements → acknowledge learning → then rebut) | **Gap, plus a deeper wiring gap** | See below — real peer critique text exists upstream but is currently discarded before reaching the stage that would use it. |
| Cooperation over preference | Partially covered (no personas = no identity to defend) but never stated | Folded into the same Stage 1 framing addition. |
| Egalitarianism / inclusion | **Already an open, unresolved question on file** (`pipeline-architecture-spec.md` §8.4 — should Gemini Flash's review vote be weighted below the three frontier seats it's nominally a peer to) | Not silently resolved here — resurfaced to the user, not decided unilaterally. |
| Iterative modification until a consent threshold is met | **In tension with an already-established, cited decision** | This project already rejected unconditional/repeated revision rounds, grounded in cited literature (ARMOR-MAD, "Revision or Re-Solving?", Deliberative Illusion) showing iterative LLM revision often degrades quality — the opposite of what tends to help in human deliberation. **Not adopted.** Documented explicitly here so the tension isn't silently dropped either way. |
| Addressing the "gap" ("what would change your mind" vs "why are you against this") | **Gap, implementable** | Reframe Stage 2.75's revision prompt. |
| Willingness to change one's mind | Already structurally present (Stage 2.75's citation-gated revision) | No change needed. |

## The deeper wiring gap (found while checking Rapoport's Rules feasibility)

Rapoport's Rules requires restating what the other side actually *said*.
Checked what the model doing Stage 2.75 revision currently sees of its
peers' critique: `pipeline_runner.py::build_critique_from_rubric` builds
`ModelAnswer.critique` from `_rubric_scores_for_model`, which extracts only
the five **numeric** rubric dimensions (accuracy/relevance/completeness/
conciseness/clarity) and averages them — e.g. "Reviewers scored your
response — accuracy: 7.2/10 ... Weakest dimension: completeness (5.4)."

But direct inspection of the installed package
(`llm_council.council_stages.stage2_collect_rankings`, rubric-scoring
branch — confirmed live, `evaluation.rubric.enabled: true` in this
project's own `llm_council.yaml`) shows each reviewer's per-response JSON
also includes a `"notes": "<brief justification>"` field — real free-text
critique content. `_rubric_scores_for_model` never reads it; it is silently
discarded before Stage 2.75 ever runs. Rapoport's Rules has nothing to
restate under the current wiring — this needs fixing first, or the new
prompt instruction below would be asking for something the model has no
real material to do.

## Contract 1 — thread peer free-text notes into the revision critique

**File**: `scripts/pipeline_runner.py`.

**Acceptance criteria:**
1. Given `stage2_results` entries whose `parsed_ranking.evaluations[label]`
   dict includes a non-empty `"notes"` string for the model being scored,
   When a new `_rubric_notes_for_model(model, stage2_results,
   label_to_model)` function runs, Then it returns the list of those notes
   (one per reviewer that provided one), in `stage2_results` order.
2. Given a reviewer's evaluation entry has no `"notes"` key, an empty
   string, or `parsed_ranking`/`evaluations` missing entirely (same defensive
   shapes `_rubric_scores_for_model` already handles), When the function
   runs, Then that reviewer contributes nothing to the list — never raises.
3. Given `build_critique_from_rubric` is called, When notes exist, Then its
   returned string appends them after the existing numeric summary (e.g.
   `"... Weakest dimension: completeness (5.4). Reviewer notes: <note 1> |
   <note 2>."`) — existing numeric-summary behavior and its exact wording
   is unchanged when no notes exist (backward compatible for the "No peer
   scores available" and no-notes cases).

## Contract 2 — Stage 2.75 revision prompt: Rapoport's Rules + addressing the gap

**File**: `scripts/revision_round.py`, `build_revision_prompt`.

**Acceptance criteria:**
4. Given the prompt is inspected, Then it instructs the model, before
   deciding whether to revise, to restate the critique in its own words
   well enough that a reviewer would recognize it as fair, and to note any
   part it agrees with — framed explicitly as being in service of reaching
   the best shared answer, not defending the original one (dialectic, not
   eristic).
5. Given the prompt is inspected, Then the existing citation-gated revision
   rule is unchanged in substance (only a real fact id may trigger a
   revision; peer agreement alone is still explicitly disallowed,
   `_NO_SWITCH_SENTENCE` preserved verbatim) — the new restate/agree step is
   additive framing, never a new path to revise without evidence.
6. Given the prompt is inspected, Then when the model is NOT revising, it
   is instructed to state what specific new finding would change its mind,
   rather than only restating why it disagrees (addressing the gap).
7. Given the prompt instructs the model to place its restatement/reasoning
   *before* the `[[cite:<id>]]` marker and the actual revised answer text
   *after* it, When `parse_revision_response` runs, Then only the text
   after the marker becomes `revised_text` — reasoning preceding the marker
   must never leak into the synthesized answer.

## Contract 3 — `parse_revision_response`: keep only post-marker text

**File**: `scripts/revision_round.py`.

**Objective**: today `_CITE_MARKER_RE.sub("", response_text, count=1)`
removes only the marker itself, leaving any preceding text in place — safe
today only because no existing instruction asks a model to write anything
before the marker. Contract 2 changes that, so this must change too.

**Acceptance criteria:**
8. Given `response_text` contains a valid citation marker with reasoning
   text before it (e.g. `"I agree with X. [[cite:5]] The corrected
   answer."`), When `parse_revision_response` runs, Then `revised_text` is
   only the text after the marker (`"The corrected answer."`), not the
   preceding reasoning.
9. Given the marker is the very first thing in `response_text` (today's
   only real-world shape, still fully supported), When
   `parse_revision_response` runs, Then behavior is byte-identical to
   today — confirms this is backward compatible, not a breaking change for
   the existing shape.
10. Given a second, later literal occurrence of citation-marker-shaped text
    appears in the response, When `parse_revision_response` runs, Then it
    still survives untouched in `revised_text` (only the *first* match is
    consumed to locate the split point, matching today's `count=1`
    single-marker-consumption behavior) — regression test for the existing
    `test_parse_revision_response_strips_exactly_one_citation_marker_occurrence`
    case.

## Contract 4 — Stage 1 dialectic/cooperative framing

**File**: `scripts/council_adapter.py`, `_STAGE1_REFERENCE_INSTRUCTION_BLOCK`
(or an adjacent, equally-shared block appended the same way).

**Acceptance criteria:**
11. Given `build_stage1_prompt` is called, When the returned prompt is
    inspected, Then it states the goal is a shared, well-supported answer,
    not winning an argument against the other models — framed once, applies
    identically to every seat, no persona, matching the existing
    no-per-model-variance rule.
12. Given the addition, When inspected, Then it names no subject-matter
    category — domain-neutral, matching every other clause in this repo's
    prompt templates.

## Explicitly not adopted

- **Iterative modification until consensus** — stays a single, CSS-gated
  revision pass. Reopening this needs new counter-evidence against this
  project's own already-cited literature, not a general human-deliberation
  best practice applied uncritically.
- **Resolving the Gemini-Flash equal-weight review question** — real,
  already on file, explicitly not decided here; surfaced to the user
  instead of unilaterally resolved, since it was already flagged as "not
  resolved yet — flagging for a decision" the first time it came up.

## Test strategy

Direct implementation, test-first, hand-verified RED→GREEN — same
rationale as this session's other prompt/parsing contracts. Contract 3 is
the highest-risk piece (changes real parsing behavior on the production
citation-marker path) and gets explicit before/after regression coverage
against every existing `parse_revision_response` test case, not just new
cases.
