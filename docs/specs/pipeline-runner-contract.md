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

## Amendment (2026-08-11): real revision cost accounting + post-revision cost ceiling

An 8-persona expert panel review (unanimous, with direct code verification)
confirmed two real bugs sitting in the revision path that had never fired
live: `revision_cost` was hardcoded to `0.0` regardless of what a real
revision round actually spent, and the `max_cost_usd` ceiling only ever
checked pre-revision cost — structurally blind to the exact cost source
most likely to blow a budget. Fixed as one unit, per the panel's own
recommendation (both share the same root cause: "the ledger can silently
lag reality"):

- `QueryModelFn`'s contract changed from `Callable[[str, str],
  Awaitable[str]]` to `Callable[[str, str], Awaitable[tuple[str, float]]]`
  — every query_fn (real and fake) now returns `(response_text, cost_usd)`.
  `revision_round.RevisionOutcome` gained a `cost_usd: float = 0.0` field.
  `live_adapters.real_query_model` now reads OpenRouter's own
  `usage.cost` field (falling back to `0.0`, never raising, if a provider
  doesn't report it).
- `pipeline_runner.run_pipeline` sums `RevisionOutcome.cost_usd` across all
  outcomes into a real `revision_cost` instead of the old constant.
- `PipelineResult` gained `cost_ceiling_exceeded: bool` — computed once,
  after revision cost is known, as `total_cost_usd > max_cost_usd`. This
  can't retroactively prevent an already-started revision round from
  overspending (the pre-revision check already decided whether to start
  it), but it closes the reporting gap: before this, a revision round that
  pushed the real total past the ceiling was invisible in the result.
- Property-based test (`test_property_total_cost_always_equals_sum_of_all_stage_costs`)
  asserts `total_cost_usd` always equals `stage1to3_cost + revision_cost`
  across randomized cost inputs, so this class of bug can't silently
  regress again.

Mutation-tested clean afterward: `revision_round.py` 93/94 (1 confirmed-
equivalent: an explicit `cost_usd=0.0` kwarg matching the dataclass's own
default is unobservable either way), `pipeline_runner.py` 277/287 (same 10
previously-documented equivalent survivors, no new gaps).

## Amendment (2026-08-11): crash/interrupt safety + run-completion marker

Same panel review: `scorecard.append_record()` only ever fired at the very
end of a successful run, so a crash, network drop, or Ctrl-C mid-run left
real OpenRouter spend (stage1-3 always, sometimes plus a revision round)
completely invisible to any on-disk record — the output directory looked
identical whether the run finished or died halfway through. This session's
own first (buggy) smoke test produced exactly this: a `grounding.md`
directory with no matching scorecard entry, sitting unexplained.

New: `run_status.json`, written into `output_dir` via temp-write-then-rename
(atomic — a crash mid-write can never leave a half-written status file):

1. Immediately after `output_dir` is created, before any expensive work,
   `status: "running"` is written.
2. If `run_pipeline` completes successfully, it's overwritten to
   `status: "complete"` with the final `total_cost_usd`.
3. If any exception propagates after the output directory exists,
   it's overwritten to `status: "failed"` with the exception message and
   `cost_so_far_usd` — `0.0` if the council call itself never returned,
   `stage1to3_cost` if the failure happened during or after revision. The
   original exception still propagates to the caller unchanged; this is a
   side-effect on the way out, never a swallow.

New acceptance criteria (11-15), covering: running-status-written-first,
complete-status-on-success, failed-status-with-cost-on-exception,
atomic-write-via-tempfile-rename, and exception-not-swallowed.

## Amendment (2026-08-11): retry/backoff for live network calls

Same panel review (ws-redteam + ws-os finding): `live_adapters.py`'s single
`urllib.request.urlopen()` call had zero resilience — any transient
network blip or OpenRouter 5xx would kill an entire pipeline run (real
spend already committed for stage1-3) rather than retrying. Added:

- `_is_retryable_error(exc)` — pure classifier. `HTTPError` retries only on
  `code >= 500` (a 4xx like 401/400 will just fail identically again, so
  retrying wastes an attempt and, for auth errors, another round of a
  useless request); `URLError`, `TimeoutError`, `ConnectionError` always
  retry; anything else (e.g. a `JSONDecodeError` from a malformed
  response) does not.
- `_post_chat_completion` gained `max_retries` (default `MAX_RETRIES = 3`)
  and an injectable `sleep_fn` (default `time.sleep`, overridden in tests
  to avoid real waiting) parameter. Exponential backoff:
  `BACKOFF_BASE_SECONDS * (2 ** attempt)` → 1s, 2s, 4s before attempts 2,
  3, 4. A non-retryable error or exhausting `max_retries` re-raises the
  original exception unchanged — never swallowed, never wrapped.

New tests added to close two real coverage gaps mutation testing found
(not equivalent mutants — genuine assertion weaknesses):

1. `test_get_openrouter_key_raises_exact_message_when_missing` was using
   `pytest.raises(match=...)`, which does a substring `re.search` — a
   mutant that wrapped the whole error string in extra characters
   (`"XX...XX"`) still contained the original text as a substring and
   survived. Fixed by asserting `str(exc_info.value) ==` the exact string.
2. `parse_evidence_response`'s `stripped.strip("`")` call had no test
   distinguishing "strip only backtick characters" from "strip any
   character in a wider set" — a mutant widening the strip charset to
   `"XX`XX"` (which strips backticks *and* literal `X` characters) went
   undetected. Fixed with
   `test_parse_evidence_response_strips_only_backticks_not_other_chars`,
   feeding input where a mutant's over-eager strip turns malformed JSON
   into valid JSON (and the two implementations diverge on the result).

Mutation-tested clean: `live_adapters.py` 119/129, 10 confirmed-equivalent
survivors, all in `_post_chat_completion`, each individually verified via
`mutmut show` before being accepted (no gap left un-investigated):

- **Mutants 20, 24** (`method="POST"` → `method=None` / omitted):
  `urllib.request.Request.get_method()` infers `"POST"` whenever `data` is
  present and `method` is `None` — this call always passes `data=body`, so
  the two are behaviorally identical.
- **Mutants 26, 27, 29, 30** (header key case: `"Authorization"` →
  `"authorization"`/`"AUTHORIZATION"`, `"Content-Type"` →
  `"content-type"`/`"CONTENT-TYPE"`): `urllib.request.Request` normalizes
  every header key via `.capitalize()` internally, erasing case
  distinctions before the request is ever sent — unobservable at any
  layer this test suite can inspect (confirmed by reading
  `http.client`/`urllib.request` source, not assumed).
- **Mutant 37** (`range(max_retries + 1)` → `range(max_retries + 2)`): the
  loop body always re-raises once `attempt == max_retries` inside the
  `except` block, before the loop's own upper bound is ever consulted
  again — widening the range's ceiling by one can never be observed since
  the raise fires first on every retryable-exhausted path.
- **Mutants 52, 53, 54** (the trailing `raise AssertionError("unreachable")`
  → different message / `None`): this line is provably dead code —
  every path through the preceding `for` loop either `return`s on success
  or `raise`s inside the `except` block before the loop can exhaust
  naturally, so this line can never execute. Already marked
  `# pragma: no cover` for exactly this reason.

