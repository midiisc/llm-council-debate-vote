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
