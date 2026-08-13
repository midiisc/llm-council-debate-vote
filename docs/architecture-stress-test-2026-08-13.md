# Architecture Stress Test — 2026-08-13

Red/Blue adversarial review (`adversarial-review` workflow) of the full MAD
pipeline: config, all 10 implemented scripts, all specs/contracts, and the
3 specced-but-unbuilt additions from this session (Stage 3.75, reasoning-effort
wiring, slug-freshness precheck). Fanned finders across the six standard
angles + this project's own domain concerns, then adversarially verified every
finding with independent skeptics prompted to refute it.

**Result: 42 findings survived adversarial refutation, 8 did not.**
Severity: 7 critical, 12 high, 12 medium, 11 low. Findings below are grounded
in direct execution/reproduction where stated, not just source reading —
several were confirmed by actually running the pipeline against faked
response shapes or running the real test suite.

**One finding already fixed as part of producing this report**: the RED test
(`test_ac18_loads_this_projects_actual_llm_council_yaml_debate_resilience_block`)
hardcoded the dead Kimi slug this session's own earlier fix moved away from —
updated to match, full suite reconfirmed green (382 passed). See "Medium /
correctness" below for the original finding text.

Everything else below is **reported, not yet fixed** — this file is the
punch list; prioritization/scheduling is a decision for whoever picks this up
next, not implied by severity label alone.

---

## Critical (7)

1. **Pipeline crashes (not a clean error) when every model fails Stage 1.**
   `pipeline_runner.py:255` reads `metadata["quality_metrics"]["core"]
   ["consensus_strength"]` unconditionally; the real all-models-failed
   response shape from `council_adapter.py` has no `quality_metrics` key at
   all. Reproduced live. Result: cryptic `error: "'quality_metrics'"`
   instead of the actionable failure the surrounding warning logic implies.

2. **Pipeline crashes when exactly one model survives Stage 1** — the
   explicitly-supported `degraded_mode='single_model'` path.
   `_compute_outliers` (`pipeline_runner.py:179-188`) accesses
   `entry["borda_score"]` unconditionally, but the single-model degraded
   shape has no `borda_score` key (confirmed byte-identical to upstream via
   `inspect.getsource`). `audition_tracking.py` already handles this exact
   shape defensively (`r.get("borda_score", 0.0)`) — this is a confirmed
   oversight, not an inherent constraint. Crashes *after* Stage 1-3 (and
   possibly 2.75/4) spend, discarding a completed synthesis and never
   writing a scorecard record. **The not-yet-built Stage 3.75 gate depends
   on this same function's output** — wiring it in today would inherit this
   crash on every degraded single-model run.

3. **Wall-clock ceiling is undersized against the retry+backup engine's own
   worst case.** Single Stage-1 slot worst case: 920s on retries alone, up
   to ~3680s if all 3 backups are also exhausted — against a 1200s total
   ceiling. Git history shows the wall-clock-ceiling commit predates the
   retry/backup commit by ~3 hours; nothing cross-checked the budget after
   resilience was added. **Net effect: under exactly the sustained-outage
   scenario the resilience layer exists to survive, the wall-clock kill
   fires first**, turning a recoverable degraded run into a hard failure.