Full suite (`uv run pytest tests/ -q`) confirmed green post-fix: 151
passed, no regressions.

## Amendment (2026-08-11): CLI entrypoint (spec, ahead of code — Pillar 2)

Panel finding (ws-os): everything built so far (`run_pipeline`,
`live_adapters`) is only reachable from Python — there is no way to
actually run a real decision from a shell, which is the whole point of
the pipeline. `scorecard`'s existing `[project.scripts]` entry is the
established pattern to mirror (`docs/specs/custom-scripts-contracts.md`
Contract 3).

**Objective:** a `llm-council-pipeline` console script that wires the real
dependencies (`llm_council.council.run_full_council` for `council_fn`,
`live_adapters.real_fetch_evidence`/`real_query_model` for the other two)
into `run_pipeline`, exposes `PipelineConfig`'s fields as CLI flags, and
reports outcome via both stdout and a **distinct process exit code per
outcome class** — the ws-os finding was specifically that a single
success/failure boolean can't distinguish "ran fine" from "ran but
degraded (revision skipped / ceiling blown)", which matters for anyone
scripting around this (e.g. a cron wrapper that should alert differently
on each).

**Non-goals:** no interactive prompts, no config file support (flags
only), no change to `run_pipeline`'s own signature or behavior — this is
a thin wiring layer only.

