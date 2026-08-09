# pipeline_runner.py contract (Pillar 2 — spec before code)

Status: ready to implement. Closes the §8.3 orchestration gap in
`docs/pipeline-architecture-spec.md` — chains Stage 0.5 → 1 → 2 → 2.5 →
[2.75] → 3 → 3.5 → scorecard into one real, folder-scoped run.

## Grounded integration decisions (2026-08-09, verified by direct execution, not guessed)

- Uses `llm_council.council.run_full_council(query, models=None)` — NOT
  `consult_council` (MCP tool, returns an unparseable formatted string) or
  `run_council_with_fallback` (returns a trimmed dict with no Stage 2 detail).
  `run_full_council` returns `(stage1_results, stage2_results, stage3_result,
  metadata)` with full Stage 2 structure. `models=None` correctly falls back
  to our configured `council.models` (verified via source + a live cheap
  `quick`-tier call, 2026-08-09).
- `stage2_results` shape (verified via source, `council_stages.py`
  `stage2_collect_rankings`): one entry per reviewer —
  `{"model": <reviewer>, "ranking": <raw text>, "parsed_ranking": {"ranking":
  [...], "scores": {label: weighted_score}, "evaluations": {label:
  {accuracy, relevance, completeness, conciseness, clarity}},
  "rubric_scoring": true}}`. `label_to_model` (from Stage 2) maps
  `"Response A"` etc. back to real model names.
- **`revision_round.ModelAnswer.critique` is built from structured rubric
  data, not raw reviewer prose** — extracting a clean per-model critique
  from free-text `ranking` blobs covering all candidates at once is fragile;
  the `evaluations` dict is already structured per model. This is a
  deliberate adaptation of Contract 2 (§`custom-scripts-contracts.md`) to
  the real API shape, not a shortcut: the critique is still specific to that
  model's own review results, just derived from scores rather than prose.
  Format: `"Reviewers scored your response — accuracy: X.X/10 (n
  reviewers), relevance: X.X/10, completeness: X.X/10, conciseness: X.X/10,
  clarity: X.X/10. Weakest dimension: <name> (<score>)."`, averaged across
  every reviewer that scored that model's response (self-review excluded,
  matching `exclude_self_votes: true`).

## Signature

```python
@dataclass
class PipelineConfig:
    topic_label: str                    # short label for scorecard + folder slug
    query: str                          # the actual council question
    raw_claims_text: str = ""           # Stage 0.5 input; empty = skip grounding
    max_cost_usd: Optional[float] = None  # None = no ceiling
    output_root: Optional[Path] = None  # defaults to Path.cwd() / "council-runs"

@dataclass
class PipelineResult:
    output_dir: Path
    css: float
    revision_triggered: bool
    revision_skipped_for_cost: bool
    total_cost_usd: float
    scorecard_appended: bool
    synthesis: str

# Injected dependencies (same pattern as grounding_pass/revision_round -
# keeps orchestration logic pure/testable, real implementations live in
# live_adapters.py, not here)
FetchEvidenceFn = Callable[[list[Claim]], Awaitable[dict[str, list[Evidence]]]]
CouncilFn = Callable[[str], Awaitable[tuple[list, list, dict, dict]]]  # wraps run_full_council
QueryModelFn = Callable[[str, str], Awaitable[str]]  # for revision_round

async def run_pipeline(
    config: PipelineConfig,
    fetch_evidence: FetchEvidenceFn,
    council_fn: CouncilFn,
    query_model: QueryModelFn,
) -> PipelineResult: ...

def slugify(topic_label: str) -> str: ...
def make_output_dir(output_root: Path, topic_label: str, timestamp: str) -> Path: ...
def build_critique_from_rubric(
    model: str,
    stage2_results: list[dict],
    label_to_model: dict[str, dict],
) -> str: ...
def extract_rubric_scores_for_scorecard(
    stage2_results: list[dict],
    label_to_model: dict[str, dict],
) -> dict[str, dict[str, float]]:  # model -> {accuracy, relevance, completeness, conciseness, clarity}
    ...
```

