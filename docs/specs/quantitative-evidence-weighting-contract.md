# Quantitative evidence weighting — spec (Pillar 2, before code)

Status: ready for implementation. Grounding:
`docs/quantitative-evidence-weighting-decision-2026-08-13.md` (adversarial
4-judge panel, unanimous `adopt-with-modification`; every file/line citation
in it independently re-verified against live source before this spec was
written). Implements that decision's §3a/3b (prompt changes) and §4.1-4.2
(mandatory guardrails) as one atomic change, per the decision's own explicit
requirement that weighting and guardrails ship together, never staged.

## Problem this closes

The user asked for published quantitative industry data (surveys,
consulting-firm forecasts, executive statements, market data) to be weighted
higher than qualitative claims, "heavily," across as many stages as possible,
under a zero-hallucination/retrieval-only constraint. The unqualified version
of that ask does not survive scrutiny (see decision doc §1) and, implemented
naively, would increase fabrication risk rather than reduce it — nothing in
this codebase today checks whether a model-reported "source" for a
quantitative claim actually resolves to anything real.

## Non-goals (explicit, per decision doc §2 and §6)

- **Stage 2** (`stage2_collect_rankings`) is not touched — it is called with
  the raw, unwrapped `user_query` (`council_adapter.py:292-294`), so no
  repo-owned prompt reaches it; forking upstream internals is out of scope.
- **Stage 1** and **Stage 3** get no changes in this contract — both are
  low-leverage text-only riders (Stage 1's reference block is only passively
  visible to Stage 2, never enforced; Stage 3's query is an opaque
  concatenated string). Not worth the added surface for this ship.
- **No blanket "numbers beat words" instruction.** The weighting only ever
  applies to evidence that has already passed Stage 0.5's retrieval gate, and
  even then only when a real source resolves.
- **`Evidence.is_quantitative` schema flag** (decision doc §3c) is explicitly
  deferred — a separate unit, not required for this ship.
- **No treating a real, non-fabricated citation as proof the underlying
  prediction is accurate.** Source-competence framing (near-term/narrow vs.
  long-range/macro) is documentation guidance for the prompt wording, not a
  new automated check this contract builds.

## Contract 1 — Stage 0.5 evidence-prompt weighting + anti-fabrication clause

**File**: `scripts/live_adapters.py`, `build_evidence_prompt`.

**Objective**: instruct the retrieval model to prefer sourced, dated,
verifiable specifics over vague assertions when a real one exists, paired
atomically with an explicit anti-fabrication instruction and the
default-polarity inversion from decision doc §1 (unverified
quantitative-sounding claims read as *lower* trust than hedged qualitative
ones, not higher).

**Acceptance criteria:**
1. Given `build_evidence_prompt(claim)` is called, When the returned prompt
   string is inspected, Then it contains an instruction telling the model to
   prefer a specific, dated, sourced, resolvable finding over a vague one
   *when one genuinely exists* — wording must not name any subject-matter
   category (no "revenue," "market share," "growth rate" etc. — domain-
   neutral per decision doc §5).
2. Given the same prompt, When inspected, Then it also contains an explicit
   instruction that the model must never invent a plausible-sounding
   source/report/survey name to satisfy the preference in AC1 — if no real
   resolvable source is found, the verdict must be `"unverifiable"`, not a
   fabricated `"supports"`/`"contradicts"` with an invented source.
3. Given the same prompt, When inspected, Then it states the default-polarity
   inversion explicitly: an unverified quantitative-sounding claim is *lower*
   trust than a hedged, sourced qualitative one, not higher — this must be
   its own sentence, not implied.
4. Given `claim.text` is still wrapped in the existing
   `_CLAIM_SECTION_BEGIN`/`_CLAIM_SECTION_END` delimiters, When the new
   instructions are added, Then they sit outside those delimiters, in the
   same position/order as the existing verdict-shape instruction — confirms
   this is additive to the existing prompt-injection guard, not a
   restructuring of it.
