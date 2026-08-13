# Daily slug-freshness precheck contract (Pillar 2 — spec before code)

Status: **spec only, ready for blind-TDV — not yet implemented.** User
explicitly scoped this session to docs/config, not code (see
`docs/agent-model-reasoning-config.md` §7). Queued as the next
implementation unit.

Grounding: `docs/upstream-deltas.md`, "Kimi K3 slug drift (2026-08-13)" —
this project has now hit two real dead-slug incidents in its lifetime
(`z-ai/glm-5.2-20260616` on 2026-08-12, `moonshotai/kimi-k3-20260715` on
2026-08-13), both caught only by manual live-catalog grepping after the
fact. User's explicit request: validate every configured slug is live
*before* the first debate call of each calendar day, cached so the check
runs at most once/day, loud not silent on failure.

## Problem this closes

Today, nothing in this project checks whether a configured OpenRouter slug
is still live before spending a real API call on it. A dead slug fails at
call time — for `pipeline_runner.py`'s path via `resilient_query.py`
(once built per `debate-resilience-contract.md`), a dead primary burns its
full retry budget (up to `max_attempts` tries × backoff delay) before
falling back to a backup, even though a dead slug fails deterministically
and retrying it can never succeed — wasted latency and a confusing status
(`"error"`, not obviously "this model doesn't exist anymore") for what is
actually a config-drift problem, not a transient failure.

## Contract — `scripts/slug_freshness.py`

**Objective:** given the full set of configured slugs (core roster +
backup pool), confirm each one still exists on live OpenRouter, but do the
live check at most once per calendar day — cached and keyed to the config
itself, so an intraday edit to `llm_council.yaml` still gets checked, but
repeated pipeline runs on the same day and same config don't re-fetch the
full model catalog every time.

**Signature:**
```python
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

FetchModelsFn = Callable[[], Awaitable[list[str]]]
# Returns every live model id from OpenRouter's /api/v1/models (the "id"
# field of each entry in the response's "data" array). No API key required
# — confirmed this session: a plain unauthenticated GET returns the full
# public catalog (410 models, 2026-08-13 fetch). Injected so tests never
# make a real network call.

@dataclass
class FreshnessResult:
    checked_slugs: list[str]         # every configured slug this check covers, in input order
    missing_slugs: list[str]         # configured slugs NOT found live, in input order (empty = all good)
    warning: Optional[str]            # human-readable, non-None iff missing_slugs is non-empty
    cache_hit: bool                   # True if served from same-day/same-config cache, no fetch performed
    fetch_error: Optional[str]        # non-None iff fetch_fn raised — distinct from "checked, found missing"

def check_slug_freshness(
    configured_slugs: list[str],
    cache_path: Path,
    fetch_fn: FetchModelsFn,
    today: str,          # caller-supplied "YYYY-MM-DD" — no datetime.now() inside, keeps this pure/testable
    force: bool = False,  # bypass cache regardless of date/config-hash match
) -> Awaitable[FreshnessResult]:
    ...
```

Cache file (`cache_path`, JSON): `{"date": "YYYY-MM-DD", "config_hash":
"<sha256 of sorted(configured_slugs) joined>", "missing_slugs": [...]}`.
Folder-scoped per this project's §7 convention (e.g.
`./council-runs/.slug_freshness_cache.json`, gitignored) — caller's
responsibility to pass the path, this module never hardcodes a location.

**Acceptance criteria (Given/When/Then):**

1. Given no cache file exists at `cache_path`, When `check_slug_freshness`
   runs, Then `fetch_fn` is called exactly once, `missing_slugs` is computed
   by comparing `configured_slugs` against the fetched live-id set, a new
   cache file is written with `today`'s date and the current config hash,
   and `cache_hit=False`.

2. Given a cache file exists with `date == today` and `config_hash`
   matching `sha256(sorted(configured_slugs))`, When it runs with
   `force=False`, Then `fetch_fn` is **not** called, the cached
   `missing_slugs` is returned verbatim, and `cache_hit=True`.