## Acceptance criteria (Given/When/Then)

1. Given `raw_claims_text` is empty, When `run_pipeline` runs, Then
   `fetch_evidence` is never called and no `grounding.md` is written —
   Stage 0.5 is opt-in, not forced.
2. Given `raw_claims_text` is non-empty, When `run_pipeline` runs, Then
   `fetch_evidence` is called once with the parsed claims, `grounding_pass`'s
   `run_grounding_pass` writes `grounding.md` into the output dir, and its
   verified-facts list is available for a later revision round.
3. Given `council_fn` returns a CSS ≥ 0.50 (from `metadata["quality_metrics"]
   ["core"]["consensus_strength"]`), When `run_pipeline` runs, Then
   `query_model` is never called (no-op revision, cost-safe) and
   `revision_triggered` is `False`.
4. Given `council_fn` returns CSS < 0.50 and `max_cost_usd` is `None` or not
   yet exceeded, When `run_pipeline` runs, Then `revision_triggered` is
   `True`, `run_revision_round` is invoked with `ModelAnswer` objects built
   from `stage1_results` + `build_critique_from_rubric`, and
   `revision_skipped_for_cost` is `False`.
5. Given CSS < 0.50 but the Stage 1-3 cost already reported in `metadata`
   meets or exceeds `max_cost_usd`, When `run_pipeline` runs, Then
   `query_model` is never called, `revision_triggered` is `False`, and
   `revision_skipped_for_cost` is `True` — the run completes with the
   original (unrevised) synthesis rather than silently overspending.
6. Given a run completes (with or without revision), When `run_pipeline`
   finishes, Then exactly one `ScorecardRecord` is appended via
   `scorecard.append_record`, using `extract_rubric_scores_for_scorecard`
   for the per-model dimension scores and `scorecard.default_scorecard_path`
   for the path (folder-scoped, never `~/.llm-council/`) unless
   `config.output_root` is set, in which case the scorecard lives under
   `output_root/scorecard.jsonl`.
7. Given `build_critique_from_rubric` is called for a model with zero
   reviewers scoring it (edge case — shouldn't happen with 4 models and
   self-exclusion, but must not crash), When called, Then it returns a
   clear "no peer scores available" string rather than raising or dividing
   by zero.
8. Given the output directory's parent doesn't exist, When `run_pipeline`
   runs, Then `make_output_dir` creates the full path
   (`<output_root>/<timestamp>-<slug>/`), matching Pillar/§7's folder-scoping
   rule — no writes outside this directory tree, ever.
9. Given `topic_label` contains spaces/punctuation/mixed case, When
   `slugify` is called, Then it returns a filesystem-safe, lowercase,
   hyphen-separated slug with no double hyphens or leading/trailing hyphens.
10. Given the pipeline completes, When `PipelineResult` is inspected, Then
    `total_cost_usd` reflects Stage 1-3 cost plus revision-round cost if
    triggered (not just Stage 1-3), sourced from `metadata`'s usage summary
    plus each `query_model` call's reported cost if available, or a
    documented estimate if the injected `query_model` doesn't report cost.

## Mutation testing result (2026-08-09)

266/277 mutants killed. The remaining 11 are traced and confirmed genuinely
equivalent (identical output for every possible input, not merely untested):

- `slugify`'s `strip("-")` vs `strip("XX-XX")`: both are character-set
  strips over `{X, -}` vs `{-}` — since `slugify`'s own regex guarantees
  only `[a-z0-9-]` ever reaches this line, no lowercase `x` collides with
  the mutant's extra `X`s. Unkillable by construction.
- `_compute_outliers`'s `len(scores) < 2` vs `<= 2` / `< 3`: worked through
  the algebra — for exactly 2 points, `median - 1.5*pstdev` is provably
  never exceeded by either point (proof: for `a < b`, the threshold reduces
  to `1.25a - 0.25b`, and `a < 1.25a - 0.25b` iff `a > b`, contradiction).
  The guard and the real computation converge to the same all-`False`
  result for `len <= 2` regardless of the exact boundary chosen.