5. Given the existing JSON verdict shape (`verdict`/`source`/`date`), When
   this contract ships, Then that shape is unchanged — no new required JSON
   key (§3c's schema extension is explicitly deferred).

## Contract 2 — Stage 2.75 revision-prompt weighting

**File**: `scripts/revision_round.py`, `build_revision_prompt` (via
`_build_facts_section` or a new adjacent instruction line — implementer's
choice, whichever keeps `_build_facts_section` a pure rendering function).

**Objective**: among facts that have already passed Stage 0.5's verification
gate, instruct the revising model to weigh dated, sourced, specific findings
more heavily than vague or unsourced ones — never as a blanket override of
the existing single-source caveat.

**Acceptance criteria:**
1. Given `build_revision_prompt(...)` is called with a non-empty
   `verified_facts` list, When the returned prompt is inspected, Then it
   contains a sentence instructing the model to weigh specific/dated/sourced
   findings more heavily than vague/unsourced ones, appended to (not
   replacing) the existing "Single-source research findings... weigh
   accordingly, do not treat as infallible" sentence.
2. Given the same prompt, When inspected, Then the existing single-source
   caveat sentence is still present, byte-for-byte, immediately followed by
   the new sentence — confirms this is additive, never a relaxation of the
   existing caveat (decision doc §4.4).
3. Given `verified_facts` is empty, When `build_revision_prompt` is called,
   Then the facts section still renders `(no verified facts available)`
   exactly as today, and the new weighting sentence is *not* appended in
   that case (nothing to weigh) — confirms this doesn't add a dangling
   instruction with no facts section to apply it to.
4. Given the `answer`/`source_document`/`max_document_tokens` parameters and
   overall prompt structure, When this contract ships, Then all existing
   behavior covered by the current `revision_round.py` test suite still
   passes unmodified — no signature change, purely additive text.

## Contract 3 — URL-reachability guardrail before VERIFIED/CONTRADICTED

**File**: `scripts/live_adapters.py`, `real_fetch_evidence`'s `_fetch_one`
(post-processing after `parse_evidence_response` returns, before the result
is included in `EvidenceMap`).

**Objective**: today `parse_evidence_response` accepts any non-empty string
as `source` — a vague blog title and a fabricated "McKinsey 2026 State of the
Market Report" pass identically into a `VERIFIED`/`CONTRADICTED` tag. Add a
resolvability check on the `source` field before a claim is allowed to keep
its `supports`/`contradicts` verdict; an unresolvable source forces the
claim's evidence list empty (→ `UNVERIFIABLE` when `grounding_pass.tag_claim`
runs, since `tag_claim` already treats an empty evidence list as
`UNVERIFIABLE`).

**Signature addition:**
```python
async def _source_is_reachable(
    url: str, timeout: float = 5.0,
) -> bool:
    """HEAD request (async via asyncio.to_thread, matching
    _post_chat_completion_async's pattern - must not block the event loop
    or reintroduce the wall-clock-preemption gap Contract 2 of
    wallclock-cost-budget-contract.md already closed). Returns False on
    any exception, any non-2xx/3xx status, or an empty/non-http(s) url -
    never raises. A network hiccup on an otherwise-real source is treated
    the same as an unresolvable one: conservative by design, per the
    decision doc's explicit 'a false-positive quantitative tag is worse
    than a missed one' framing."""
```

**Acceptance criteria:**
1. Given `parse_evidence_response` returns a non-empty `Evidence` list with
   a `source` URL that resolves (2xx/3xx), When `_fetch_one` finishes, Then
   the evidence is kept unchanged in the returned tuple.
2. Given `parse_evidence_response` returns a non-empty `Evidence` list with a
   `source` URL that does NOT resolve (connection error, timeout, 4xx/5xx,
   or a non-`http(s)://` string), When `_fetch_one` finishes, Then the
   evidence list for that claim is replaced with `[]` — so
   `grounding_pass.tag_claim` (called downstream by the pipeline, unchanged
   by this contract) tags it `UNVERIFIABLE` regardless of how specific or
   numeric the original claim text was.
3. Given the reachability check itself raises (DNS failure, malformed URL,
   any exception), When it's caught, Then it's treated identically to AC2
   (unresolvable) — the check function itself must never propagate an
   exception into `_fetch_one` and crash the whole grounding pass.
4. Given `max_concurrency` bounds the existing evidence-fetch semaphore,
   When the reachability check is added, Then it runs *inside* the same
   semaphore-guarded section as the existing `:online` call (not a second,
   unbounded concurrent fan-out) — confirms no new unbounded-concurrency
   surface.
5. Given a claim whose evidence was empty already (verdict was
   `"unverifiable"` from `parse_evidence_response` itself), When `_fetch_one`
   runs, Then no reachability check is attempted at all (nothing to check) —
   confirms this is purely additive to the already-verified path, not a
   redundant check on the already-unverifiable path.
6. Given this contract ships, When `real_fetch_evidence`'s existing return
   shape (`EvidenceMap` with `cost_usd`/`truncated`) is inspected, Then it is
   unchanged — the reachability check adds no new cost (HEAD requests are
   not billed OpenRouter calls) and no new field.

## Contract 4 — dry-run verification gate (process, not code)

Per decision doc §4.6 and this project's own Real-money gate (Pillar 6):
before this ships against any real decision, run the pipeline on a low-stakes
test decision specifically constructed so at least one claim has no real
quantitative source available, and confirm the pipeline returns
`UNVERIFIABLE` for it rather than fabricating one. This is a manual
verification step recorded in `docs/upstream-deltas.md` once performed, not
an automated test — but Contract 3's AC2/AC3 already cover the equivalent
behavior at the unit level with a faked unreachable URL, so this dry run is
confirmatory, not the only line of defense.

## Test strategy

Direct implementation, test-first, hand-verified RED→GREEN (not isolated
blind-TDV dispatch) — this contract's riskiest surface (Contract 3's new
network-reachability check) needs the implementer's own judgment on
exception handling and is small enough in scope that isolated dispatch adds
process overhead without a corresponding reliability gain, per this
project's own documented experience with batch-dispatch non-delivery this
session. Every new test must be run and its failure/pass genuinely observed,
not assumed from a report.
