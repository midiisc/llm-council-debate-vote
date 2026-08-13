# Pending stage wiring — spec (Pillar 2, before code)

Status: ready for implementation. Grounding: this session's own dossier
"still open" list and design docs for each item below - all already
decided/specced in an earlier session pass; this contract only covers
*wiring already-built or already-designed pieces into the live pipeline*,
not new design decisions.

## Contract 1 — daily slug-freshness precheck, wired into `pipeline_runner.py main()`

**Files**: `scripts/live_adapters.py` (new `real_fetch_live_model_ids`),
`scripts/slug_freshness.py` (new `default_slug_freshness_cache_path`),
`scripts/pipeline_runner.py` (`main()`).

**Acceptance criteria:**
1. Given a new `real_fetch_live_model_ids() -> list[str]` async function in
   `live_adapters.py`, When called, Then it GETs
   `https://openrouter.ai/api/v1/models` (no API key required, matching
   `slug_freshness.py`'s own `FetchModelsFn` docstring), parses the JSON
   response's `data[].id` fields via raw `json.loads` (never a summarizing
   fetch tool - this project's own documented reason: WebFetch previously
   truncated/misreported entries on this exact catalog), and returns them
   as a plain list - via `asyncio.to_thread`, matching every other real
   HTTP call in this file.
2. Given a new `default_slug_freshness_cache_path(cwd: Path) -> Path` in
   `slug_freshness.py`, When called, Then it returns
   `cwd / "council-runs" / "slug_freshness_cache.json"` - folder-scoped,
   never `~/.llm-council/`, matching `default_scorecard_path`/
   `default_audition_path`'s existing convention.
3. Given `pipeline_runner.py main()` runs, When it builds the model list to
   check, Then it combines `get_config().council.models` with
   `council_adapter._load_debate_resilience_config().backup_models` -
   every slug this project actually configures, not just the primary
   roster.
4. Given the check completes with `missing_slugs` non-empty, When `main()`
   proceeds, Then it prints a loud warning to stderr before continuing -
   never silently proceeds with a known-dead slug, and never blocks the
   run either (the resilience/backup mechanism already handles a dead slug
   at call time; this precheck's job is visibility, not gatekeeping).
5. Given `fetch_fn` itself raises (network error), When `main()` runs,
   Then the pipeline still proceeds - `check_slug_freshness` already
   converts a fetch failure into `fetch_error` rather than propagating, and
   a broken freshness check must never block a real debate.

## Contract 2 — Stage 5 (reasoning graph) wired into `pipeline_runner.py`

**File**: `scripts/pipeline_runner.py`.

**Acceptance criteria:**
6. Given Stage 4 completes (or is skipped), When `_run_stages()` continues,
   Then it calls `reasoning_graph.build_reasoning_graph` with the Stage 3
   synthesis text and `verified_facts`, per
   `docs/specs/reasoning-graph-contract.md`'s existing design (truly last,
   self-contained, gated on cost ceiling, exception-isolated - a failure
   here must never crash an otherwise-complete run).
7. Given the graph builds successfully, When the stage completes, Then
   `write_reasoning_graph_files` persists it into `output_dir` (matching
   the existing 3-file persistence design) and `debug_log` records success
   with node/edge counts.
8. Given the graph build raises or the cost ceiling is already exceeded,
   When the stage runs, Then it's skipped loudly (`debug_log` entry naming
   why) - never silently, never crashing the run.

## Contract 3 — Stage 3.75 (devil's-advocate critique)

Covered by its own, larger spec:
`docs/specs/stage-3-75-critique-contract.md`.

## Test strategy

Direct implementation, test-first, hand-verified RED->GREEN, matching this
session's established practice.
