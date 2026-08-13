# Proposal A: Stage 1 reference reporting + Stage 3 grounding (Pillar 2 — spec before code)

Status: ready for blind-TDV. Decision grounding:
`docs/citation-and-structured-reasoning-decision-2026-08-13.md` (research +
4-judge adversarial panel, user approved — "proceed").

## Problem this closes

Today: (1) Stage 1 asks each model for a free-prose answer with zero
reference-reporting instruction, so peer reviewers at Stage 2 have no
explicit signal of what grounded a claim beyond the prose itself; (2) Stage
3's chairman synthesis call sees only `stage1_results`/`stage2_results`/
`aggregate_rankings` — never Stage 0.5's already-verified facts, even though
they exist and are trusted (used by Stage 2.75's revision path already);
(3) `revision_round.py`'s `facts_block` (built from `verified_facts`) has no
delimiter of its own, unlike `_build_document_section`'s BEGIN/END-guarded
source-document text — flagged High-severity in
`docs/architecture-stress-test-2026-08-13.md` as an unguarded injection
surface, and this change adds a *second* consumer of that same list (Stage
3), making the gap more load-bearing, not less.

## Contract 1 — Stage 1 reference-reporting instruction

**File**: `scripts/council_adapter.py`, `run_council_with_timeouts`, the
`messages` build (`messages = [{"role": "user", "content": user_query}]`).

**Objective**: append one uniform, format-neutral instruction to every
Stage 1 prompt, identical across all 4 models (never per-model — preserves
CSS's same-question precondition), asking each model to note what grounds
each substantive claim, restricted to two checkable classes.

**Signature**:
```python
def build_stage1_prompt(user_query: str) -> str:
    """Appends a uniform reference-reporting instruction to user_query.
    Never varies by model. General/background-knowledge claims may be
    noted but must be labeled unverified — never presented as a citable
    reference (fabrication risk: model confidence is uncorrelated with
    citation correctness, arXiv:2607.11127)."""
    ...
```

**Acceptance criteria:**
1. Given any `user_query`, When `build_stage1_prompt` runs, Then the
   returned string contains `user_query` verbatim plus an appended
   instruction block — never truncates or rewrites the original query.
2. Given the instruction block, When compared across every call, Then it is
   byte-identical regardless of input (no per-model branching anywhere in
   this function — there is no model parameter to branch on, by design).
3. Given the instruction text, When inspected, Then it names exactly two
   checkable grounding classes (input document, Stage 0.5 verified facts)
   and explicitly instructs that general/background knowledge may be
   mentioned but must be labeled unverified — never phrased as a directive
   to fabricate or omit sourcing.
4. Given this instruction is new, When `run_council_with_timeouts` builds
   `messages`, Then it calls `build_stage1_prompt(user_query)` in place of
   the raw `user_query` — a one-line call-site change, no other Stage 1
   logic touched (resilience/retry/backup substitution all operate on
   `messages` unchanged).

**Non-goals**: no parsing/validation of what a model actually reports here
— that's Contract 2's job, and only for Stage 2.75/Stage 3, not Stage 1
itself (Stage 1's reference notes ride passively into Stage 2's existing
free-text peer review with no new extraction step, per the approved
design's "passive surfacing, not active verification").

## Contract 2 — `facts_block` delimiting fix (Fix-on-Sight precondition)

**File**: `scripts/revision_round.py`, `build_revision_prompt`.

**Objective**: wrap `facts_block` in its own delimited section, mirroring
`_build_document_section`'s existing BEGIN/END pattern, so a claim's `text`
(sourced from an automated web search against potentially
attacker-influenced document content) cannot forge text that reads as
prompt instructions when concatenated into the revision/synthesis prompt.

**Signature**:
```python
def _build_facts_section(verified_facts: list[TaggedClaim]) -> str:
    """Renders verified_facts as its own delimited section, textually
    distinct from surrounding prompt instructions — mirrors
    _build_document_section's BEGIN/END pattern. Empty list -> a single
    '(no verified facts available)' line, still inside the delimiters
    (never a silently absent section)."""
    ...
```

**Acceptance criteria:**
1. Given a non-empty `verified_facts` list, When `_build_facts_section`
   runs, Then the output wraps the existing `[id] (tag, source: ...) text`
   line format inside `--- BEGIN VERIFIED FACTS ---`/`--- END VERIFIED
   FACTS ---` markers (or equivalently-named, consistently-used constants —
   exact header text is an implementation choice, but it MUST be a fixed,
   grep-able string this project's own tests can assert on).
2. Given an empty `verified_facts` list, When it runs, Then the output is
   still delimiter-wrapped, containing the existing
   `(no verified facts available)` placeholder inside the markers — not an
   empty string (mirrors `_build_document_section`'s "no source document ->
   no section at all" being a *documented, tested* exception; here there is
   always a facts section, even if empty, because the surrounding prompt
   text uses it unconditionally today).
3. Given `build_revision_prompt`, When it's updated to call
   `_build_facts_section` instead of inlining `facts_block` directly, Then
   every existing test asserting on `facts_block`'s prior undelimited
   format must be updated to expect the delimited form — this is a
   deliberate breaking change to the prompt's exact text, not a
   backward-compatible addition (unlike Contract 1's `source_document`
   default).
4. Given a crafted `Claim.text` containing something that looks like a
   `[[cite:<id>]]` marker or prompt-injection-style instruction, When it
   flows through `_build_facts_section`, Then the delimiters make it
   textually distinct from the surrounding prompt's own instructions in
   the same way `_build_document_section` already achieves for the source
   document — this AC is a regression test using the existing
   `test_stress_adversarial.py` fuzzing pattern, not a new fuzzing
   framework.

## Contract 3 — Stage 3 synthesis context threading

**Files**: `scripts/council_adapter.py` (`run_council_with_timeouts`),
`scripts/pipeline_runner.py` (`CouncilFn` type alias, the `council_fn`
closure at line 457, the call site at line 236).

**Objective**: give Stage 3's `stage3_synthesize_final` call a distinct
`stage3_query` string — `user_query` plus Stage 0.5's `verified_facts`,
delimited via Contract 2's `_build_facts_section` — without changing what
Stage 1 sees (Stage 1 continues to receive `build_stage1_prompt(user_query)`
from Contract 1, unrelated to this).

**Signature**:
```python
CouncilFn = Callable[
    [str, list[TaggedClaim]],  # (user_query, verified_facts) — was [str] alone
    Awaitable[tuple[list, list, dict, dict]],
]

async def run_council_with_timeouts(
    user_query: str,
    verified_facts: list[TaggedClaim] = [],  # NEW, defaults empty — backward compatible for any other caller
    stage1_timeout: float = 300.0,
    stage2_timeout: float = 300.0,
    stage3_timeout: float = 300.0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    ...
```

**Acceptance criteria:**
1. Given `verified_facts` is empty (default), When
   `run_council_with_timeouts` runs, Then `stage3_synthesize_final` is
   called with `user_query` unchanged — byte-identical behavior to today,
   confirming this is a strictly additive change.
2. Given a non-empty `verified_facts` list, When it runs, Then
   `stage3_synthesize_final` is called with a `stage3_query` built as
   `user_query + "\n\n" + _build_facts_section(verified_facts)` (or
   equivalent, as long as `user_query` appears verbatim and unmodified
   within it) — never the raw `user_query` alone.
3. Given `verified_facts`, When Stage 1's `messages` are built (Contract 1),
   Then `verified_facts` plays no role there — confirms Stage 1 and Stage 3
   are independently controllable, per the direct code read that resolved
   the judges' feasibility disagreement (`council_adapter.py:224-231`'s
   `stage3_synthesize_final` call already takes its own `user_query`
   argument, separately from Stage 1's `messages` at line 147).
4. Given `pipeline_runner.py`'s `council_fn` closure (line 457) and its
   call site (line 236), When updated for the new `CouncilFn` shape, Then
   `verified_facts` (already computed at line 224, before `council_fn` is
   invoked) is passed through — no new Stage 0.5 call, no re-computation,
   reusing the existing grounding-pass result.
5. Given any existing test that constructs a fake `council_fn`/`CouncilFn`
   double with the old 1-argument signature, When the suite runs after this
   change, Then those doubles must be updated to accept
   `(query, verified_facts)` — this is a deliberate, tracked breaking
   change to an internal type alias, not a silent one (grep
   `tests/test_pipeline_runner*.py` for every `council_fn`/`CouncilFn`
   double before starting implementation, per this project's own "verify
   the actual merged state" lesson from the 2026-08-12 concurrent-contract
   incident).

**Non-goals**: no change to `stage1_5_normalize_styles`, no change to
`stage2_collect_rankings`'s call shape — Stage 2 continues reading only
`stage1_results`/`user_query` exactly as today (Decision doc's "passive
surfacing at Stage 2" is a property of Stage 1's response text itself, via
Contract 1, not a Stage 2 code change). No active cross-model verification
of references — explicitly rejected in the decision doc, not part of this
contract.

## Sequencing

Contract 2 (delimiting fix) should land first or alongside Contract 3
(the new consumer) — Contract 3 makes the existing gap load-bearing for a
second call site, so shipping Contract 3 without Contract 2 first would
knowingly ship a known security gap into new surface. Contract 1 is
independent and can land in any order relative to 2/3.