4. **Real spend is invisible in `run_status.json` when the wall-clock kill
   fires mid-call.** `cost_so_far` is only assigned *after* `council_fn`
   fully returns — so the timeout case most likely to matter (mid-flight,
   per #3) reports `cost_so_far_usd: 0` even though real, already-billed
   OpenRouter spend exists. Directly contradicts the pipeline-runner
   contract's own documented behavior and this project's Pillar 6 gate.

5. **Stage 0.5 grounding-pass spend is never tracked anywhere.**
   `real_fetch_evidence` makes one real, unbounded-count OpenRouter call per
   claim with no cost figure returned and no cap on claim count.
   `--max-cost-usd` cannot bound this stage at all — a claims-dense document
   can spend real, unbounded money before cost-ceiling logic even runs.

6. **Mutation-testing gate (Pillar 3) has silently regressed off 5 of 11
   implemented scripts.** `setup.cfg`'s `only_mutate` lists only 5 files;
   `completeness_check.py`, `scorecard.py`, `debate.py`, `grounding_pass.py`,
   `live_adapters.py` generate zero mutants today despite the ledger
   documenting `live_adapters.py` as mutation-tested clean in an earlier
   session. No sync check between this list and `scripts/*.py` exists — a
   future "mutation-gate clean" claim for one of the 5 missing files would
   be a false-green (0 survivors because 0 mutants were generated).

7. **No durable transcript/synthesis persistence anywhere in the
   pipeline.** The architecture spec and `.gitignore` both say the per-run
   output folder holds Stage 1-3 transcripts, CSS, dissent, and a final
   memo. It doesn't — `pipeline_runner.py` only ever writes `run_status.json`
   (status + cost) and conditionally `grounding.md`; the actual synthesis
   text only goes to stdout. Empirically confirmed against a real
   `council-runs/` folder: `run_status.json` contains only status+cost, no
   answer. `debate.py` persists nothing at all. **If the terminal session is
   lost, a real-money decision's actual output is unrecoverable except by
   re-running and paying again.**

## High (12)

- The "always-on" wall-clock ceiling can't actually preempt Stage 0.5/2.75 —
  their HTTP calls are synchronous (`urllib`), so `asyncio.wait_for`'s
  deadline can't fire while the event loop is blocked inside them.
- `RetryPolicy` has no bounds check against `backoff_seconds` length — a
  plausible one-number config edit (`max_attempts: 4` with the current
  2-entry backoff list) raises an uncaught `IndexError` that crashes the
  *entire* Stage 1 call, not just one slot.
- **Safety gate is computed but never enforced or read** — `evaluation.safety.enabled: true`
  exists specifically because this pipeline ingests untrusted documents, but
  `result['safety_check']` is never read anywhere else in the codebase.
  Nothing caps a flagged response's score, aborts, or warns. Also only
  scans raw Stage-1 drafts, never the Stage 3 synthesis actually shown to
  the user.
- **Untrusted document text has no anti-injection delimiting outside one
  narrow case.** `revision_round.py` deliberately isolates the source
  document to prevent forged `[[cite:<id>]]` markers — but the
  `facts_block` built from claim text has no equivalent guard, and the same
  unguarded text is sent directly to a web-search-enabled model
  (`gemini-3.6-flash:online`) with no allowlisting — an indirect
  prompt-injection chain from a hostile document into live external search.
- Stage 0.5 evidence fetching is fully sequential with no concurrency and no
  claim-count cap.
- Stage 2.75 revision queries all models sequentially (unlike Stage 1),
  multiplying wall-clock cost by N specifically on the path most likely to
  already be under stress (CSS < 0.50).
- `scripts/debate.py`, the documented recommended interactive entry point,
  has **no overall wall-clock ceiling at all** — the exact unbounded-hang
  failure mode the 2026-08-12 hardening was meant to close remains fully
  open on this CLI.
- `council_adapter.py` vendors upstream's *private* (underscore-prefixed)
  orchestration functions, pinned to unlabeled source line numbers, with no
  automated drift check — despite Pillar 5 explicitly requiring one for
  exactly this coupling.
- `pipeline_runner.py` crashes with an opaque `KeyError` and **loses its own
  debug_log** on the failure path — the accumulated diagnostic trail that
  exists specifically to avoid masked-cause failures isn't persisted when a
  crash happens.
- Cost-so-far accounting is lost on mid-stage timeout/cancellation (same
  root cause as critical #4, different call site).
- `setup_mcp_timeout.sh`'s own body sets `MCP_TIMEOUT` to the exact same
  default value its comment identifies as insufficient — a no-op for one of
  the two variables it claims to fix, with no indication in its success
  message.
- `pipeline-architecture-spec.md` still teaches "Stage 3.5" for the
  post-synthesis critique — the exact name `agent-model-reasoning-config.md`
  explicitly renamed to "Stage 3.75" this session to avoid a real collision.
  A manual worksheet (`templates/premortem_prompt.md`) still cites the stale
  section by name.

## Medium (12) — condensed

- **Test suite was RED** (dead-slug regression from this session's own
  Kimi fix) — **fixed while producing this report**, suite reconfirmed
  green.
- The un-built slug-freshness precheck spec has no defined behavior for a
  corrupt/truncated cache file, and its exact-match assumption against
  routing-suffixed slugs (`:online`, `:batch`) is ungrounded.
- `scripts/debate.py` has zero cost-ceiling enforcement — the Real-money
  gate this project requires is simply absent from one of its two CLIs.
- `audition.jsonl` is fully linear-scanned per model on every run, unbounded
  growth, no rotation.
- The un-built slug-freshness fetch has no timeout and sits outside the
  wall-clock backstop as specced.
- `run_pipeline` maintains two independently-hand-synced cost totals with no
  single source of truth or consistency test.
- Stage results cross two functions as a bare 9-element positional tuple —
  a `PipelineResult` dataclass exists one function below and isn't used for
  this.
- The config-nesting regression test only covers the `high` tier, not
  `reasoning` — the tier actually resolved in production.
- `_raw_claims.txt` can orphan on failure; no durable copy of the original
  claims text is kept once `grounding.md` is written.
- Stage 3.75's design has no specified persistence — inherits critical #7's
  gap by construction.
- Stage-4's dropped-facts warning shows bare numeric claim IDs with no text
  and no pointer to `grounding.md`, where the mapping lives.

## Low (11) — condensed

- `_compute_outliers`'s 1.5×-stdev threshold has no statistical grounding at
  N=3-4, and Stage 3.75 would wire it directly to real spend.
- Citation regex fails on trailing punctuation (`[[cite:12.]]`), silently
  rejecting an otherwise-valid revision.
- No defense against a backup model overlapping a primary — would silently
  dedupe/undercount if the pools were ever not disjoint.
- Muse Spark's documented `disable_tools=True` precondition is prose only,
  not enforced anywhere in code (today's blast radius is zero — no
  `tools=` payload exists anywhere in this codebase yet).
- `scorecard.jsonl` shares the same unbounded-scan pattern as `audition.jsonl`.
- Two independent, hand-rolled OpenRouter HTTP clients exist
  (`live_adapters.py` raw urllib vs. `council_adapter.py`'s package client)
  that will drift on any future API-contract change.
- `run_status.json`'s "running" state has no heartbeat/PID for crash
  reconciliation.
- The reasoning-effort import-swap plan is inert today (`USE_GATEWAY_LAYER`
  is always False) but has no test pinning that, so a future upstream
  change could silently reintroduce a bypass.
- The Kimi slug fix (and this session's other edits) sat uncommitted at
  review time — one `git checkout`/reset away from reverting to the known-dead
  slug.
- Stage 3.75 has no defined operator-facing report surface (no field name,
  no debug_log format) — breaks this pipeline's own otherwise-uniform
  per-stage convention.
- `debug_log` interleaves two overlapping stage-numbering schemes
  (package-internal "3.5" vs. project-defined "2.5") with no explanation,
  undercutting its own "no reverse-engineering" design promise.

## Refuted (8)

8 findings were raised by finders but killed by majority-refute during
adversarial verification — not reproduced here since they didn't survive;
full detail in the workflow journal if needed later
(`subagents/workflows/wf_ac8a9a20-095/journal.jsonl`).
