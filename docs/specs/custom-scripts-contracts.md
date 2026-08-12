# Custom script contracts (Pillar 2 — spec before code)

Status: ready for blind-TDV. Domain-neutral (Pillar per §6 of the architecture
spec — no hardcoded lens/domain language anywhere below), folder-scoped
(§7 — all output under `<cwd>/council-runs/`, never `~/.llm-council/`).

Design note shared by all three: none of these scripts perform their own web
retrieval or LLM calls directly. Retrieval (grounding pass evidence) and model
calls (revision queries) are dependency-injected — the orchestrating layer
(Claude Code itself, using WebSearch/MCPs per Pillar 1, or `consult_council`)
supplies evidence/query functions. This keeps each script a deterministic,
testable unit instead of requiring live-API mocking for every test.

## Contract 1 — `grounding_pass.py` (Stage 0.5)

**Objective:** given a raw context file with numbered factual claims and
pre-fetched evidence per claim, tag each claim VERIFIED/CONTRADICTED/
UNVERIFIABLE and render the annotated file that feeds Stage 1 prompts.

**Signature:**
```python
@dataclass
class Evidence:
    source: str
    date: str
    supports: bool  # True = corroborates the claim, False = contradicts it

@dataclass
class Claim:
    id: str          # preserves original numbering, e.g. "3"
    text: str

@dataclass
class TaggedClaim:
    claim: Claim
    tag: Literal["VERIFIED", "CONTRADICTED", "UNVERIFIABLE"]
    evidence: list[Evidence]

def parse_claims(raw_text: str) -> list[Claim]: ...
def tag_claim(claim: Claim, evidence: list[Evidence]) -> TaggedClaim: ...
def render_output(tagged: list[TaggedClaim], original_text: str) -> str: ...
def run_grounding_pass(
    input_path: Path,
    evidence: dict[str, list[Evidence]],   # claim.id -> evidence list
    output_dir: Path,
) -> Path:  # returns path to written grounding.md
    ...
```

**Acceptance criteria (Given/When/Then):**
1. Given a raw file with N numbered claims and evidence for each, When
   `run_grounding_pass` runs, Then it writes `<output_dir>/grounding.md` with
   every claim tagged exactly one of the three tags, original numbering and
   text preserved verbatim.
2. Given a claim with ≥1 supporting evidence item and 0 contradicting items,
   When tagged, Then the result is `VERIFIED` citing that evidence's source
   and date.
3. Given a claim with ≥1 contradicting evidence item (regardless of any
   supporting items also present), When tagged, Then the result is
   `CONTRADICTED` — contradiction always wins over support (conservative).
4. Given a claim with an empty evidence list, When tagged, Then the result is
   `UNVERIFIABLE`, and the rendered output shows it demoted to `ASSUMPTION`.
5. Given claims numbered non-sequentially in the input (e.g. "3." then "7."),
   When parsed, Then `parse_claims` preserves the original id strings —
   never renumbers.
6. Given `output_dir` doesn't exist, When `run_grounding_pass` runs, Then it
   creates the directory (folder-scoped output, no writes outside it).

## Contract 2 — `revision_round.py` (Stage 2.75)

**Objective:** correction-biased revision, triggered only when CSS < 0.50 —
each model may revise only by citing a specific verified fact contradicting
its own claim; "others agree" is explicitly disallowed as a reason to switch.

**Signature:**
```python
@dataclass
class ModelAnswer:
    model: str
    original_text: str
    critique: str  # this model's own Stage 2 critique, not another model's

@dataclass
class RevisionOutcome:
    model: str
    original_text: str
    revised_text: Optional[str]   # None if revision rejected/not offered
    cited_fact_id: Optional[str]  # the verified_facts id it cited, if any
    accepted: bool                # True only if a valid citation was found

def should_trigger_revision(css: float, threshold: float = 0.50) -> bool: ...
def build_revision_prompt(
    answer: ModelAnswer,
    verified_facts: list[TaggedClaim],  # only VERIFIED/CONTRADICTED tagged claims
) -> str: ...
def parse_revision_response(response_text: str, verified_facts: list[TaggedClaim]) -> tuple[Optional[str], Optional[str]]:
    # returns (revised_text_or_None, cited_fact_id_or_None)
    ...
async def run_revision_round(
    css: float,
    answers: list[ModelAnswer],
    verified_facts: list[TaggedClaim],
    query_fn: Callable[[str, str], Awaitable[str]],  # (model, prompt) -> response
) -> list[RevisionOutcome]:
    ...
```

**Acceptance criteria:**
1. Given CSS ≥ 0.50, When `run_revision_round` is called, Then it returns
   immediately with no calls to `query_fn` (cost-safe no-op) and every
   `RevisionOutcome.accepted` is `False`.
2. Given CSS < 0.50, When `run_revision_round` is called, Then `query_fn` is
   called exactly once per model in `answers`, each with a prompt built from
   *that model's own* `original_text`/`critique` only — never another
   model's critique.
3. Given a model's response doesn't cite a specific `verified_facts` id,
   When `parse_revision_response` runs, Then `revised_text` is `None` and
   `accepted` is `False` — the original answer is kept unchanged.
4. Given a model's response does cite a specific `verified_facts` id, When
   parsed, Then `revised_text` is the new text, `cited_fact_id` is set, and
   `accepted` is `True`.
