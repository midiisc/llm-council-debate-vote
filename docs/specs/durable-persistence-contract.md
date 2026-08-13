# Durable persistence contract (Pillar 2 — spec before code)

Status: ready for blind-TDV. Grounding:
`docs/architecture-stress-test-2026-08-13.md`, Critical #7 ("No durable
transcript/synthesis persistence anywhere in the pipeline"), plus the
`docs/pipeline-architecture-spec.md` §7 design decision this closes ("all
session output is folder-scoped... Stage 1-3 transcripts, CSS, scorecard
data, dissent flags, premortem, final memo") that was never actually built.

## Problem this closes

Today, `pipeline_runner.py` only ever writes `run_status.json` (status +
cost, via the already-atomic `_write_run_status`) and, conditionally,
`grounding.md`. The actual synthesis text, Stage 1 drafts, Stage 2 peer
reviews, CSS, and revision outcomes exist only in memory and get printed to
stdout/stderr — lost the moment the process exits. `debate.py` persists
nothing at all. If a real-money decision's terminal session is lost, the
answer is gone and must be re-run (and re-paid for) to recover.

**Key design requirement, not optional**: each file must be written
**incrementally, as soon as its stage completes** — not batched at the end
— so a mid-run crash or wall-clock timeout still leaves everything that
*did* finish on disk. This mirrors the incremental `cost_so_far` fix
already landed this session and is the whole point of this contract; a
"write everything at the end" implementation would not actually close
Critical #7 for the crash case, which is the case that matters most.

**Overlap note**: `docs/specs/reasoning-graph-contract.md`'s Stage 5 (if/when
wired in) also writes a `synthesis.md` as part of its own output. This
contract's `write_synthesis` must be idempotent with that — write
unconditionally right after Stage 3 completes (not gated on Stage 5 running
at all, since Stage 5 is itself gated/optional and synthesis must be saved
regardless), and if Stage 5 later writes the same file, an identical
overwrite is harmless. Do not make this contract depend on Stage 5 landing
first or at all.

## Contract — `scripts/transcript_writer.py`

