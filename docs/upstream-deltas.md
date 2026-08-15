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

**2026-08-14: Stage 3 chairman identity leak — found during a full-pipeline
bias/isolation audit, locally fixable, fix in progress.** Direct source
read of the installed `llm-council-core==0.40.1` package
(`llm_council/council_stages.py::stage3_synthesize_final`, lines 904-922)
shows the chairman-synthesis prompt is built with **full, real model
identity** — `f"Model: {result['model']}\nResponse: ..."` for every Stage 1
draft, `f"Model: {result['model']}\nRanking: ..."` for every Stage 2
reviewer, and real model slugs in the aggregate-rankings block. This
repo's own `scripts/council_adapter.py::_stage3_query_fn` calls that
function directly (confirmed by grep, no wrapping/anonymization applied),
inheriting the leak unmodified. Unlike the shuffle-order limitation above,
this one is **not** a vendor-internals problem needing an upstream fix —
Stage 2 already proves this repo can locally reproduce a package prompt-
builder with anonymization applied (`_build_stage2_real_ranking_prompt`,
same "reproduce, not vendor" pattern this module's own docstring
documents), so the same technique applies to Stage 3. Real, not
hypothetical: since **Claude Opus 4.8 is both a Stage 1 drafter and the
chairman**, it can recognize and potentially favor its own earlier draft
during synthesis — exactly the self-preference/brand-halo bias class
Stage 2's anonymization exists to prevent, left open one stage later.
Fix: `docs/specs/stage3-chairman-anonymization-contract.md` — anonymize
Stage 1/Stage 2/aggregate-rankings identity in the copy passed to the
chairman using the same `Response A/B/C` labels Stage 2 already assigns,
then resolve labels back to real model names in the synthesis text before
it becomes the human-facing answer (never expose a raw internal label to
a human reader).

**Fixed, 2026-08-14.** Three pure functions landed in
`scripts/council_adapter.py` via this repo's standard blind-TDV process
(isolated `ws-verifier` authored 23 ACs from the contract only, `ws-builder`
implemented blind in a worktree, watch-RED → GREEN → mutation gate):
`_build_stage3_identity_map` (inverts Stage 2's `label_to_model` and mints a
fresh continuing label for any Stage-2-only reviewer with no Stage 1 draft
of its own), `_anonymize_for_stage3` (returns anonymized copies of
`stage1_results`/`stage2_results`/`aggregate_rankings` with only `"model"`
swapped for its label, never mutating the real-named originals), and
`_resolve_response_labels` (reverse substitution, longest-label-first, so
the final synthesis text is never left with a raw internal label). Blind-TDV
mutation gate: 760/781 mutants killed, 0 real survivors (21 pre-existing
equivalents, documented inline). Wired into `_stage3_query_fn` immediately
after landing: `stage3_synthesize_final` now receives the anonymized copies
instead of the real-named lists, and the returned synthesis text is resolved
back to real model names before becoming `stage3_result["response"]` — the
value written to `synthesis.md` and returned to the caller. Chairman
identity/cost/usage accounting is unaffected (`_get_chairman_model()` never
reads these lists). A follow-up scoped mutmut pass on the wiring itself
(811 mutants, `only_mutate` temporarily narrowed to `council_adapter.py`
alone, then restored) found 2 new genuine equivalents beyond the blind-TDV
gate's own — both documented inline with the same "verified by direct
execution, traced by hand" convention as this file's other equivalents:
`stage1_results` is an unused parameter in the `_build_stage3_identity_map`
call site (every Stage 1 drafter's label is already fully recoverable from
`label_to_model`, kept in the signature only for shape-symmetry with
`_anonymize_for_stage3`), and `.get("response", "")`'s default at the
label-resolution call site is unreachable dead code (the same
always-present-key invariant already established for every other
`.get("response", ...)` site in this function). Three existing tests that
pinned the old leaky behavior as correct were updated to assert the new
anonymized-copy behavior instead — not silenced —
(`tests/test_council_adapter.py::test_ac_comprehensive_normal_path_exact_field_values`,
`tests/test_council_adapter.py::test_single_model_branch_degraded_mode_and_untouched_stage1_5_stage2_usage`,
`tests/test_council_adapter_resilient_stage1.py::test_stage3_synthesize_final_receives_correct_aggregate_rankings`).
Full suite: 781 passed.

**2026-08-14 follow-up: label anonymization alone left a stylistic-
fingerprint side channel open, found and fixed same day.** User question
("what about the response having a model's identity traces, and a model
able to understand this — is this true?") prompted a re-check of exactly
what text the chairman reads, not just how it's labeled. Direct read of
the wiring found `_anonymize_for_stage3` was being called with the raw
`stage1_results` (each draft's original, un-normalized text) rather than
`responses_for_review` (the output of Stage 1.5's `stage1_5_normalize_styles`,
which exists specifically "to reduce stylistic fingerprinting... making it
harder for reviewers to identify which model produced each response" —
`llm_council/council_stages.py::stage1_5_normalize_styles` docstring,
`style_normalization: true` in `llm_council.yaml`). Stage 2 reviewers
already read the normalized text; Stage 3's chairman did not — so even
after the label fix above, the chairman could still infer authorship from
each draft's un-scrubbed prose style (word choice, formatting habits,
hedging patterns), the same identity-adjacent bias channel Stage 1.5
exists to close for Stage 2. Confirmed real by writing a regression test
with a style-transforming fake (an identity-passthrough fake, which is
what every existing test used, can't distinguish "chairman sees raw text"
from "chairman sees normalized text" since they're equal in those fixtures)
and watching it fail (RED) against the pre-fix code.
**Fixed** by introducing `stage1_for_stage3` in `run_council_with_timeouts`:
`responses_for_review` when Stage 1.5 ran (num_responses >= 2), the raw
`stage1_results` only in single-model degraded mode (Stage 1.5 never runs
there — nothing to normalize against with no peer review). Regression test:
`tests/test_council_adapter_resilient_stage1.py::test_stage3_receives_style_normalized_stage1_text_not_raw_draft`.
Scoped mutmut re-run on `council_adapter.py` (813 mutants): 788 killed, 25
survived, all 25 re-confirmed as the same already-documented equivalents
(renumbered by the 2 added lines) — 0 new real gaps. Full suite: 782 passed.

**Correction, 2026-08-14 (same day): the "not fixed" note above was wrong —
fixed, on user follow-up ("check and correct").** Re-examined the cost/
quality tradeoff originally cited for not fixing this and found it didn't
actually hold: normalization only ever applies to the COPY built for
Stage 3's chairman prompt (matching `stage1_for_stage3`'s own pattern
above) — the real `stage2_results` used earlier for
`parse_ranking_from_text`/`calculate_aggregate_rankings`, and returned to
the caller for the human-facing transcript, is never touched. So "risks
flattening the reasoning a human wants to audit" was a non-issue (the
human-facing copy is unaffected); the only real cost is the extra model
call itself. Fixed by adding `_normalize_stage2_for_stage3` (new function,
`scripts/council_adapter.py`): reuses the exact same `stage1_5_normalize_
styles` call Stage 1 drafts already go through (same config gate, same
`normalizer_model`) by mapping Stage 2's `"ranking"` field to `"response"`
and back — no second mechanism invented. Wired in alongside
`stage1_for_stage3`, feeding `stage2_for_stage3` (not raw `stage2_results`)
into `_anonymize_for_stage3`. Cost tracked in a new `total_usage["stage2_
normalize"]` bucket, summed into `metadata["usage"]["total"]["cost_usd"]`
like every other bucket — visible to the Real-Money-gate cost ceiling, not
a hidden spend. Verified RED→GREEN with a genuinely style-transforming
fake (an identity-passthrough fake — what most existing tests use — can't
tell "normalized" from "raw" since they're equal either way): confirmed
the un-fixed code failed the new test, then confirmed the fix passes it.
**A real bug surfaced by running the full suite, not caught by reasoning
alone:** the first implementation called `stage1_5_normalize_styles`
unconditionally, including for `stage2_results = []` (single-model
degraded mode) — that vendor function reads its `style_normalization`
config setting *before* looking at its input list, and some test config
doubles for that code path don't define the attribute at all (it was never
touched there before), so 3 single-model-branch tests crashed with
`AttributeError`. Fixed with an explicit empty-input early return.
Scoped mutmut (855 mutants) caught a second real gap on the first pass:
the empty-input return's exact usage-dict shape/values were completely
untested (9 survivors) — a wrong key or nonzero value there would have
silently corrupted `metadata["usage"]["total"]` for every single-model run.
Added a direct unit test pinning the exact `([], {zeroed usage})` return.
Re-run: 828/855 killed, 27 survived, all pre-existing or newly-confirmed
equivalents (documented inline, including the `.get(..., r["ranking"])`
fallback — provably unreachable since `stage1_5_normalize_styles`
guarantees one output entry per input, preserving `"model"`). 0 real
gaps. Full suite: 785 passed.

**2026-08-14, same day: `normalizer_model` pinned — grounded research +
a real breakage found along the way.** Background research (WebSearch/
WebFetch, this session) on 2025-2026 LLM-debate anonymization literature
surfaced a residual-fingerprinting quantification directly relevant to the
"which model does the rewriting" question §5's "inherit the package
default" reasoning (pipeline-architecture-spec.md) never actually
answered: **"Voice Under Revision"** (Tom van Nuenen, arXiv:2604.22142v1,
submitted April 2026, cs.CL) rewrote 300 personal narratives through three
frontier LLMs under three prompt conditions and measured re-identification
via nearest-neighbor stylometric matching (character n-gram + word-length
features, PCA-projected) against the 300 source texts. Exact figures,
verified by fetching the paper's HTML full text directly (not taken from
a secondary summary): **GPT-5.4 (`gpt5.42026-02`): 14.3% matched; Claude
Sonnet 4.6 (`claudesonnet4.620260215`): 1.7%; Gemini 3.1 Pro
(`gemini3.1pro`): 1.0%** — against a 0.3% chance baseline. Gemini was the
clear best performer, GPT the clear worst, by an order of magnitude.
Caveats the paper itself states, not glossed over: single English-language
corpus, short texts (244 words avg., well below the 2,500-5,000 words
traditional authorship-attribution work recommends), no cross-genre/
cross-lingual claim, and the paper cannot attribute the effect to
pretraining data vs. instruction-tuning vs. alignment vs. system defaults.
Not the same task as this pipeline's own use (technical/analytical debate
responses, not personal narratives) — treated as directionally relevant
evidence, not a direct transfer.

**Real breakage found while checking whether the tested models are even
available today:** live OpenRouter fetch (2026-08-14) confirms **no
Gemini Pro-tier text model exists at all** (matches the already-recorded
Gap 1 in pipeline-architecture-spec.md §4) and **no Claude Sonnet 4.x
model exists** (superseded by `anthropic/claude-sonnet-5`, a different,
untested generation) — neither the paper's best nor second-best exact
model can be used. Separately, and more urgently: `google/gemini-3.1-
flash-lite-preview`, the model `normalizer_model` was silently inheriting
as the package's hardcoded default (confirmed by reading `unified_config.py`
directly), **is also dead on live OpenRouter** — leaving this field unset
was not a neutral "defer the decision" choice, it meant Stage 1.5 (and the
new Stage 2 commentary-normalization extension above) would fail outright
the next time either actually ran against a real query.

