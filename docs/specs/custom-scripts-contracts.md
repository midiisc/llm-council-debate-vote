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