3. Given a cache file exists with `date != today`, When it runs, Then
   `fetch_fn` **is** called (a stale-by-date cache never short-circuits the
   check) and the cache file is overwritten with `today`'s date.

4. Given a cache file exists with `date == today` but a `config_hash` that
   does **not** match the current `configured_slugs` (e.g. the user edited
   `llm_council.yaml`'s model list intraday), When it runs, Then `fetch_fn`
   **is** called — a config change invalidates the cache even on the same
   day, so a freshly-added slug is never silently assumed valid off a
   stale prior check.

5. Given `force=True`, When it runs, Then `fetch_fn` is always called
   regardless of any existing cache's date or config hash.

6. Given `fetch_fn` returns a live-id list missing one or more
   `configured_slugs`, When the result is built, Then `missing_slugs`
   contains exactly those absent slugs in their original input order, and
   `warning` is a non-`None` string naming every one of them by exact slug
   string (not a count, not truncated — matches this project's
   human-legible-output rule: the reader must be able to act on the
   message without re-deriving which slug failed).

7. Given every `configured_slugs` entry is present in the fetched live-id
   list, When the result is built, Then `missing_slugs` is `[]` and
   `warning` is `None`.

8. Given `fetch_fn` raises (network error, malformed response, etc.), When
   it runs, Then the exception is caught, `fetch_error` is set to a
   human-readable description, `missing_slugs` is `[]` (never silently
   treated as "all slugs missing" or conflated with a real drift finding),
   `cache_hit=False`, and no cache file is written (a failed check must
   never poison the cache with a false "all clear" or block a real check
   from running next call). This is the same "distinguish couldn't-run
   from ran-and-clean" pattern this project already applies in
   `PipelineResult.completeness_check_parse_failed`.

9. Given the cache **write** fails after a successful live check (e.g.
   read-only filesystem, disk full), When it runs, Then the already-computed
   `FreshnessResult` (correct `missing_slugs`/`warning`) is still returned —
   a cache-write failure must never invalidate or block the check result
   itself, only be surfaced as a secondary note the caller can log.

10. Given `configured_slugs` is empty, When it runs, Then `fetch_fn` is
    still called if the cache doesn't already cover today (an empty
    roster is itself a config state worth a loud check, not a silent
    no-op), `missing_slugs` is `[]`, `warning` is `None`.

**Non-goals:**
- No retry logic for the catalog fetch itself — a transient network blip
  on this precheck surfaces via `fetch_error`; the caller decides whether
  to proceed anyway or abort. Retry-with-backoff is `resilient_query.py`'s
  job for actual per-model debate calls, not this precheck.
- No automatic remediation or backup substitution — this module only
  detects and reports. If a primary slug is dead, the existing
  `debate_resilience`/`resilient_query.py` mechanism (once built) is what
  actually substitutes a backup at call time; this precheck exists so that
  substitution doesn't have to be discovered the expensive way (a burned
  retry budget against a slug that can never succeed).
- No API key handling — OpenRouter's `/api/v1/models` is a public,
  unauthenticated endpoint (confirmed this session).

## Integration (not yet built — for the implementer)

Call once at the very start of `pipeline_runner.py::run_pipeline` and
`scripts/debate.py`'s entrypoint, before Stage 0.5, covering
`council.models + debate_resilience.backup_models` (chairman and any
gated seat are already members of `council.models`, no separate check
needed). Log the result into `debug_log` (matching the existing per-stage
debug-line convention). If `missing_slugs` is non-empty but not every
*primary* model is missing, warn loudly (CLI + debug_log) and proceed —
the resilience layer already degrades gracefully. If **every** primary
model is missing (catastrophic drift, e.g. an OpenRouter-wide outage or a
config file pointing at a dead model generation entirely), abort with a
clear, actionable error rather than proceeding into a run that cannot
possibly succeed.

**Known scope limitation, consistent with this project's existing
two-call-path distinction** (`docs/upstream-deltas.md`, "Two separate call
paths" entry): this precheck covers `pipeline_runner.py`'s call path only.
The raw MCP `consult_council` tool is package-native and cannot be
wrapped with a precheck without forking installed code — same limitation
already accepted for the resilience/retry mechanism itself.