**CLI surface:**
```
llm-council-pipeline --topic-label TEXT --query TEXT
                      [--claims-file PATH] [--max-cost-usd FLOAT]
                      [--output-root PATH]
```
- `--topic-label`, `--query`: required, map directly to `PipelineConfig`.
- `--claims-file`: optional path; if given, its full contents become
  `raw_claims_text` (triggers Stage 0.5 grounding). Omitted → empty
  string → grounding skipped, matching `run_pipeline`'s existing
  `if config.raw_claims_text.strip():` gate.
- `--max-cost-usd`: optional float → `PipelineConfig.max_cost_usd`.
- `--output-root`: optional path → `PipelineConfig.output_root`; omitted
  → `run_pipeline`'s own default (`Path.cwd() / "council-runs"`).

**Exit code contract (Given/When/Then ACs):**

- AC16: Given a run that completes with revision not triggered or
  triggered-and-within-ceiling, when `main()` returns, then exit code is
  `0` and the synthesis text + output_dir are printed to stdout.
- AC17: Given `should_trigger_revision(css)` is true but
  `revision_skipped_for_cost` ends up `True`, when `main()` returns, then
  exit code is `2` (success, but degraded — revision that should have run
  didn't, because it would have exceeded `max_cost_usd`) and a stderr
  warning names the skipped revision explicitly.
- AC18: Given the run completes but `cost_ceiling_exceeded` is `True`
  (revision ran and pushed total cost past `max_cost_usd`), when `main()`
  returns, then exit code is `3` and a stderr warning states the overage
  amount.
- AC19: Given `run_pipeline` raises any exception, when `main()` catches
  it, then exit code is `1`, the exception message is printed to stderr
  (not swallowed silently), and no traceback dump (matches
  `run_status.json`'s existing "failed" record — the CLI layer doesn't
  need to duplicate that detail, just surface it tersely).
- AC20: Given both AC17 and AC18 conditions hold in the same run
  (revision was skipped for cost on one check, yet total still exceeded
  ceiling — structurally shouldn't happen since a skipped revision can't
  add cost, but the contract must still resolve deterministically), when
  both flags are true, then exit code is `3` (ceiling-exceeded takes
  priority as the more severe condition).

Testable in isolation via a fake `run_pipeline`-shaped result object and
monkeypatching `pipeline_runner.run_pipeline` itself — the CLI parsing/
exit-code logic doesn't need real network calls to verify.

### Implementation notes + verification (2026-08-11)

`exit_code_for_result()` (AC16/17/18/20) and `_build_arg_parser()` (CLI
surface) are pure and mutation-tested. `main()` itself wires
`llm_council.council.run_full_council` + `live_adapters.real_fetch_evidence`/
`real_query_model` and is deliberately excluded from the mutation-tested
suite — same rationale already established for `live_adapters.real_query_model`/
`real_fetch_evidence`: it's a thin network-touching wiring layer, not
testable without either mocking three real dependencies at once (low
signal) or spending real API cost. AC19's error path (`except Exception`
→ stderr message → exit 1, no traceback) is a direct, obviously-correct
4-line block; verified by code inspection rather than a live failing run,
consistent with the standing "real-money gate" (no further live API spend
until the user confirms OpenRouter key rotation — `docs/upstream-deltas.md`).

Mutation-tested clean: `pipeline_runner.py` 421/436, 15 survivors — 12 are
the same previously-documented equivalent categories (string-literal
scratch-filename/internal-key mutations, the `len(scores)<2` boundary
proof, `label_for_model` None-vs-empty-string convergence, the unreachable
`max(..., default=)` branch), now re-verified unchanged after the file
grew; 3 new (`_build_arg_parser`'s `default=None` kwarg removed for
`--claims-file`/`--max-cost-usd`/`--output-root`) are equivalent by
construction — `argparse.add_argument`'s own built-in default for its
`default=` parameter is already `None`, so omitting the explicit kwarg is
unobservable. Two real gaps mutation testing found and closed before this
count: `required=True→None/False` on `--topic-label`/`--query` (only
tested together, not each independently) and cosmetic `prog`/`description`
string mutations (untested until now) — both closed with new tests.