**Decision:** pinned `normalizer_model: google/gemini-3.6-flash` in
`llm_council.yaml` — same vendor/family as the paper's single best-evidenced
performer, and already this pipeline's own proven, live-verified 3rd core
council seat (Resource & Stability Gate: proven integration over an
untested one). Considered and rejected: `google/gemini-3.7-flash` (newer,
cheaper, but zero operational track record in this project — the paper
gives no reason to prefer it over 3.6 specifically), `google/gemini-3.5-
flash-lite` (cheapest, but a lower capability tier for a task that needs to
genuinely rewrite style while preserving content, not just cheaply
paraphrase). Not claimed to replicate the paper's 1.0% figure — that
number is Pro-tier-specific and on a different task; this is a reasoned
choice under real, disclosed uncertainty, not a re-run of the study.
Verified by direct execution: `load_config(Path("llm_council.yaml"))`
resolves `cfg.council.normalizer_model == "google/gemini-3.6-flash"`,
every other field unchanged. Full suite: 785 passed (no test asserts on
this specific config value, so none needed updating).

**Real-money gate (Pillar 6): satisfied, 2026-08-14, same session.** Ran
a real, live dry run against a low-stakes test decision ("should a solo
developer building a new CLI with zero users prioritize tests or docs
first before release?") via `python3 -m scripts.pipeline_runner
--topic-label dry-run-normalizer-verification --query "..." --max-cost-usd
1.00`. **Complete success, zero warnings/errors.** Cost & Tokens summary:
**total cost $0.3551**, CSS=0.752 (above the 0.50 gate, so Stage 2.75/3.75
both correctly skipped — this run didn't happen to exercise the new Stage
2 commentary-normalization path's cost inside the full pipeline). Stage 3
synthesis correctly displayed real model names (`anthropic/claude-opus-4.8`,
`openai/gpt-5.5`, `google/gemini-3.6-flash`, `z-ai/glm-5.2`) in the
human-facing output — direct live confirmation that `_resolve_response_
labels` genuinely restores identity for the human reader after the
chairman's own anonymized reasoning pass, not just in tests. Stage 5
extraction failed gracefully again (`malformed_extraction_response`, same
pre-existing, already-tracked minor gap as the 2026-08-13 dry run — not a
regression, not urgent). Output: `council-runs/2026-08-14T01-41-00-dry-
run-normalizer-verification/` (gitignored, local only).

**Separately, directly verified the new normalizer path itself** (the
full pipeline run's CSS didn't happen to trigger it) — called
`_normalize_stage2_for_stage3` directly against one real synthetic
reviewer critique. Confirmed: real live call to `google/gemini-3.6-flash`,
genuine subtle style rewrite ("is clearly the strongest here — I would
rank it first" → "is the strongest option and should be ranked first"),
real tracked cost (`cost_usd: 0.002853`, `cost_known: True`, 876 total
tokens). `normalizer_model` pin confirmed working end-to-end against the
live gateway, not just against `load_config()`'s parse step.

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
- 2026-08-13 — **Two roster/design questions resolved by explicit user decision, not code**: (1) Gemini 3.6 Flash's peer-review vote stays an equal vote, not weighted down - egalitarianism/inclusion was already an explicit design value this session (human-debate-characteristics mapping), and no evidence exists that a cheaper-tier model judges already-written text worse. (2) Live OpenRouter catalog check (`/api/v1/models`, fetched fresh) found the only "pro"-tier Gemini model available today is `google/gemini-3.1-pro-preview` - generation 3.1, actually OLDER than the `gemini-3.6-flash` already in the roster (no `gemini-3.6-pro` exists yet), ~13x the prompt price ($2.00 vs $0.15/M) and ~1.6x completion price ($12.00 vs $7.50/M) for the same 1M context. Decision: keep `gemini-3.6-flash`, do not swap to the older/pricier preview tier. Both decisions presented to the user with the live numbers, not decided unilaterally.
- 2026-08-13 — **Reasoning-effort wiring for Stage 2.75/3.75/4** (`docs/specs/reasoning-effort-wiring-contract.md`), the item explicitly deferred in the entry above. Fresh grounding this pass corrected an assumption in `docs/agent-model-reasoning-config.md`'s original mechanism-gap note: the nested `reasoning: {effort, max_tokens, exclude}` object has `effort`/`max_tokens` as **mutually exclusive** (live-fetched from `openrouter.ai/docs/api-reference/parameters`), with per-provider support differing (Anthropic: `max_tokens` only, min 1024/max 128000; OpenAI/Google Gemini 3: `effort`) - the installed package's own `ReasoningParams` dataclass always sends all three fields together, which would violate this for any Anthropic call. Sidestepped by using OpenRouter's separate, simpler top-level `reasoning_effort` string field instead, confirmed via a live `/api/v1/models` fetch to be in every one of this project's 4 core models' own `supported_parameters` (`anthropic/claude-opus-4.8`, `openai/gpt-5.5`, `google/gemini-3.6-flash`, `z-ai/glm-5.2`). Also found and NOT used for this reason: the installed package's `llm_council.metadata.get_provider().supports_reasoning(model)` check is stale, reporting `False` for `gpt-5.5`/`gemini-3.6-flash` against the live catalog's own contradicting evidence.
  - `scripts/live_adapters.py`: `_post_chat_completion`/`_post_chat_completion_async`/`real_query_model` gained an additive `reasoning_effort: Optional[str] = None` parameter - `None` (the default) is byte-identical to pre-contract behavior, a non-`None` value adds a top-level `"reasoning_effort"` request-body key.
  - `scripts/pipeline_runner.py`: `run_pipeline` gained an additive `query_model_with_effort: Optional[ReasoningQueryModelFn] = None` parameter (`(model, prompt, effort) -> (text, cost)`); `None` (every existing call site/test) leaves Stage 2.75/3.75/4 calling plain `query_model` exactly as before. When supplied, a local `_query_model_for_effort(effort)` closure routes Stage 2.75 revision and Stage 3.75 critique through `"high"`, Stage 4 completeness through `"low"`. Stage 5's reasoning-graph extraction stays on plain `query_model` - explicitly out of scope (wasn't in the original per-stage effort table). `main()` wires a real closure that calls `real_query_model(model, prompt, reasoning_effort=effort)`, so a live CLI run actually sends the effort-tagged requests end to end.
  - Scoped mutmut pass (`live_adapters.py` + `pipeline_runner.py`, 1301 mutants): found and fixed 22 real gaps, all in the new code - `_post_chat_completion_async`/`real_query_model`'s new tests only checked `reasoning_effort` and missed `model`/`prompt`/`max_tokens`/`max_retries`/cost-fallback passthrough (12 gaps); `_query_model_for_effort`'s prompt content and `main()`'s `_query_model_with_effort` closure forwarding were asserted on identity/presence but never on the actual values reaching `real_query_model` (10 gaps). All closed with tests that assert on real captured values, not just "was called." Full suite: 731 passed.
  - Stage 1/Stage 2/Stage 3 remain unwired for the reasons already on file in `agent-model-reasoning-config.md` (Stage 1 needs a call-path swap requiring its own blind-TDV pass; Stage 2/3 have no `reasoning_params` kwarg in the installed package at all - upstream limitation, not this repo's to fix).
- 2026-08-13 — **Real dry run against the reasoning-effort/Stage-3.75/Stage-5 wiring**, per Pillar 6 (a live behavioral change to the request shape of 3 stages triggers the Real-money gate same as a model-pool change). Query: whether a solo maintainer should migrate a small Python CLI's packaging from setup.py/requirements.txt to a PEP 621 `pyproject.toml` this quarter, 2 real claims (pip 21.3 PEP 621 support, uv's pyproject.toml-native design) through Stage 0.5. `--max-cost-usd 1.00`, real OpenRouter. **Result: complete success, $0.4428 total cost, zero crashes.** Stage 0.5 correctly demoted both claims to `UNVERIFIABLE (demoted to ASSUMPTION)` (no fabricated evidence). CSS=0.682 (above the 0.50 gate) - Stage 2.75 correctly skipped. **Stage 3.75 fired for real, via the outlier clause, not the CSS clause** (CSS was above threshold, so at least one model was flagged `is_outlier`) - the first live confirmation that the outlier gate catches a case CSS alone misses, exactly the scenario it was designed for; produced a real, substantive devil's-advocate critique, persisted to `critique_memo.md`. Stage 4 correctly skipped (no verified facts, since both claims were unverifiable). **Stage 5 attempted extraction and failed gracefully** (`malformed_extraction_response`) - never crashed the run, debug_log recorded why, exactly the exception-isolation behavior the contract requires; a real, if minor, finding that the extraction prompt/parse path needs a follow-up look, not urgent since the stage is purely additive. Grounding-tag enforcement confirmed live: 49 `[unverified]` tags across the 4 Stage 1 drafts, **zero occurrences of "Stage 0.5"/"grounding note" leaking into any draft** (direct grep against the real transcript, not just belief the fix worked). Safety-gate and slug-freshness precheck produced no warnings (clean roster, no flagged model). Transcripts preserved in `council-runs/2026-08-13T15-42-38-pyproject-migration-effort-check/` (gitignored, local only).
- 2026-08-13 — **Checked whether Google AI Studio (direct, non-OpenRouter) has a Gemini tier OpenRouter is missing**, after the user pasted a live AI Studio API key into chat (flagged immediately as compromised - never stored, never used; user advised to rotate it). Live-fetched `ai.google.dev/gemini-api/docs/models` directly (Google's own model list, not OpenRouter's mirror): confirmed no `gemini-3.6-pro` exists anywhere, even natively - `gemini-3.1-pro-preview` is Google's own highest Gemini-3-generation tier today, the same one already live on OpenRouter. No capability gap found; the earlier "keep `gemini-3.6-flash`, don't swap" decision stands. Direct-to-Google integration (bypassing OpenRouter, a second gateway/key-storage path) was not pursued - no grounded reason to, and it would be a real architecture change needing its own spec, not a config edit.
- 2026-08-14 — **Root-caused a live "deepseek timing out" report to an MCP server registration bug, not a debate-engine bug; separately ran an expert panel on the deeper resilience question.**
  - **MCP registration root cause, confirmed by direct process inspection, not guessed:** `llm-council` was registered at Claude Code **user scope** (one global server binary for every project). The package's own `_find_config_file()` (`unified_config.py`) resolves config relative to the server process's cwd at start time (`LLM_COUNCIL_CONFIG` env var → `./llm_council.yaml` → `~/.config/llm-council/llm_council.yaml` → hardcoded `UnifiedConfig()` defaults). Found **two other running `llm_council.mcp_server` processes** on the machine: one (pid 1559792, started 2026-08-13 19:25) had cwd `~/Documents/Xspecies_Tech_deck_revenue_2_produicts_Ravi`, a directory with no `llm_council.yaml` of its own - so it silently fell through to the package's hardcoded defaults, which are exactly `openai/gpt-5.4`, `google/gemini-3.1-pro-preview` (both confirmed dead OpenRouter slugs, this ledger's earlier entries), `anthropic/claude-opus-4.8`, and `deepseek/deepseek-v4-pro` - the same default list the 2026-08-09 config-nesting bug already caught once. This project's own live `council_health_check` was confirmed clean throughout (`council_size: 4`, no deepseek) - the leak was specific to that other process, not this project's config.
  - **Fixed:** re-registered `llm-council` at **local scope** for this project (`claude mcp add llm-council -s local -e LLM_COUNCIL_CONFIG=/data/llm-council-debate-vote/llm_council.yaml -- <tool-venv>/bin/python -m llm_council.mcp_server`) - local-scope entries take precedence over the user-scope one for this project directory, and the explicit `LLM_COUNCIL_CONFIG` makes resolution deterministic regardless of ambient cwd. Verified via `claude mcp get llm-council` (shows the pinned env var, `Connected`) and a fresh `council_health_check` call (still clean). The stale wrong-cwd process (pid 1559792) was killed - that other project's session will respawn its own server on next use, unaffected by this project's local-scope fix (still exposed to the same cwd-dependent default-fallback risk if it doesn't get its own `llm_council.yaml` or pinned registration - out of this project's scope to fix, flagged to the user).
  - **`MCP_TOOL_TIMEOUT` scoping (panel fix-on-sight finding):** the 2026-08-12 bump (60000→900000ms) was applied machine-wide in `~/.bashrc`, affecting every project's MCP tool-call budget, not just this one. Added a project-local, gitignored override (`.claude/settings.local.json`'s `env` block, same values) so this project is self-contained regardless of the global default. The global `~/.bashrc` bump itself was **not** reverted - that's a cross-project decision outside this repo's scope, left to the user.
  - **Expert panel** (`ws-os`/`ws-builder`/`ws-agentic`/`ws-warden`/`ws-redteam`/`ws-privacy`/`ws-operator`/`ws-backend`/`ws-scientist`, run via the `expert-panel` workflow) on the deeper "how to stop this recurring" question, converged unanimous, no blocks:
    - **Real, confirmed gap:** Stage 1 has full retry+backup+shortfall-warning coverage (`resilient_query.py`), but Stage 2 (`stage2_collect_rankings`) and Stage 3 (`stage3_synthesize_final`) still call the package's raw single-attempt functions directly - confirmed by grep, zero resilience coverage there. This is the most likely cause of any future "model didn't participate" symptom once the MCP registration bug above is out of the picture. Spec: `docs/specs/stage2-3-debate-resilience-contract.md`.
    - **Two panel-flagged risks, checked by direct source read before writing that spec, both resolved as non-issues:** (a) whether CSS/Borda aggregation assumes a fixed N=4 - confirmed **no**: `council_rankings.py::calculate_aggregate_rankings` normalizes by `num_candidates = len(label_to_model)` and accumulates scores per whatever `stage2_results` actually arrived, dynamically, not fixed-N (the function's own docstring: "a 3-model council and 10-model council produce incomparable scores" without this normalization - it exists precisely to handle a variable count). A dropped model reduces sample size, it does not corrupt the score. (b) whether Stage 2 breaks when a reviewer has no Stage-1 draft of its own to rank - confirmed **no**: reviewers are drawn from the full static config (`_get_council_models()`, since `council_adapter.py` never passes an explicit `models=` override), independent of who actually drafted; self-vote exclusion only fires on an actual author match, never on a missing draft.
    - **User's "expand to 5 models for a voting majority" question: rejected, not left open.** `pipeline-architecture-spec.md` §2 (already grounded 2026-08-09/12) confirms this pipeline resolves via CSS/Borda-weighted aggregation + a single chairman synthesis, not nose-count majority voting - there is no even-panel tie-break failure mode here for a 5th seat to fix, and 5 would override the project's own documented distinctness/O(N²)/interpretability rationale to solve a problem this architecture doesn't have.
    - **ADR-026 reasoning-effort layer:** stays off, unanimous, no monkeypatch workaround pursued (would add new unversioned-internals coupling needing its own spec) - not a new decision, reaffirms the 2026-08-12 entry.
    - Full panel output (must-address items now folded into the new spec's ACs, open decisions still pending the user): idempotent retry/no double-billing, a visible degraded-mode marker on Stage 3's actual output (not just `debug_log`), backoff jitter, and a privacy note that extending backup-pool substitution into Stage 2 exposes the full anonymized candidate set (not just one draft) to Kimi/Qwen/Grok on a reviewer-slot substitution - flagged as needing explicit sign-off before Contract A of the new spec ships, not silent inheritance from Stage 1's existing approval.
- 2026-08-14 — **Contract B (Stage 3 chairman resilience) landed and wired live**, blind-TDV per Pillar 3: isolated `ws-verifier` authored tests from the contract alone (`tests/test_council_adapter_synthesize_resilient_stage3.py`), `ws-builder` implemented `_synthesize_resilient`/`ChairmanUnreachableError` blind, watched RED, GREEN, scoped mutmut gate on the isolated unit (523/535, 0 real survivors). Contract A (Stage 2 reviewer resilience)'s test-author agent hit a transport-level `API Error: Connection lost mid-response` mid-run and aborted before producing anything - an infrastructure failure, not a rejected contract; re-queued, not yet landed.
  - **Wired into the real call site** (`run_council_with_timeouts`'s Stage 3 section) immediately after landing, rather than left as a tested-but-unused function - `stage3_synthesize_final` itself never raises on a chairman failure (confirmed by direct source read: it already uses the same status-preserving `query_model_with_status` Stage 1 uses, and returns an `error_status`/`error_detail`-bearing dict instead of raising), so `_stage3_query_fn` re-runs the full prompt-build-and-query on every retry attempt and translates that dict into the status shape `_synthesize_resilient` expects. `ChairmanUnreachableError` is left deliberately uncaught at the wiring site - it propagates to `pipeline_runner.py`'s existing broad `except Exception` around this call (confirmed by direct read: already builds a "failed" `PipelineResult` with `debug_log`), so the "loud, non-silent failure" AC is satisfied by infrastructure that already existed, not new exception-handling code.
  - **Two real bugs found and fixed during wiring, not by the blind-TDV gate itself** (both only surfaced once the isolated unit was connected to real callers - exactly why "wire it in and run the tests" matters even after a clean mutation gate): (1) a type-checker-driven "fix" attempted first (`last_status: str` default `"error"` instead of `None`) turned out to be **wrong** - it silently changed real, already-tested behavior for the `max_attempts=0` degenerate case (a valid `RetryPolicy` per its own `__post_init__`) and broke a passing blind-authored test; reverted, and `ChairmanUnreachableError.last_status`'s type widened to `Optional[str]` instead, matching what the implementation and test had already independently agreed was correct. Lesson: a type-checker complaint is not always the code being wrong - here the annotation was wrong, not the logic, and the test suite (not Pyright) was the tiebreaker. (2) wiring `_get_chairman_model()` directly into `run_council_with_timeouts` broke 22 existing tests across `test_council_adapter.py`/`test_council_adapter_deadline.py`/`test_council_adapter_resilient_stage1.py`/`test_proposal_a_contract.py` with `AttributeError: 'SimpleNamespace' object has no attribute 'chairman'` - every one of those files' `_make_config`/config-double helper had `council=SimpleNamespace(models=...)` with no `.chairman`, because chairman resolution used to live entirely inside the (fully-mocked) `stage3_synthesize_final` call and was never exercised directly. Fixed by adding `chairman="fake-chairman-model"` to each of the 4 duplicated config-double helpers - a one-line fix per file once the actual mocking seam was found, not a design problem.
  - **Simplified during the same pass**: the original wiring threaded `stage3_synthesize_final`'s `verdict_result` through a holder dict so it could reach the final return tuple - direct inspection of the installed source showed `verdict_result` stays `None` for the `VerdictType.SYNTHESIS` mode this call always uses (only `BINARY`/`TIE_BREAKER` populate it), and the pre-wiring code already discarded it under the underscore-prefixed name `_verdict_result`. Removed the holder entirely rather than keep dead plumbing.
  - **Post-wiring scoped mutmut pass** (`council_adapter.py` alone, `only_mutate` temporarily narrowed in `setup.cfg` then restored - matches this ledger's established "scoped mutmut run" pattern) found 20 new survivors from the wiring code itself, not caught by Contract B's original isolated-unit gate. 13 were real gaps, closed with 2 new integration tests (`test_stage3_transient_error_status_is_retried_then_succeeds`, `test_stage3_terminal_error_status_raises_chairman_unreachable_with_correct_model`) exercising the mapping/retry/propagation through the actual call site, not just the isolated function. The remaining 7 are true equivalent mutants, documented inline rather than faked with pointless assertions: `_stage3_query_fn`'s `error_detail` value is real, documented, currently-unread data (kept for a future per-attempt debug-log follow-up, not stripped); `_synthesize_resilient`'s `stage3_query` positional arg is unread by `_stage3_query_fn` (which closes over the same variable directly instead); `_verdict_result = None` is the now-fully-dead value discussed above. Final: 577 mutants, 19 survivors, all pre-existing-or-newly-documented equivalents, 0 real gaps. Full suite: 743 passed (up from 725 before this session's changes - 18 net new: 2 integration tests here, plus Contract B's own blind-authored unit tests).
  - `mutants/`/`.mutmut-cache` (gitignored scratch dirs mutmut regenerates) leaked stale bytecode into a subsequent plain `pytest` collection twice during this pass (`import file mismatch` against `/data/llm-council-debate-vote/mutants/tests/...`) - cleaned both times (`rm -rf mutants .mutmut-cache` + stray `__pycache__`), not a real code problem, noted here only because it cost real turnaround time and is worth remembering for the next scoped mutmut run in this repo.
- 2026-08-14 — **Contract A (Stage 2 reviewer resilience) landed and wired live.** Blind-TDV's isolated pass (`_collect_rankings_resilient` + a deliberately minimal `_build_stage2_ranking_prompt`, per the contract's own non-goals) passed clean (694/713, 0 real survivors) but wiring it into the real call site would have downgraded every Stage 2 call - not just the resilience fallback - from this project's actual rubric-scoring evaluation to a crude 1-10 holistic score, since the real `stage2_collect_rankings` uses the richer prompt and the blind contract explicitly excluded prompt-text correctness. Asked the user directly (not decided unilaterally): wrap the real rubric prompt instead. Result: `_build_stage2_ranking_prompt` + `_collect_rankings_resilient` removed (superseded, not left as unused dead code) in favor of `_build_stage2_real_ranking_prompt` - a faithful, direct-source-read reproduction of the real rubric/holistic prompt branches, with exactly one shuffle per Stage 2 round (calling the real `stage2_collect_rankings` per-reviewer, mirroring Stage 3's wrap-the-real-function pattern, was ruled out because that function's internal `random.shuffle` would give each reviewer a different label-to-model mapping, making votes unmergeable). `query_models_resilient`'s retry/backup engine is reused unchanged, fed this prompt instead. Wired into `run_council_with_timeouts`'s Stage 2 section with cross-stage backup exclusivity (a backup already spent by Stage 1 is filtered out before Stage 2 ever calls `query_models_resilient`, per AC3) and Stage 1+Stage 2 substitutions/shortfall-warnings merged into one flat `metadata` view.
  - Wiring broke the same class of test gap as Contract B: 33 existing tests crashed on `AttributeError: no attribute 'rubric'` (4 config-double fixtures across `test_council_adapter.py`/`test_council_adapter_deadline.py`/`test_council_adapter_resilient_stage1.py`/`test_proposal_a_contract.py` needed `evaluation.rubric.enabled/.weights` added, since Stage 2 now reads that directly instead of it staying inside a fully-mocked `stage2_collect_rankings` call) or asserted exact values a fake `stage2_collect_rankings` used to hand-supply (e.g. a `"evaluations"` key the REAL `parse_ranking_from_text` never actually produces - confirmed by direct source read, only `"ranking"`/`"scores"` - the old fixture was already testing an imagined shape that had never been real, just never exercised through the real parser before). Also needed `random.shuffle` no-op'd in every affected fixture for deterministic ordering, and several fakes patching `query_models_resilient`/`query_model_with_status` needed a `"<responses_to_evaluate>"`-content discriminator added so a fake written for Stage 1 doesn't also silently swallow Stage 2's now-independent call through the same shared engine.
  - Post-wiring scoped mutmut pass: 53 survivors after wiring landed, most in the new `_build_stage2_real_ranking_prompt` (zero direct unit coverage existed - only exercised indirectly). Added direct tests: label/display_index assignment, shuffle-copy-not-mutate, HTML escaping, multi-candidate join separator, and two full exact-text golden tests (rubric-enabled and holistic-disabled branches, transcribed directly from the installed source, both passed on first write). Also closed real integration gaps with 5 more tests: cross-stage backup exclusivity end-to-end (real `query_models_resilient`, a permanently-unreachable model in both stages, confirming the consumed backup is filtered before Stage 2 ever attempts it and the resulting shortfall is surfaced), Stage 2 usage-accumulation defaults on a missing `usage` key, a single-model branch has no phantom `stage2_shortfall_warning` leak, a missing-`content`-key reviewer response defaults `ranking` to `""`, and the `" | ".join(...)` separator when both stages fall short simultaneously. Final: 0 real survivors, all remaining pre-existing-or-documented equivalents (percentage-weight golden test needed boundary-sensitive weights - `0.3*100`/`0.3*101` both truncate to the same int - to actually exercise the `*100` literal). Full suite: 754 passed.
- 2026-08-14 (later) — **Closed the machine-wide `deepseek-v4-pro` leak at its root, not just this project's local-scope shadow.** User reported seeing `deepseek/deepseek-v4-pro` again in a council panel run from a *different* folder, same day as reboot - re-verified this project's own `council_health_check` was still clean (`council_size: 4`, no deepseek), so the leak was external. Traced via direct inspection of `~/.claude/.config.json` (the file Claude Code's CLI actually reads/writes for MCP registrations - `~/.claude.json`'s top-level `mcpServers` key is a stale unread legacy location, per [[mcp-cli-subprocess-registry-mismatch]] in global memory): a **user-scope (global) `llm-council` registration exists with `env: {}` - no `LLM_COUNCIL_CONFIG` pin.** This project's 2026-08-14-earlier fix (local-scope override, pinned env var) only shadows that global entry when cwd is this project; every other folder, including `~/Documents/Xspecies_Tech_deck_revenue_2_produicts_Ravi` (confirmed registered with an empty `mcpServers` override), falls through to the unpinned global entry, hits `_find_config_file()`'s empty 2nd/3rd tiers, and gets the package's hardcoded defaults (deepseek included) - exactly the same mechanism already documented above, just from a different folder.
  - **Fix (root, not per-project shadowing this time):** populated `_find_config_file()`'s own 3rd resolution tier - `~/.config/llm-council/llm_council.yaml`, previously absent - with the same live-verified 4-model roster and the same two package-bug workarounds (double-nested `council:` key, explicit `tiers.pools.*`) as this project's file. This requires zero MCP re-registration and applies automatically, machine-wide, to any cwd with no `./llm_council.yaml` and no `LLM_COUNCIL_CONFIG` pin - covering this session's originally-flagged Xspecies gap and every future unconfigured folder, not just the one incident.
  - **Verified by direct execution**, not just written and assumed: ran the installed tool's own `_find_config_file()` + `load_config()` from a clean cwd (scratchpad, outside every registered project) with `LLM_COUNCIL_CONFIG` unset - resolved to the new global file, `council.models` and `tiers.pools.reasoning.models` both returned the correct 4-model roster, no deepseek.
  - **Scope note on take-effect timing:** MCP server connections are established once at session startup, not hot-reloaded (per [[mcp-cli-subprocess-registry-mismatch]]) - this project's own already-running session was unaffected either way (it already resolves correctly via its local-scope pin). Any *other* folder's session that was already open before this file was written will keep whatever config its already-running `llm_council.mcp_server` process loaded at its own startup; only a session/process started after this file existed will pick it up. Not yet re-verified live against the actual Xspecies folder's own MCP session (would require the user to open/restart one there) - flagged, not assumed. Confirmed no in-process reload path exists at all: `get_config()` caches into a module-global on first call (`reload_config()` exists but nothing in `mcp_server.py` ever calls it) and `mcp_server.py`'s `COUNCIL_MODELS = _get_council_models()` runs once at module-import time (line 101) - the only way to pick up a config change is a fresh process (new session, or a per-server reconnect if the user's Claude Code client exposes one - this repo's own sandboxed Bash tool can't see or signal that other process to force it).
- 2026-08-14 (later still) — **"Deep research mode" + "reasoning tier as primary" - user request, panel-deliberated, one real gap found and specced.**
  - **User's two asks**: (1) trigger the highest reasoning + "deep research" capability per model, per stage, blocking/sequential; (2) make high-reasoning-effort the pipeline's default/primary mode, not a conditional add-on, since this council exists for ambiguous/high-order questions.
  - **Grounding before the panel** (live, this session): OpenRouter has no per-request "deep research" toggle for any of this project's 4 roster models - "Deep Research" is a wholly separate, specialized model product (`openai/o3-deep-research` and similar), not a flag (openrouter.ai/docs/features/web-search, live fetch + WebSearch cross-check). What IS available per-model is web-search augmentation (`:online` suffix / `web` plugin) - already used narrowly by this project (`live_adapters.py`'s `EVIDENCE_MODEL = "google/gemini-3.6-flash:online"`, Stage 0.5 evidence-fetching only, not the 4 core council models).
  - **Panel** (`panel-review` orchestrator, 9 of 17 personas: standing quartet + Scientist/Cloud/Warden/Red-Team/Privacy - guardrail trio pulled in because Q1 touches network egress and a new model-selection surface): ran Q1/Q2 as two separate debates (different mechanisms - egress/model-selection vs. request-parameter/gating-policy - converging them risked burying Q2's real answer under Q1's genuinely-new engineering surface), converged 13 concerns raised/11 resolved/2 needing user decision.
  - **Q1 verdict: rejected as a new feature.** No distinct deep-research model call (wrong cost/latency shape - multi-minute, no async-loop support in this pipeline). No `:online` expansion to Stage 1 (Agentic Architect, RED: would homogenize the 4 models' independent drafts, undermining the Knowledge-Divergence rationale (arXiv:2603.05293) that gives Stage 2's cross-review signal its value) or to Stage 3 (Red-Team/Warden/Privacy, RED: unthreat-modeled prompt-injection-via-search-result vector directly on the chairman's synthesis, plus egress expansion beyond the accepted core-4-model boundary - neither evaluated, not shipping on this pass). What the user actually meant by "highest reasoning, blocking, per stage" collapses into this project's own already-partially-wired `reasoning_effort` mechanism.
  - **Q2 verdict: rejected as an unconditional blanket policy, but found to already be ~true by design.** "Council exists for ambiguous questions, so always max effort" proves too much - would also argue for removing Stage 2's deliberate `effort=none` (no evidence more effort improves ranking) and making Stage 2.75/3.75 unconditional, reopening the unreliable-auto-revision failure mode this project's own cited literature (ARMOR-MAD, Deliberative Illusion, "Revision or Re-Solving?") already argues against. High effort is already the target for every generation-shaped stage (1, 2.75, 3, 3.75) - not a policy gap, a wiring gap.
  - **The one real gap both questions converged on**: Stage 1 (draft) and Stage 3 (chairman synthesis) - arguably the two highest-leverage calls - don't actually send `reasoning_effort` yet. Stage 3 stays upstream-blocked (`stage3_synthesize_final` has no `reasoning_params` kwarg at all). **Stage 1 is fixable.**
  - **Post-panel correction, found while turning the recommendation into a spec (direct source read, not assumed from the panel's summary):** the panel/docs' proposed fix - swap `resilient_query.py`'s `query_fn` to `llm_council.openrouter.query_model_with_status`, which does accept `reasoning_params` - is NOT actually safe. Read `llm_council/gateway/openrouter.py::build_openrouter_payload` directly: (1) it always sends `{"effort","max_tokens","exclude"}` together whenever `reasoning_params` is set, violating the already-documented Anthropic effort/max_tokens mutual-exclusivity rule for the opus-4.8 seat specifically - the seat needing `effort=high` most; (2) it gates injection on `provider.supports_reasoning(model)`, the same capability check already flagged elsewhere in this project's docs as stale (`False` for `openai/gpt-5.5`/`google/gemini-3.6-flash` despite live catalog support) - would silently drop the parameter for 2 of 4 seats, no error. Real fix: a new project-owned function using the same top-level-`reasoning_effort`-field raw-HTTP pattern as Contracts 1-3 (not the package's nested-object path), built to `resilient_query.py`'s own documented `QueryFn` status-dict shape. Spec'd as Contract 4 in `docs/specs/reasoning-effort-wiring-contract.md`; `docs/agent-model-reasoning-config.md` §3's Stage 1 row corrected to match - this ledger entry is the "why," that file stays authoritative for "current config."
  - **User decisions (both resolved, 2026-08-14)**: (1) schedule the Stage 1 wiring now, not queued - spec written this session; (2) gate rollout on the medium-vs-high dry-run comparison (not fast-follow) - added to Contract 4 as an explicit rollout precondition, mirroring the Stage-1-prompt-enrichment dry-run discipline already on file (§5 of the config doc).
  - **Not yet done**: blind-TDV implementation of Contract 4 itself - spec only this pass, per this project's Pillar 2 (spec before code).
- 2026-08-14 (even later) — **Contract 4 (Stage 1 reasoning-effort wiring) landed via blind-TDV**, per Pillar 3: an isolated `blind-tdv` workflow run (ws-verifier authoring tests from ACs 11-23 alone, ws-builder implementing blind in a worktree) added `scripts/live_adapters.py::query_model_with_status_and_effort` (new, single-attempt, full STATUS_* taxonomy, top-level `reasoning_effort` only - never the package's unsafe nested `reasoning_params` path) and wired it into Stage 1 via a new `council_adapter.py::_stage1_query_fn` closure over the hardcoded `_STAGE1_REASONING_EFFORT` map (opus-4.8/gpt-5.5 -> high, gemini-3.6-flash/glm-5.2 -> medium, unmapped/backup models -> None). Watched RED confirmed real (a discriminating test assertion had to be added when the first version silently passed against reverted code - documented inline in `tests/test_council_adapter_resilient_stage1.py`). Scoped mutmut (both changed files, hermetic, full 785-test baseline green first): 1327 mutants total scope, 85 initial survivors traced by hand - 63 pre-existing/already-documented-equivalent (untouched by this diff, confirmed by scanning for `query_fn`/`_stage1_query_fn`/`_STAGE1_REASONING_EFFORT` in their diffs), 22 real gaps in the new code closed with a new 39-test file (`tests/test_reasoning_effort_stage1_contract.py`, derived directly from ACs 11-18 plus direct `_STAGE1_REASONING_EFFORT`/`_stage1_query_fn` unit coverage). Final targeted re-run on just the new/changed surface: 178 mutants, 177 killed, 1 true equivalent (the `Retry-After` header's "60" fallback string - both a digit and non-digit fallback resolve to the same `retry_after=60`, now documented in-source at `live_adapters.py`'s 429 branch, matching this project's established inline-equivalent-mutant convention rather than leaving the justification only in the workflow transcript). Independently re-verified by direct execution after the workflow reported PASS, not taken on trust: `pytest tests/test_reasoning_effort_stage1_contract.py tests/test_council_adapter_resilient_stage1.py tests/test_council_adapter.py` (97 passed) and the full suite `pytest tests/` (824 passed, no regressions).
  - **Not done yet, deliberately**: the Pillar 6 rollout-gating dry-run (medium vs high effort, Stage 1 CSS/rubric comparison) per the user's 2026-08-14 decision. The code is merged and will send `reasoning_effort` on every real Stage 1 call from here on - there is no separate feature flag, matching how Contracts 1-3 shipped - so this dry-run is a live process gate, not optional cleanup: do not run this pipeline against a real decision until it's done.
- 2026-08-14 (final) — **Contract 4 dry-run executed - found "high" effort DROPS CSS, not raises it. Shipped default reverted to medium.**
  - **Method**: real OpenRouter, no mocking, executed directly via `council_adapter.run_council_with_timeouts()` (bypassing Stage 0.5 grounding/`pipeline_runner.py`'s CLI - out of scope for a Stage-1-focused comparison, cuts cost, and means both runs show `hallucination_risk: 1.0`/`grounded: false` identically, a `verified_facts=[]` artifact, not a differentiator between the two runs). Same low-stakes test query both times (a small OSS CLI's logging-format decision, not a real project decision), one run with all 4 Stage 1 seats at `reasoning_effort="medium"` (baseline), one with the originally-shipped map (opus-4.8/gpt-5.5=`high`, flash/glm=`medium`, unchanged).
  - **Result**: baseline CSS 0.721 ($0.362, 57,781 tokens) vs shipped-high CSS 0.572 ($0.360, 58,011 tokens) - a 21% relative CSS **drop** for `high` effort, at essentially identical real cost. Not the hypothesized gain; the opposite.
  - **Interpretation, explicitly caveated, not overclaimed**: single trial (n=1), real run-to-run variance exists. CSS measures cross-model *ranking consensus* (do peer reviewers agree on which answer is best), not correctness - a drop is genuinely ambiguous between "high effort made these 2 models' answers less reliable" (bad) and "high effort made them reason more independently, so peer review disagreed more" (could reflect richer, not worse, thinking - the same Knowledge-Divergence tension the panel raised when rejecting `:online` expansion to Stage 1 earlier the same day). Neither this ledger nor the code change below claims to have resolved which explanation is correct - only that the gate's own stated condition ("if high doesn't show a measurable gain, report it, don't ship anyway") was met by a real, not a hypothetical, negative result.
  - **Decision (user, presented with 4 options - revert/run-more-trials/ship-anyway/investigate-first - explicitly chose revert)**: `council_adapter.py::_STAGE1_REASONING_EFFORT` changed from `{opus-4.8: high, gpt-5.5: high, flash: medium, glm: medium}` to all-`medium`, with an in-source comment recording the finding and an explicit instruction not to re-promote without fresh evidence. `tests/test_reasoning_effort_stage1_contract.py`'s two hardcoded-map assertions updated to match (legitimate re-specification backed by dry-run evidence, not a weakened test hiding a bug - the contract's target value itself changed). `docs/specs/reasoning-effort-wiring-contract.md` and `docs/agent-model-reasoning-config.md` §3 updated to record medium as the live default and link back to this entry. Full suite re-run after the revert: still green (verify command run, see below).
  - **What did NOT change**: `query_model_with_status_and_effort` itself, `_stage1_query_fn`'s lookup logic, and every AC 11-23 test - the capability (Contract 4) is fully shipped, tested, and available; only the DEFAULT effort values it's called with changed. Re-promoting opus-4.8/gpt-5.5 to `high` later needs only a `_STAGE1_REASONING_EFFORT` edit plus a fresh dry-run showing an actual gain, not new engineering.
- 2026-08-14 (later still) — **Contract 4 implemented via the `blind-tdv` workflow — process gap found and fixed, not silently accepted.** The workflow's own pipeline reported `PASS: false`; it was not treated as done on that basis alone.
  - **What the workflow actually did, per its journal**: (1) the isolated test-author agent's handoff arrived with an empty `CONTRACT:` section (a workflow-script defect, not this agent's fault) - it correctly refused to guess and produced no tests, rather than inventing ACs from repo docs. (2) The implementer agent then built the real feature (`live_adapters.py::query_model_with_status_and_effort`, `council_adapter.py::_STAGE1_REASONING_EFFORT`/`_stage1_query_fn`) with no blind-authored tests waiting for it - a structural break of blind isolation (implementation happened before/without independent tests, not the reverse). (3) The watch-RED agent, to its credit, refused to fabricate a pass: it reverted only the two implementation files via `git stash` and found the two pre-existing integration tests it was handed (`test_stage2_excludes_a_backup_already_consumed_by_stage1`, `test_ac23_happy_path_calls_query_fn_exactly_once_per_primary_no_backups`) passed identically with the feature reverted - their `fake_query_model_with_status(model, messages, timeout, *a, **kw)` fakes were `**kw`-tolerant and never inspected `kw`, so they couldn't distinguish "routes through the new effort-aware function" from "still uses the old one." Reported `watchedRed: false` and the gap explicitly instead of claiming success. (4) The mutation-gate agent found 22 real coverage gaps in the new code (0 prior coverage of `_stage1_query_fn`/`_STAGE1_REASONING_EFFORT` at all) and wrote 39 new tests (`tests/test_reasoning_effort_stage1_contract.py`) to close them - but wrote them with the real implementation already visible, which is non-blind by construction even though individually well-targeted at Contract 4's ACs 11-18.
  - **Verified this wasn't just a workflow-reporting quirk**: mutation numbers on their own were solid (1263/1327 killed; the 64 "survivors" the top-level summary showed = 63 pre-existing/out-of-scope equivalents already documented from prior sessions + exactly 1 newly-verified true equivalent, traced by hand - a Retry-After-header-default mutation where both the real and mutant code produce the identical downstream value, confirmed genuinely unobservable). The real, correctly-flagged defect was narrower and specific: two integration tests with a discrimination gap, not a broken mutation gate.
  - **Fix applied directly** (not re-delegated - the gap was fully understood from the journal, a surgical correction, not new design work): confirmed by direct source read that `_stage1_query_fn` ALWAYS calls `query_model_with_status_and_effort(model, messages, timeout, reasoning_effort=effort)` as an explicit keyword (even `None` for an unmapped model), while the OLD path (`query_models_resilient` invoking `query_fn(model, messages, timeout)` positionally, 3 args only, per its own `QueryFn` type) can never produce that kwarg at all - so asserting `"reasoning_effort" in kw` (Stage 1) / `"reasoning_effort" not in kw` (Stage 2, confirming Contract 4 stayed Stage-1-only) is a real, non-fabricated discriminator. Added to both tests, plus an exact-value assertion against `ca._STAGE1_REASONING_EFFORT.get(model)`.
  - **Re-verified by direct execution, not re-delegated to another agent**: GREEN first (`pytest tests/test_council_adapter.py tests/test_council_adapter_resilient_stage1.py -q` - 58 passed). Then genuine RED: `git stash push --keep-index -- scripts/council_adapter.py scripts/live_adapters.py` (implementation-only revert, tests kept at their strengthened state) - both target tests now FAIL for the exact expected reason (`AssertionError: assert 'reasoning_effort' in {}`), watch-RED genuinely established this time. Stash popped, GREEN reconfirmed. Full suite: 824 passed (785 baseline + 39 Contract-4-ACs tests from the mutation-gate agent), 0 failures.
  - **Lesson for future blind-TDV runs in this repo**: a `**kw`-tolerant fake in an integration test is a latent blind spot - it survives a call-signature change silently instead of failing loud. When strengthening a pre-existing fake to cover a new call path, assert on the presence/value of the NEW kwarg specifically, not just that the call still succeeds.
  - Files touched this entry: `scripts/live_adapters.py` (new), `scripts/council_adapter.py` (Stage 1 wiring), `tests/test_council_adapter.py`, `tests/test_council_adapter_resilient_stage1.py` (both strengthened), `tests/test_reasoning_effort_stage1_contract.py` (new, 39 tests). Not yet committed - working-tree changes only, per this session's practice of leaving commits to explicit user request.
  - **Still pending, unchanged from the spec**: the Pillar 6 real-money-gate dry-run (medium vs high effort, Stage 1 CSS/rubric comparison) - rollout stays gated on it, per the user's explicit 2026-08-14 decision; not run this pass.
- 2026-08-14 (final, CSS correction) — **The dry-run's revert decision was based on a mistaken reading of CSS - caught by direct user challenge, corrected same session, restored.**
  - **What happened**: the Contract 4 dry-run (previous entry) found CSS drop 0.721->0.572 for `high` vs `medium` Stage 1 effort and this was reported as "a real signal against shipping high effort" - the user was told to revert, and did. The user then asked directly: "css measures agreement of models, if so it should not be a reason to switch down, right?"
  - **Checked, not assumed**: direct read of `llm_council.quality.consensus.consensus_strength_score`'s own docstring confirms CSS measures cross-model RANKING AGREEMENT, not correctness - and the package's own interpretation bands (`get_consensus_interpretation`) place both 0.721 ("moderate_consensus") and 0.572 ("weak_consensus") inside the pipeline's two normal, fully-handled operating bands; neither crossed into "significant_disagreement" (<0.50), the actual failure-adjacent threshold that triggers extra deliberation stages. This pipeline treats low CSS as an ACTED-ON trigger (Stage 2.75 revision / Stage 3.75 critique exist specifically to consume it), not a failure state - and for a debate architecture whose premise is independent reasoning, lower cross-model agreement is not self-evidently bad. The revert's justification did not hold up.
  - **Corrected**: `_STAGE1_REASONING_EFFORT` restored to `high` for opus-4.8/gpt-5.5 (the pre-revert, originally-shipped default). `tests/test_reasoning_effort_stage1_contract.py`'s two map assertions restored to match. `docs/specs/reasoning-effort-wiring-contract.md`/`docs/agent-model-reasoning-config.md` updated to remove the "CSS proves it's worse" framing. New reference section added, `docs/pipeline-architecture-spec.md` §9 "Quality-metric interpretation reference" - explains what CSS actually measures, the package's interpretation bands, and this exact incident as the worked example, specifically so this mistake isn't repeated. Full suite re-verified green after restoring (824 passed).
  - **Standing lesson, not just a one-off fix**: a metric moving in an unexpected direction is not automatically evidence of a quality regression - check what the metric actually measures (and what this pipeline already does in response to it) before treating a number's direction as a verdict. Applies beyond CSS - the same caution belongs on any future metric-driven rollout decision in this project (rubric scores, deliberation_depth, hallucination_risk, etc.) - read what a metric is actually FOR before citing its movement as an argument.
  - **Still open**: a genuine content-quality comparison (Stage 1 draft text itself, against the Stage 2 rubric) between `medium` and `high` effort for the 2 promoted seats, per the user's explicit follow-up decision - CSS could never have answered this either way. Not run yet as of this entry; see the next entry once it lands.
- 2026-08-14 (absolute final) — **Content-quality dry-run executed - split result, no evidence either way. Default settled at `high` by user decision, not by data.**
  - **Method**: direct Stage 1-only calls (`live_adapters.query_model_with_status_and_effort`, bypassing Stage 1.5/2/3 - cheaper, and unnecessary for a content read), same query as the CSS dry-run, opus-4.8 and gpt-5.5 each queried at `medium` and `high`. **Blinded self-judging**: both drafts per model written to a file labeled only "Draft A"/"Draft B" with effort assignment shuffled and withheld in a separate key file; judged against this project's own Stage 2 rubric dimensions (accuracy/relevance/completeness/conciseness/clarity) BEFORE reading the key, to avoid confirming the hypothesis the reverted-then-restored default already implied.
  - **Result**: split 1-1. For opus-4.8, the `medium`-effort draft was judged marginally better (more natural self-rebuttal of its own counterpoints). For gpt-5.5, the `high`-effort draft was judged marginally better (more concrete named examples, more actionable forward guidance). Both differences were modest and subjective, single-judge, single-trial per model - not a confident signal in either direction. Cost was already established as a wash from the CSS dry-run ($0.362 vs $0.360).
  - **Conclusion**: neither CSS nor direct content reading found real evidence that `high` effort improves Stage 1 output for either promoted seat - nor evidence it hurts. Presented to the user as a genuine coin-flip from available data, with further trials flagged as real additional cost for a possibly-unmeasurable effect rather than a promise of resolution.
  - **Final decision (user, presented with keep-high/switch-medium/invest-in-more-trials, chose keep-high)**: `_STAGE1_REASONING_EFFORT` stays at `{opus-4.8: high, gpt-5.5: high, flash: medium, glm: medium}` - the pre-dry-run original default. This is a judgment call made WITH full knowledge that the data doesn't clearly support it, not a data-backed verdict - recorded here explicitly so a future session doesn't mistake "kept `high`" for "proven `high` is better."
  - **Full arc of this incident, useful as a standing lesson**: shipped default (high) -> CSS dry-run showed a drop -> misread as quality regression -> reverted to medium -> user challenge caught the misread -> corrected, restored to high, added `docs/pipeline-architecture-spec.md` §9 -> content dry-run to actually check quality -> came back split -> settled on high by explicit judgment call, not data. Total real-money cost across all dry-run legs this session: ~$0.72 (two CSS-comparison runs) + 4 Stage-1-only calls (content comparison, not separately logged but small relative to the CSS runs).
- 2026-08-16 — **Gemini seat swapped `3.6-flash` -> `3.7-flash` across the whole pipeline, on user request originating from a mistaken "Gemini 2.7 Flash" name (no such model exists on OpenRouter's live catalog - only a 2.5 generation and the 3.5-3.7 generation, confirmed via direct `/api/v1/models` fetch this session).**
  - **Live grounding**: fetched `https://openrouter.ai/api/v1/models` directly. `google/gemini-3.7-flash` is real and live, with IDENTICAL `context_length` (1,048,576), `max_completion_tokens` (65,536), and `supported_parameters` (incl. `reasoning_effort`, `reasoning`, `tools`) to `google/gemini-3.6-flash` - but at exactly half the price: prompt $0.000000375 vs $0.00000075/token, completion $0.000001875 vs $0.00000375/token (both also carry identical `web_search: 0.014` pricing, confirming `:online` support parity).
  - **Context**: the 2026-08-13/14 decision (see the `normalizer_model` entries above) explicitly considered and rejected `3.7-flash` in favor of `3.6-flash`, citing only "newer, cheaper, but zero operational track record in this project" - not a technical gap. That reasoning is unchanged; only the pricing delta is new information (the exact 2x figure wasn't on file before). Presented to the user as a real reversal of a prior explicit decision, with the Pillar-6 real-money-gate caveat surfaced; user chose "swap everywhere."
  - **Applied**: `llm_council.yaml` (`council.models`, `tiers.pools.{high,balanced,reasoning}.models`, `council.normalizer_model`), `scripts/council_adapter.py` (`_STAGE1_REASONING_EFFORT` map key), `scripts/live_adapters.py` (`EVIDENCE_MODEL`, `COMPLETENESS_CHECK_MODEL`), `scripts/pipeline_runner.py` (`PipelineConfig.completeness_check_model` default). Old comment blocks explaining the original 3.6-flash choice left in place (not deleted) with a dated "SUPERSEDED" note pointing here, per this project's own convention of preserving decision history rather than silently overwriting it.
  - **Tests**: mechanically renamed the same slug string in `tests/test_reasoning_effort_stage1_contract.py`, `tests/test_debate.py`, `tests/test_pipeline_runner.py` (these assert against the module's *default* model value / illustrative fixture data, not a business rule tied to "3.6" specifically - renaming keeps them in sync with the new default rather than weakening any assertion). Full suite re-run: 824 passed, 0 failures.
  - **Verified by direct execution, not just re-reading YAML**: `llm_council.unified_config.load_config(Path("llm_council.yaml"))` against the installed `llm-council-core==0.40.1` confirms `cfg.council.models`, `cfg.council.normalizer_model`, and all three tier pools resolve to `google/gemini-3.7-flash` post-edit - the documented nesting-bug workaround still applies correctly to the new slug.
  - **Still pending, unchanged from Pillar 6**: a fresh dry-run Cost & Tokens summary is required before the next real-decision run, since this changes the model pool/pricing tier for the first time since the swap. Not run this session - config/code change only.
- 2026-08-16 (later) — **OpenRouter-config effectiveness review: research -> panel -> real LLM council -> a second panel re-examining a conflict with a 2-day-old prior decision. Two concrete outcomes; nothing shipped yet, spec still to write.**
  - **Research** (6 parallel live-doc-verification agents, all citing URLs + this session's date): confirmed real - `openrouter:web_search`/`web_fetch` server tools (top-level `tools` array, `max_uses` tool-param + top-level `max_tool_calls` step budget, both genuinely OpenRouter-enforced, confirmed via a follow-up direct fetch of `openrouter.ai/docs/guides/features/server-tools/web-search`), structured outputs (`response_format`, all 4 roster models support it per live `supported_parameters`), Response Healing plugin (JSON-syntax-only, non-streaming-only), provider routing/model-fallback array, Presets (hosted-only), OpenRouter's own MCP server, and Fusion (a real managed panel+judge product). Confirmed absent: request-level `mcp_servers` passthrough, completion webhooks. Root architectural fact, confirmed by direct source read of the installed `llm_council/gateway/openrouter.py::build_openrouter_payload`: it accepts ONLY `reasoning_params`/`max_tokens`/`temperature`/`disable_tools` - no `plugins`/`tools`/`response_format`/`provider` field exists on the Stage 1 (debate) call path, so none of the above are reachable without a project-owned raw-HTTP bypass, same shape as the already-shipped Contract 4.
  - **First panel + first real council consult** (both used the current `gemini-3.7-flash` roster - required a manual MCP server reconnect first, since it had cached the pre-swap `3.6-flash` config from its own startup; a Pillar-6 dry run was run before the real consult, $0.2273/33k tokens, all 4 models healthy, swap validated live): converged on shipping a dormant, already-installed prompt-cache activation "immediately" and building web-search as a narrow Contract-5-style Stage-1 bypass (3 of 4 models - `claude-opus-4.8`/`gpt-5.5`/`gemini-3.7-flash` at confirmed native pricing $0.01/$0.01/$0.014 per call; `z-ai/glm-5.2` excluded, no native search engine, unconfirmed Exa/Parallel fallback price), fixed per-model-per-round cap rather than extending the downstream cost ceiling backward, fail-closed on budget exhaustion. The real council's 4 models independently added a requirement neither the internal panel nor the research pass had: **search provenance (queries + source URLs) must be threaded into Stage 2/3 synthesis, not just logged for forensics** - both Claude and GLM-5.2 called this a hard requirement, since "search results are framed as reference data" alone isn't a reliable prompt-injection mitigation on its own. (Note: the real council's chairman synthesis for this specific call came back labeled `(Fallback - single model response)` with `Council status: partial` and no `Cost & Tokens` block, unlike the dry run - the chairman-level cross-model synthesis step appears to have degraded for this call; the underlying 4 individual Stage 1 opinions and Stage 2 peer-review labels were all present and used, but the real per-call cost for this consult is unknown and not recorded here. Flagged as a Pillar-5 gap - `mcp__llm-council__consult_council`'s chairman-synthesis reliability - not investigated further this pass.)
  - **Conflict found before writing any spec**: `docs/specs/reasoning-effort-wiring-contract.md`'s own "Non-goals" section states web-search expansion to Stage 1 was "explicitly considered and rejected by the 2026-08-14 panel" - see this file's 2026-08-14 "Deep research mode" entry, Q1 verdict: rejected specifically because it "would homogenize the 4 models' independent drafts, undermining the Knowledge-Divergence rationale (arXiv:2603.05293) that gives Stage 2's cross-review signal its value." Neither today's internal panel nor the real council re-examined or even mentioned this - both focused on cost/pricing/injection. Surfaced to the user rather than silently proceeding or silently dropping the feature; user chose "re-examine with the homogenization concern in scope, then decide."
  - **Paper re-grounding, direct read not the 2026-08-14 panel's paraphrase** (arXiv:2603.05293, "Knowledge Divergence and the Value of Debate for Scalable Oversight", Robin Young, 2026-03-05, `cs.LG`/`cs.CL`): the paper's homogenization mechanism (§3.4 "Dynamic Subspaces Under Debate") formally models PEER-TO-PEER revelation across debate rounds (model A reading model B's argument mid-debate and absorbing it, monotonically shrinking debate advantage toward zero). This mechanism cannot fire in this pipeline's actual Stage 1, confirmed via `docs/pipeline-architecture-spec.md` as a single-pass independent-draft stage with zero peer visibility (peer exposure starts at Stage 2) - the 2026-08-14 panel's Red finding never cited §3.4 specifically, so this is a genuine sharpening of an imprecisely-mechanized objection, not a strawman. The mechanism that DOES apply is the paper's static framework (§2): shared knowledge inputs reduce debate value proportionally to their overlap, not wholesale - and the paper's own remark that models from different pretraining pipelines have higher "effective rank" of private knowledge (more robust to a single shared-knowledge injection) directly favors this project's 4-different-labs roster. Paper's own Limitations section: explicitly a stylized/idealized model, "the debate advantage Δ... describes the ceiling of debate's value, not its floor" - not an empirical claim about this specific pipeline.
  - **Second panel (homogenization re-examination), grounded in the direct paper read above, not the original paraphrase**: converged **8 concerns raised, 6 resolved, 2 needing user decision** - explicitly neither "proceed as scoped" (today's earlier convergence) nor "uphold the 2026-08-14 rejection" wholesale, but proceed WITH these made hard requirements, not optional hardening: (1) each of the 3 search-enabled models forms and issues its OWN independent search query - no shared/pooled search step (the worst case for homogenization, previously unaddressed); (2) search is claim-scoped (verifying a specific claim the model itself proposes), not open-ended exploratory search, reusing the existing Stage 0.5 grounding/epistemic-clause machinery rather than inventing a 5th distinct search "shape"; (3) GLM-5.2's search exclusion becomes a codified, tested, permanent invariant (a constant + test asserting at least one core-4 model is always unsearched at Stage 1) rather than an artifact of today's pricing gap that might silently disappear once GLM-5.2's price is confirmed; (4) provenance must distinguish "no search fired" from "search fired" for every model, not just log successful calls; (5) `max_uses` server-side enforcement and retry/backup non-duplication (does a Stage 1 retry re-issue and re-bill a search call?) must be verified before shipping, not assumed; (6) `docs/specs/stage1-web-search-contract.md` must be written capturing all of the above before any blind-TDV implementation - nothing here is shippable yet per Pillar 2.
  - **Prompt-cache activation, independently re-scoped and found narrower than either panel assumed**: direct read of the installed package confirms `cache_context.set_cache_context()` is called ONLY from `llm_council/verification/api.py` (the `verify()`/ADR-034 code path) - it is not a general drop-in for `consult_council`'s Stage 1-3 debate path, contrary to how this was described to both panels and the real council this session. However, `CacheContext.matches()` returns `False` immediately on an empty `segments` list (confirmed by direct read of `cache_context.py`), so a `CacheContext` with only `session_id` set (no `segments`) is a verified-safe no-op for the Anthropic-specific `cache_control` breakpoint logic while still getting the `session_id` sticky-routing benefit unconditionally (`build_openrouter_payload` adds `session_id` to the payload whenever `cache_ctx` is not `None`, before the Anthropic-specific branch). The FULL cache_control-breakpoint savings would require reverse-engineering the package's *internal* Stage 1-3 prompt-assembly structure to build a correct `segments` map - a separate, Contract-4-scale investigation, not attempted this pass. Narrower, session_id-only activation is the actual "ship now" candidate, not the fuller feature both panels assumed was free.
  - **The 2 open user decisions, both resolved (2026-08-16)**: (1) **gate first real-decision Stage-1-search use on a fact-vs-judgment measurement dry-run** (not proceed-by-judgment-call) - mirrors the Contract 4 CSS/content dry-run precedent; rationale given: this is a novel risk class (prompt injection, draft homogenization), not a tuning knob like reasoning-effort was, so real evidence is warranted before real use. (2) **GLM-5.2's Stage-1 search exclusion is a permanent architectural rule**, not a pricing-driven default - codify as a tested invariant (constant + test asserting at least one of the 4 core models is always unsearched at Stage 1), independent of any future GLM-5.2 fallback-pricing confirmation. Both decisions folded directly into `docs/specs/stage1-web-search-contract.md`, written next.
  - **Prompt-cache session-affinity activation: shipped.** `docs/specs/
    prompt-cache-session-affinity-contract.md` written (small-contract tier,
    Contracts 1-3 style - direct test-first, not blind-TDV, since it wraps
    the Stage 1 call site without altering its resilience behavior). Test-
    first RED confirmed real (3 of 4 new tests failed before the code
    change, for the exact expected reason - `set_cache_context`/
    `clear_cache_context` not yet called). Implemented: `council_adapter.py::
    run_council_with_timeouts` now sets a fresh `CacheContext(segments=[],
    session_id=str(uuid.uuid4()))` before its body runs and clears it in a
    `finally` (covers exception paths, not just the success return). 4 new
    tests (ACs 1-4) plus the full 828-test suite (824 baseline + 4 new) re-
    run GREEN. AC 4 asserts against the REAL installed `build_openrouter_
    payload`, not a mock - confirms `segments=[]` really is a safe no-op for
    the Anthropic `cache_control` branch and `session_id` really lands in
    the payload. **Still pending, per its own Rollout precondition**: a real
    dry-run pair (with/without this change) to capture the actual cost/
    latency delta before citing an expected saving as fact - not required
    pre-merge (this doesn't change request content, only routing metadata),
    but required before claiming the saving is real. Not run this pass.
  - **`docs/specs/stage1-web-search-contract.md` written** (Contract 5),
    incorporating every hard requirement from the second panel plus two
    corrections found via one real, direct OpenRouter test call made while
    grounding the spec (not guessed from docs prose): (1) the real
    `message.annotations`/`usage.server_tool_use_details.
    web_search_requests` schema, confirmed live - the docs' prose had the
    wrong field name (`server_tool_use`, missing `_details`); (2) `max_uses:
    1` did NOT strictly cap search calls to 1 in this live test - 2 fired,
    billed at 2x the naive per-model price ($0.028 = 2 x $0.014, confirmed
    via the response's own `usage.cost` minus token cost) - AC 10 now
    requires the dry-run Cost & Tokens summary state BOTH the naive and the
    2x-observed ceiling, not just the naive figure. **Not yet implemented** -
    next step is blind-TDV per the contract's own Test strategy section,
    matching Contract 4's precedent for a Stage-1-resilience-call-site
    change.
- 2026-08-16 (later still) — **External 8-point "research round + convergence
  loop" proposal reviewed: research -> panel -> real LLM council, unanimous
  reject-the-architecture / adopt-the-kernel verdict. Run in parallel with
  Contract 5's in-flight blind-TDV workflow (different files, no collision;
  no repo edits made by this pass beyond this ledger entry and the new spec
  below).**
  - **The proposal**: independent-vs-shared web search for a new "research
    round," a dedup/merge pass on models' surfaced "open questions," a
    debate round feeding each model its own answer + all peers' answers +
    merged questions (+ shared sources), and a stopping rule tracking the
    COUNT of open questions round over round - keep looping while flat/
    growing.
  - **Grounding check before any panel**: this project has rejected
    unconditional/iterative multi-round revision three separate times,
    citing ARMOR-MAD (arXiv:2606.13197), "Revision or Re-Solving?"
    (arXiv:2604.01029), "Deliberative Illusion" (arXiv:2606.03032) - most
    recently in `docs/specs/human-debate-characteristics-contract.md`:
    "Not adopted." The proposal's debate+convergence-loop core is
    structurally close to that rejected pattern.
  - **Fresh literature re-check** (direct arXiv abstract/section reads):
    ARMOR-MAD's own method IS a genuinely signal-gated bounded loop (agreement
    score per round, stop at threshold, hard cap `T_max`) - its central
    finding (conditional beats fixed-round debate) really does distinguish a
    gated loop from what was rejected. BUT its validated signal is
    agreement/consensus, NOT "open-question count" (unvalidated, no support
    in any cited paper here); and "Revision or Re-Solving?"/"Deliberative
    Illusion" are agnostic to gating mechanism - their risks (second-pass
    gains are often just re-solving; factual attrition compounds across
    rounds) apply to whatever happens INSIDE a round regardless of how it's
    triggered, so a gated loop is only safe if each round stays narrow/
    constrained, matching this project's existing Stage 2.75
    (`scripts/revision_round.py`: single-pass, citation-gated, "others
    agree" explicitly disallowed as a reason to switch).
  - **5 live-verified technical findings** (research workflow, direct
    fetches, not docs prose): (1) only Google documents its own web-search
    backend ("Grounding with Google Search," `ai.google.dev`) - Anthropic
    and OpenAI's own docs name NO backend at all, so `engine`-pinning can't
    verifiably fix the cross-model evidence-consistency confound for 2 of 3
    web-search models; (2) `engine` has 6 real values (`auto`/`native`/
    `exa`/`firecrawl`/`parallel`/`perplexity`), forcing a shared non-native
    engine is supported but not a clean cost win (`exa` $0.007-0.015 vs
    native $0.01-0.014; `parallel` $0.001-0.005 is the cheap option but
    quality/recency tradeoffs are undocumented); (3) `response_format` +
    `tools` (web_search) combine in one request with no documented
    restriction, but per-model support is ENDPOINT-ROUTING-DEPENDENT -
    `openai/gpt-5.5` loses structured-output support on its Bedrock
    endpoint, `z-ai/glm-5.2` is inconsistent across 43 endpoints (16 missing
    `structured_outputs`) - would need `provider` pinning to guarantee,
    itself new scope Contract 5 already deferred; (4) the "Response
    Healing" plugin's exact request shape is `"plugins":
    [{"id": "response-healing"}]`, requires `response_format`
    (`json_schema`/`json_object`) AND non-streaming to activate - JSON
    syntax repair only, never schema/field correctness; (5) this session's
    `session_id`-only prompt-cache activation lives INSIDE the installed
    package's `build_openrouter_payload` - it does NOT automatically extend
    to any new project-owned raw-HTTP call site (any future contract needs
    its own explicit `session_id`).
  - **Panel verdict** (9 personas: standing quartet + guardrail trio +
    Scientist + Backend, pulled in for the literature-adjacent claim and the
    new API-contract-design question): **13 concerns raised, 10 resolved, 3
    needing user decision.** Reject the full architecture; this project's own
    docs already flagged, but never specced, the actual literature-supported
    kernel - "extend Stage 2.75 with ARMOR-MAD-style Pre-debate Agreement
    Routing... as one small additive function, not a rewrite"
    (`docs/upstream-deltas.md`, earlier entry). Gate on CSS (already
    validated, already computed), never "open-question count." No new
    search step needed for the gate itself - descope search from this
    change entirely. Skip `response_format`/Response Healing/structured
    `open_questions` (no consumer under the smaller scope; would need
    undelivered provider-pinning). Skip partial engine-pinning as a
    "consistency guarantee" (only verifiably true for 1 of 3 vendors).
  - **Real LLM council verdict** (`mcp__llm-council__consult_council`,
    `confidence=high`, $0.4731/73.1k tokens - chairman synthesis fired
    correctly this time, unlike the earlier partial/fallback result):
    **unanimous across all 4 models on every one of 5 questions**, matching
    the panel exactly. Two things the real council added beyond the panel:
    (1) Claude Opus (top-ranked, 0.889) - the single most load-bearing
    safeguard: the agreement gate must decide WHETHER the existing narrow
    revision runs, never change WHAT that revision is allowed to do when it
    fires - loosening the citation-gated constraint when the gate trips
    would silently reopen the exact risk this project already spent effort
    avoiding; (2) GLM-5.2/GPT-5.5's compromise on engine-pinning - don't ship
    a fake guarantee, but DO log per-vendor search-backend knowledge (known/
    unknown per vendor) as an acknowledged confound in eval output - cheap,
    honest, worth doing whenever search is eventually built (not now, since
    search itself is descoped from this change).
  - **Decided** (delegated to this session, "decide as apt" per explicit
    user instruction): adopt the small Pre-debate Agreement Routing
    extension to Stage 2.75, gated on existing CSS, with Opus's
    whether-not-what safeguard as a hard requirement. Reject: the full
    research-round architecture, "open-question count" as any kind of
    signal, pooled/shared search, partial engine-pinning as a shipped
    "guarantee," `open_questions` structured output. Defer indefinitely (no
    concrete consumer, no assigned priority): per-vendor search-backend
    confound logging, and GPT-5.5's minority "test the full architecture
    offline behind a research flag, never production" suggestion - noted as
    a reasonable future option, not scheduled.
  - **Sequencing**: spec written now (`docs/specs/stage2-75-agreement-
    routing-contract.md`) - doesn't touch `scripts/council_adapter.py`/
    `scripts/live_adapters.py`, so no collision with Contract 5's in-flight
    diff. Implementation deliberately NOT started this pass - waits for
    Contract 5 to land and be verified clean, since Agreement Routing's
    natural landing site (`pipeline_runner.py`'s stage-2 orchestration)
    shares enough surface with Contract 5's changes to warrant sequencing
    rather than parallel implementation.
- 2026-08-16 (later) — **Contract 5 (Stage-1 web search) landed via
  blind-TDV. Workflow reported `PASS: false`; verified directly rather than
  accepted or silently overridden, per this project's established Contract-4
  practice.**
  - **What the workflow reported**: 89 mutants scoped (`scripts/
    live_adapters.py` + `scripts/council_adapter.py`), 24 killed, 65
    survivors grouped into 7 "survived"-status entries (41 individual
    mutants, spanning `_post_chat_completion`, `real_fetch_evidence`,
    `real_fetch_live_model_ids`, `_source_is_reachable`,
    `_load_debate_resilience_config`, `_resolve_response_labels`,
    `_normalize_stage2_for_stage3`) plus 3 "equivalent"-status entries (24
    individual mutants). The workflow's own pass/fail gate counts any
    non-"equivalent" entry as a real gap regardless of justification text,
    producing `realSurvivors: 7` and `PASS: false`.
  - **Verified directly, not taken on trust**: `git diff -U0` on both
    changed files confirms all 7 "survived"-status functions have ZERO diff
    hunks - genuinely untouched by this contract, pre-existing survivors
    from before this session (same "OUT OF SCOPE" pattern Contract 4
    accepted for 63 of its own 85 initial survivors). Spot-checked one
    equivalent-mutant justification against the real source
    (`CacheContext(segments=[], session_id=...)` vs. `segments` omitted) -
    matches this session's own earlier direct verification that
    `CacheContext`'s dataclass default for `segments` is also `[]`,
    confirmed identical behavior. The math is exact: 41 out-of-scope + 24
    equivalent = 65 = 89 total - 24 killed - every single surviving mutant
    is accounted for, zero unaddressed real gaps in substance, even though
    the workflow's blunt entry-count metric reported `PASS: false`.
  - **Read the actual implementation directly** (not just the agent's
    summary): `scripts/live_adapters.py::_extract_web_search_provenance`
    and `query_model_with_status_and_effort`'s `enable_web_search` branch
    match the contract's ACs 1-6 exactly - byte-identical payload when
    disabled, correct `tools`/`max_tool_calls` shape when enabled, correct
    three-state provenance classification. `scripts/council_adapter.py`'s
    `_STAGE1_WEB_SEARCH_ENABLED_MODELS` set and `_stage1_query_fn` wiring
    match ACs 7-9 exactly - `z-ai/glm-5.2` never in the set,
    `_stage1_query_fn`'s call signature unchanged.
  - **Full suite**: 871 passed (828 baseline + 43 new tests from the blind
    test author's `tests/test_web_search_stage1_contract.py`), 0 failures.
  - **Verdict: accepted as a genuine pass.** The web-search capability
    exists, is tested, and is mutation-verified for its own new code - the
    reported `PASS: false` was a reporting-granularity artifact (grouping
    by function/entry rather than counting genuinely-addressed vs. genuinely-
    open mutants), not an actual defect. Not committed - working-tree
    changes only, per this session's practice of leaving commits to
    explicit user request.
  - **Still pending, unchanged from the contract's own Rollout precondition
    (Pillar 6, stricter than Contract 4's)**: the fact-vs-judgment
    measurement dry-run gating first real-decision use, per the user's
    explicit 2026-08-16 decision. Not run this pass - implementation only.
- 2026-08-16 (later still) — **Attempted to write the Pre-debate Agreement
  Routing spec decided above; found a real architectural mismatch before
  writing any code, so nothing was specced or built.**
  - CSS (`metadata["quality_metrics"]["core"]["consensus_strength"]`,
    confirmed via `pipeline_runner.py:437`) is computed FROM Stage 2's peer
    rankings (`calculate_quality_metrics(stage2_rankings=...)`) - it cannot
    exist before Stage 2 runs. The original ARMOR-MAD-aligned idea this
    session's panel/council endorsed ("skip Stage 2/3 entirely when
    Round-0 [Stage 1] responses already agree", `docs/upstream-deltas.md`'s
    earlier entry citing ARMOR-MAD) needs an agreement signal computed from
    STAGE 1 ALONE, before Stage 2 exists - CSS cannot serve that role no
    matter how the code is arranged.
  - Both the internal panel and the real LLM council answered "(2) use the
    existing consensus-strength score, not open-question count" to the
    general gating-signal question - correct for what's ALREADY BUILT
    (Stage 2.75's existing `should_trigger_revision(css)` gate,
    `scripts/revision_round.py`), but neither actually designed a new
    Stage-1-only agreement metric, which is what a genuine pre-Stage-2 skip
    would require. Answering "use CSS" for a question that implicitly
    assumed CSS could gate something earlier than it structurally can was
    an unexamined gap in both review passes, not caught until specing
    started.
  - **Conclusion: nothing new to build.** The existing Stage 2.75 mechanism
    (single-pass, citation-gated, triggered only when CSS < 0.50) already
    correctly implements the literature-validated kernel - "skip/limit
    revision when consensus is already strong" - confirmed correct by two
    independent review passes plus the real council, with no code changes
    needed. The more ambitious "skip Stage 2 peer-review entirely before it
    runs" version remains a genuinely distinct, not-yet-designed
    possibility - it would need its own fresh grounding pass to define what
    a Stage-1-only agreement signal even measures (e.g. embedding
    similarity across the 4 draft texts, a cheap classifier call, or
    something else - unexamined) before it could be specced, let alone
    built. Not pursued this session - out of scope for what was actually
    decided (adopt the small, already-validated kernel; reject the
    speculative larger architecture), and inventing a new metric now would
    itself be exactly the kind of unvalidated-signal risk this session's
    literature re-check warned against.
  - No files changed by this entry beyond this ledger note - `docs/specs/
    stage1-web-search-contract.md`/Contract 5 remain the only spec/code
    output from this session's second research-panel-council pass.
- 2026-08-16 (final) — **Both pending rollout dry-runs executed for real,
  against live OpenRouter. One clean positive result; one real, material
  cost-model correction that changes Contract 5's economics - reported
  honestly, not smoothed over, per this project's own established practice.**
  - **Prompt-cache session-affinity dry-run** (per its own Rollout
    precondition): ran `council_adapter.run_council_with_timeouts()` twice,
    same low-stakes query ("single requirements.txt vs. requirements.txt +
    requirements-dev.txt for a solo dev"), once with `set_cache_context`/
    `clear_cache_context` monkeypatched to no-ops (reproducing the exact
    pre-change request shape) and once with the real, shipped code.
    **Result: real, measurable saving.** Without: $0.3736, 62,263 tokens.
    With: $0.3276, 55,794 tokens - a **12.3% cost reduction** ($0.046
    saved this trial), cached-token count rose 2,917 -> 4,669 (consistent
    with the sticky-routing mechanism actually engaging), latency
    unaffected (178.7s vs 179.1s, no regression). CSS differed between the
    two runs (0.682 vs 0.438) but per this project's own established
    Contract-4-dry-run lesson (`docs/pipeline-architecture-spec.md` §9),
    CSS measures cross-model ranking agreement, not correctness/quality -
    this single n=1 trial's CSS delta is NOT claimed as evidence the
    change affected debate quality either direction, only that the cost/
    latency/cache-hit numbers are real. **Verdict: the expected saving is
    confirmed real, not merely assumed - safe to cite going forward.**
  - **Contract 5 web-search dry-run** (per its own, stricter Rollout
    precondition): ran direct Stage-1-only calls (bypassing Stage 0.5/2/3,
    matching Contract 4's own dry-run method) for all 3 enabled models
    (`claude-opus-4.8`, `gpt-5.5`, `gemini-3.7-flash`), once with
    `enable_web_search=False` (baseline) and once `True` (search), same
    low-stakes query (Python packaging-tool choice for a solo maintainer -
    chosen specifically because it has both a genuine judgment component
    AND a factual/currency component a real search could resolve
    differently).
    - **(a) Content convergence - real read of the text, not a CSS proxy,
      per the contract's own AC**: partial and narrow, not wholesale.
      All 3 models ALREADY independently agreed on the headline
      recommendation (`uv`) even at baseline, before any search happened -
      search didn't manufacture that agreement, it was already there.
      What DID measurably narrow: the SPECIFIC build-backend sub-
      recommendation. Baseline responses hedged/varied (`hatchling` vs.
      `hatchling or uv_build` vs. `uv_build` specifically); with search,
      the two models that actually searched (Claude, GPT-5.5) both
      converged tightly onto naming `uv_build` specifically, citing
      overlapping real sources (`packaging.python.org`, `docs.astral.sh`).
      Gemini, which had access but chose NOT to search this round
      (`web_search_provenance: enabled_no_search` - a real, working
      instance of that state), kept its baseline framing (`hatchling`)
      unchanged - so search-enabled models ended up MORE distinct from the
      non-searching model on this sub-point than the 3 baseline models
      were from each other. Each model's overall reasoning style,
      structure, and secondary framing (Claude's "not having to decide is
      a feature" editorial point; GPT-5.5's explicit A/B choice table)
      stayed distinct in both conditions. **This matches, with real
      evidence, the theoretical prediction from this session's earlier
      arXiv:2603.05293 re-grounding: homogenization from shared knowledge
      is partial/dimension-specific, not wholesale - here it concentrated
      on one current, verifiable fact (a backend's name), not on judgment
      or reasoning style.**
    - **(b) Search behavior**: confirmed all 3 provenance states work
      correctly on real responses - `enabled_searched` (Claude: 1 query,
      5 sources; GPT-5.5: 1 query, 3 sources) and `enabled_no_search`
      (Gemini, had access, chose not to use it this round) both observed
      for real, not just in unit tests.
    - **(c) Cost - a real, material correction to AC 10's ceiling
      framing, not just a bigger number than expected.** Direct inspection
      of the raw `usage` dict (not just the top-line `cost` field) shows
      the dominant cost driver is NOT the flat per-search-call fee AC 10's
      ceiling was built around - it's **extra PROMPT tokens from the
      fetched search-result content being injected into the model's own
      context**, billed at that model's normal (often expensive) per-
      prompt-token rate. Claude Opus's prompt tokens jumped 85 -> 13,837
      tokens when search fired (GPT-5.5: 57 -> 10,918) for a SINGLE search
      call each. Real per-round cost this trial: $0.107 (baseline, 3
      models) -> $0.254 (search, 2 of 3 actually searched) - a **~$0.147
      delta**, well above AC 10's naive ($0.034) or even 2x-observed
      ($0.068) ceilings, both of which only ever accounted for the flat
      search-tool fee. **`max_uses` does not meaningfully bound this cost
      driver** - the expensive part is proportional to how much RESULT
      CONTENT one search call returns (`max_results`, default 5, matches
      Claude's 5 citations from its 1 permitted call here), not how many
      calls are made. A future revisit of Contract 5's cost controls
      should look at `max_results`/`max_characters`/`search_context_size`
      tool parameters, not just `max_uses`, if tighter cost bounding is
      wanted - not implemented this pass, flagged as a real, undelivered
      gap in the shipped contract's cost-control design, not silently
      absorbed.
  - **Neither dry-run's code was changed as a result of these findings**
    (both contracts already shipped and pass their own tests/mutation
    gates) - this entry is the required, honest reporting step per each
    contract's own Rollout precondition, including the null/inconclusive
    and worse-than-assumed parts, not just the confirming ones. Both
    capabilities remain available for real use; the corrected cost
    expectation for Contract 5 (materially higher than AC 10 states) should
    be read alongside the contract before relying on its documented
    ceiling for a real budget decision.