- `run_pipeline`'s `revision_cost` `+`/`-`: `revision_cost` is a documented
  known-0 placeholder (§ AC10 note below) since `query_model`'s signature
  can't report cost — `+0.0` and `-0.0` are identical until that's wired to
  a real cost-reporting adapter.
- `datetime.now(timezone.utc)` vs `datetime.now(None)`: both produce a
  validly-formatted timestamp string; UTC-vs-local isn't observable from
  the directory name alone without injecting a controllable clock.
- The internal `_raw_claims.txt` scratch filename: written and unlinked
  with the same (mutated) name consistently — never a documented part of
  the contract, purely an implementation-internal scratch file.
- `build_critique_from_rubric`'s `max(..., default=0/None/1)`: the
  preceding `if not averages: return ...` guard makes the `default=`
  branch of that `max()` call structurally unreachable — `averages`
  non-empty implies at least one non-empty score list exists.
- `_rubric_scores_for_model`'s `label_for_model = None` vs `""` initial
  value: when no match is found, `None` triggers an early return while
  `""` falls through to `evaluations.get("")` (always `None`) — both
  converge to the same empty result via different paths.

None of these represent a real behavioral gap; re-verify this list only if
the corresponding functions change.

## Real end-to-end validation (2026-08-09)

First real run of the fully-wired pipeline (grounding → council → conditional
revision → scorecard), from a throwaway directory, decision: "Markdown-in-git
vs. Confluence wiki for a 5-person team." Caught one more real bug in the
process: `stage3_result`'s actual key is `"response"`, not `"synthesis"` —
my own test fixture had baked in the same wrong assumption as the
implementation, which is exactly why 27 passing unit tests didn't catch it.
Fixed both, mutation gate re-confirmed clean afterward (same 266/277, same
11 confirmed-equivalent survivors).

Result: **CSS 0.672** (above the 0.50 threshold — revision round correctly
not triggered; that branch remains verified via unit tests with injected
low-CSS fixtures rather than a live controversial-topic run, which would
cost more without adding meaningfully to confidence given the thorough
mutation-tested coverage on that path). Total cost **$0.313**. Folder-scoping
confirmed clean end to end — nothing written to `~/.llm-council/` at any
point.

Grounding pass caught something real, not just plumbing: claim 2
("Git-based Markdown wikis require every contributor to know git") came back
**CONTRADICTED**, citing GitHub/GitLab's own wiki docs (both major git hosts
ship a web UI wiki editor that needs no git knowledge) — a genuine catch, not
a null result. Claim 1 ("Confluence is the most widely used enterprise wiki
software") came back UNVERIFIABLE and was correctly demoted to ASSUMPTION
rather than asserted.

Real per-model rubric scores, delivered for the first time — the original
point of adding GLM-5.2:

| Model | Accuracy | Relevance | Completeness | Conciseness | Clarity | Rank | Cost |
|---|---|---|---|---|---|---|---|
| Claude Opus 4.8 | 9.5 | 10 | 9.25 | 9.0 | 9.5 | 1 | $0.2047 |
| GPT-5.5 | 9.25 | 9.75 | 9.5 | 7.5 | 9.0 | 2 | $0.0700 |
| Gemini 3.6 Flash | 9.25 | 10 | 8.75 | 8.25 | 8.75 | 3 | $0.0364 |
| GLM-5.2 | 9.25 | 10 | 8.25 | 8.0 | 9.0 | 4 | $0.0019 |

GLM-5.2 ranked last but wasn't flagged as a statistical outlier
(`is_outlier: false` for all 4 models this session) — its scores track
closely with the core three rather than diverging sharply, at roughly 1% of
Claude Opus's cost share. One session is far below the 10-session
"insufficient" confidence floor — this is a first data point for
`scorecard.py`'s tracking, not a verdict.