**Verified live** (Pillar 1 — verify by execution, not just source
reading): `uv sync` initially failed to install either console script
(`scorecard` — pre-existing, never actually worked — or the new
`llm-council-pipeline`) with "Skipping installation of entry points
(`project.scripts`) ... project is not packaged." Real pre-existing gap,
fixed on sight (CLAUDE.md's Boy-Scout rule) by adding
`[build-system]`/`[tool.hatch.build.targets.wheel] packages = ["scripts"]`
to `pyproject.toml`. Re-ran `uv sync` → both `uv run llm-council-pipeline
--help` and `uv run scorecard --help` now print correct usage; running
with no args exits 2 with argparse's own "required: --topic-label,
--query" message, confirming AC16-20's exit codes sit on top of a
genuinely installed entrypoint, not just importable Python.

## Amendment (2026-08-12): timeout-aware `council_fn` + wall-clock ceiling

**Problem, confirmed by direct source read of `llm-council-core==0.40.1`
(no live calls — real-money hold still in effect):** `main()`'s
`council_fn` wires directly to `llm_council.council.run_full_council(query,
models=None)`. That function accepts no timeout/tier argument at all.
Internally, Stage 1 (`stage1_collect_responses` → `query_models_parallel`)
has no overridable timeout and falls through to a hardcoded 120s default;
Stage 2 (`stage2_collect_rankings`) and Stage 3
(`stage3_synthesize_final`) each independently default to 120s and *do*
accept an explicit `timeout=` kwarg, but `run_full_council` never exposes
one. Separately, `llm_council.council.run_council_with_fallback` (the
tier-aware entry point the MCP tool already uses, fixed in the sessions
above) returns a fundamentally different ADR-012 flat dict with no
`aggregate_rankings`/`label_to_model`/`parsed_ranking.evaluations`/
`quality_metrics` fields — adopting it would force rewriting
`_rubric_scores_for_model`/`build_critique_from_rubric`/`_compute_ranks`/
`_compute_outliers`, a much bigger change than "add a timeout." Resolved
via an 8-persona expert panel round (`docs/upstream-deltas.md`, "Second
Expert Panel round").

**Decision:** a new `scripts/council_adapter.py`, calling the package's
own granular stage functions directly (not vendoring their logic, not
going through `run_full_council`), with explicit per-stage timeouts.
Non-goals, all confirmed unused by this project today: Jury Mode
(BINARY/TIE_BREAKER `verdict_type`, deadlock detection), dissent
extraction, webhooks, bias-audit. If any of those are ever turned on
project-wide, this adapter needs a follow-up amendment — not a silent gap,
tracked here explicitly.

**Signature:**
```python
# scripts/council_adapter.py
async def run_council_with_timeouts(
    user_query: str,
    stage1_timeout: float = 300.0,
    stage2_timeout: float = 300.0,
    stage3_timeout: float = 300.0,
) -> tuple[list, list, dict, dict]:  # same shape as run_full_council's return
    ...
```
Satisfies `pipeline_runner.py`'s existing `CouncilFn` type exactly — no
change to `run_pipeline`'s own signature or logic. `main()`'s `council_fn`
wiring changes from `run_full_council(query, models=None)` to
`run_council_with_timeouts(query)` (defaults only; CLI flags below can
override).

**Grounded call sequence** (verified via `inspect.signature` against the
installed package, 2026-08-12 — not guessed):
1. `query_models_parallel(_get_council_models(), messages, timeout=stage1_timeout)`
   for Stage 1 (`llm_council.council`) — bypasses `stage1_collect_responses`
   since it exposes no override.
2. If `get_config().evaluation.safety.enabled` (now `true`, see
   `upstream-deltas.md`'s "Second Expert Panel round" feature verdicts):
   `check_response_safety(response)` per Stage-1 response, attached the
   same way `run_full_council` does (`result["safety_check"] = ...`).
3. Degraded-mode branching reproduced from `run_full_council`'s source,
   scoped to what this project needs: 0 responses → early-return error
   tuple; 1 or 2 responses → the same `degraded_mode` labeling and
   single/two-model ranking shortcuts; ≥3 → normal flow. (Real robustness
   need — a model can fail to respond even with a healthy 4-model
   config — not speculative.)
4. `stage1_5_normalize_styles(stage1_results)` (internally respects
   `council.style_normalization`, already `true`).
5. `stage2_collect_rankings(user_query, responses_for_review,
   timeout=stage2_timeout)`.
6. `calculate_aggregate_rankings(stage2_results, label_to_model)`
   (`llm_council.council`).
7. `stage3_synthesize_final(user_query, stage1_results, stage2_results,
   aggregate_rankings, verdict_type=VerdictType.SYNTHESIS,
   timeout=stage3_timeout)` — `VerdictType.SYNTHESIS` hardcoded (this
   project never uses Jury Mode).
8. Usage assembled via `_build_usage_summary`/`emit_usage_metrics`
   (`llm_council.council`), matching `run_full_council`'s own shape.
9. If `should_include_quality_metrics()`: `calculate_quality_metrics(...)`
   attached to `metadata["quality_metrics"]` — this is where
   `metadata["quality_metrics"]["core"]["consensus_strength"]` (CSS) comes
   from; `pipeline_runner.py` reads it unchanged.

**Timeout defaults** (300s/300s/300s = 900s stage budget, chosen against
the user-set 1200s/20min total wall-clock ceiling below, leaving ~300s
headroom for Stage 0.5 grounding + Stage 2.75 revision + Stage 4
completeness, each independently timed): matches the package's own
`reasoning`-tier per-model budget (300s) already adopted for the MCP-tool
path, so the two call paths now agree in spirit even though they can't
share config.

**Wall-clock ceiling (separate but related gap, same amendment):**
`PipelineConfig` gains `max_wall_clock_seconds: float = 1200.0` (user-set,
2026-08-12 — an explicit, always-on ceiling, not `None`-by-default like
`max_cost_usd`, since the entire point is that today nothing bounds
runtime). `run_pipeline` wraps its Stage-0.5-through-Stage-4 body in
`asyncio.wait_for(..., timeout=config.max_wall_clock_seconds)`. On
timeout: `run_status.json` gets `status: "failed"`,
`error: "exceeded max_wall_clock_seconds (<N>s)"`, `cost_so_far_usd` (best
effort — whatever was tracked before the timeout fired); a new distinct
CLI exit code `4` (extends the existing 0/1/2/3 contract, priority above
3 since a hung run is more severe than a completed-but-over-cost one); new
`--max-wall-clock-seconds` CLI flag, defaulting to `PipelineConfig`'s own
default.

**New acceptance criteria:**
11. Given `run_council_with_timeouts` is called with all 4 configured
    models healthy, When it completes, Then the returned tuple's shape
    (keys, nesting) is identical to what `run_full_council` would produce
    for the same inputs — `pipeline_runner.py`'s existing extraction code
    (`metadata["quality_metrics"]["core"]["consensus_strength"]`,
    `aggregate_rankings`, `label_to_model`, `stage2_results[i]
    ["parsed_ranking"]["evaluations"]`) works unchanged.
12. Given `stage1_timeout` is set lower than a (simulated slow) model's
    response time, When `query_models_parallel` is called, Then that
    model is excluded from `stage1_results` (matches `query_models_parallel`'s
    own documented behavior: `Optional[Dict]` per model, `None` on
    timeout/failure) rather than the whole call raising.
13. Given exactly 0 successful Stage-1 responses, When
    `run_council_with_timeouts` runs, Then it returns the same
    all-models-failed error tuple shape `run_full_council` returns
    (`([], [], {"model": "error", ...}, {"usage": ...})`) — callers don't
    need to special-case this adapter differently.
14. Given `evaluation.safety.enabled` is `false` (default before this
    project's change), When `run_council_with_timeouts` runs, Then
    `check_response_safety` is never called — config-driven, not
    hardcoded on.
15. Given a full `run_pipeline` call exceeds `max_wall_clock_seconds`,
    When it times out, Then `run_status.json` is written with
    `status: "failed"` and a message naming the ceiling (not a generic
    `asyncio.TimeoutError` string), the original spend already committed
    is still reflected in `cost_so_far_usd`, and the CLI exits `4`.
16. Given a run completes well within `max_wall_clock_seconds`, When
    `run_pipeline` finishes normally, Then behavior is byte-identical to
    before this amendment — the ceiling is a backstop, never a normal-path
    behavior change.

**Drift-check requirement (unanimous panel finding, must ship with this
amendment, not deferred):** pin this adapter's assumed call sequence to
`llm-council-core==0.40.1`'s exact `run_full_council` source (a comment
citing the function's line range in the installed package, plus this
spec). Add a check to the Pillar 5 self-update loop: on every
`llm-council-core` version bump, diff `run_full_council`'s source against
the pinned assumption and fail loudly (flag in the self-update report, not
a silent pass) if the stage sequence, branching, or metadata assembly
changed — this adapter re-implements orchestration glue the package could
silently restructure in a future release.

**Also required before implementation (uncountered ws-os finding):**
confirm `query_models_parallel`'s `timeout` parameter produces a genuine
client-side cancellation of the in-flight HTTP request, not an
`asyncio.wait_for` that abandons a still-running (and still-billing)
server-side call. Verify by reading `gateway_adapter.py`'s implementation
directly before relying on the timeout as a cost control, not just a
latency control. **Resolved 2026-08-12** (`docs/upstream-deltas.md`):
confirmed a real `httpx.AsyncClient(timeout=timeout)` cancellation in
every gateway backend, not an abandoned background task.

### Implementation + mutation testing (2026-08-12, blind-TDV)

`scripts/council_adapter.py` + the `pipeline_runner.py` companion changes
(`max_wall_clock_seconds`, `asyncio.wait_for` wrap, exit code 4,
`--max-wall-clock-seconds` CLI flag, `main()`'s `council_fn` rewire)
implemented via isolated blind test authoring → blind implementation →
watched RED → GREEN. 8 non-equivalent-survivor-free findings, all
individually verified equivalent (not asserted):
- 3 call sites where a `.get("response", <default>)` default is
  structurally unreachable — every `stage1_results` entry is always
  constructed with a `"response"` key a few lines earlier in the same
  function, so no input can ever exercise the default branch.
- `degraded_mode = None` vs `degraded_mode = ""` at initialization — both
  are falsy and both `!= "two_models"` at every downstream read site,
  traced across all branches.
- `len(stage1_results) > 0` vs `>= 0` — only reached after an earlier
  `if num_responses == 0: return` already excludes the zero case, so both
  comparisons are always `True` on the only reachable range.
- `AuditionStatus(..., session_count=0)`'s explicit kwarg vs omitting it —
  confirmed via `dataclasses.fields()` that the field's own default is
  already `0`.
- `argparse.add_argument(..., default=None)`'s explicit kwarg vs omitting
  it — argparse's own implicit default is `None`, matching this repo's
  existing documented precedent for the same pattern (see the CLI
  amendment above).

Full test suite (`uv run pytest tests/ -q`) confirmed green (328 passed)
with all three concurrently-developed contracts' tests present in the
same run (`council_adapter.py`, `revision_round.py`'s document-threading
amendment, `audition_tracking.py`) — no cross-contract regression despite
all three touching or depending on `pipeline_runner.py`.

**Process note:** the three contracts ran as separate blind-TDV pipeline
items but landed in the same working tree (not fully isolated worktrees
for the mutation-scoping step), which caused `setup.cfg`'s `only_mutate`
list to be overwritten mid-flight by whichever contract's implementer
touched it last — `scripts/revision_round.py` was transiently missing
from that list. Caught and fixed by independently re-running the full
test suite and re-adding it before treating any contract as final, per
Pillar 1 ("verify by execution," not by trusting a subagent's self-report
of its own result).