**Objective:** a small set of pure, dependency-free write functions (one
per stage's output), each called immediately when its stage completes, all
writing into the run's existing `output_dir`.

**Signature:**
```python
from __future__ import annotations
from pathlib import Path
from typing import Any

def write_stage1_transcripts(output_dir: Path, stage1_results: list[dict]) -> Path:
    """Writes stage1_transcripts.md: one section per model, its raw
    response verbatim, in the order given. Returns the path written."""
    ...

def write_stage2_summary(
    output_dir: Path,
    stage2_results: list[dict],
    aggregate_rankings: list[dict],
    css: float | None,
    is_outlier: dict[str, bool],
) -> Path:
    """Writes stage2_summary.md: CSS, the ranking table (model/rank/score),
    each model's peer-review notes if present in stage2_results, and which
    models (if any) were flagged as outliers. css=None (e.g. single-model
    degraded mode) renders as 'N/A - single model, no peer review' rather
    than a blank/error. Returns the path written."""
    ...

def write_synthesis(output_dir: Path, synthesis_text: str, chairman_model: str) -> Path:
    """Writes synthesis.md: the verbatim chairman synthesis, with the
    chairman model name as a header. Returns the path written."""
    ...

def write_revision_outcomes(output_dir: Path, outcomes: list[Any]) -> Path:
    """Writes revision_outcomes.md: one section per model's revision
    outcome (revised text + cited fact id, or 'not revising'). Only ever
    called when Stage 2.75 actually ran - the caller skips calling this at
    all when Stage 2.75 didn't fire, rather than this function handling an
    empty-outcomes case that shouldn't occur. Returns the path written."""
    ...
```

**Acceptance criteria (Given/When/Then):**

1. Given `stage1_results` (list of `{"model": str, "response": str}`),
   When `write_stage1_transcripts` runs, Then it writes a file containing
   every model's name and its full response text verbatim (no truncation),
   and returns `output_dir / "stage1_transcripts.md"`.

2. Given `stage1_results` is empty (the all-models-failed degraded case),
   When it runs, Then it still writes a file (not skipped) containing an
   explicit "no models responded" statement — never silently absent.

3. Given `aggregate_rankings` and a `css` value, When
   `write_stage2_summary` runs, Then the written file contains the CSS
   value formatted to a reasonable precision, and a table/list with every
   model's rank and score from `aggregate_rankings`.

4. Given `css=None` (single-model degraded mode, matches the shape
   `council_adapter.py` actually produces for that case), When it runs,
   Then the file states plainly that no peer review occurred (single
   model) rather than rendering `None`/`null`/blank or raising.

5. Given `is_outlier` flags one or more models `True`, When it runs, Then
   those models are visibly called out as outliers in the written file,
   not just present in the raw ranking table indistinguishably from
   non-outliers.

6. Given `synthesis_text` and `chairman_model`, When `write_synthesis`
   runs, Then the file contains the model name and the verbatim synthesis
   text with no alteration (no truncation, no re-wrapping that could
   change meaning).

7. Given a list of revision outcomes (mixed: some models revised with a
   cited fact id, some did not revise), When `write_revision_outcomes`
   runs, Then every model's outcome is represented distinctly — a model
   that revised shows its revised text and cited fact id; a model that did
   not revise is explicitly labeled "not revising", never conflated or
   omitted.

8. Given any of these functions is called with a `Path` `output_dir` that
   already exists (guaranteed by `pipeline_runner.py`'s `make_output_dir`
   contract, called before any stage runs), When it writes, Then it never
   raises for the directory already existing, and uses `.write_text(...)`
   (or equivalent) directly — no atomic-temp-rename dance is required for
   these files (unlike `run_status.json`, which is read back mid-run by
   nothing else; these are write-once-per-stage, append-never files, so a
   partial write on true process-kill is an accepted, documented residual
   risk, not something this contract needs to solve with the same rigor as
   the repeatedly-rewritten status file).

## Integration (`pipeline_runner.py` and `scripts/debate.py`)

**`pipeline_runner.py`**: call each write function immediately after its
corresponding stage completes inside `_run_stages()` — `write_stage1_transcripts`
right after Stage 1 returns (before Stage 1.5/2 even start), `write_stage2_summary`
right after Stage 2.5's CSS/outlier computation, `write_synthesis` right
after Stage 3 returns (unconditionally — see "Overlap note" above),
`write_revision_outcomes` right after Stage 2.75 completes, only if it
actually ran. Each call wrapped in the same best-effort
`try/except Exception` idiom already used for Stage 5's extraction block
(a disk-write failure must never crash an otherwise-successful pipeline
run) — log any write failure to `debug_log`, never silently swallow it
either.

**`scripts/debate.py`**: currently writes nothing anywhere. Add a minimal
output directory (mirroring `pipeline_runner.py`'s `make_output_dir`
pattern — folder-scoped, timestamped, under a new `--output-root` flag
defaulting to `./council-runs/`) and call `write_synthesis` at minimum
after a successful run, so an ad hoc real-money debate isn't only ever
visible in a terminal that might get closed. Full transcript/Stage-2
writes are a nice-to-have here but not required by this contract — `debate.py`'s
own stated purpose is quick ad hoc use, and `write_synthesis` alone closes
the worst case (the actual answer being unrecoverable).

## Non-goals

No change to `run_status.json`'s own existing atomic-write pattern. No
new database, index, or cross-run aggregation of these files — this
contract is per-run, folder-scoped persistence only, matching
`pipeline-architecture-spec.md` §7's existing "no writes to
`~/.llm-council/`... all session output is folder-scoped" design. No
retroactive backfill for past runs whose output is already lost. Does not
attempt to fix `run_status.json`'s own separate "no liveness signal for
crash reconciliation" Low finding — tracked separately.
