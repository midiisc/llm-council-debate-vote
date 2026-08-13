# Citation/Reference Flow & Structured Reasoning — Decision (2026-08-13)

User proposed two architecture changes for the MAD pipeline, both aimed at
improving output quality (not adopted for their own sake): (A) force models
to report references at Stage 1+, let other models cross-check them for
misreads, and build a centralized reference list feeding Stage 3; (B) have
models output a structured concept/knowledge graph + reasoning graph in
JSON instead of prose, reasoned over downstream, for token efficiency.

Run as a `Workflow`: 2 parallel grounded research agents (citation/attribution
literature; structured-graph-vs-prose-reasoning literature) → 4 adversarial
judges (agentic-composability, ML-research-rigor, red-team, backend-
feasibility) → 1 tie-breaking synthesis. **Process note, worth recording**:
two of the four judge slots degenerated to a literal `"test"` placeholder
across two separate runs of this same workflow (once the agentic slot, once
the red-team slot, on the re-run) — the exact placeholder-output failure
mode this project already documented once before (the 4th-seat diversity
panel's Grok research agent, 2026-08-12). Both were caught by directly
checking response length/content before trusting the panel, not assumed
clean from a green completion status, and fixed by re-running with an
explicit anti-placeholder instruction. **This is now confirmed to be a
recurring failure mode for this class of parallel-agent workflow, not a
one-off — future panels of this shape should spot-check every judge's
output length before trusting synthesis, as standard procedure, not an
exception.**

## Proposal A — forced reference reporting + cross-verification: ADOPT-MODIFIED

Adopted, in a narrower form than specced:

1. **Stage 1 only.** One uniform, format-neutral instruction added to the
   `messages` payload `council_adapter.py` already builds locally: ask each
   model to note what grounded each substantive claim, restricted to two
   checkable classes — input-document text, and Stage 0.5's
   `verified_facts`. General background knowledge may be mentioned but is
   explicitly labeled unverified and never treated as citable downstream —
   the fabrication literature (confidence uncorrelated with correctness,
   91% of fabricated citations asserted at ≥0.8 confidence) means an
   unchecked self-reported "reference" is closer to theater than grounding.
2. **Passive surfacing at Stage 2, not active cross-verification.** The
   reference block rides inside the existing Stage-1 response text, so it's
   naturally visible to Stage 2's existing free-text peer notes — zero new
   model calls, zero new O(N²) cost, no touching the upstream package's
   `stage2_collect_rankings`. A *forced* cross-check ("does peer's claim
   match its own reference," a new rubric dimension or new stage) is
   explicitly **not** adopted — the literature's positive
   citation-verification results come from much heavier structured
   apparatus (external retrieval, dedicated verifier pipelines), not "one
   model eyeballs another's citation list," and unstructured peer-anchored
   (vs. ground-truth-anchored) verification can destabilize consensus into
   oscillation rather than improving it.
3. **Stage 3 gets richer input — but from the list this pipeline already
   trusts, not a new one.** No new "centralized reference list" merging
   four models' self-reported (possibly document-lifted) references — that
   would concentrate, not diffuse, the already-documented `facts_block`
   prompt-injection gap (`docs/architecture-stress-test-2026-08-13.md`,
   High severity). Instead, Stage 3's synthesis call gets Stage 0.5's
   `verified_facts` appended via a distinct `stage3_query` string, wrapped
   in the same delimited pattern `revision_round.py`'s
   `_build_document_section` already uses. Confirmed architecturally cheap
   by direct code read (`council_adapter.py:224-231` — `user_query` at that
   call site is repo-owned and independently controllable, no upstream
   forking needed), resolving a factual disagreement between two judges on
   exactly this point.
4. **If active verification is ever wanted later**: extend the *already-
   built* Stage 2.75 `[[cite:<id>]]` mechanism upstream to Stage 1, checked
   once per model against the fixed `verified_facts` list (O(N), zero new
   model calls, pure parsing) — not O(N²) peer-to-peer. Not adopted now, no
   evidence it's needed beyond passive surfacing.

**Precondition**: fix the already-flagged `facts_block` delimiting gap
before or alongside shipping this — it now feeds a second downstream
consumer (Stage 3), not just Stage 0.5's original purpose.

## Proposal B — structured graph/JSON reasoning instead of prose: REJECT as specced

**Unanimous, all four judges independently, no real dissent.** Full
replacement of Stage 1-3 prose with a concept/knowledge graph + reasoning
graph is rejected. Reasons that converged from different angles:
- Graph-of-Thoughts/Tree-of-Thoughts gains are demonstrated on
  combinatorial/checkable tasks with a scoreable local objective per node —
  not this pipeline's open-ended judgment task.
- The one 2026 paper building reasoning graphs at all does so *post-hoc*
  over completed prose, as a diagnostic — not as a generation-time
  replacement. Even the field's own frontier work treats graphs as an
  analysis lens, not a substitute.
- **Concrete, mechanism-matched risk this pipeline can't afford**:
  JSON-mode/structured-output requests measurably homogenize model answers
  (surprisal 1.80→1.58 bits in the cited study) — this would inflate the
  Consensus Strength Score without genuine independent convergence, silently
  disabling the Stage 2.75 revision trigger exactly when it's most needed.
  This isn't a token-cost tradeoff, it's a threat to the pipeline's primary
  quality gate.
- The stated goal (token efficiency) targets the wrong axis: Stage 2's
  actual cost driver is call *count* (O(N×(N-1)) reviews), not prose
  *length*. The field's proven fix for multi-agent-debate token cost
  (S²-MAD, ~94.5% reduction; GroupDebate, up to ~51.7%, accuracy maintained)
  works by sparsifying *who reviews whom*, not by reformatting what they
  read.
- Implementation-independent kill: Stage 2/3 are upstream-owned, opaque
  functions with no graph-aware code path — making them "reason over
  structure" requires forking upstream internals, the same High-severity
  vendoring risk already on file.

**Not adopted in any form this round** — not even a "draft-then-structure"
post-hoc summary variant, since nothing in the current pipeline needs one
and it would be a net token *increase* unless it also replaces what Stage
2/3 read (reopening the forking problem). **Recommended alternative for the
user's actual stated goal (token efficiency)**: pursue Stage-2 communication-
topology sparsification (S²-MAD/GroupDebate-style — prune/group which model
pairs review each other) as its own separate, future-scoped proposal. This
directly targets the real, already-identified O(N²) cost bottleneck and
needs no prompt/format change or upstream fork.

## Cross-cutting caution, preserved not resolved

All four judges independently raised the same caution: the one study in the
evidence set testing multi-agent-debate machinery on an actual open-ended,
human-expert-judged writing task found domain experts *preferred* a single
frontier-model pass over MAD output — one MAD variant burned ~30x the
tokens for a *worse-liked* result. This is why both decisions above are
deliberately narrow, reversible slices rather than full adoptions, even
where a mechanism turned out to be technically cheap to build.

## Status

**User approved both decisions ("proceed"), same session.** Proposal A's
implementation spec: `docs/specs/proposal-a-reference-grounding-contract.md`
(3 contracts: Stage 1 reference instruction, `facts_block` delimiting fix,
Stage 3 context threading). Proposal B stays rejected as specced — no code
change.

## Follow-up: post-hoc structured-artifact stage (later same session)

User then asked for exactly the "post-hoc, additive" form this doc already
flagged as legitimate — a final Stage 5 building a reasoning graph/KG/CG/
mind-map/reference-grounding-graph "for later use and grounding." Ran a
focused 3-judge design panel (no fresh research phase — the structured-vs-
prose evidence above already settles the underlying question; this panel
only had to design the artifact's shape). Converged, unanimous on the core
call: **build ONE unified typed-graph artifact, not five separate ones** —
the user's own framing already half-conceded KG/CG/mind-map overlap, and a
"reference grounding graph" substantially overlaps Proposal A's own
citation mechanism. Full design, schema, and gating:
`docs/specs/reasoning-graph-contract.md`. Key points:
- Reference nodes + `cites` edges are built **deterministically in plain
  Python** from `verified_facts` — zero LLM involvement, zero hallucination
  surface for roughly half the schema.
- Concept/claim nodes + relationship edges come from one LLM call over the
  Stage 3 synthesis only (not all 4 Stage 1 drafts — keeps prompt cost and
  injection surface down); every claim node and supports/contradicts/
  derives-from edge must carry a verbatim source-text span, validated by
  plain-Python substring check; anything that fails is **dropped and
  counted, never flagged-and-kept** (a dropped hallucinated node can't be
  mistaken for real; a visually-flagged one still could be).
- Placement: truly last, after Stage 4, self-contained persistence
  (3 files into the existing `output_dir`) — does not block on fixing the
  stress test's Critical #7 (no durable persistence pipeline-wide), which
  stays open and explicitly flagged, not silently resolved as a side
  effect.
- Gated on cost ceiling + a new self-policing wall-clock soft-budget check +
  exception isolation — skip loudly, never crash an otherwise-complete run.
- **Explicitly declined, flagged for separate approval**: wiring the
  already-dead `safety_check` result into anything here — real, independently
  re-confirmed finding, but a repo-wide gap shared by Stage 2.75/Stage 4
  too, out of scope for a new-stage spec ("discovery is free, healing is
  gated").

Implementation for both Proposal A and Stage 5 queued together.