5. Given any revision prompt is built, When rendered, Then it contains the
   verbatim sentence "The other models agreeing with each other is not a
   valid reason to switch." — not paraphrased.
6. Given a revision is accepted, When the outcome is recorded, Then both
   `original_text` and `revised_text` are retained (audit trail) — chairman
   synthesis uses `revised_text` when present, but nothing overwrites
   `original_text`.

### Amendment (2026-08-11): evidence-poisoning / injection risk mitigation

Panel finding (ws-redteam #10): `build_revision_prompt` renders `[id]
(TAG) text` only — the reviewing model never sees *where* a "VERIFIED"
claim came from. Stage 0.5's `real_fetch_evidence` (`live_adapters.py`)
makes exactly **one** web-search call per claim via a single model
(`google/gemini-3.6-flash:online`); `tag_claim` marks a claim VERIFIED off
that one source. Two real risks follow:
1. **Injection surface** — a single adversarial or compromised page could
   get scraped as "supporting" evidence, and the revision prompt's
   "verified fact" framing tells the reviewing model to trust it
   unconditionally (the prompt explicitly *instructs* revision on citing
   a verified fact — an attacker's goal would be exactly to get a false
   claim tagged VERIFIED so a model cites it and revises toward the
   attacker's content).
2. **Overclaiming** — "VERIFIED" from one automated search call reads as
   stronger than it is; a human skimming a run's output could reasonably
   assume multi-source corroboration that never happened.

**Non-goals:** this amendment does not add multi-source corroboration,
source-reputation scoring, or content sanitization of fetched evidence —
those are real mitigations but out of scope for a prompt/labeling fix;
tracked as a known limitation instead (see `docs/upstream-deltas.md`).

**Change:** `build_revision_prompt` now renders each fact as
`[id] (TAG, source: <url or "no source">) text` instead of `[id] (TAG)
text`, and the section header changes from "Verified facts (id, tag,
text)" to "Single-source research findings (id, tag, source, text) — each
comes from one automated web search, not multi-source verification. Weigh
accordingly, do not treat as infallible." The citation mechanism itself
(`[[cite:<id>]]`, `parse_revision_response`'s `valid_ids` matching) is
unchanged — this is a framing/transparency fix, not a structural one.

**New acceptance criteria:**
7. Given a `TaggedClaim` with non-empty `evidence`, When
   `build_revision_prompt` renders it, Then the fact's source URL(s)
   appear in the rendered line (joined with `"; "` if more than one).
8. Given a `TaggedClaim` with empty `evidence` (defensive — shouldn't
   normally reach this function per the existing VERIFIED/CONTRADICTED-only
   filter, but must not crash if it does), When rendered, Then the source
   field reads `"no source"` rather than raising or rendering an empty gap.
9. Given any revision prompt is built with at least one fact, When
   rendered, Then it contains the phrase "not multi-source verification"
   — the softened-authority framing is present, not just the old
   "Verified facts" header.

**Mutation testing (2026-08-11):** `revision_round.py` 104/105 after the
change, same 1 previously-documented equivalent survivor as before this
amendment (`RevisionOutcome`'s explicit `cost_usd=0.0` kwarg matching the
dataclass's own default field value — unobservable either way). No new
gaps from the prompt-template change. Full project suite: 165 passed.

### Amendment (2026-08-12): thread the source document into Stage 2.75

**Problem, confirmed by direct source read:** `build_revision_prompt`
includes the model's own Stage-1 answer, its own critique, and the
verified-facts block — never the original `user_query`/source document.
Native Stage 1/2/3 (`council_stages.py`, confirmed by grepping every
`user_query` reference) all correctly re-include the full document at
every stage; this gap is specific to this project's own Stage 2.75
addition. For a large document, a model revising in Stage 2.75 works from
its own prior summary, not the text itself. Resolved via an 8-persona
expert panel round (`docs/upstream-deltas.md`, "Second Expert Panel
round"): threshold-gated inclusion, not always-verbatim (data-minimization
concern — re-exposing the full document N-models × M-revisions times with
no visibility) and not smart passage-selection (unproven complexity,
unjustified at this project's 2-4 decisions/month scale).

**Decision:** `revision.max_document_tokens = 32000` (user-set,
2026-08-12), token-based via a deliberately simple approximation — this
repo has no tokenizer dependency today (`tiktoken` not installed, checked
directly) and the threshold is a soft cost/egress control, not a
context-fit requirement (all 4 configured models have ~1M-token context,
confirmed live against OpenRouter). Approximation:
`estimate_tokens(text) = len(text) // 4` (documented, conservative
English-text heuristic — never exact, never claimed to be). Below
threshold: the document is threaded into the prompt, kept **structurally
distinct** from `facts_block` (own labeled section, own delimiter) so a
crafted/adversarial document can never forge text matching the
`[[cite:<id>]]` citation-guardrail pattern (unanimous, uncountered
ws-redteam finding — this is a real injection-surface gap the fix itself
would open if the document and facts were rendered adjacently in a way a
document could imitate). Above threshold: a visible, structured omission
marker is rendered in the document's place, and the same marker is
surfaced in the pipeline's Cost & Tokens summary output (not just a debug
log line) — matches the existing `completeness_check_parse_failed`
no-silent-degradation precedent from Contract 4.

