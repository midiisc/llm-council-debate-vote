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

## Check log
- 2026-08-09 — initial grounding pass (2 parallel research checks: package/CLI/config verification, competitive tool survey). Populated this ledger for the first time.
- 2026-08-09 — MCP registration + live `council_health_check` execution caught 2 further live bugs beyond the initial grounding pass: wrong stdio entrypoint in the doc's registration command, and the `load_config()` council-nesting bug above. Both confirmed by direct execution, not just source reading — reinforces that even grounded source-reading isn't a substitute for actually running the thing.
