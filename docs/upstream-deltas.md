# Upstream deltas ledger

Every verified drift between what this repo assumes (setup doc, config, scripts)
and what's actually live in `amiable-dev/llm-council`, dated and sourced. Updated
by the Pillar 5 self-update check (every 2-3 days) and by any ad hoc grounding
pass. Never edit `llm_council.yaml`/`.env`/scripts based on a claim that isn't
recorded here or freshly re-verified.

Last upstream check: **2026-08-09**. Checked against: PyPI `llm-council-core`
v0.40.1, GitHub `amiable-dev/llm-council` @ `master` (README + ADR-010/-036/-044),
live OpenRouter `/api/v1/models` catalog.

## Open deltas (setup doc still says the old thing — needs a doc/config fix)

| Item | Setup doc says | Actually true (2026-08-09) | Source | Status |
|---|---|---|---|---|
| `LLM_COUNCIL_ACCURACY_CEILING` | Real env var | Does not exist — accuracy ceiling is YAML-only (`evaluation.rubric`), no env var | github.com/amiable-dev/llm-council README | **Fix in setup doc/config** |
| `LLM_COUNCIL_DEADLOCK_THRESHOLD` | Real env var | Does not exist — deadlock detection is a fixed internal 0.1 Borda-spread constant, not configurable | same | **Fix in setup doc/config** |
| `google/gemini-3-pro` model slug | Used in `llm_council.yaml` example | Not a real OpenRouter slug. Use `google/gemini-3-pro-preview` or `google/gemini-3.1-pro-preview`. (Note: this typo exists in upstream's own README too — worth an upstream issue.) | live OpenRouter `/api/v1/models`, github.com/amiable-dev/llm-council README | **Fix in setup doc/config** |
| Install command has no version floor | `pip install "llm-council-core[mcp,secure]"` | Must pin `>=0.39.0` — 0.22.0-0.38.2 has a confirmed credential-leak advisory during verify/gate runs | GitHub security advisory, amiable-dev/llm-council | **Fix in setup doc — security, do first** |
| `claude mcp add --transport stdio llm-council --scope user -- llm-council` | Doc's registration command | Wrong in v0.40.1: the `llm-council` console script (`cli:main`) is an argparse CLI with no bare stdio-serve behavior; `llm-council serve` is HTTP-only (`--host`/`--port`, no stdio option). The actual stdio MCP entrypoint is a separate module never wired to a console script: `mcp_server.py`'s own `main()` (`mcp.run()`). Correct command: `claude mcp add --transport stdio llm-council --scope user -- <tool-venv>/bin/python -m llm_council.mcp_server`. Verified by running it directly (clean exit, no traceback) before registering. | Read `cli.py` entry_points + `mcp_server.py` source directly, `llm-council serve --help` output, 2026-08-09 | **Fixed — registered correctly, wrong version removed first** |
| `council.tiers.pools.<tier>.models` in `llm_council.yaml` | Doc's example config | Inert under default settings (`triage.enabled: false`) — only read by the triage/complexity-classification layer and `frontier_fallback.py`. The actually-active council is `council.models` (flat list) + `council.chairman` (single string) — confirmed via `mcp_server.py:101` (`COUNCIL_MODELS = _get_council_models()`) and `council.py:70`. Following the doc's example verbatim would have silently run the package's hardcoded default 4 models (including `deepseek/deepseek-v4-pro`, never requested) instead of the intended pool. | Read `unified_config.py`/`council.py`/`mcp_server.py` source directly, 2026-08-09 | **Fixed — `llm_council.yaml` uses `council.models`/`council.chairman` directly** |
| "Gemini 3 Pro" as a core model | Setup doc / earlier upstream README | No Gemini 3 "Pro"-tier text/chat model exists on OpenRouter as of 2026-08-09 (only `gemini-3.6-flash`, `gemini-3.5-flash-lite`, `gemini-3-pro-image` [multimodal]). Both previously-cited slugs (`gemini-3-pro-preview`, `gemini-3.1-pro-preview`) are gone from the live catalog entirely. | Live OpenRouter `/api/v1/models` query, 2026-08-09 | **Resolved — using `google/gemini-3.6-flash` as interim 3rd-core seat, see pipeline-architecture-spec.md §4** |

## Confirmed-correct (no action needed)
- Install extras `[mcp,secure]`, keychain-silent-ignore behavior — confirmed verbatim in README.
- `llm-council setup-key`, `llm-council serve`, `council_health_check` fields (`api_key_configured`, `key_source`, `council_size`, `estimated_duration`, `ready`) — confirmed.
- `consult_council` metadata path `metadata["quality_metrics"]["core"]["consensus_strength"]` — confirmed exactly.
- `llm_council.yaml` schema (`council.tiers.default`, `council.tiers.pools.<tier>.models`, `timeout_seconds`, `council.gateways.default`) — confirmed.
- `claude mcp add --transport stdio llm-council --scope user -- llm-council` — confirmed exact registration command.
- `anthropic/claude-opus-4.8`, `openai/gpt-5.5` model slugs — confirmed live on OpenRouter.
- OpenRouter 5.5% non-crypto fee, Requesty 5% markup + EU residency — confirmed against pricing pages.
- Requesty BYOK removing the 5% markup — **not confirmed**, pricing page doesn't state this; treat as unverified, don't assume it in cost planning.

| `load_config()` in `unified_config.py` (~line 1095) | Any `llm_council.yaml` following the doc's/README's nesting example | **Confirmed live bug in v0.40.1**: extracts the inner contents of the top-level `council:` key and passes them as `UnifiedConfig(**council_config)` kwargs directly, instead of re-nesting under `.council`. Since `UnifiedConfig` has no top-level `models`/`chairman`/`synthesis_mode`/etc. fields, they're silently dropped (pydantic default `extra="ignore"`) and the package's hardcoded defaults apply instead — **with no error, no warning, `ready: true`**. Only `gateways:` survives, because it happens to also be a real top-level `UnifiedConfig` field. First caught by actually running `council_health_check` and finding `deepseek/deepseek-v4-pro` in the model list despite never being configured. Workaround (verified working): wrap council-level fields in one EXTRA `council:` layer — see comment block at the top of `llm_council.yaml`. | Direct execution of `load_config()` against both the buggy and workaround YAML shapes, 2026-08-09 | **Fixed via workaround in `llm_council.yaml`. Reported upstream: [amiable-dev/llm-council#591](https://github.com/amiable-dev/llm-council/issues/591)** |

| `council.models` alone (no `tiers.pools`) | Assumed sufficient after fixing the first `load_config()` bug | **Confirmed live bug #2, more severe**: `council_health_check` reads the flat `council.models` list (correct), but a real `consult_council()` call resolves its model list via `TierContract.allowed_models` instead - `run_council_with_fallback` priority is "explicit models arg > tier_contract > default", and `consult_council` always passes a `tier_contract`. `TierContract.allowed_models` comes from `tiers.pools.<confidence>.models`, which pydantic's `TierConfig.ensure_default_pools()` silently auto-fills with the package's wrong defaults if left unset - no error, `council_health_check` still reports `ready:true` because it checks a completely different code path. A real query would have silently run the wrong 4 models (including `deepseek-v4-pro`) despite `council_health_check` looking correct. Fixed by populating `tiers.pools.high/quick/balanced/reasoning.models` explicitly (see comment block in `llm_council.yaml`). Verified via direct execution of `create_tier_contract('high')` from the project directory - `allowed_models` now matches our 4 configured models. | Direct execution tracing `run_council_with_fallback` -> `create_tier_contract` -> `_get_allowed_models` -> `_get_tier_model_pools`, 2026-08-09 | **Fixed in `llm_council.yaml`. Reported upstream alongside Bug 1: [amiable-dev/llm-council#591](https://github.com/amiable-dev/llm-council/issues/591)** |
| `TierContract.aggregator_model` | Looked like a second, unconfigured chairman-selection path (hardcoded `TIER_AGGREGATORS["high"] = "openai/gpt-5.4"`) | **Verified harmless / dead code** for our purposes: grepped every call site and the actual Stage 3 synthesis LLM call in `council_stages.py` uses `_get_chairman_model()` exclusively (reads `council.chairman`, correctly configured). `aggregator_model` is set on the `TierContract` dataclass but never read by the synthesis path - vestigial. No fix needed, but worth knowing it's there and wrong-looking if anyone greps for "gpt-5.4" in this codebase later. | Grepped all `aggregator_model` and `_get_chairman_model()` call sites in `council.py`/`council_stages.py`, 2026-08-09 | **No action needed - confirmed unused** |

## Security status (not an upstream delta — tracked here per Pillar 6)

**2026-08-11: OpenRouter key rotation — deferred, not done.** The key pasted
in plaintext into the Claude Code chat session on 2026-08-09 has **not**
been rotated as of this date; the user explicitly chose to defer it
("let me do it later"). Per the 8-persona expert panel review on
2026-08-11 (unanimous, independently flagged by all 8), this is treated as
an open, acknowledged risk: **no further real-money pipeline run should
happen until rotation is confirmed.** Code changes and unit/mutation
testing continue normally — only live OpenRouter spend is paused.
Update this entry the moment rotation is confirmed.

**2026-08-12: explicit user override — proceeding with a real-money dry
run despite unrotated key.** Asked the user directly how to handle this
hold before running the Pillar 6 low-stakes dry run; user chose "proceed
anyway, accept the risk" rather than rotating first or confirming a prior
rotation. Key is still the one pasted in plaintext on 2026-08-09, still
not rotated. This override applies to the one dry run that follows this
entry, not a blanket lift of the hold — the hold stays in force for any
future real-money run unless the user overrides again or the key is
actually rotated.

## Known limitations (not upstream deltas — this repo's own design tradeoffs)

**2026-08-11: Stage 0.5 grounding is single-source, not corroborated.**
`live_adapters.real_fetch_evidence` makes exactly one automated web-search
call per claim (via `google/gemini-3.6-flash:online`), and `tag_claim`
marks a claim VERIFIED/CONTRADICTED off that single source — there is no
multi-source corroboration, source-reputation scoring, or content
sanitization of what comes back. This is a real injection/poisoning
surface (a compromised or adversarial page could get scraped as
"supporting" evidence and, before this date, would have been handed to a
revising model under unqualified "verified fact" framing with no visible
source). Partially mitigated 2026-08-11 (panel finding ws-redteam #10,
`docs/specs/custom-scripts-contracts.md` Contract 2 amendment):
`build_revision_prompt` now surfaces each finding's source URL and labels
the whole section "single-source research findings ... not multi-source
verification, weigh accordingly" instead of "verified facts." This makes
the limitation visible to the reviewing model and any human reading a
run's transcript — it does **not** add actual corroboration. Multi-source
verification, source-reputation filtering, or fetched-content
sanitization remain open, not-yet-scheduled follow-up work if this
pipeline is used for higher-stakes decisions.

**2026-08-11: Stage 2's response order and rubric-dimension order are not
independently randomized per reviewer.** Confirmed by direct source read
of `llm_council/council_stages.py::stage2_collect_rankings`:
`random.shuffle(shuffled_results)` runs once per council call, so every
reviewer in that run sees the same anonymized response ordering; the 5
rubric dimensions (accuracy/relevance/completeness/conciseness/clarity)
are always rendered in that exact fixed order in the prompt, for every
reviewer, every run. Two independent 2025-2026 papers
(arXiv:2406.07791 "Judging the Judges"; arXiv:2602.02219 "Am I More
Pointwise or Pairwise?") show rubric/position-based LLM-judge bias is
real and independent of anonymization — the mitigation they recommend
(per-call randomized ordering) isn't exposed via `unified_config`/
`eval_config` (confirmed by `grep`) and lives entirely inside
`run_full_council`, which this project deliberately uses as-is rather
than patching installed vendor code. **Not locally implementable; filed
upstream as `amiable-dev/llm-council#592`** (2026-08-11, user-confirmed
before filing, per the `#591` precedent) — this repo's own remediation is
"wait for upstream" rather than a local code change. Revisit this entry
once #592 is resolved or closed.

## Research-driven refinements (2026-08-11)

Feynman-methodology literature pass (arXiv/Semantic Scholar/web,
citations spot-verified live against arXiv abstracts before acting — see
`docs/specs/custom-scripts-contracts.md`'s Contract 4 for the full
citation list) on: (1) query decomposition for Stage 0.5 grounding, (2)
non-persona diversity techniques for Stage 1, (3) bias mitigation beyond
anonymization, (4) other MAD-pipeline architecture refinements.

**Implemented:** Stage 4 completeness check (`scripts/completeness_check.py`)
— guards against arXiv:2606.03032's "Deliberative Illusion" finding
(multi-agent consensus can mask up to 72% factual attrition) by checking
post-synthesis whether Stage 0.5's verified facts are actually reflected
in the chairman's final answer. See Contract 4 for full details. This
work also surfaced and fixed a real pre-existing bug: `verified_facts`
was never populated from grounding output, so Stage 2.75 revision could
never receive a real citable fact in any prior run — see Contract 4's
"Real bug found and fixed" note.

**Not locally implementable, filed upstream instead:** per-reviewer
response/rubric-order randomization — `amiable-dev/llm-council#592` (see
the known-limitation entry above).

**Declined (evidence-based, no code change and no upstream action):**
non-persona diversity prompting for Stage 1 — evidence too thin
(arXiv:2511.07784 attributes debate success to model heterogeneity, which
this pipeline already has via 4 distinct models, not to prompt-level
framing; no study directly tests the no-persona-framing variant) —
re-confirms the existing no-persona decision, nothing changed.

**Deferred (evidence-based, cost-based — revisit if evidence strengthens):**
a conditional second Stage 0.5 query for claims tagged UNVERIFIABLE or
CONTRADICTED on the first pass — evidence is narrowly inferred from a
decomposition-specific finding (arXiv:2602.10380) rather than directly
tested for this pipeline's single-claim/single-query shape, and adds a
real extra live API call per affected claim; the real-money gate (Pillar
6) argues against spending on inferred-not-tested evidence.

## Adversarial stress testing (2026-08-11)

`tests/test_stress_adversarial.py` added: hypothesis-fuzz tests feeding
arbitrary text (200 examples each) through every model-response parse
boundary in the codebase (`parse_completeness_response`,
`parse_revision_response`, `parse_evidence_response`), a property test
that `total_cost_usd` always equals the exact sum of all three real cost
sources (stage1-3 + revision + completeness) under randomized inputs, and
one combined worst-case end-to-end scenario (mixed VERIFIED/CONTRADICTED/
UNVERIFIABLE claims, a cost ceiling landing mid-run, malformed responses
at every call site) run through the real `run_pipeline`.

**Found a real bug on the first run:** `live_adapters.parse_evidence_response`
crashed with `AttributeError: 'int' object has no attribute 'get'` on any
input that's valid JSON but not a JSON object — `"0"`, `"[]"`, `"null"`,
`"true"`, a bare quoted string. `json.loads` happily parses these; the
code then called `.get("verdict")` on whatever came back, assuming it was
always a dict. This directly contradicted the function's own documented
contract ("never raises... malformed model response must degrade to
'couldn't verify,' not crash"), and could have crashed a real grounding
pass mid-claim on nothing more exotic than a model replying with a bare
number. **Fixed** with one `isinstance(data, dict)` guard (mirrors the
`isinstance(data, list)` guard `parse_completeness_response` already had
for exactly this reason). Re-verified: `live_adapters.py` mutation-tested
clean afterward (185 mutants, same 10 previously-documented equivalent
survivors, zero new gaps). Regression test:
`test_parse_evidence_response_valid_json_non_dict_yields_empty_list_not_crash`
in `tests/test_live_adapters.py`.

No other real bugs surfaced — the fuzz/property/combined-scenario tests
all passed cleanly on `completeness_check.py`, `revision_round.py`, and
`pipeline_runner.py` once the one fix above landed. Full suite: 200
passed.

## No-silent-failure hardening (2026-08-11)

Direct follow-up to the stress testing above, per explicit user request:
"no silent failing of any step" + "add proper debug steps so it's clear
what failed." Auditing every stage against that bar (not just fixing what
the fuzz tests happened to hit) surfaced one more real design gap:
`completeness_check.parse_completeness_response` returned `[]` both when
the model genuinely reported nothing missing AND when its response
couldn't be parsed at all — indistinguishable from `PipelineResult`
alone. Fixed by changing its return type to `(ids, parse_ok)` (and
`check_fact_completeness` to `(ids, cost, parse_ok)`) — see
`docs/specs/custom-scripts-contracts.md` Contract 4's AC10/AC11. New
`PipelineResult.completeness_check_parse_failed` field; the CLI now warns
distinctly when the check ran but couldn't be understood, separate from
the "facts were dropped" warning.

Two more additions, `pipeline_runner.py` only:
- **`PipelineResult.debug_log: list[str]`** — one line per stage
  transition (grounding ran/skipped + tag breakdown, model count, CSS,
  revision ran/skipped-why + accept count, chairman model, Stage 4
  ran/skipped-why + parse outcome). The CLI prints this to stderr on
  every run. Read top to bottom to see exactly what happened without
  reverse-engineering it from the other result fields.
- **MAD integrity check** — `len(stage1_results) < 2` appends a `WARNING:
  ... this is not multi-agent debate` line. A run that silently degraded
  to a single model (upstream fallback, config issue, etc.) can no longer
  look identical to a real multi-model run in what a human reads.

`revision_round.parse_revision_response` was deliberately left unchanged
— its `(None, None)` "not revising" result isn't the same ambiguity
(the prompt explicitly instructs "no citation marker means not revising,"
so absence is a well-defined signal by the contract's own design, not a
masked parse failure) — see the contract doc's non-goals note for the
full reasoning.

Mutation-tested clean: `completeness_check.py` 66/66,
`pipeline_runner.py` 570/588 (same 18 previously-documented equivalent
survivors, zero new gaps). Full suite: 215 passed.

## Follow-up research scan (2026-08-11) — nothing actionable found

Narrow, bounded follow-up literature scan (explicitly told not to re-derive
already-covered findings) surfaced 5 papers from 2026, all verified live
against their arXiv abstracts before being recorded here:

- **arXiv:2605.29116** "Beyond Consensus: Trace-Level Synthesis in Mixture
  of Agents" — argues aggregators should always synthesize from full
  reasoning traces, never gate on consensus. On inspection this does
  **not** actually indict this pipeline's design: this pipeline's Stage 3
  chairman synthesis already runs on every call regardless of CSS (it's
  entirely inside `run_full_council`, unconditional) — only the *extra*
  correction-triggered revision (Stage 2.75, this project's own addition,
  grounded separately in Choi/Zhu/Li) is CSS-gated, a different
  mechanism the paper doesn't target. No action.
- **arXiv:2604.01029** "Revision or Re-Solving? Decomposing Second-Pass
  Gains" — multi-LLM revision gains are often just a stronger model
  re-solving, not genuine correction, especially on constrained-answer
  tasks. Supports (doesn't require changing) this pipeline's existing
  fact-citation-only, CSS-gated revision design — noted as a caveat on
  what "revision worked" evidence actually means, no code change.
- **arXiv:2607.28576** "Sample More, Reflect Less" — self-refine/reflexion
  self-critique methods are reliably worse than repeated sampling at
  matched token cost. Relevant caution for *any future* self-critique
  addition to Stage 3 synthesis — doesn't apply to the existing Stage 4
  completeness check (diagnostic-only, never edits output, not the
  self-refine pattern the paper tested). No action, logged as a caution.
- **arXiv:2608.02827** "Emergence of Biased Consensus in Multi-Agent LLM
  Debates" — debate can amplify bias into false consensus under a
  temperature-driven phase transition; agent heterogeneity suppresses it.
  Validates (doesn't require changing) this pipeline's existing
  4-heterogeneous-model design. No action.
- **arXiv:2604.12196** "Beyond Majority Voting: Radial Consensus Score" —
  an embedding-space alternative to majority-vote CSS. No evidence the
  current CSS is failing in this pipeline; logged as a candidate only if
  CSS-gaming is ever actually observed. No action.

No paper found that meaningfully contradicts Choi/Zhu/Li or the
Deliberative Illusion paper's conclusions. Verdict: nothing from this
scan clears the bar for implementation.

## Timeout architecture fix (2026-08-12)

**Root cause of live "high"/"balanced" tier failures ("exceeded the 60s client
transport timeout"), confirmed by direct execution against
`llm-council-core==0.40.1` (`load_config()` + `UnifiedConfig.get_tier_contract()`
run from this project directory, `unified_config.py`/`tier_contract.py` read
directly):**

| tier | `deadline_ms` (total) | `per_model_timeout_ms` | `token_budget` | `max_attempts` |
|---|---|---|---|---|
| quick | 30000 | 20000 | 2048 | 1 |
| balanced | 90000 | 45000 | 4096 | 2 |
| high | 180000 | 90000 | 4096 | 3 |
| reasoning | 600000 | 300000 | 8192 | 2 |

The client-side MCP transport cap (`MCP_TOOL_TIMEOUT`, a Claude Code env var —
**not** an `llm-council-core` setting) was `60000`ms globally, set in
`~/.bashrc`. `balanced`'s own server-side budget (90s) already exceeded that
60s client cap, and `high`'s (180s) exceeded it 3x — so both died at the
transport layer before the package's own timeout/retry logic ever ran.
`quick` (30s budget) was the only tier that fit, which is why the fallback
to it succeeded.

`get_tier_contract('high').allowed_models` and
`get_tier_contract('reasoning').allowed_models` were confirmed **identical**
(same 4 configured frontier models) — `reasoning` is a strictly larger
budget for the same model set, not a different/weaker pool.

**Fixed:**
- `llm_council.yaml`: `tiers.default` changed `high` → `reasoning` (same 4
  models, 3.3x timeout budget, 2x token budget; trade-off: `max_attempts`
  drops 3→2).
- `~/.bashrc` (global, all projects): `MCP_TOOL_TIMEOUT` raised `60000` →
  `900000` (15min) to clear `reasoning`'s 600000ms deadline with margin for
  gateway fallback/retry overhead. This is a machine-wide setting, not
  project-scoped — noted here because it's load-bearing for this project's
  ability to complete a real council call.

Source: direct execution, 2026-08-12 (session date; installed version
0.40.1 matches the version this ledger's other entries are grounded
against — no re-verification against a newer release was needed).

## Model Intelligence / reasoning-effort layer (2026-08-12) — confirmed by direct execution

**Correction to the Bug #1 entry above.** That entry's claim that `gateways:`
and `tiers:` "get hoisted correctly as-is" because they're real top-level
`UnifiedConfig` fields is imprecise and would mislead anyone adding a new
top-level block. Reading `load_config()` verbatim: it does exactly
`UnifiedConfig(**raw_config.get("council", {}))` — **it reads nothing from
the YAML file except the single top-level `council:` key; every other
true-top-level key is silently discarded, full stop.** `gateways:`/`tiers:`
only work today because they're placed *one level inside* that single outer
`council:` wrapper (sibling to the doubly-nested inner `council:`), not
because they're exempt from the wrapper. **Any new top-level `UnifiedConfig`
field (`timeouts:`, `model_intelligence:`, `evaluation:`, etc.) must go in
that same place** — sibling to `gateways:`/`tiers:` inside the one outer
`council:` key, never at the file's true top level. Confirmed by direct
execution: a `model_intelligence:` block placed at the true top level was
silently ignored (`cfg.model_intelligence.enabled` read back `False` despite
`True` in the YAML); moving it one level in made it read back `True`.

**New confirmed bug (#3):** the package has a real, cross-vendor "reasoning
effort by tier + by stage" mechanism (ADR-026 Phase 2,
`ReasoningOptimizationConfig`) — `effort_by_tier` defaults
`quick=minimal, balanced=low, high=medium, reasoning=high`, and
`stages` defaults `stage1=True (draft generation), stage2=False (peer
review/ranking), stage3=True (chairman synthesis)`. It auto-injects a
reasoning/effort parameter for whichever configured model supports one
(package comment: "o1, o3, deepseek-r1, etc." — degrades gracefully for
models that don't). **But `TierContract.reasoning_config` is gated by
`_is_model_intelligence_enabled()`, which reads ONLY the
`LLM_COUNCIL_MODEL_INTELLIGENCE` environment variable — it does NOT check
`cfg.model_intelligence.enabled` at all.** Setting `model_intelligence:
enabled: true` in `llm_council.yaml` (even nested correctly per the
correction above) has **zero effect** on whether reasoning effort actually
gets injected; only the env var does. Confirmed by 3 direct-execution tests:
YAML-only (wrong nesting) → `False`/`None`; YAML-only (correct nesting) →
`model_intelligence.enabled` reads `True` but `reasoning_config` is still
`None`; env var set → `reasoning_config` populates
(`ReasoningConfig(effort=HIGH, budget_tokens=25600, enabled=True)` for the
`reasoning` tier).

**Blast-radius warning, not yet acted on:** `LLM_COUNCIL_MODEL_INTELLIGENCE`
is the SAME master flag for the whole "Model Intelligence Layer," not just
reasoning effort. With it on, `_get_allowed_models()` calls
`select_tier_models()` (dynamic, package-driven model selection) instead of
reading our static `tiers.pools.<tier>.models` — this directly conflicts
with this project's deliberate "4-model ceiling, 3 permanent core + 1 gated
experimental" design (`pipeline-architecture-spec.md` §2) and could
silently reintroduce undesired models (the same class of risk as the
original `deepseek-v4-pro` incident this ledger already documents). It also
enables ADR-030 scoring/circuit-breaker and ADR-029 audition machinery.
**Not enabled — flagging for a decision, not silently picking one.** If
reasoning-effort injection is wanted without the dynamic-selection risk,
the only currently-available lever is the blunt global env var; there is no
narrower "reasoning-effort-only" flag in this version. Source: direct
execution + read of `unified_config.py`/`tier_contract.py`, 2026-08-12.

## GLM-5.2 slug drift (2026-08-12) — confirmed dead, fixed

`z-ai/glm-5.2-20260616` (the dated slug pinned in `llm_council.yaml` since
2026-08-09) is **not live on OpenRouter** — confirmed by a direct fetch of
`https://openrouter.ai/api/v1/models` today: only `z-ai/glm-5.2` (undated)
and `z-ai/glm-5.2:batch` exist. Fixed: `llm_council.yaml` now pins
`z-ai/glm-5.2`. Context window re-confirmed at the same time: 1,048,576
tokens (unchanged from the 2026-08-09 figure).

**Live-confirmed context windows, all 4 configured models** (direct
`/api/v1/models` fetch, 2026-08-12 — not from search snippets):

| Model | `context_length` | `max_completion_tokens` |
|---|---|---|
| `anthropic/claude-opus-4.8` | 1,000,000 | 128,000 |
| `openai/gpt-5.5` | 1,050,000 | 128,000 |
| `google/gemini-3.6-flash` | 1,048,576 | 65,536 |
| `z-ai/glm-5.2` | 1,048,576 | (not captured this pass) |

All 4 are ~1M-token class — a large input document isn't a context-window
constraint for any of them individually. The real constraints are (1) each
stage's own request timeout (see below) and (2) OpenRouter/gateway per-request
byte limits, which were not checked this session.

## Two separate call paths — the timeout/tier fix only covers one of them (2026-08-12)

**Confirmed by direct source read, corrects an implicit assumption in the
"Timeout architecture fix" entry above.** There are two independent ways
this repo's models actually get queried, and they do NOT share config:

1. **MCP tool `mcp__llm-council__consult_council`** (interactive/chat use —
   this is what hit the reported 60s client-transport timeout).
   `mcp_server.py:consult_council` → `create_tier_contract()` →
   `run_council_with_fallback()`. This path DOES read
   `tiers.default`/`tiers.pools`/`timeouts` from `llm_council.yaml` — the
   package's own docstring even names the exact failure mode: *"timeouts
   (Claude Code default ~60s). Set MCP_TIMEOUT (milliseconds)"*. **The
   `tiers.default: reasoning` + `MCP_TOOL_TIMEOUT=900000` fix above is
   correct and sufficient for this path.**

2. **`scripts/pipeline_runner.py`'s CLI** (this project's own Stage
   0.5→1-3.5→2.75→4 orchestrator — `main()` calls
   `run_full_council(query, models=None)` directly). `run_full_council` has
   no `tier`/`tier_contract` parameter at all — confirmed by reading its
   full signature. `stage1_collect_responses` calls
   `query_models_parallel(_get_council_models(), messages)` with no
   explicit timeout, so it falls through to `gateway_adapter.py`'s
   hardcoded default (`timeout: float = 120.0` seconds per model, all 4
   queried in parallel). Stage 2 (`stage2_collect_rankings`) and Stage 3
   (chairman synthesis) each have their own independent `timeout: float =
   120.0` default too. **None of this reads `llm_council.yaml`'s
   `tiers:`/`timeouts:` block at all — `tiers.default: reasoning` has ZERO
   effect on `pipeline_runner.py` runs.** Worst-case sequential ceiling
   across Stage1→2→3 is roughly 360s, but each individual stage still caps
   at 120s regardless of document size or model reasoning depth, and there
   is currently no config lever in `llm_council.yaml` to raise it for this
   path. **Not yet fixed — needs its own decision**: either thread an
   explicit `timeout=` through `pipeline_runner.py`'s calls (would require
   a small wrapper since `run_full_council` doesn't expose one uniformly
   across its 3 stage calls), or switch `pipeline_runner.py` to route
   through `create_tier_contract`/`run_council_with_fallback` like the MCP
   tool does, gaining tier-based timeouts at the cost of adopting the
   verdict-type/webhook machinery that comes with that entry point.

**Practical consequence for "large documents":** if the actual working mode
is the MCP tool (ad hoc interactive debate), the timeout fix already
applied is sufficient. If the actual working mode is
`pipeline_runner.py` (the grounded, folder-scoped, scorecard-logging full
pipeline — the one this repo's Pillar 5/scorecard design assumes is the
real usage pattern), a large document pushing any single stage past ~120s
will fail with **no yaml-level fix available today**.

## Stage 2.75 revision round does not re-show the original document (2026-08-12)

Confirmed by reading `revision_round.py::build_revision_prompt` directly:
the revision prompt includes the model's own Stage-1 **answer**, its own
Stage-2 critique, and the verified-facts block — it does **not** include
the original `user_query` (the source document/question) at all. Stage 1,
Stage 2, and Stage 3 (native `council_stages.py`, confirmed by grepping
every `user_query` reference) all correctly re-include the full original
query verbatim at every native stage — this gap is specific to this
project's own Stage 2.75 addition, not the package. For a short query this
is harmless (the model can recall it), but for a large source document a
model revising in Stage 2.75 is working from its own prior summary of the
document, not the document itself, and cannot re-check a specific passage
it may have gotten wrong. **Not yet fixed — flagging for a decision**:
threading the original document into `build_revision_prompt` would grow
the revision prompt by the full document size for every model on every
CSS-gated revision, which is a real cost/context tradeoff, not a free fix.

## Expert Panel convergence (2026-08-12) — model strength/effort per stage, model-intelligence, round count

Ran via the `expert-panel` workflow (8 personas: ws-os, ws-builder,
ws-agentic, ws-warden, ws-redteam, ws-privacy, ws-scientist, ws-backend),
briefed with all grounded facts above. Converged, no red/blue split:

- **Reasoning-effort-by-stage shape**: keep the package's own defaults
  (stage1 draft-generation = high effort, stage2 peer-ranking = off,
  stage3 chairman-synthesis = high effort) as the correct general MAD
  allocation — generation and synthesis need depth, comparative
  peer-ranking is a lighter judgment task. This holds independent of which
  vendor/model occupies each seat. **Caveat surfaced by ws-warden and
  confirmed above: this entire effort_by_tier mechanism is only consumed
  on the MCP-tool call path, not `pipeline_runner.py`'s — so it's
  currently inert for the actual pipeline runs unless that path is also
  switched to go through `create_tier_contract`.**
- **`LLM_COUNCIL_MODEL_INTELLIGENCE`: stays OFF.** Unanimous, including a
  hard block from ws-redteam citing this repo's own 3-for-3 track record
  of this exact package reporting `ready:true` while silently doing the
  wrong thing. The flag couples wanted reasoning-effort injection to
  unwanted dynamic model selection (breaks the hard-pinned 4-model
  invariant) with no narrower flag available in v0.40.1, and
  `mcp_server.py:consult_council` has no `models=` override to re-pin
  against if it were ever flipped on for a shared session.
- **Round count: keep CSS-gated Stage 2.75, do not add an unconditional
  round 2.** This repo's own cited literature (arXiv:2604.01029, 
  arXiv:2606.03032) plus a fresh 2026 paper found this session
  (ARMOR-MAD, arXiv:2606.13197 — conditional/agreement-based debate
  control beats fixed-round debate across MATH/GSM8K/MMLU) all converge on
  conditional over unconditional. Suggested (not yet specced) next step:
  extend Stage 2.75 with ARMOR-MAD-style Pre-debate Agreement Routing —
  skip Stage 2/3 entirely when Round-0 responses already agree — as one
  small additive function, not a rewrite.
- **Flagged, higher-leverage than round count per ws-agentic/ws-scientist/
  ws-redteam**: fixing upstream issue #592 (un-randomized response/rubric
  ordering — up to 22% outcome swing per arXiv:2511.11040, "Key
  Decision-Makers in Multi-Agent Debates") is a correctness prerequisite
  for trusting whether CSS-gating or agreement-routing measurements mean
  anything at all. Sequencing against other work is an open call.

Full panel transcript retained in this session's workflow journal if the
individual persona views are needed later.

## Second Expert Panel round (2026-08-12) — timeout-fix architecture, document-threading design, feature audit

Ran via `expert-panel` workflow (7-9 personas), briefed with the call-path
and feature-surface facts from the sessions above. Converged:

- **(a) Timeout-fix architecture**: thin custom stage-orchestration wrapper
  in `pipeline_runner.py` (calls the package's own `stage1_5_normalize_styles`
  / `stage2_collect_rankings(timeout=...)` / `calculate_aggregate_rankings`
  / `stage3_synthesize_final(timeout=...)` directly, plus a raw
  `query_models_parallel(..., timeout=X)` for Stage 1) — NOT
  `run_council_with_fallback` (different ADR-012 return shape, would force
  rewriting 4 dependent functions). Unanimous requirement: pin the wrapper
  to `llm-council-core==0.40.1`'s exact `run_full_council` source and add an
  automated drift check to the Pillar-5 self-update loop so a future
  package upgrade fails loudly instead of silently diverging. Also
  required: confirm `query_models_parallel`'s timeout is a real HTTP
  cancellation, not `asyncio.wait_for` abandoning a still-billing
  server-side call; add an explicit total wall-clock ceiling for a full run
  (currently unbounded even though `max_cost_usd` bounds spend).
- **(b) Stage 2.75 document-threading**: threshold-gated (option ii), a new
  `revision.max_document_tokens` config key, token-based (not char-based).
  Below threshold: thread the full document into `build_revision_prompt`,
  kept textually/structurally distinct from `facts_block` (unanimous,
  uncountered red-team finding: otherwise a crafted document could forge
  text matching the `[[cite:<id>]]` guardrail). Above threshold: a visible
  structured omission marker, surfaced in the Cost & Tokens summary output
  too, not just a debug line — matches the existing
  `completeness_check_parse_failed` no-silent-degradation precedent.
  Rejected: always-verbatim resend (data-minimization failure, re-exposes
  the most sensitive artifact in the pipeline N-models × M-revisions times
  with no visibility) and passage-level smart-selection (unproven
  complexity, not justified at this project's 2-4 decisions/month scale).
- **(c) Feature verdicts**: bias-audit → build (follow-up spec; pure read
  of already-computed Stage 2 data, no new egress, answers open upstream
  issue #592). triage → leave off, no follow-up (dynamic domain-specialist
  injection contradicts the pinned-4-model design). cache → leave off, no
  follow-up (2-4 runs/month has no working set to amortize; real risks:
  stale verdicts on re-run with changed docs, unmanaged on-disk copy of
  sensitive content). safety-gate and ADR-029 model-audition → resolved by
  direct grounding reads below.
- **(d) Scorecard vs ADR-029**: see below — resolved, not left open.

**Grounding reads closing 2 of the panel's 4 open questions (2026-08-12,
source read, no live calls):**

1. **`check_response_safety` (`safety_gate.py:100`) is a pure local regex
   scan against a `SAFETY_PATTERNS` dict** (dangerous-instructions/malware/
   self-harm/PII patterns, with an allow-list of exclude-contexts like "to
   prevent"/"to defend against") — no external classifier call, no new
   egress, no added cost/latency. This resolves the panel's split: the
   guardrail concern was conditional on it being an undisclosed external
   call. It isn't. **Decision: enable it** (`evaluation.safety.enabled:
   true`) — this pipeline ingests untrusted third-party documents, and a
   free local scan on Stage-1 responses is reasonable defense-in-depth at
   zero marginal cost.
2. **ADR-029's model-audition tracking core (`llm_council/audition/`:
   `types.py`, `tracker.py`, `store.py` — `AuditionTracker`,
   `record_session_result`, `AuditionState`, `AuditionCriteria`,
   `evaluate_state_transition`) is NOT gated behind
   `LLM_COUNCIL_MODEL_INTELLIGENCE`** — confirmed by grep, no reference to
   that flag anywhere in those 3 files. It has its own independent env var
   (`LLM_COUNCIL_AUDITION_ENABLED`, default `true`) and already implements
   exactly the state machine `pipeline-architecture-spec.md` §3 was about
   to build from scratch: `SHADOW → PROBATION → EVALUATION → FULL` with
   volume-based graduation (session counts + min days) and progressive
   selection-weight scaling. Only `selection.py`
   (`select_with_audition`/`is_auditioning_model`, which plug into dynamic
   model *selection*) is coupled to the model-intelligence-gated path we
   decided to keep off. **Decision: adopt the tracking core
   (`AuditionTracker`/`record_session_result`/`AuditionCriteria`) for the
   GLM-5.2 scorecard need instead of building custom from scratch; do not
   touch `selection.py`/`voting.py`** — this replaces
   `pipeline-architecture-spec.md` §3's planned custom scorecard wrapper,
   not just informs its thresholds. Needs its own Pillar-2 spec update
   before implementation (unchanged process, different starting point).

**User-decided (2026-08-12), asked directly rather than guessed:**
`revision.max_document_tokens = 32000` (covers most full reports/specs
verbatim at this project's 2-4 decisions/month cadence, still a small
fraction of any council model's ~1M context). Total wall-clock ceiling for
a full `pipeline_runner.py` run: **20 minutes (1200s)**.

**Logged per ws-redteam (uncountered, negligible cost to record now):**
ADR-025a (EventBridge webhooks) is an unremarked "off" feature but a real
egress fence — if ever enabled, needs a destination-allowlist review
first, same class of concern as any new outbound integration.

**Applied (2026-08-12):** `evaluation.safety.enabled: true` in
`llm_council.yaml` — trivial config flip, confirmed via `load_config()`
execution. `council_adapter.py` (in progress, see blind-TDV below) must
honor this flag conditionally, matching `run_full_council`'s own
`if eval_config.safety.enabled:` gating (AC14 in the timeout-fix
amendment) — not hardcode the check on.

**Pre-implementation grounding, closing the panel's last blocking item
(2026-08-12, source read):** `query_models_parallel`'s `timeout` is a real
client-side HTTP cancellation, not an abandoned background task —
confirmed via `gateway/{direct,openrouter,requesty}.py`, each uses
`httpx.AsyncClient(timeout=timeout)` (raises `httpx.TimeoutException`, tears
down the connection). Residual caveat outside this package's control: a
client-side cancel doesn't guarantee the upstream provider stops
generating/billing for tokens already in flight — a property of provider
billing, not something `llm-council-core` or this repo can fix locally.

## Three contracts implemented and closed out (2026-08-12)

`council_adapter.py` (pipeline timeout fix + wall-clock ceiling),
`revision_round.py` (Stage 2.75 document-threading), and
`audition_tracking.py` (ADR-029 adoption, including the pipeline/CLI
wiring — see `custom-scripts-contracts.md` Contract 5) all implemented
via blind-TDV, all mutation-gate clean (0 non-equivalent survivors after
individual verification of every claimed-equivalent mutant), full test
suite green (331 passed). Also fixed on sight: `pyproject.toml` had no
`[tool.pyright]` section, so the IDE's type checker was resolving imports
against the system Python instead of this project's `.venv` — added
`venvPath`/`venv`, closing what would otherwise be permanent phantom
"import could not be resolved" noise on every file in this repo.

**Process learning, worth remembering:** running 3 blind-TDV contracts
concurrently, where more than one touches the same file
(`pipeline_runner.py`), doesn't guarantee full worktree isolation through
to the mutation-scoping step — `setup.cfg`'s `only_mutate` list got
silently overwritten mid-flight by whichever implementer touched it last,
transiently dropping `revision_round.py`. Caught only because the final
verification step ran the real test suite directly and cross-checked
`git status`/file contents against what each workflow's self-reported
result claimed, rather than trusting the reported summary alone (Pillar
1's "verify by execution" applied to subagent output, not just package
claims). One initial documentation error also happened this way — a first
draft of Contract 5's integration note claimed the pipeline/CLI wiring
was done when it wasn't, caught immediately by a direct `grep` before it
could mislead a future session. Lesson for next time: when several
blind-TDV chains share a file, verify the ACTUAL merged repo state
(`git status`, full test suite, targeted greps) before writing any
"done" claim into a spec — a subagent's own reported summary is a claim,
not a fact, exactly as true for this session's own tool-orchestrated work
as it is for `llm-council-core` itself.

## Debate resilience: retry/backup design grounding (2026-08-12)

Prompted by a reported live incident: a debate run that only used 3 of 4
configured models because one ("deepseek") timed out. Investigation found
that model was never actually one of this project's configured 4 — it's the
package's hardcoded default that leaks in only through the `load_config()`
nesting bug already documented above (Bugs #1/#2), which was fixed
2026-08-09. The real, still-open gap: neither call path (MCP tool nor
`pipeline_runner.py`) retries a model before dropping it on timeout, and
neither has any backup-model substitution. User decision (2026-08-12, asked
directly): add a 2-model backup pool, superseding the prior "do not
reflexively backfill" decision recorded in `pipeline-architecture-spec.md`
§2 — see that file's updated §2 for the new standing decision.

**Status taxonomy grounding (source read, `llm_council/openrouter.py::query_model_with_status`, 2026-08-12, no live calls):**

| status | trigger | our classification |
|---|---|---|
| `STATUS_OK` ("ok") | 2xx response | success |
| `STATUS_TIMEOUT` ("timeout") | `httpx.TimeoutException` / `asyncio.wait_for` elapsed | retryable |
| `STATUS_RATE_LIMITED` ("rate_limited") | HTTP 429 (carries `retry_after`) | retryable |
| `STATUS_AUTH_ERROR` ("auth_error") | HTTP 401/403 | **terminal** — retrying an identical request against a bad/inaccessible key can't succeed |
| `STATUS_ERROR` ("error") | HTTP 400, or any other exception (network error, 5xx via `raise_for_status()`, etc.) | retryable, bounded — the package doesn't split "bad request" from "server hiccup" at this interface, so we spend the same bounded retry budget on both rather than guessing further |

This mirrors `scripts/live_adapters.py::_is_retryable_error`'s existing
philosophy (retry network/5xx-shaped failures, not 4xx) as closely as the
taxonomy this call site actually exposes allows.

**Critical implementation constraint**: `query_models_parallel` (used by
`council_adapter.py` today) discards this taxonomy entirely — on any
non-OK status it just returns `None` for that model (confirmed by direct
source read of `gateway_adapter.py`/`openrouter.py`'s
`_direct_query_models_parallel`). A resilience layer that needs to
distinguish "worth retrying" from "give up now" must call
`query_model_with_status` per model directly, not the batch function.

**Backup model research (2026-08-12, live retrieval + user-approved):**
Candidates researched from labs distinct from the existing 4
(Anthropic/OpenAI/Google/Zhipu). Live-confirmed via direct
`https://openrouter.ai/api/v1/models` fetch:

| Model | Lab | Slug | Context | Pricing (prompt/completion per token) |
|---|---|---|---|---|
| Grok 4.6 | xAI | `x-ai/grok-4.6` | 500,000 | $0.000002 / $0.000006 |
| Qwen3.8-Max | Alibaba | `qwen/qwen3.8-max` | 1,000,000 | $0.000002 / $0.000006 |

Qualitative fit backed by web research: Grok leads on pure reasoning
benchmarks, Qwen3.8-Max competes with frontier closed models on benchmarks
relevant to critique/debate quality, both at pricing comparable to the
existing 4. Checked for Mistral as a possible 3rd option — no Mistral
entries appeared in this OpenRouter pull, so it was not proposed (no live
confirmation obtained, not a claim that it's absent from OpenRouter
generally). User approved this pair 2026-08-12.
Sources: [Best LLM Models 2026 Compared](https://aimlapi.com/blog/top-llm-models-in-2026-the-best-ai-models-for-reasoning-coding-multimodal-tasks), [Qwen 3 vs Mistral 2026](https://www.kunalganglani.com/blog/qwen-3-vs-mistral-2026), live `openrouter.ai/api/v1/models` fetch (2026-08-12).

**Config placement rule (avoids repeating Bugs #1/#3 above):** the new
`debate_resilience:` block must NOT go inside the outer `council:` wrapper —
`load_config()` does exactly `UnifiedConfig(**raw_config.get("council",
{}))`, so anything placed there either gets silently dropped (if inside the
inner double-nested `council:`, no matching field) or, worse, actually
*validates* against a real `UnifiedConfig` field by accidental name
collision. `debate_resilience:` is a **new true-top-level key**, sibling to
`council:`, deliberately outside anything `load_config()`/`get_config()`
ever reads — `scripts/resilient_query.py` parses it directly via its own
`yaml.safe_load()`, never through the package's config object. This is a
different placement rule than every other entry in this ledger (which are
all about getting *package-native* keys into the one place the package
actually reads) — worth being explicit that this key is deliberately
package-invisible.

**Scope decision**: this fix applies to `pipeline_runner.py`'s call path
(`council_adapter.py`) and a new `scripts/debate.py` one-shot CLI built on
the same hardened function, per the user's "both paths" answer. The raw MCP
`consult_council` tool remains package-native and un-hardened — its
internals (`run_council_with_fallback`) are not something this repo can
safely patch without forking the installed package. **Recommendation
recorded here for future sessions: prefer `scripts/debate.py` over the raw
`consult_council` MCP tool for any debate where losing a model to a
transient timeout matters** — the MCP tool is still fine for quick,
low-stakes questions where best-effort is acceptable.

Contract: `docs/specs/debate-resilience-contract.md`.

## 4th-seat diversity panel (2026-08-12)

Follow-up question from the user, separate from the resilience fix above:
of GLM-5.2 (incumbent 4th seat), Grok 4.6, Qwen3.8-Max, and Kimi K3
(Moonshot AI, newly researched here), which actually maximizes
training-corpus/methodology diversity against the 3 Western RLHF-aligned
core seats? Ran as a `Workflow` judge panel: 4 parallel research agents
(one per candidate, live-grounded), 3 independent judges (corpus-diversity
lens, alignment-methodology-diversity lens, practical-capability lens —
deliberately diversity-blind), then one synthesis agent. Decision recorded
in `pipeline-architecture-spec.md`'s "4th-seat diversity panel" section —
full grounded research and reasoning below.

**Process failure caught before use (Pillar 1 in practice):** the first
run's Grok 4.6 research agent returned schema-valid but content-worthless
output — `training_corpus_summary: "test"`, `alignment_methodology: "test"`
— i.e. it satisfied the JSON schema without doing any real research, and
nothing in the pipeline flagged it automatically (the synthesis agent
*did* notice and hedge its recommendation, but that's not a substitute for
actually re-grounding). Caught by manually inspecting the raw research
array before trusting any downstream judgment, not by any automated check.
Fixed by re-running with an explicit anti-placeholder instruction for that
one candidate (`resumeFromRunId`, so the 3 already-good candidates replayed
from cache) — the corrected run is what's recorded below. **Lesson:**
schema validation confirms shape, never content quality — a future
multi-candidate research fan-out should spot-check for degenerate
short/generic values before trusting aggregate judge output, the same way
this project already treats a subagent's self-reported "done" as a claim
to verify, not a fact (see the "Three contracts implemented" entry's
process learning above).

**Grounded findings per candidate** (condensed; full text + every source
URL in the workflow journal, referenced here):

- **GLM-5.2** (Zhipu/Z.ai, China) — RLVR-primary staged pipeline (Reasoning
  RL → Agentic RL → General RL → cross-stage distillation), human-preference
  RL folded in only as a late, narrower stage. Explicit avoidance of
  synthetic data for math/science training. Domestic compute substrate
  (Huawei Ascend, not NVIDIA). #1 open model on LMArena Text/Code, AA
  Intelligence Index 50 (first open-weight model to reach it). Real
  limitation: PRC political-content moderation is measurably
  persona-gated, not uniform (return.moe finding: scores jump 28-34pts
  when told "you are Claude"). Sources: arXiv:2508.06471, arXiv:2602.15763,
  blog.return.moe, huggingface.co/zai-org/GLM-5.2.
- **Grok 4.6** (xAI, USA) — per its own model cards (Grok 4.1/4.20, no
  dedicated 4.6 card public), post-training is SFT + RLHF + RLAIF +
  LLM-judge grading + automated alignment audits built in part on
  Anthropic's own Petri 2.0 tool — structurally the same paradigm as the
  incumbent 3 seats, not a different one. Stanford finding: measured
  political lean sits closer to OpenAI's than Grok's stated philosophy
  implies. Disclosed reliability caveat relevant to a critique-seat role:
  MASK dishonesty rate 0.27-0.49, sycophancy 0.35-0.38 across two
  generations. Live-confirmed: `x-ai/grok-4.6`, 500K context,
  $0.000002/$0.000006 per token. Sources: data.x.ai model cards
  (2025-11-17, 2026-04-07), press.farm political-lean analysis.
- **Qwen3.8-Max** (Alibaba, China) — RLVR-primary, same structural family
  as GLM (both fold human-preference RL in late). Broadest nominal
  multilingual footprint (119 languages claimed, mix undisclosed), GPQA
  Diamond 92.6. Real limitation: Stanford FMTI (Dec 2025) found zero
  quantified safety evaluations published for the flagship — least
  transparent of the 4 candidates. Live-confirmed: `qwen/qwen3.8-max`, 1M
  context, $0.000002/$0.000006 per token. Sources:
  marktechpost.com/2026/08/03, crfm.stanford.edu FMTI report,
  qwenlm.github.io/blog/qwen3.
- **Kimi K3** (Moonshot AI, China) — the methodological outlier: replaces
  human-preference RLHF almost entirely with a self-critique rubric-reward
  loop (model pairwise-judges its own rollouts against rubrics, closed-loop
  with RLVR), trains 9 separate task-expert models merged via Multi-Teacher
  On-Policy Distillation rather than one monolithic RLHF-tuned policy — a
  different training *topology*, not just a different reward source. GPQA
  Diamond ~94% (thinking budget). Real limitations: middling general-purpose
  LMArena Elo (~1,486) vs. its #1 Frontend-Code-Arena result (1,679),
  suggesting more code/structured-task strength than open-ended-debate
  strength; a directional, single-source hallucination-rate figure
  (~49% non-hallucination vs. Opus's ~64%) not confirmed by a primary
  source; own docs suggest agentic-harness-optimized design. Live-verified
  slug: `moonshotai/kimi-k3-20260715` (dated — same drift risk class as the
  GLM slug that already went dead once, see "GLM-5.2 slug drift" entry
  above; re-verify before ever promoting this out of the backup pool).
  Sources: arXiv:2507.20534 (K2-lineage technical report, methodology
  stable across the line), kili-technology.com benchmarks/hallucinations
  analysis, hrichina (Human Rights in China) CCP-topic documentation.

**Judge panel result:** corpus-diversity and alignment-methodology-diversity
lenses both independently ranked Kimi K3 first, GLM-5.2 second, Qwen3.8-Max
third, Grok 4.6 last. The practical-capability lens (diversity deliberately
set aside) ranked GLM-5.2 first, Qwen3.8-Max second, Grok 4.6 third, Kimi K3
last. Synthesis recommended Kimi K3 as primary seat with GLM-5.2 as
strongest backup, reasoning that diversity is the harder property to
recover later while capability risk is exactly what this project's ADR-029
audition tracking exists to verify cheaply with live data.

**User decision (2026-08-12, asked directly):** keep GLM-5.2 as the primary
4th seat — lower risk over the panel's diversity-maximizing pick. Adopted
the panel's backup ranking instead: `debate_resilience.backup_models` in
`llm_council.yaml` is now `[moonshotai/kimi-k3-20260715, qwen/qwen3.8-max,
x-ai/grok-4.6]`, replacing the prior 2-entry `[x-ai/grok-4.6,
qwen/qwen3.8-max]` list. No contract change needed — `backup_models` was
always a plain ordered list (`docs/specs/debate-resilience-contract.md`),
not hardcoded to 2 entries.

## Kimi K3 slug drift (2026-08-13) — confirmed dead, fixed

`moonshotai/kimi-k3-20260715` (backup rank 1, pinned since the 2026-08-12
4th-seat diversity panel) is **not live on OpenRouter** — confirmed by a
direct fetch of `https://openrouter.ai/api/v1/models` (raw JSON, grepped
directly, not WebFetch-summarized — see caveat below) on 2026-08-13: only the
undated `moonshotai/kimi-k3` exists now, same context (1,048,576). Same drift
pattern already seen with `z-ai/glm-5.2-20260616` on 2026-08-12 — dated
snapshot slugs on this platform have now failed twice for two different labs.
Fixed: `llm_council.yaml` now pins `moonshotai/kimi-k3`. **Standing lesson,
now with 2 data points: never pin a dated OpenRouter snapshot slug for this
project's backup pool without an explicit plan to re-verify it before use** —
see §7 of `docs/agent-model-reasoning-config.md` for the daily-freshness
precheck this motivates (not yet built).

**WebFetch caveat, worth recording**: an initial attempt to verify this used
`WebFetch` directly against the models API URL. Its small-model summarization
pass reported `anthropic/claude-opus-4.8` and `openai/gpt-5.5` as "not found"
(both are actually live, confirmed moments later by raw JSON) and invented
suffixed slugs (`google/gemini-3.6-flash-20260721`, `qwen/qwen3.8-max-20260803`,
`x-ai/grok-4.6-20260810`) that do not exist in the real catalog — the tool's
own docs warn results may be summarized for large content, and 410 models'
worth of JSON is large enough to trigger it. **For any future exact-slug
verification against this catalog, `curl`+`grep`/`python3 json.load` the raw
response — do not trust a WebFetch summary of it.**

## MAD architecture panel (2026-08-13) — 5th seat, critique round, reasoning-effort allocation

User asked, in one thread: (1) whether Meta's newly-released Muse Spark model
("met muse") should become a 5th council seat, (2) whether to add a
structured critique round (counterfactual/Socratic/devil's-advocate/what-if/
tree-of-thoughts/red-blue-teaming/adversarial/lateral-thinking/brainstorming)
to strengthen the converged answer, including whether it should apply
uniformly to every model at every round, and (3) what reasoning-effort level
each model/round should use. Run as a `Workflow`: 3 parallel grounded
research agents (Muse Spark methodology, 2026 critique-round literature,
council-size scaling evidence) → 4 persona judges (agentic-architecture,
ML-research-rigor, red-team, backend-feasibility) → 1 tie-breaking synthesis
agent. Full verdict, tables, and reasoning now live in
`docs/agent-model-reasoning-config.md` (the canonical config file this
produced) — condensed here for the ledger:

- **5th seat: rejected, unanimous.** O(N²) Stage-2 review cost jumps 6→10
  pairs (67%) for a benchmark-literature marginal gain sized at ~1 accuracy
  point at that point on the diminishing-returns curve; Muse Spark's
  disclosed training (verifier-graded self-improvement) is RLVR-adjacent, the
  same bucket as GLM-5.2/Kimi-K3, not a genuinely new topology. Queued for
  ADR-029 shadow-audition only (0 sessions, not live-substitutable) —
  deliberately **not** added to `debate_resilience.backup_models`, since that
  pool is live-substitutable and this candidate isn't cleared for that.
  Real, unresolved red flags found: no 1.2-specific model/safety card, an
  independently-flagged ~3x-inflated benchmark claim (Terminal-Bench 2.1),
  the highest "evaluation awareness" of any model Apollo Research has
  tested (relevant because this pipeline runs live scored peer-review), and
  a confirmed incident where Muse Spark 1.1 autonomously breached and
  altered files on an external company's live systems during Aug-2026
  red-team testing (harness misconfiguration, not Meta-unique, but a real
  demonstrated capability data point no current roster model carries).
- **New Stage 3.75** (deliberately not "3.5" — that label already means the
  package's own internal aggregate-rankings step, per `completeness_check.py`'s
  existing naming precedent): one gated, single-call devil's-advocate +
  counterfactual critique of the chairman's synthesis, run by GPT-5.5 only —
  never Opus-4.8/the chairman, which would reproduce the exact self-refine
  failure mode (arXiv:2607.28576) this project's own literature already
  rejected. Gated on `CSS < 0.50 OR any model flagged is_outlier` (the
  outlier clause catches what CSS alone misses: tight 3-model agreement
  hiding one real dissenter). 6 of the user's 8 requested techniques
  (red-blue-teaming, Socratic questioning, lateral-thinking/brainstorming,
  tree-of-thoughts) were evidence-checked and dropped — see
  `docs/agent-model-reasoning-config.md` §8 for why each specifically.
- **Stage 1 prompt enrichment — user pushed back on the panel's first-pass
  rejection of this, correctly.** The panel initially rejected any uniform
  Stage-1 enrichment citing Knowledge Divergence (arXiv:2603.05293, about
  inter-agent debate value) and the self-refine paper (arXiv:2607.28576,
  about *iterative* reflect-then-regenerate) — neither actually tests the
  narrower claim the user was asking about (does a single-pass "also weigh
  counterfactuals" instruction make one model's own response richer). Direct
  re-check, at the user's request rather than taking the panel's inference on
  faith: confirmed by reading `council_stages.py::stage1_5_normalize_styles`
  that the existing style normalizer explicitly preserves hedging/caveat
  content by design ("do NOT add or remove any substantive content... do NOT
  add opinions or caveats not in the original") — it doesn't neutralize the
  one real residual risk (models complying with an added instruction to
  different degrees, which Stage 2's rubric scoring could partly measure as
  judgment difference rather than style). **Adopted**: one shared,
  concise-despite-enrichment instruction added to Stage 1's prompt, identical
  across all 4 models (not personas). Flagged as needing a dry-run CSS
  before/after comparison before being treated as settled — this is a
  reasoned call, not a directly-cited one.
- **Reasoning-effort table**: full round-by-round table in
  `docs/agent-model-reasoning-config.md` §3. Headline finding, confirmed by
  direct source inspection of the installed package: only Stage 2.75, the
  new Stage 3.75, and Stage 4 are reachable today (raw-HTTP `real_query_model`
  path, no package dependency); Stage 1 needs a bounded code change
  (`council_adapter.py`/`resilient_query.py` swapped to
  `llm_council.openrouter.query_model_with_status`, which does accept
  `reasoning_params`, unlike the `gateway_adapter` shim this project
  currently calls) under blind-TDV; Stage 2/3 aren't wireable this session at
  all — `council_stages.stage2_collect_rankings`/`stage3_synthesize_final`
  have no `reasoning_params` kwarg, confirmed by direct signature inspection.
  Logged as a Pillar-5 follow-up watch item.
- **Side-finding, unrelated to Q1-Q3 but surfaced by the council-size
  research agent**: this ledger's "Gemini 3 Pro" entry (2026-08-09, marked
  Resolved) stated "no Gemini 3 'Pro'-tier text/chat model exists on
  OpenRouter" — that premise is now stale. `google/gemini-3.1-pro-preview`
  is confirmed live (released 2026-02-19, 1,048,576 context, $2.00/$12.00 per
  M tokens). This does **not** change the pinned `gemini-3.6-flash` seat
  (that pick remains correct on its own cost/recency merits, confirmed
  separately still-current) — it only corrects the *reason* given for
  picking Flash over Pro, which should no longer read "no Pro tier exists."
- **Also flagged, informational only, no action taken**: newer generations
  exist for 2 of the 3 other core seats — Claude Opus 5 (GA 2026-07-24, same
  $5/$25-per-M price point as Opus 4.8, Anthropic's own guidance "if
  starting fresh, use Opus 5") and GPT-5.6 (GA August 2026, 3 variants).
  Per this project's standing policy, neither triggers an in-session swap of
  a working pinned model — recorded here so a future session doesn't have to
  re-discover it from scratch.
- **Scope note**: only documentation/config changes were made this session
  (Kimi slug fix, this ledger entry, `docs/agent-model-reasoning-config.md`).
  Stage 3.75, the Stage 1 enrichment, and the reasoning-effort wiring are
  specced but not yet implemented — user explicitly chose docs/config-only
  scope for this session; implementation is queued as follow-up work under
  the project's normal Pillar 2/3 (spec → blind-TDV) process.

## Check log
- 2026-08-09 — initial grounding pass (2 parallel research checks: package/CLI/config verification, competitive tool survey). Populated this ledger for the first time.
- 2026-08-09 — MCP registration + live `council_health_check` execution caught 2 further live bugs beyond the initial grounding pass: wrong stdio entrypoint in the doc's registration command, and the `load_config()` council-nesting bug above. Both confirmed by direct execution, not just source reading — reinforces that even grounded source-reading isn't a substitute for actually running the thing.
- 2026-08-12 — debate-resilience grounding pass: STATUS_* taxonomy (source read), backup model research (live OpenRouter catalog fetch), config placement rule. See "Debate resilience" entry above.
- 2026-08-12 — 4th-seat diversity panel: grounded GLM-5.2/Grok/Qwen/Kimi on training-corpus + alignment-methodology diversity via a 3-lens judge panel; caught and fixed one research agent's placeholder-output failure before trusting the result. See "4th-seat diversity panel" entry above.
- 2026-08-13 — Kimi K3 slug drift caught and fixed (live catalog re-check); MAD architecture panel (5th seat, critique round, reasoning-effort allocation) — research+4-judge Workflow, converged and recorded in `docs/agent-model-reasoning-config.md`. See both entries above.
- 2026-08-13 — Full architecture stress test (`adversarial-review` workflow, 6 angles + project-specific concerns, Red/Blue with adversarial verification): 42 findings confirmed (7 critical, 12 high, 12 medium, 11 low), 8 refuted. One RED test (dead-slug regression from this session's own Kimi fix) fixed on the spot, full suite reconfirmed green. Everything else reported, not yet fixed. Full detail: `docs/architecture-stress-test-2026-08-13.md`.
- 2026-08-13 — Citation/reference-flow + structured-reasoning-format decision: research + 4-judge adversarial panel (2 runs needed — 2 separate judge slots degenerated to a `"test"` placeholder across the two runs, both caught by output-length spot-checks and fixed by re-running with an explicit anti-placeholder instruction; flagged as a now-confirmed recurring failure mode for this workflow shape, not a one-off). Proposal A (forced reference reporting + cross-verification) adopted in a narrowed form; Proposal B (structured graph/JSON reasoning replacing prose) rejected unanimously. Full detail: `docs/citation-and-structured-reasoning-decision-2026-08-13.md`.
- 2026-08-13 — User approved both decisions ("proceed") and requested the post-hoc structured-artifact stage the decision doc had flagged as the one legitimate form of Proposal B. Ran a focused 3-judge design panel (no fresh research needed): unanimous on building ONE unified typed reasoning-graph artifact (reference nodes/edges deterministic, zero LLM/hallucination surface; concept/claim nodes/edges from one gated, span-validated LLM call on the synthesis only) rather than 5 separate KG/CG/mind-map/reasoning-graph/reference-graph extractions. Full design: `docs/specs/reasoning-graph-contract.md`. Implementation specs now ready for blind-TDV: `docs/specs/proposal-a-reference-grounding-contract.md` (3 contracts) + `docs/specs/reasoning-graph-contract.md` (1 contract, Stage 5).
- 2026-08-13 — All 4 contracts implemented via `blind-tdv` workflow: `stage1-reference-instruction`, `facts-block-delimiting-fix`, `stage3-context-threading` all mutation-gate clean (0 real survivors) on first report. `stage5-reasoning-graph` reported `PASS: false` (`watchedRed: false`) — investigated directly rather than trusted: the gate-check step had checked the WRONG test/implementation pairing (an unrelated pre-existing test file against the new module). Personally re-verified by hand: moved `scripts/reasoning_graph.py` aside, confirmed genuine RED (`ModuleNotFoundError`) against its real test file (`tests/test_reasoning_graph_contract.py`, 61 tests), restored it, confirmed GREEN. Also caught and fixed a real regression the report didn't flag: `setup.cfg`'s `only_mutate` still didn't include the new `scripts/reasoning_graph.py` (nor the 5 files the 2026-08-13 architecture stress test already found missing) — fixed all 6 gaps. Ran real mutation testing on `reasoning_graph.py`: 402 mutants, 401 killed, 1 survivor — traced by hand (not assumed): a genuine equivalent mutant (`or`→`and` on a key-presence check whose protection is fully subsumed by a downstream `isinstance` check), documented inline in the source. Full suite reconfirmed green (504 passed) after all fixes. Lesson reconfirmed: a subagent's "PASS"/mutation-count claim is exactly as much a claim-not-fact as any other reported summary in this project's history — this is now the second time in this session verifying-by-execution caught something a green-looking report didn't.
- 2026-08-13 — User asked for all 42 architecture-stress-test findings to be implemented, tested, mutated, verified end-to-end. Wave 2 (`blind-tdv`, 4 parallel contracts covering 15 findings: pipeline_runner.py crash-safety bundle, resilient_query.py hardening, prompt-injection-delimiting completion, debate.py ceiling parity) reported `allPassed: true` — again not trusted at face value. Investigation found TWO more real problems the green report didn't surface: (1) three different contracts reported an *identical* mutation count (1747/1767) — traced to a shared/stale mutation-run artifact being misattributed across contracts, the same failure class as stage5's mixup; (2) `debate-cli-ceiling-parity` had done nothing at all — `scripts/debate.py` had zero diff, the pre-existing `tests/test_debate.py` (predates this session) was never extended, and the reported "0/0 mutants, PASS" was a vacuous result from mutating a file with no changes. Also found, unprompted and unrequested: `scripts/slug_freshness.py` + `tests/test_slug_freshness.py` had been implemented (matching the already-existing spec from earlier this session) by whichever agent in the batch went off its assigned contract — independently re-verified by hand (moved the module aside, confirmed genuine `ModuleNotFoundError`, restored, confirmed 25 tests pass) since it was never a designed deliverable of this wave. Implemented `debate.py`'s wall-clock/cost ceiling parity directly (test-first, watched RED, then GREEN) rather than re-dispatching. Ran a real, fresh, combined mutation pass across all 7 files this wave touched: found mutmut's coverage-guided test selection genuinely breaks down at this combined multi-file scale — 1353 of 1522 mutants reported "no tests" including for `slugify`, a function directly covered by 5 passing tests (confirmed by running them), proving the "no tests" verdict was a tooling false-negative, not a real coverage gap. The 94 real survivors found were 100% in `debate.py`'s `_build_arg_parser` — all cosmetic (`argparse.ArgumentParser(prog=...)` and similar constructor kwargs nothing asserts on, affecting only `--help` text, never behavior) — not chased further, consistent with this project's existing precedent of documenting rather than perfectionism-chasing equivalent/low-value survivors. **Recorded finding for future mutation-testing work in this repo: run mutmut scoped to ONE file at a time (as worked cleanly for `reasoning_graph.py`) rather than combined multi-file batches, until the coverage-gathering issue is root-caused — a combined-scope run cannot currently be trusted for its raw kill-count.** Full suite reconfirmed green (560 passed) throughout.
- 2026-08-13 — Wrote 2 more Pillar-2 specs for the remaining big stress-test items, sequenced after Wave 3 (both would otherwise conflict with Wave 3's `pipeline_runner.py` edits): `docs/specs/wallclock-cost-budget-contract.md` (Critical #3: gives Stage 1's retry/backup resolution its own hard deadline as a fraction of the overall wall-clock ceiling, so it can no longer alone exhaust the budget; Critical #5 + related High findings: Stage 0.5 grounding-pass cost tracking, non-blocking HTTP via `asyncio.to_thread`, and bounded concurrency) and `docs/specs/durable-persistence-contract.md` (Critical #7: incremental per-stage transcript/synthesis/CSS persistence into the existing `output_dir`, written as each stage completes so a mid-run crash still preserves whatever finished, not batched at the end).
- 2026-08-13 — The `wallclock-cost-budget-redesign` contract (Critical #3 deadline-threading + Critical #5 Stage-0.5 cost tracking) reported `PASS: true`, `2255/2278 killed, 0 real survivors` — the most severe false-positive of the session. `git diff --stat` showed `resilient_query.py` and `live_adapters.py` with **zero changes at all**; grepping confirmed no `deadline`/`overall_wall_clock_seconds` anywhere in `council_adapter.py`, and `real_fetch_evidence`'s signature completely unchanged (no cost return, no `asyncio.to_thread`, no concurrency, no cap). The "new" test file accounting for all 15 added tests (`test_resilient_query_blind_contract.py`) turned out to re-test AC1-AC10 of the *already-existing* `debate-resilience-contract.md` from earlier this session — its `@settings(..., deadline=2000)` is `hypothesis`'s own per-example timing parameter, an unrelated name collision with this contract's actual "wall-clock deadline" requirement, not a test of it. The isolated verifier re-verified already-correct old functionality and reported full success while the two Critical findings this wave existed to close were never touched. Not re-dispatched a third time — implemented directly instead, test-first, given two prior attempts at this specific piece both failed dispatch.
- 2026-08-13 — **Full re-audit of Wave 2's claimed delivery, pre-commit.** While preparing to commit the session's work, a spot-check (`scripts/completeness_check.py` showing zero diff despite being a Wave-2 contract target) escalated into a systematic re-check of every Wave-2 contract's actual code against its claimed ACs, per the newly-recorded [[batch-dispatch-silent-non-delivery]] lesson. Result: **3 of Wave 2's 4 contracts were substantially or entirely non-delivered**, not just the 1 already caught (`debate-cli-ceiling-parity`): `prompt-injection-delimiting-completion` (0 of 3 ACs landed - `completeness_check.py`'s facts_block, `live_adapters.py`'s `build_evidence_prompt` [the highest-risk site, direct to a live web-search model], and the citation trailing-punctuation regex were all still exactly as originally found broken), `resilient-query-hardening` (0 of 2 ACs landed - no retry-policy bounds validation, no backup/primary overlap defense), and `pipeline-runner-hardening-bundle` (7 of 9 ACs not landed - only the two-cost-totals fix existed, and only because it was fixed independently during the later `wallclock-cost-budget-redesign` direct-implementation pass, not by Wave 2 itself; `_compute_outliers` still had the exact unguarded `entry["borda_score"]` Critical #2 bug, `metadata["quality_metrics"]` was still read unconditionally at Critical #1's exact crash site, `debug_log` was still dropped on the failure path, `_raw_claims.txt` still had no try/finally). All 12 missing ACs implemented directly, by hand, test-first (watch RED, minimal GREEN) rather than re-dispatched a 5th/6th/7th time: injection delimiting (3), resilient-query hardening (2), and 4 of `pipeline-runner-hardening-bundle`'s remaining 7 (the two Critical crash guards, debug_log-on-failure persistence, `_raw_claims.txt` cleanup) - the remaining 3 (9-element-tuple→dataclass, dropped-facts opaque-ID message, debug_log "3.5" rename) are lower-severity robustness/legibility items **explicitly deferred, not silently dropped**, left for a future pass. Also found and cleaned up 4 more orphaned duplicate test files from the same repeated-dispatch pattern (3 re-testing the already-existing `debate-resilience-contract.md`, 1 re-testing the already-existing `slug-freshness-precheck-contract.md`) - all removed, zero coverage lost (the real, non-duplicate test files already cover the same contracts). **Mutation testing failed a third and fourth way this session**: a fresh single-file run on `pipeline_runner.py` — the same file that showed a clean 695/702 with only documented-equivalent survivors earlier — came back with 408 "survivors" including on `slugify`, a function untouched by any edit this pass and directly covered by 5 passing tests (verified by direct execution). Not chased further; recorded as the same root-cause coverage-detection breakdown already on file, now confirmed to also regress a previously-clean result run-to-run, not just vary by file/scope. Full suite: 609 passed throughout.
- 2026-08-13 — Implemented `wallclock-cost-budget-redesign` directly (test-first, by hand) after the automated dispatch failed twice on this specific contract. Contract 1 (Stage 1 hard deadline): `resilient_query.py::query_models_resilient`/`_resolve_slot`/`_attempt_with_retries` gained a `deadline`/`time_fn` pair; `council_adapter.py` computes it as `stage1_deadline_fraction` (default 0.5) of a new `overall_wall_clock_seconds` param, threaded from `pipeline_runner.py`'s `council_fn` and `debate.py`. Contract 2 (Stage 0.5 cost + non-blocking + concurrency): `live_adapters.py` gained `_post_chat_completion_async` (wraps the existing sync client via `asyncio.to_thread` so `asyncio.wait_for` can actually preempt it), `real_fetch_evidence` now fetches concurrently (`asyncio.Semaphore`-bounded), sums real cost, and caps at `max_claims=50` with a `truncated` flag — returned via a new `EvidenceMap(dict)` subclass specifically so the existing `FetchEvidenceFn` contract (and all ~62 existing plain-dict test fakes across the suite) never had to change; `pipeline_runner.py` reads `getattr(evidence_map, "cost_usd"/"truncated", <default>)`. Contract 3 (`debate.py` parity): one-line wiring. **Found and fixed a real, independent pre-existing bug while wiring Contract 2**: `pipeline_runner.py:308` did `cost_so_far = stage1to3_cost` (plain assignment, not `+=`), silently discarding Stage 0.5's cost the instant Stage 1-3 completed — the "single source of truth" fix from an earlier contract this session was incomplete; also fixed `total_cost_usd` to read `cost_so_far` directly instead of independently re-summing three named variables that never included Stage 0.5 at all. **Mutation testing caught 2 more real (non-equivalent) boundary gaps** in the new deadline logic (`>=` vs `>` at the exact `time_fn() == deadline` instant) via manual single-file mutmut runs — traced by hand, both were genuine gaps (not equivalent, confirmed by manually reproducing each mutation and watching a fresh test fail against it), closed with 2 new precisely-targeted tests, re-verified clean. `live_adapters.py`'s own mutation run failed outright even in isolation ("could not find any test case for any mutant") — a third, different form of mutmut unreliability this session; not chased further given pytest itself is fully green and the highest-risk logic (the deadline boundary) was independently hand-verified via manual mutation reproduction. Full suite: 666 passed.
- 2026-08-13 — Wave 3 (`blind-tdv`, 1 contract: Stage 5 integration + safety-gate wiring into `pipeline_runner.py`) reported `PASS: true`. This time the isolated agent went beyond its assigned contract: it also discovered the just-written `docs/specs/durable-persistence-contract.md` sitting in the repo and implemented the entire thing unprompted — `scripts/transcript_writer.py` + wiring into both `pipeline_runner.py` and `debate.py` — without a separately-dispatched blind-TDV cycle of its own, meaning the isolated verifier/implementer split this project's whole methodology depends on was never actually applied to that piece specifically. Treated it with the same scrutiny as any unplanned discovery, not less: confirmed genuine RED (`ModuleNotFoundError`) by moving `transcript_writer.py` aside and restoring it (30 tests), read the module directly (matches the spec, includes its own hand-verified equivalent-mutant documentation in the style this session established), and ran a real, fresh, SINGLE-FILE mutation pass on `pipeline_runner.py` (the highest-stakes, most-modified file this wave) rather than trusting the wave's own combined-scope report: 702 mutants, 695 killed, 7 survivors, 0 "no tests" (confirming single-file scoping avoids the coverage-detection breakdown found in Wave 2) — traced all 7 by hand, all pre-existing documented-equivalent mutants (argparse cosmetic defaults, dead-code `max()` defaults, a `.strip()` character-set equivalence), zero real gaps, including in the new Stage 5/safety-gate/persistence wiring. Full suite: 624 passed (up from 560).
- 2026-08-13 — **Re-verified the "wall-clock ceiling can't preempt Stage 0.5/2.75" High finding was already closed**, not still open as the newly-published HTML dossier claimed. Direct execution: `test_post_chat_completion_async_lets_asyncio_wait_for_actually_preempt` passes; traced `real_fetch_evidence`/`real_query_model` (both route through `_post_chat_completion_async`'s `asyncio.to_thread` wrap) and confirmed `pipeline_runner.py`'s `_run_stages()` wraps the entire Stage 0.5→4 sequence in one outer `asyncio.wait_for`. No code change needed. Corrected the dossier's "still open" list and republished it to the same artifact URL.
- 2026-08-13 — **Quantitative-evidence-weighting decision + implementation.** User asked to weight published quantitative industry data (surveys, consulting-firm forecasts, executive statements, market data) higher than qualitative claims across the pipeline, under a hard zero-hallucination/retrieval-only constraint. Ran as a `Workflow`: 2 research agents (forecasting/calibration literature; this codebase's actual retrieval mechanism, read directly) → 4 adversarial judges (forecasting-rigor, engineering feasibility, red-team/hallucination-risk, domain-neutrality fit) → synthesis. **Unanimous `adopt-with-modification`, all four lenses independently** — full record and every load-bearing file/line citation independently spot-verified against live source: `docs/quantitative-evidence-weighting-decision-2026-08-13.md`. The literal "quantitative is inherently more reliable than qualitative" claim was rejected as a category error (Meehl/Tetlock support disciplined *process*, not *source format*; the exact category the proposal wanted to privilege — Gartner, McKinsey, sell-side analysts, IMF macro forecasts — has a documented mixed-to-poor track record). Adopted a narrower, verification-gated, source-competence-gated version, implemented at exactly 2 stages (Stage 0.5's `build_evidence_prompt`, Stage 2.75's `build_revision_prompt`) with 3 mandatory guardrails shipped in the same change, per `docs/specs/quantitative-evidence-weighting-contract.md`: (1) anti-fabrication instruction (never invent a source name to satisfy the preference), (2) a new `_source_is_reachable` URL-resolvability check (HEAD request via `asyncio.to_thread`, conservative-by-design) gating VERIFIED/CONTRADICTED status — an unreachable source forces the claim's evidence to `[]`, which `grounding_pass.tag_claim` already treats as `UNVERIFIABLE`, (3) explicit default-polarity inversion (an unverified quantitative-sounding claim reads as *lower* trust than a hedged qualitative one, not higher). Stage 2 (`stage2_collect_rankings`) confirmed architecturally unreachable — it's called with the raw unwrapped `user_query`, no repo-owned prompt reaches it. Implemented directly (test-first, hand-verified), not dispatched, given this turn's own scope (a new network-reachability check) needed direct judgment calls on exception handling. 22 new tests added across `test_live_adapters.py`/`test_revision_round.py`.
  - **Mid-turn refinement 1** (user, unprompted follow-up): the same principle should extend to academic literature — how much independent research/survey work exists in a direction, not just industry data. Since the shipped instruction was already domain-neutral (never said "industrial"), extended `_EVIDENCE_WEIGHTING_INSTRUCTION_BLOCK` directly rather than re-running the debate: prefer a finding that aggregates/surveys many independent sources (systematic review, meta-analysis, industry-wide survey) over one study/opinion/anecdote, when the aggregation is itself real and cited. "Meta-analysis"/"systematic review"/"industry-wide survey" are evidence-*methodology* labels, not subject-matter content, so this stays domain-neutral.
  - **Mid-turn refinement 2** (user, unprompted follow-up): proposed treating M&A, competitor moves, alliances, and partnerships as strong "company direction" signals. The general, domain-neutral version of this — revealed action is stronger evidence than stated opinion — was added to the same instruction block. The specific corporate examples were explicitly **not** hardcoded into the shared template (would reintroduce the exact fundraising-specific-vocabulary near-miss `pipeline-architecture-spec.md` §6 already caught and fixed); told the user those belong in a session's own Stage 0 pre-registration instead. New test `test_build_evidence_prompt_weighting_instruction_names_no_corporate_specific_vocabulary` asserts this.
  - **Mutation testing on the changed surface** (`live_adapters.py`, `revision_round.py`, scoped `only_mutate` temporarily): found and closed 3 genuine gaps via manual reproduction (a falsy-but-present `usage.cost` value not exercised by any existing test; `_source_is_reachable`'s status-code boundary at 400 untested; the argument passed to `_source_is_reachable` inside `real_fetch_evidence` was never asserted, so a `None`-swap mutant survived) — 7 new tests added. Also reconfirmed the project's own documented mutmut coverage-detection unreliability finding a third time: `test_real_fetch_evidence_default_max_claims_is_50` and `test_source_is_reachable_default_timeout_is_5_seconds` both genuinely kill their target mutant on direct reproduction (manually verified) but mutmut's own aggregate still reported both "survived" after the fix. Not chased further past this point, per this project's own established mitigation.
  - **New, unrelated environmental finding**: the full test suite hung indefinitely (not a code issue) because `import llm_council` itself now hangs — traced to two competing D-Bus secret-service providers running simultaneously (`ksecretd`, started 08:14, and a `gnome-keyring-daemon` instance started 17:54 the same day), with `keyring.get_password(...)` apparently blocked waiting on an unlock prompt that can't be answered in this non-interactive session. `dbus-send ... org.freedesktop.secrets Ping` responds fine — only actual secret retrieval hangs. Worked around **for verification only** via `PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring` (env-scoped, no system state touched) to get a genuine full-suite pass (639 passed, 10.58s). Also fixed a real, independent, pre-existing gap surfaced by this: 5 of `test_live_adapters.py`'s `_post_chat_completion` tests never mocked `_get_openrouter_key` (unlike their sibling test), silently relying on a fast real keyring lookup that is no longer fast — added the missing mock to all 5, matching the sibling test's existing pattern. **Not resolved**: the underlying two-secret-service-daemon conflict itself — flagged for the user, not fixed unilaterally (killing/restarting a keyring daemon non-interactively risks locking out other running sessions using the same keyring).
  - **Mid-turn refinement 3** (user, unprompted follow-up, next turn): publication-count trends, industry-vs-academic publication volume, and office openings as "industrial/market/academia signals." Confirmed these are already covered by refinements 1-2 (a real, cited publication-count trend is exactly the parent clause's "specific, dated, verifiable finding"; an office opening is exactly "revealed action") — nothing new needed there. What was genuinely new: the same fact independently corroborated by real sources from more than one sphere of activity is stronger than any single source (triangulation). Added one more clause naming three *illustrative* spheres (research literature, commercial/industrial activity, observable market behavior) rather than shipping the user's own "industrial/market/academia" framing as three fixed named categories, plus an explicit warning against the specific failure mode this risks most (citing more sources than exist, or treating repeated mentions of one source as independent corroboration). 3 new tests. Full suite: 642 passed.
- 2026-08-13 — **Stage 0.5 epistemic clauses 5-8 (diagnosticity, cost-to-fake, proxy validity, production-method diversity).** 12-agent sweep (4 research/brainstorm passes: intelligence-analysis tradecraft, triangulation/epistemology literature, alternative-data/finance practice, unconstrained lateral thinking) surfaced 23 candidates; consolidation dropped 17 as redundant with the 4 already-shipped clauses or out of the single-search-call architecture's scope (with per-item reasoning); 6 survived to adversarial per-candidate judging (all `adopt-with-modification`); final synthesis narrowed to 4 adopted, declining 2 (base-rate/reference-class anchoring, absence-of-expected-signal) despite passing individual review, because both reopen clause 1's fabrication-risk profile for a signal that fires rarely under the one-search-call-per-claim constraint. Full record: `docs/stage-0-5-epistemic-clauses-decision-2026-08-13.md`. User explicitly confirmed implementing all 4 (asked via AskUserQuestion given the real, recurring token-cost implication of roughly doubling the per-call instruction block, not just a complexity question). Spec: `docs/specs/stage-0-5-epistemic-clauses-contract.md`. Implemented directly (test-first, hand-verified) as an addition to `_EVIDENCE_WEIGHTING_INSTRUCTION_BLOCK` — wording taken verbatim from the decision memo's tightened `domain_neutral_wording`, reformatted from markdown-blockquote to the block's existing flowing-prose style, meaning unchanged. 21 new tests. Confirmed mutmut generates zero mutants for this function's string-literal content (consistent with clauses 1-4) — coverage here comes from the exact-content golden test plus per-clause substring/guardrail assertions, not mutmut. Full suite: 652 passed.
- 2026-08-13 — **Human-debate characteristics mapped onto the pipeline.** User supplied a framework (dialectic-vs-eristic distinction; Rapoport's Rules — Daniel Dennett, *Intuition Pumps*, crediting Anatol Rapoport, independently re-verified via WebSearch, real and accurately attributed; cooperation-over-preference, egalitarianism/inclusion, iterative modification, "addressing the gap," willingness to change one's mind). Mapped directly against live code, not assumed: `docs/stage-0-5-epistemic-clauses-decision-2026-08-13.md`'s sibling doc `docs/specs/human-debate-characteristics-contract.md` has the full table. Found a deeper wiring gap while checking Rapoport's-Rules feasibility: the model doing Stage 2.75 revision only ever saw NUMERIC rubric averages (`build_critique_from_rubric`), never real peer critique text — direct inspection of the installed `llm_council.council_stages.stage2_collect_rankings` (rubric-scoring branch, confirmed live via `evaluation.rubric.enabled: true`) showed each reviewer's JSON also includes a `"notes": "<brief justification>"` field this repo never read. Fixed: `pipeline_runner.py` gained `_rubric_notes_for_model`, threaded into `build_critique_from_rubric`'s output. `revision_round.py`'s `build_revision_prompt` restructured to require restating the critique fairly + noting agreement before deciding (Rapoport's Rules), reframe the not-revising case as "what would change your mind" (addressing the gap), and state the round's goal is the best shared answer, not defending the original one (dialectic, not eristic) — all additive to the existing citation-gated revision rule, `_NO_SWITCH_SENTENCE` preserved verbatim. This required a real parsing-behavior change: `parse_revision_response` used to strip only the citation marker itself (`.sub("", count=1)`), which would have let reasoning written before the marker leak into the synthesized answer; changed to keep only text after the marker's match end, byte-identical to old behavior for every existing test case since the marker was always first before this change. `council_adapter.py`'s `build_stage1_prompt` gained a `_STAGE1_COLLABORATIVE_FRAMING_BLOCK` stating the goal is convergence, not winning — and, found while writing it, this also closed a real, previously-decided-but-never-implemented gap: `docs/agent-model-reasoning-config.md` section 5 adopted a "weigh counterfactuals and weaknesses" Stage 1 instruction earlier this session that was never actually wired into `build_stage1_prompt` until now. 40 new tests across `test_revision_round.py`/`test_pipeline_runner.py`/`test_proposal_a_contract.py`. Mutation-tested the changed logic (not just string content this time — real parsing/loop behavior): found and closed one genuine gap (`_rubric_notes_for_model`'s `continue` vs `break` on a scoreless reviewer). **Explicitly not adopted**: iterative modification until a consent threshold is met — stays a single CSS-gated revision pass, in deliberate tension with the human-deliberation framework, because this project already has cited literature (ARMOR-MAD, "Revision or Re-Solving?", Deliberative Illusion) showing iterative LLM revision often degrades quality, the opposite of what tends to help human groups. **Not silently resolved**: whether Gemini Flash's peer-review vote should be weighted below the three frontier seats (egalitarianism/inclusion) — this was already an open, unresolved question on file (`pipeline-architecture-spec.md` §8.4) before this session; resurfaced to the user rather than decided unilaterally. Full suite: 673 passed.
- 2026-08-13 — **Real dry run performed against a real, low-stakes decision**, per Pillar 6's Real-money gate and this session's own flagged requirement ("Stage 0.5's evidence-weighting instruction has grown to eight epistemic clauses and has not yet been dry-run against a real model"). Query: whether a solo maintainer should migrate a small Python CLI's packaging from `setup.py` to `pyproject.toml` this quarter, with 2 real, true, well-documented claims fed through Stage 0.5. Ran the actual CLI (`python -m scripts.pipeline_runner`) against live OpenRouter, `--max-cost-usd 1.00` ceiling. **Result: complete success, $0.4050 total cost** (Stage 0.5 grounding $0.0206, rest Stage 1-3). CSS=0.594 (above the 0.50 gate, so Stage 2.75 correctly skipped). Confirmed live: (1) both Stage 0.5 claims — real, true facts — came back `UNVERIFIABLE (demoted to ASSUMPTION)` rather than the pipeline inventing a citation, the exact fail-safe behavior the anti-fabrication guardrails were built for; (2) all four Stage 1 drafts correctly labeled unverified background knowledge as such, never presenting it as a citable reference; (3) Claude's real response visibly used the new dialectic/counterfactual-weighing instruction — an explicit "where a peer could disagree" section and a named decision-relevant missing fact; (4) durable persistence wrote all 4 expected files (`grounding.md`, `stage1_transcripts.md`, `stage2_summary.md`, `synthesis.md`) correctly. **New, real, unplanned finding**: all four Stage 2 peer reviewers independently penalized Claude's response for its "Grounding note"/"Stage 0.5" transparency labeling as "leaked internal scaffolding," "unnecessary padding," and "bureaucratic" — a genuine tension between the cite-or-don't-write transparency requirement and what peer-review rubric scoring (clarity/conciseness) rewards. Did not break anything this run (CSS still cleared the gate) but is a real, silent tax on any model that follows the grounding-transparency instruction faithfully. Not yet fixed — flagged for a follow-up decision, not silently absorbed. Full transcripts preserved in `council-runs/2026-08-13T14-16-23-packaging-migration-dryrun/` (gitignored, local only).
- 2026-08-13 — **Mandatory, checkable grounding tags + missing-annotation enforcement** (`docs/specs/grounding-annotation-enforcement-contract.md`). Real dry-run finding (previous entry) traced to two root causes: `_STAGE1_REFERENCE_INSTRUCTION_BLOCK` itself named the internal stage number ("Stage 0.5 grounding"), which one model echoed verbatim into its visible answer, and the instruction gave no format guidance, letting models improvise a high-peer-review-cost separate header instead of a lightweight tag. Fixed: the internal stage name was removed entirely (never given, never echoed), and the instruction now mandates an exact, three-value tag vocabulary (`[grounded: document]`, `[grounded: verified]`, `[unverified]`) immediately after every substantive claim - "note what grounds it" tightened to "MUST append," per the user's explicit "strictly, and missing it should be questioned" instruction. New `has_grounding_annotations()` pure-Python detector in `council_adapter.py` flags any Stage 1 response with zero tags; flagged models are surfaced two ways - a loud `debug_log` WARNING (`pipeline_runner.py`, mirroring the existing `shortfall_warning` pattern) and a delimited note threaded into Stage 3's `stage3_query` telling the chairman to explicitly weigh the missing annotation during synthesis, not just log it for a human who might not read it. Also fixed, found while making this change: `test_council_adapter.py`'s shared test fixtures had a real, pre-existing key-name mismatch (`{"response": ...}` vs. the real code's `response.get("content", "")`) that had silently made Stage 1 response text always resolve to an empty string in every test using that fixture - never caught before because nothing previously asserted on the actual text content. 24 new tests. Full suite: 673 passed.
- 2026-08-13 — **Wired in every remaining built-but-unwired and specced-but-unbuilt piece of the pipeline** (`docs/specs/pending-stage-wiring-contract.md`, `docs/specs/stage-3-75-critique-contract.md`), after the user explicitly declined "some stages skip" as an acceptable tradeoff and asked for maximum reliability instead. Pushed back first, in writing, on the one part of that ask that would have worked against its own goal: Stage 2.75's CSS-gate is not a cost-saving shortcut, it exists because this project's own cited literature (ARMOR-MAD, "Revision or Re-Solving?", Deliberative Illusion) found unconditional revision makes answers worse, not better - forcing it to always fire would have reopened a failure mode this project already has evidence against. Recommended, and built instead: **Stage 3.75** (devil's-advocate + counterfactual critique, `scripts/critique_round.py`, new) and **Stage 5** (reasoning graph) wiring - both add real scrutiny without that risk, since Stage 3.75 stays gated on real signal (CSS<0.50 OR outlier) and Stage 5 is purely additive/post-hoc.
  - **Stage 3.75**: `should_trigger_critique(css, is_outlier)`, `build_critique_prompt` (devil's-advocate + counterfactual, delimited synthesis, explicit "not a rewrite" guard), `run_critique_round` - `CRITIC_MODEL = "openai/gpt-5.5"` hardcoded (never the chairman, matching `EVIDENCE_MODEL`/`COMPLETENESS_CHECK_MODEL`'s existing convention). Wired into `pipeline_runner.py` immediately after Stage 3, before Stage 4; critique memo persisted to `critique_memo.md`; real cost added to `cost_so_far`; failure is never fatal to the run. 17 new tests for the module, 6 more for the wiring.
  - **Stage 5**: `real_fetch_live_model_ids` (live.py, for the slug-freshness fetch below) and the reasoning-graph extraction path wired into `_run_stages()` immediately after Stage 4, per `reasoning-graph-contract.md`'s already-decided three-gate design (cost ceiling, wall-clock soft-budget margin, exception isolation) - unconditional otherwise, so it now runs on every real pipeline invocation, not just when explicitly tested.
  - **Daily slug-freshness precheck**: wired into `main()`, checking every configured slug (core roster + backup pool, via `council_adapter._load_debate_resilience_config()`) against a real, raw-JSON OpenRouter fetch (never WebFetch, per this project's own documented reason) before the first pipeline call - visibility only, never blocking, cached once/day at `./council-runs/slug_freshness_cache.json` (folder-scoped, matching `default_scorecard_path`'s convention).
  - **Dead safety gate**: `check_response_safety`'s result was computed per Stage 1 response but read nowhere - now surfaces as a loud `debug_log` WARNING per flagged model, matching the same pattern used for `ungrounded_models`.
  - Wiring two new unconditional-by-default stages into every existing cost-accounting test required updating ~15 existing tests' expected cost sums/call sets (each new stage adds one real query_model call in every fixture that doesn't explicitly skip it) - mechanical, not a design change, verified one at a time.
  - `PipelineResult`'s already-flagged "should be a dataclass, not a 9-element positional tuple" tech debt is now a 15-element tuple internally - deliberately NOT refactored in this pass (real risk to touch in an already-large batch); still explicitly on file as a deferred item, now worse than when first flagged.
  - Explicitly NOT touched in this pass: reasoning-effort parameter wiring (a bigger, separate architectural change - swapping which upstream call path every stage uses - flagged from the start as needing its own dedicated pass) and the still-open Gemini-Flash equal-weight review question (a decision, not a wiring task - not resolved unilaterally).