**Signature changes:**
```python
def estimate_tokens(text: str) -> int:
    # len(text) // 4 - documented approximation, not exact
    ...

def build_revision_prompt(
    answer: ModelAnswer,
    verified_facts: list[TaggedClaim],
    source_document: str,               # NEW - the original user_query/document
    max_document_tokens: int = 32000,   # NEW
) -> str: ...
```
`run_revision_round` gains a `source_document: str` parameter, threaded
straight through to every `build_revision_prompt` call (same document for
every model in a given round — no per-model variation).

**New acceptance criteria:**
10. Given `source_document` is non-empty and `estimate_tokens(source_document)
    <= max_document_tokens`, When `build_revision_prompt` renders, Then the
    full `source_document` text appears verbatim in its own labeled
    section, textually separated from `facts_block` by a distinct header
    (not concatenated or interleaved).
11. Given `estimate_tokens(source_document) > max_document_tokens`, When
    rendered, Then the document section contains a structured omission
    marker naming the threshold (e.g. `"[document omitted from revision
    prompt - exceeds 32000-token threshold]"`) instead of the document
    text — never a silent, unmarked absence.
12. Given a `source_document` is crafted to contain a literal
    `[[cite:<id>]]`-shaped substring for a real `verified_facts` id, When
    `parse_revision_response` later parses the MODEL's response (not the
    prompt), Then this is unaffected — AC12 exists to confirm the
    document's own placement in the prompt cannot itself satisfy or
    contaminate the citation-parsing contract, since parsing only ever
    looks at the model's response text, never the prompt. Regression test
    specifically renders a document containing a fake citation marker and
    confirms `build_revision_prompt`'s output keeps it inside the
    document's own delimited section, distinguishable from a real
    model-authored citation if a human or a future parser change ever
    looks at raw prompt text.
13. Given `source_document` is empty string (defensive — Stage 2.75 is
    only ever triggered with a real query in practice, but must not
    crash), When rendered, Then no document section is rendered at all
    (not an empty-but-present section) — mirrors `verified_facts`' own
    empty-list handling (`"(no verified facts available)"` for facts,
    the whole section omitted for an empty document is the equivalent
    stance since "empty document" isn't a meaningful degraded state to
    label, unlike "no facts found").
14. Given `estimate_tokens` is called on typical English text, When
    compared against a real tokenizer's count (spot-check only, not a
    contract requirement), Then the approximation is documented as
    conservative-but-approximate in a code comment — no test asserts
    exact parity with any real tokenizer, since none is a dependency.

**Non-goals:** no real tokenizer dependency added; no per-model document
variation; no passage-level/smart truncation (option iii from the panel,
rejected as unproven complexity at this project's scale).

**Mutation testing (2026-08-12, blind-TDV — isolated test author,
implementer, RED→GREEN watched):** 5 non-equivalent survivors, all in
`_build_document_section`, all mutating exact header/footer/
omission-marker text literals (e.g. wrapping `"--- BEGIN SOURCE DOCUMENT
---"` or the omission-marker prefix in `"XX...XX"`, or case-flipping
them). Accepted, not fixed — independently re-verified (not just trusting
the mutation-testing agent's self-report) by reading both
`_build_document_section`'s actual implementation and
`tests/test_revision_round_document_amendment.py`'s own documented
assumptions section: the tests deliberately check for a case-insensitive
`"document"` keyword in the header and a case-insensitive `"omit"` +
literal threshold value in the marker, not byte-exact wording — a direct,
correct reading of this amendment's own contract language above ("exact
wording is the implementer's choice as long as it names the numeric
threshold and is unambiguous that omission occurred"; the header
requirement was "own labeled section... distinct header", never "this
exact string"). Pinning exact wording would over-specify beyond what was
asked, not close a real behavioral gap. Full test suite (`uv run pytest
tests/ -q`) confirmed green (328 passed) with all other contracts'
tests present in the same run — no cross-contract regression.

## Contract 3 — scorecard wrapper (`scorecard.py` + `scorecard` CLI)

**Objective:** append one record per session to a folder-scoped log; report
confidence-gated, non-prescriptive statistics — never auto-recommends
keep/drop.

**Signature:**
```python
@dataclass
class ScorecardRecord:
    timestamp: str
    topic_label: str
    css: float
    rubric_scores: dict[str, dict[str, float]]  # model -> {accuracy, relevance, completeness, conciseness, clarity}
    ranks: dict[str, int]                        # model -> Stage 2 Borda rank
    is_outlier: dict[str, bool]                  # model -> dissent-flagged this session
    cost_usd: dict[str, float]                   # model -> this session's cost share

def build_scorecard_record(session_result: dict, topic_label: str, timestamp: str) -> ScorecardRecord: ...
def default_scorecard_path(cwd: Path) -> Path:
    # cwd / "council-runs" / "scorecard.jsonl" — never ~/.llm-council/
    ...
def append_record(record: ScorecardRecord, path: Path) -> None: ...
def load_records(path: Path, cross_folder: bool = False, search_root: Optional[Path] = None) -> list[ScorecardRecord]:
    # cross_folder=True requires search_root; walks for council-runs/scorecard.jsonl files
    ...
def confidence_tier(n: int) -> Literal["insufficient", "preliminary", "moderate", "high"]:
    # n<10 insufficient, 10<=n<20 preliminary, 20<=n<50 moderate, n>=50 high
    ...
@dataclass
class ScorecardReport:
    session_count: int
    tier: str
    model_avg_vs_others: dict[str, float]   # per rubric dimension, target model avg minus mean-of-others avg
    outlier_sessions: list[tuple[str, str]] # (timestamp, topic_label) where target model was flagged
    cost_share_pct: float

def compute_report(records: list[ScorecardRecord], target_model: str) -> ScorecardReport: ...
def render_report(report: ScorecardReport, target_model: str) -> str: ...  # plain-language, no keep/drop verdict
```

**Acceptance criteria:**
1. Given a completed session result with rubric scores/ranks/CSS/cost for N
   models, When `build_scorecard_record` runs, Then the record includes every
   model present in the session result — none silently dropped.
2. Given `default_scorecard_path(cwd)` is called, When executed, Then it
   returns `cwd/council-runs/scorecard.jsonl` — never any path under
   `Path.home()`.
3. Given `append_record` is called twice against the same path, When the
   file is read back, Then it contains exactly 2 JSON lines, each a valid,
   independently-parseable JSON object (true JSONL — one call never
   corrupts or rewrites a prior line).
4. Given `load_records(path, cross_folder=False)`, When called from a
   directory containing other `council-runs/` folders elsewhere, Then only
   the exact `path` given is read — no implicit aggregation.
5. Given exactly 9, 10, 19, 20, 49, and 50 records, When `confidence_tier`
   is called on each count, Then it returns
   insufficient/preliminary/preliminary/moderate/moderate/high respectively
   (boundary-exact, not off-by-one).
6. Given a target model was `is_outlier=True` in some records, When
   `compute_report` runs, Then `outlier_sessions` lists exactly those
   records' timestamp+topic_label — available for manual review, never
   auto-excluded from the averages.
7. Given `render_report` is called, When the output is inspected, Then it
   contains no keep/drop/recommend/should-remove language of any kind —
   only counts, tier, averages, and the outlier list (explicit non-goal,
   confirmed by string-absence check in tests).
8. Given zero records exist, When `render_report` runs, Then it returns a
   clear "no sessions recorded yet" message — not a crash, not a fabricated
   N=0 average line.

## Contract 4 — `completeness_check.py` (Stage 4, added 2026-08-11)

Named Stage 4, not 3.5 — `run_full_council` already uses "3.5" internally
for its own aggregate-rankings (Borda count) computation
(`llm_council/council.py`, `calculate_aggregate_rankings`), confirmed by
direct source read. This check runs strictly after everything upstream
does (grounding → 1 → 2 → 2.5 → [2.75] → 3 → 3.5), so Stage 4 is the
non-colliding, chronologically accurate label.

**Grounded research driving this** (Feynman-methodology literature pass,
2026-08-11 — see `docs/upstream-deltas.md`'s "Research-driven refinements"
entry for the full ranked findings and citations, all verified live
against arXiv abstracts before acting): Wan et al., "The Deliberative
Illusion: Diagnosing Factual Attrition and Stance Homogenization in
Multi-Agent LLM Deliberation" (arXiv:2606.03032) — multi-agent discussion
can erase up to 72% of issue-critical facts while apparent consensus
strengthens ("agree more while knowing less"). This is a direct structural
risk to this pipeline's own CSS gate: a high Consensus Strength Score
could reflect factual attrition rather than correctness, and nothing in
the current pipeline would ever surface that.

**Objective:** after the chairman's Stage 3 synthesis is produced, check
whether Stage 0.5's VERIFIED/CONTRADICTED facts are actually reflected in
the final answer, and surface (never silently drop) any that aren't. This
is diagnostic-only — it never blocks, edits, or re-triggers synthesis; it
just makes factual attrition visible where it would otherwise be
invisible.

**Non-goals:** this does not re-run or edit the chairman synthesis, does
not feed dropped facts back into another revision round (that would be a
much bigger structural change — conflating this diagnostic with Stage
2.75's correction-biased revision loop, which has its own separate,
already-validated trigger condition), and does not change
`should_trigger_revision`'s CSS-based gate. A no-op (zero cost) when no
grounding happened, matching every other conditional stage in this
pipeline.

**Signature:**
```python
QueryFn = Callable[[str, str], Awaitable[tuple[str, float]]]  # (model, prompt) -> (response, cost_usd)

def build_completeness_prompt(verified_facts: list[TaggedClaim], synthesis: str) -> str: ...
def parse_completeness_response(raw_content: str, verified_facts: list[TaggedClaim]) -> list[str]:
    # returns the subset of verified_facts' ids judged NOT addressed in the synthesis
    ...
async def check_fact_completeness(
    verified_facts: list[TaggedClaim],
    synthesis: str,
    model: str,
    query_fn: QueryFn,
) -> tuple[list[str], float]:
    # returns (dropped_fact_ids, cost_usd)
    ...
```

**Acceptance criteria:**
1. Given `verified_facts` is empty, When `check_fact_completeness` is
   called, Then it returns `([], 0.0)` immediately with no call to
   `query_fn` — cost-safe no-op, same pattern as
   `run_revision_round`'s CSS≥threshold no-op.
2. Given `verified_facts` is non-empty, When `check_fact_completeness`
   runs, Then `query_fn` is called exactly once (a single batched check
   covering every fact, not one call per fact — cost discipline; the
   research finding is about aggregate attrition, not per-fact drift, so
   one call is the right granularity).
3. Given the model's response is a JSON array of ids not addressed, When
   `parse_completeness_response` runs, Then it returns exactly those ids,
   filtered to only ids that actually exist in `verified_facts` (defensive
   against a hallucinated id).
4. Given the model's response is malformed JSON, not a JSON array, or
   otherwise unparseable, When `parse_completeness_response` runs, Then it
   returns `[]` (assume nothing dropped) rather than raising — a
   diagnostic check must never crash the pipeline over its own parse
   failure; degrading to "couldn't determine, assume fine" is the correct
   failure mode here (mirrors `live_adapters.parse_evidence_response`'s
   same never-raise contract).
5. Given `build_completeness_prompt` is called, When rendered, Then it
   contains every fact's id, tag, and text, and the full `synthesis` text
   verbatim — the check has no basis to judge completeness without both.

**Integration into `pipeline_runner.run_pipeline`:** runs once, after
`synthesis` is computed, gated the same way Stage 2.75 revision is gated
against `config.max_cost_usd` (skipped, not silently over-spent, if
`cost_so_far` already meets the ceiling — new field
`completeness_check_skipped_for_cost: bool` on `PipelineResult`, mirroring
`revision_skipped_for_cost`). New `PipelineResult.dropped_facts: list[str]`
field (empty list = nothing dropped or check didn't run). The CLI
(`main()`) prints a stderr warning naming any dropped fact ids but does
**not** get a new exit code for this — dropped facts are a quality signal,
not a cost-outcome signal, and the existing exit-code contract (AC16-20)
is scoped to cost outcomes specifically; conflating the two without a
fresh panel review would be scope creep on an already-settled contract.

### Real bug found and fixed while wiring this in (2026-08-11)

Implementing Stage 4 required reading `verified_facts` — and found
`run_pipeline` had been hardcoding it to `[]` and **never populating it**
from `run_grounding_pass`'s output, even when grounding ran and produced
real VERIFIED/CONTRADICTED tags. `run_grounding_pass`'s own contract
(Contract 1, AC1) correctly writes `grounding.md` to disk with real tags —
that part worked — but its return type is `Path` (the written file), not
the tagged claims, and nothing in `run_pipeline` ever independently
computed them. The result: **Stage 2.75's correction-biased revision has
never, in any real run, been able to receive an actual citable fact** —
`run_revision_round`'s `verified_facts` parameter was always an empty
list, so a model could never satisfy "cite a specific verified fact id"
even when Stage 0.5 had genuinely verified one. Zero existing tests caught
this because every `test_pipeline_runner.py` test exercising revision used
`raw_claims_text=""` (grounding skipped) or never inspected revision
prompt content for grounded-fact text — a second instance of the same
"mocks matching a wrong mental model prove nothing" pattern already
recorded in this doc's 2026-08-09 real-run validation entry (the
`stage3_result["synthesis"]` bug).

**Fix:** `run_pipeline` now also imports `tag_claim` and, immediately
after `run_grounding_pass` writes the file, independently computes
`tagged = [tag_claim(c, evidence_map.get(c.id, [])) for c in claims]` and
filters to `verified_facts = [tc for tc in tagged if tc.tag in
("VERIFIED", "CONTRADICTED")]` — mirroring exactly what `run_grounding_pass`
does internally, without changing that function's own tested return-type
contract. Regression tests added:
`test_verified_facts_from_grounding_reach_revision_prompt`,
`test_contradicted_claims_from_grounding_reach_revision_prompt`,
`test_unverifiable_claims_from_grounding_do_not_reach_revision_prompt` —
all assert on actual grounded-fact text appearing (or not) in the real
prompt sent to `query_model`, not just that grounding.md got written.

**Blast radius:** every real pipeline run to date that triggered Stage
2.75 revision did so with zero real facts available to cite — revision
outcomes were always rejected (`accepted=False`) unless a model
hallucinated a citation matching nothing. This did not corrupt any
recorded scorecard data (rubric scores/rankings are independent of
revision), but it means the "correction-biased revision" feature has been
silently inert since its introduction. No further action needed beyond
this fix — there is no stored state to migrate or backfill.

**Mutation testing (2026-08-11):** `completeness_check.py` 62/62, zero
survivors. `pipeline_runner.py` 475/493, same 18 previously-documented
equivalent survivors as before this change (the file grew — mutant IDs
shifted — but the underlying equivalent categories are unchanged), zero
new gaps from either the Stage 4 wiring or the `verified_facts` bug fix.
Full project suite: 189 passed.

### Amendment (2026-08-11): distinguish "verified clean" from "couldn't tell" (no silent degrade-as-success)

User-requested hardening pass, following the stress-testing work above:
"no silent failing of any step" + "proper debug steps so it's clear what
failed." Auditing every stage for this specifically surfaced one real
design gap in Contract 4 as originally written: `parse_completeness_response`
returned `[]` both when the model's response genuinely listed no dropped
facts AND when the response was malformed and couldn't be parsed at all —
identical output for two completely different situations. A run whose
Stage 4 check silently failed to parse would look, from `PipelineResult`
alone, exactly like a run where Stage 4 genuinely verified nothing was
missing. That's the opposite of what a diagnostic check is for.

**Change:** `parse_completeness_response`'s signature changes from
`-> list[str]` to `-> tuple[list[str], bool]` — `(dropped_ids, parse_ok)`.
`check_fact_completeness`'s return changes from `tuple[list[str], float]`
to `tuple[list[str], float, bool]` — `(dropped_ids, cost_usd, parse_ok)`.
`parse_ok=False` means the check ran (money was spent) but its answer is
undetermined, not "verified clean" — callers must not treat
`dropped_ids=[]` and `parse_ok=False` as good news.

**New acceptance criteria:**
10. Given the model's response is a well-formed JSON array (whether empty
    or non-empty), When `parse_completeness_response` runs, Then it
    returns `(ids, True)`.
11. Given the model's response is malformed JSON, not a JSON array, or
    otherwise unparseable, When `parse_completeness_response` runs, Then
    it returns `([], False)` — never `([], True)`, which would silently
    claim the check succeeded and found nothing.

**`pipeline_runner.run_pipeline` integration change:** new
`PipelineResult.completeness_check_parse_failed: bool` field. The CLI
prints a distinct stderr warning when this is `True` ("the check ran but
its response couldn't be understood — completeness is UNDETERMINED, not
verified"), separate from the existing dropped-facts warning.

### Amendment (2026-08-11): structured per-stage debug log + MAD integrity check

Same hardening pass. Two more additions, both to `pipeline_runner.py`
only (no other file changes):

1. **`PipelineResult.debug_log: list[str]`** — one line appended at every
   stage transition, recording what actually happened: grounding
   ran/skipped and the VERIFIED/CONTRADICTED/UNVERIFIABLE breakdown;
   how many Stage 1 model responses came back; the CSS value; whether
   revision ran/was skipped and why, and how many outcomes were accepted;
   whether Stage 4 ran/was skipped and why, and whether its parse
   succeeded. This is the direct answer to "make it clear what failed" —
   a human debugging a run reads `debug_log` top to bottom instead of
   reverse-engineering behavior from `PipelineResult`'s other fields.
2. **MAD integrity check** — after `council_fn` returns, if
   `len(stage1_results) < 2`, a `"WARNING: only N model(s) participated —
   this is not multi-agent debate"` line is appended to `debug_log`
   (surfaced, not silently accepted as normal). This project's whole
   premise is genuine multi-model debate; a run that silently degraded to
   a single model should never look identical to a real one in the
   output a human reads.

**Non-goals:** this does not change `revision_round.parse_revision_response`'s
return type. Its `(None, None)` "not revising" result is not the same
ambiguity as Stage 4's — the revision prompt explicitly instructs "if you
are not revising, do not include a citation marker," so a marker's
absence is a well-defined signal by the contract's own design, not a
parse failure being silently mistaken for success. `debug_log` still
reports revision's aggregate outcome (N responded, M accepted) for
visibility, without redesigning an already mutation-tested-clean module's
contract.

### Research findings not implemented (2026-08-11)

From the same Feynman-methodology literature pass that produced Stage 4
(all citations verified live against arXiv abstracts before being acted
on, per Pillar 1 — see `docs/upstream-deltas.md`'s "Research-driven
refinements" entry for the full ranked list):

- **Per-reviewer response-order + rubric-dimension-order randomization**
  (arXiv:2406.07791, arXiv:2602.02219 — position bias in LLM-as-judge is
  independent of anonymization). Confirmed by direct source read of
  `llm_council/council_stages.py::stage2_collect_rankings`: response order
  IS already shuffled, but only **once per run** (same order shown to
  every reviewer, not re-randomized per reviewer call), and the 5 rubric
  dimensions (accuracy/relevance/completeness/conciseness/clarity) are
  rendered in a **hardcoded fixed order** every time, for every reviewer,
  every run. Neither is exposed via `unified_config`/`eval_config`
  (confirmed by `grep`). **Not implemented** — this is entirely inside
  `run_full_council`, which this project deliberately uses as-is rather
  than patching installed vendor code (the whole reason it was selected —
  see `docs/tool-selection.md`). Tracked here as a known limitation, not
  silently dropped; worth an upstream feature request if this pipeline's
  use grows to justify it.
- **Non-persona diversity/lateral-thinking prompting for Stage 1
  drafting** (Q2 in the research pass): evidence too thin to act on —
  the closest relevant study (arXiv:2511.07784) attributes debate success
  to model heterogeneity (already satisfied by using 4 distinct frontier
  models), not prompt-level framing tricks, and doesn't test the
  no-persona-framing variant directly. Re-confirms the existing
  no-persona decision; nothing changed.
- **Conditional second query at Stage 0.5** for claims tagged UNVERIFIABLE
  or CONTRADICTED on the first pass (Q1): evidence is narrowly inferred
  from a decomposition-specific finding (arXiv:2602.10380), not directly
  tested for this single-claim, single-query setup, and adds a real extra
  API call per affected claim. Deferred — real-money gate (Pillar 6)
  argues against adding live spend on inferred-not-tested evidence.

## Contract 5 — `audition_tracking.py` (ADR-029 adoption, added 2026-08-12)

**Objective:** track each configured council model's audition-style
lifecycle state (SHADOW/PROBATION/EVALUATION/FULL/QUARANTINE) using
`llm-council-core`'s own already-shipped ADR-029 primitives instead of
building a parallel confidence-tier system by hand — pure information
surfaced in the `scorecard` report, exactly as non-prescriptive as
Contract 3's existing design. Supersedes
`pipeline-architecture-spec.md` §3's originally-planned from-scratch
"scorecard wrapper" state-tracking (that document's `confidence_tier`
thresholds became Contract 3's `confidence_tier`, already shipped and
working — this contract does **not** replace Contract 3, it adds a
second, complementary signal Contract 3 has no equivalent for).

**Revised finding, correcting the panel's initial framing
(`docs/upstream-deltas.md`, "Second Expert Panel round" — that round
worked from the ADR-029 module docstring; this spec is written after
reading `types.py`/`store.py` in full):** the panel's original phrasing
("adopt the tracking core... instead of building custom from scratch")
implied Contract 3's scorecard was redundant. On closer read it is not —
`scorecard.py`'s `confidence_tier` is a pure session-count bucket with no
concept of consecutive-failure tracking, quarantine, or quality-percentile
gating; ADR-029's `AuditionStatus`/`evaluate_state_transition` provide
exactly those signals and nothing Contract 3 already had. The two are
complementary, not overlapping — this contract adds a **new**
`audition.jsonl` log alongside the existing `scorecard.jsonl`, and extends
`scorecard`'s CLI report with one additional section. Contract 3's own
file, tests, and mutation-gate baseline are untouched.

**Confirmed by direct source read (2026-08-12), not guessed:**
- `llm_council.audition.types.evaluate_state_transition` and
  `record_session_result` are pure functions (no I/O, no global state,
  no side effects) — safe to call directly without going through
  `AuditionTracker`/`get_audition_tracker` (the package's own
  higher-level singleton wrapper, which this contract deliberately does
  NOT use, to avoid any hidden default store path).
- `llm_council.audition.store.append_audition_record`/
  `read_audition_records` take a plain `path: str` argument with no
  hardcoded default — safe to point at a folder-scoped path, same
  guarantee Contract 3's `default_scorecard_path` already makes (never
  `~/.llm-council/`).
- Neither of the above touches `LLM_COUNCIL_MODEL_INTELLIGENCE` or any
  other model-intelligence-gated code path (grep-confirmed against
  `types.py`/`store.py`) — using this module does not require enabling
  the flag this project has already decided must stay off.

**Non-goal, explicit and load-bearing:** this contract never wires
audition state to actual model selection, exclusion, or council
composition. `evaluate_state_transition`'s output (a *proposed*
transition, e.g. "this model would move PROBATION → EVALUATION") is
surfaced as one more line in the `scorecard` CLI report, in the same
plain-language, non-prescriptive register Contract 3 already established
— never auto-applied. Dropping or promoting a model stays a human decision
per `pipeline-architecture-spec.md` §2's explicit design ("a proven 3
beats a padded, unproven 4" — the user's call, not code's). This mirrors
why `llm_council.audition.selection`/`voting.py` (the package's own
selection-weight/voting-authority integration, which DOES act on state)
are intentionally never imported here.

**Signature:**
```python
# scripts/audition_tracking.py
from llm_council.audition.types import AuditionState, AuditionStatus, AuditionCriteria, evaluate_state_transition, record_session_result

def default_audition_path(cwd: Path) -> Path:
    # cwd / "council-runs" / "audition.jsonl" - never ~/.llm-council/
    ...

def get_or_init_status(model_id: str, path: Path) -> AuditionStatus:
    # most recent record for model_id per read_audition_records, or a fresh
    # AuditionStatus(model_id=model_id, state=AuditionState.SHADOW) if none exists
    ...

def quality_percentile_from_rankings(model_id: str, aggregate_rankings: list[dict]) -> Optional[float]:
    # model's borda_score's percentile rank among all models in aggregate_rankings
    # (0.0-1.0), or None if model_id isn't present (e.g. it failed to respond)
    ...

@dataclass
class AuditionUpdate:
    status: AuditionStatus                       # the new, persisted status
    proposed_transition: Optional[AuditionState]  # what evaluate_state_transition suggested, if anything

def record_session_for_model(
    model_id: str,
    participated: bool,               # True if model_id appears in stage1_results
    aggregate_rankings: list[dict],   # for quality_percentile derivation; [] if participated=False
    path: Path,
) -> AuditionUpdate:
    ...

def record_session_for_all_models(
    council_models: list[str],
    stage1_results: list[dict],
    aggregate_rankings: list[dict],
    path: Path,
) -> list[AuditionUpdate]:
    # calls record_session_for_model once per council_models entry
    ...

def render_audition_section(updates_or_statuses: list[AuditionStatus]) -> str:
    # plain-language block for scorecard's CLI report: one line per model,
    # state + session_count + (if a transition was proposed this session)
    # "would move to <STATE> next session" - never "should"/"recommend"
    ...
```

**Acceptance criteria (Given/When/Then):**
1. Given a model has no prior record at `path`, When
   `get_or_init_status` is called, Then it returns a fresh
   `AuditionStatus(model_id=model_id, state=AuditionState.SHADOW,
   session_count=0)` — never raises for a first-ever model.
2. Given a model has prior records at `path` (multiple sessions), When
   `get_or_init_status` is called, Then it returns the single
   most-recent-by-append-order record for that `model_id`, matching
   `read_audition_records`'s own "most recent per model" contract.
3. Given `model_id` appears in `stage1_results` (participated),
   When `record_session_for_model` runs, Then the persisted
   `AuditionStatus.session_count` increments by exactly 1 and
   `consecutive_failures` resets to `0` (mirrors
   `record_session_result(success=True)`).
4. Given `model_id` does NOT appear in `stage1_results` (failed/timed
   out that session), When `record_session_for_model` runs, Then
   `session_count` still increments by 1 but `consecutive_failures`
   increments by 1 too (`record_session_result(success=False)`), and
   `quality_percentile_from_rankings` is never called for that model
   (no ranking data exists for a model that didn't respond).
5. Given a model's updated status satisfies
   `evaluate_state_transition`'s criteria for a transition (e.g. exactly
   `shadow_min_sessions` sessions + `shadow_min_days` elapsed), When
   `record_session_for_model` runs, Then `AuditionUpdate.proposed_transition`
   is the new state, but the PERSISTED `AuditionStatus.state` written to
   `path` is unchanged (still the old state) — a proposed transition is
   surfaced, never auto-applied. (A human re-running a separate
   "graduate this model" step, out of scope for this contract, would be
   the only path to actually changing `state` in storage.)
6. Given `record_session_for_all_models` is called with N configured
   council models and M of them present in `stage1_results` (M <= N),
   When it runs, Then it returns exactly N `AuditionUpdate` entries, one
   per configured model — a model that failed to respond still gets a
   failure-recorded entry, never silently skipped.
7. Given `default_audition_path(cwd)` is called, When executed, Then it
   returns `cwd/council-runs/audition.jsonl` — never any path under
   `Path.home()` (same guarantee as Contract 3's AC2).
8. Given `render_audition_section` is called on a list of statuses, When
   the output is inspected, Then it contains no "should"/"recommend"/
   "keep"/"drop" language — matches Contract 3 AC7's existing
   non-prescriptive-language bar, confirmed by the same string-absence
   test pattern.
9. Given a model in `AuditionState.QUARANTINE` with `quarantine_until` in
   the past, When `evaluate_state_transition` is consulted (via
   `record_session_for_model`'s internal call), Then the proposed
   transition is `SHADOW` (cooldown expired) — this project surfaces it
   as information, exactly like any other proposed transition; it does
   not auto-exclude the model from future `council.models` regardless of
   `QUARANTINE` state (non-goal above).

**Pipeline integration:** `pipeline_runner.run_pipeline` gains one call to
`record_session_for_all_models` after `aggregate_rankings` is computed
(same point Contract 3's `extract_rubric_scores_for_scorecard` is already
called), using `_get_council_models()`-equivalent (the configured model
list, threaded in as a new small parameter or read from
`metadata["config"]` if `council_adapter.py`'s wrapper exposes it —
resolved during implementation, not a new architectural decision).
Written to `default_audition_path(output_root or Path.cwd())`, mirroring
`default_scorecard_path`'s own root-selection logic exactly. Failure to
write this log must never fail the pipeline run itself — wrap in the same
best-effort spirit as `debug_log` entries, not a hard dependency.

**`scorecard` CLI integration:** `scorecard.main()` gains an optional
`--show-audition` flag; when set, after `render_report`'s existing output,
it also loads `audition.jsonl` (same `--path`-relative directory
convention) and prints `render_audition_section`'s output for the
requested `--target-model`. Off by default — this is additive, not a
change to Contract 3's existing default CLI behavior.

**Environment:** Python 3.13, `llm-council-core==0.40.1`'s
`llm_council.audition` subpackage (already installed, no new dependency).
Mutation gate via `mutmut`, same 0-non-equivalent-survivor bar as every
other contract in this document.

### Implementation + mutation testing (2026-08-12, blind-TDV)

`scripts/audition_tracking.py` implemented via isolated blind test
authoring → blind implementation → watched RED → GREEN → mutation gate:
**98/99 mutants killed, 0 real survivors** (1 equivalent, individually
verified: the `AuditionStatus(..., session_count=0)` explicit-kwarg-vs-
omitted mutant, confirmed equivalent via `dataclasses.fields()` — the
field's own default is already `0`). Full test suite green (328 passed)
alongside the other two concurrently-developed contracts.

**Pipeline/CLI integration (2026-08-12, implemented directly, not a second
blind-TDV round-trip — thin glue over already-tested building blocks,
matching `main()`-level wiring precedent above):**
- `run_pipeline` gained a `council_models: Optional[list[str]] = None`
  parameter (additive — every existing call site keeps working unchanged).
  When set, calls `record_session_for_all_models` right after the
  scorecard append, writing `<output_root>/audition.jsonl`. Wrapped in
  `try`/`except Exception`, appending a `debug_log` line either way
  ("recorded" or "failed non-fatally (<e>)") — never fails an otherwise-
  successful run, per the contract's own non-goal.
- `main()` now reads `get_config().council.models` and threads it through.
- `scorecard` gained `--show-audition`: after the existing report, loads
  `<scorecard-dir>/audition.jsonl` via `get_or_init_status` and prints
  `render_audition_section`'s output for `--target-model`. Off by default.
- Verified: live CLI smoke test (`scorecard --show-audition` against an
  empty path correctly printed "No sessions recorded yet." plus a fresh
  SHADOW status line) and 3 new regression tests
  (`test_council_models_none_skips_audition_tracking_entirely`,
  `test_council_models_given_records_every_configured_model_including_absent_one`
  — confirms a configured-but-non-responding model still gets a
  failure-recorded entry, matching audition_tracking.py's own AC6 —
  `test_audition_tracking_write_failure_is_non_fatal_to_the_run`). Full
  suite green: 331 passed. Two pre-existing tests needed mechanical
  updates to match the new additive signature/CLI surface (not weakened —
  same assertions, updated to the real new shape): `_run_main`'s
  `fake_run_pipeline` fixture now accepts `council_models=None`;
  `scorecard`'s exact `--help` usage-line assertion now includes
  `[--show-audition]`.
