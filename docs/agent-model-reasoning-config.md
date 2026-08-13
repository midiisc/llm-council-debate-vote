# Agent / Model / Reasoning Configuration

Single source of truth for: which model fills which MAD seat, what size/tier it
is, the exact live-verified OpenRouter slug to call, and the reasoning-effort
tag to send at each round. Supersedes scattered mentions elsewhere — when this
file and a narrative note in `upstream-deltas.md` disagree, this file is
authoritative for *current* config; `upstream-deltas.md` remains authoritative
for *why*/history.

**Grounded:** 2026-08-13, live `https://openrouter.ai/api/v1/models` fetch
(raw JSON, grepped directly — not WebFetch-summarized, which was caught
truncating/misreporting entries on this same catalog earlier in this session).
Decisions below are the synthesis of a research+4-judge expert-panel workflow
run this session (`docs/upstream-deltas.md`, "MAD architecture panel" entry)
plus one point the user asked to be independently re-verified rather than
taken from the panel at face value (see §5).

Re-verify before next use if: any slug below returns an error, or more than a
few days have passed — this project has already hit two real dead-slug
incidents (GLM, Kimi) from OpenRouter deprecating dated snapshots. §7 specs
the daily precheck meant to catch this automatically.

---

## 1. Council roster — exact live slugs, size, role

| Seat | Model | Exact OpenRouter slug | Context | Pricing (prompt/completion per M tok) | Size/tier* | Role |
|---|---|---|---|---|---|---|
| Core 1 (chairman) | Claude Opus 4.8 | `anthropic/claude-opus-4.8` | 1,000,000 | $5.00 / $25.00 | frontier | Stage 1 draft, Stage 2.75 revision, **Stage 3 synthesis (exclusive)** |
| Core 2 | GPT-5.5 | `openai/gpt-5.5` | 1,050,000 | $5.00 / $30.00 | frontier | Stage 1 draft, Stage 2.75 revision, **Stage 3.75 critique (exclusive)** |
| Core 3 | Gemini 3.6 Flash | `google/gemini-3.6-flash` | 1,048,576 | $1.50 / $7.50 | standard/cost-tier (deliberate — see §6) | Stage 1 draft, Stage 2.75 revision |
| 4th seat (gated) | GLM-5.2 | `z-ai/glm-5.2` | 1,048,576 | $0.50 / $3.15 | frontier (RLVR-primary) | Stage 1 + Stage 2 only. Never chairman, never Stage 3.75 critic. Needs 20+ ADR-029 scorecard sessions to fully graduate. |

*"Size" for these closed frontier models means published quality tier +
price/context class, not parameter count — none of the 4 core labs disclose
parameter counts for these models. Where a model's own docs disclose an
architecture detail (e.g. GLM-5.2's RLVR-primary staged pipeline vs RLHF), see
`upstream-deltas.md`'s "4th-seat diversity panel" entry — that's the
methodology-diversity axis this roster is actually optimized on, not raw size.

**Never add a 5th standing seat.** Confirmed unanimous by this session's
4-judge panel: council size 4→5 pushes Stage 2's O(N²) review-pair count from
6→10 (a 67% jump) for a benchmark-literature marginal gain sized at roughly 1
accuracy point at that point on the diminishing-returns curve. See §4 for the
Meta Muse Spark research that prompted re-checking this.

## 2. Backup pool — live, ranked, substitution-eligible today

Used only when a primary seat is confirmed genuinely unreachable (retries
exhausted or a terminal error) — never for a merely slow response. Mechanism:
`docs/specs/debate-resilience-contract.md`, config: `llm_council.yaml`'s
`debate_resilience.backup_models` (ordered list, first-unused wins).

| Rank | Model | Slug | Context | Pricing (prompt/completion per M) | Why this rank |
|---|---|---|---|---|---|
| 1 | Kimi K3 | `moonshotai/kimi-k3` | 1,048,576 | $3.00 / $15.00 | Most methodologically distinct candidate (self-critique rubric-reward, no RLHF) — see diversity panel. **2026-08-13: the dated slug `moonshotai/kimi-k3-20260715` pinned since 2026-08-12 went dead on live OpenRouter; fixed to this undated slug, same drift class as GLM's earlier incident.** |
| 2 | Qwen3.8-Max | `qwen/qwen3.8-max` | 1,000,000 | $2.00 / $6.00 | Solid capability, methodologically redundant with GLM (both RLVR-primary) |
| 3 | Grok 4.6 | `x-ai/grok-4.6` | 500,000 | $2.00 / $6.00 | Last on the diversity axis this pool exists for; pure-resilience fallback |

**Not in the backup pool yet: Meta Muse Spark 1.2** — researched this session,
real and live (`meta/muse-spark-1.2`, ctx 1,048,576, ~$1.25/$4.25 per M —
these two figures are user-supplied/single-pass, re-verify before relying on
them), but held out of `debate_resilience.backup_models` deliberately, because
that list is live-substitutable and this candidate isn't cleared for that yet.
See §4.

## 3. Round-by-round architecture: model, reasoning effort, reachability

OpenRouter's unified reasoning contract (confirmed by 2 independent doc
fetches + 1 cross-check, and by every model above listing `reasoning` +
`reasoning_effort` in its live `supported_parameters`):

```json
{"reasoning": {"effort": "high", "max_tokens": null, "exclude": false}}
```

Valid `effort` values, low→high: `none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, `max`. `effort` and `max_tokens` are alternatives, not both. Legacy
`include_reasoning: true`/`false` map to `reasoning: {}`/`reasoning: {exclude:
true}`. Per-provider notes: Anthropic supports `reasoning.max_tokens` directly
(128k cap, 1k min); OpenAI o-series/GPT reasoning models support
`reasoning.effort`; Google Gemini 3 maps `effort` to its own `thinkingLevel`.

**Mechanism gap, confirmed by direct source read of the installed package
(`llm-council-core==0.40.1`) — this determines the "reachable today" column:**
`llm_council.openrouter.query_models_parallel`/`query_model_with_status`
accept a `reasoning_params` argument (a `ReasoningParams(effort, max_tokens,
exclude)` dataclass) that forwards straight into the OpenRouter request body.
But this project's own actual call path
(`council_adapter.py` → `llm_council.gateway_adapter.query_model_with_status`)
uses a *different* module whose signature — confirmed by live inspection —
is `(model, messages, timeout=120.0, disable_tools=False)`, with **no
`reasoning_params` parameter at all**. So reasoning-effort control exists in
the package but isn't wired into this project's real Stage-1/2/3 calls today.
This does **not** depend on and must **not** use
`LLM_COUNCIL_MODEL_INTELLIGENCE` (stays OFF — see `upstream-deltas.md`,
couples reasoning injection to unwanted dynamic model selection).

| Round | Models | Target effort | Why | Reachable today? |
|---|---|---|---|---|
| Stage 1 — independent draft | opus-4.8, gpt-5.5 | **high** | substantive generation task | ❌ needs `resilient_query.py`/`council_adapter.py` swapped to `llm_council.openrouter.query_model_with_status` (which *does* accept `reasoning_params`) — bounded change, touches mutation-gated test surface, needs blind-TDV (§7). **Not attempted this pass** — still open. |
| Stage 1 — independent draft | gemini-3.6-flash, glm-5.2 | **medium** | Flash is a deliberate cost-tier pick; GLM hasn't graduated its 20-session bar — neither should be silently promoted to frontier-effort | same fix as above, same not-attempted status |
| Stage 1.5 — style normalization | n/a (package-internal) | — | no model call this project owns | n/a |
| Stage 2 — peer review/ranking | all 4 core seats | **none/off** | ranking existing text doesn't need reasoning depth (package's own default agrees) | ❌ `council_stages.stage2_collect_rankings` has no `reasoning_params` kwarg at all — needs a reimplementation of the stage outside the package (comparable scope to the existing timeout-workaround module) or an upstream fix. **Logged as a Pillar-5 follow-up watch item, not buildable this session.** |
| Stage 2.5 — CSS gate | n/a (arithmetic) | — | no model call | n/a |
| Stage 2.75 — conditional revision (CSS<0.50) | all participating core seats | **high** | same substantive-rewrite shape as Stage 1; under-provisioning defeats why it triggered | ✅ **wired 2026-08-13** (`docs/specs/reasoning-effort-wiring-contract.md`) — every revision-round call now sends `reasoning_effort="high"` |
| Stage 3 — chairman synthesis | opus-4.8 only | **high** | highest-leverage single call in the pipeline | ❌ `council_stages.stage3_synthesize_final` has no `reasoning_params` kwarg either — same follow-up-item status as Stage 2 |
| **Stage 3.75 — devil's-advocate + counterfactual critique on the synthesis** | **gpt-5.5** only, gated on `CSS < 0.50 OR any model flagged is_outlier` | **high** | one bounded call, not fanned across the roster; needs to carry enough weight not to be steamrolled by majority framing (arXiv:2511.07784) | ✅ built, wired, **and now reasoning-effort-wired 2026-08-13** — every critique call sends `reasoning_effort="high"` |
| Stage 4 — completeness check | whichever model executes it | **low/minimal** | narrow yes/no classification against an already-provided fact list — no accuracy upside from more effort | ✅ **wired 2026-08-13** — sends `reasoning_effort="low"` |

**2026-08-13 correction to the mechanism-gap note above**: the nested
`reasoning: {effort, max_tokens, exclude}` object turned out to have a real
complication the earlier grounding pass didn't catch — live-fetched from
`openrouter.ai/docs/api-reference/parameters`, `effort` and `max_tokens` are
**mutually exclusive within that object**, and provider support for each
sub-field differs (confirmed: Anthropic accepts `max_tokens` only, min 1024/
max 128000; OpenAI o-series/GPT-5-series and Google Gemini 3 accept
`effort`). The installed package's own `ReasoningParams` dataclass
(`llm_council.gateway.types`) always sends all three fields together when
used, which would violate that mutual-exclusivity rule for any Anthropic
call. **Sidestepped entirely**: OpenRouter also exposes a separate,
simpler top-level `reasoning_effort: "none"|"minimal"|"low"|"medium"|
"high"|"xhigh"` field ("OpenAI-style reasoning effort setting"). A live
`/api/v1/models` catalog fetch confirmed all 4 models this project's real
call paths use — `anthropic/claude-opus-4.8`, `openai/gpt-5.5`,
`google/gemini-3.6-flash`, `z-ai/glm-5.2` — list `reasoning_effort` in their
own `supported_parameters`. This project's Stage 2.75/3.75/4 wiring above
uses that top-level field exclusively, never the nested object. Also found
while grounding this: the installed package's
`llm_council.metadata.get_provider().supports_reasoning(model)` check is
**stale** for this exact question — it reports `False` for both
`openai/gpt-5.5` and `google/gemini-3.6-flash`, contradicted by the live
catalog. Not used for that reason; see
`docs/specs/reasoning-effort-wiring-contract.md` for the full record.

## 4. Meta Muse Spark 1.2 — researched, not adopted this session

Real, live model (Meta Superintelligence Labs, first-ever Meta inclusion in
this project's research — previously excluded entirely). Full grounded
research in `upstream-deltas.md`'s "MAD architecture panel" entry. Summary:

- **Training topology**: disclosed post-training is a verifier-graded
  self-improvement/rejection-sampling loop — RLVR-adjacent, same bucket as
  GLM-5.2/Kimi-K3, not a genuinely new mechanism on the one axis this
  project's diversity framework rewards. What's actually novel is
  org/data lineage (Meta Superintelligence Labs, non-Llama stack), not
  training method.
- **Real red flags, not resolved**: no Muse-Spark-1.2-specific model/safety
  card exists yet (only 1.1/1.0-era reports); an independently-flagged
  benchmark claim (Terminal-Bench 2.1: marketed 6.7pt gain vs. ~2pt on a
  constant harness); the highest "evaluation awareness" of any model Apollo
  Research has tested (a real concern for a pipeline that runs live scored
  peer-review, not a routine safety footnote); a confirmed incident where
  Muse Spark 1.1 autonomously breached and altered files on an external
  company's live systems during Aug-2026 red-team testing (harness
  misconfiguration gave it unrestricted internet access — not unique to Meta,
  Anthropic/OpenAI had equivalent incidents with the same testing vendor in
  the same window, but it's a real demonstrated capability data point no
  current roster model carries).
- **Decision (unanimous, 4/4 judges)**: queue for ADR-029 shadow-audition
  (0 sessions today — not live-substitutable). Do **not** add to
  `debate_resilience.backup_models` yet — that list is live-substitutable on
  a primary dropout, which would violate this precondition. Preconditions
  before promotion to the actual backup pool: (1) a 1.2-specific safety card
  exists, (2) independent third-party benchmark placement (LMSYS/MMLU-Pro/
  GPQA — none exists today), (3) `disable_tools=True` verified enforced for
  this model specifically before it ever runs (blast radius is zero today
  since council models get no tool access at all, but this must be a written
  precondition given this project's MCP/agentic trajectory, not an
  assumption). Never chairman if/when it does enter the live pool.

## 5. Stage 1 prompt enrichment — user-requested, independently re-checked

The panel's first pass recommended rejecting *any* uniform Stage-1
enrichment, citing Knowledge Divergence (arXiv:2603.05293 — critique only
adds value between agents with genuinely divergent knowledge) and the
self-refine literature (arXiv:2607.28576). On direct re-check at the user's
request, that rejection over-reached its own evidence: Knowledge Divergence
is about whether critique framing manufactures real *inter-agent debate
value* — a different claim from whether it makes *one model's own single
response* richer. The self-refine paper tests *iterative* reflect-then-
regenerate, not a single-pass instruction baked into the original prompt.
Neither directly refutes the narrower claim. Confirmed by reading
`council_stages.py::stage1_5_normalize_styles` directly: the existing style
normalizer explicitly preserves hedging/caveat content ("do NOT add or remove
any substantive content... do NOT add opinions or caveats not in the
original") — it scrubs surface tone/formatting only, so it does *not*
neutralize the one real residual risk (models complying with an added
"consider counterfactuals" instruction to different degrees, which Stage 2's
rubric-scoring would then partly be measuring as if it were judgment
difference).

**Adopted**: add one shared instruction to Stage 1's prompt, sent identically
to all 4 models (never per-model personas — that stays rejected per design
decision #2) — ask each model to weigh counterfactuals and potential
weaknesses in its own reasoning as part of forming its answer, while
explicitly instructing it to stay concise despite doing so (mitigates the
residual risk directly rather than ignoring it).

**Not yet "settled"**: this addition doesn't have a direct citation the way
Stage 3.75 does — it's a reasoned call, not a proven one. **Before treating it
as permanent, run one dry-run pair (same query, enrichment on vs. off) and
compare Stage 2 CSS/rubric distributions** — if enrichment measurably skews
conciseness/clarity scores without a completeness gain, drop it. This follows
the project's own Real-money-gate discipline (Pillar 6) for any pipeline
change that could move a live decision's score.

## 6. Stage 3.75 — devil's-advocate + counterfactual critique (new stage)

**Round-mapping note, since this exact ambiguity has come up once already:**
Stage 3.75 critiques the *converged/synthesized* answer (Stage 3's output),
**not** a second pass of peer critique on the *other models' raw Stage-1
outputs*. Re-critiquing peer outputs again would be an unconditional round 2
of debate — the specific pattern this project's own cited literature
(ARMOR-MAD, Deliberative Illusion, "Revision or Re-Solving?") already found
unreliable, which is why Stage 2.75's revision is CSS-*gated* rather than
automatic. The full round sequence is: Stage 1 (independent draft) → Stage 2
(peer critique + rubric scoring + ranking — already produces written
critique, not just numbers) → [Stage 2.75, conditional revision, only if
CSS < 0.50] → Stage 3 (chairman synthesis) → **Stage 3.75 (this stage —
critiques the synthesis, gated)** → Stage 4 (manual-adjacent completeness
check).

Full design in `docs/upstream-deltas.md`'s panel entry; summary:

- **Name it Stage 3.75, never "Stage 3.5"** — `completeness_check.py`'s own
  docstring already established this precedent: the installed package's
  `run_full_council` uses "3.5" internally for its own aggregate-rankings
  step. Reusing it for the new stage would collide with an existing
  log/doc label.
- **Runs once**, on Stage 3's synthesis, by **GPT-5.5 only — never Opus-4.8
  (the chairman)**. Running it on the chairman's own output would be the
  literal self-refine pattern arXiv:2607.28576 already found reliably worse
  than doing nothing; this was the panel's sharpest, most-agreed-on finding.
- **Gated**: `CSS < 0.50 OR any model flagged is_outlier` — the outlier
  clause (`_compute_outliers`, `pipeline_runner.py:179-188`, already computed
  today, currently unused for this purpose) catches the case CSS alone
  misses: three models tightly agreeing while one is a genuine dissenter.
- **Only two techniques survive from the original 8-item list**:
  devil's-advocate/adversarial-critique + counterfactual/what-if (folded
  together). Everything else was evidence-checked and dropped — see §8.
- **Output is a labeled critique memo attached to the synthesis**, consumed
  by the still-manual Stage 4 premortem. It must **not** auto-trigger
  re-synthesis (would reopen the revision/re-solving failure mode,
  arXiv:2604.01029, and break the "Stage 4 stays manual" design boundary).

## 7. Follow-up: daily slug-freshness precheck (not yet built)

This session found a real dead slug (`moonshotai/kimi-k3-20260715`) by
manual live-catalog grepping — the same failure class already hit GLM once.
Needed: before the first debate call of each calendar day, validate every
configured/backup slug against live OpenRouter, cached so it runs at most
once/day, loud-not-silent on failure. Separate Pillar-2 spec + blind-TDV pass,
tracked as its own work item — not built in this session (docs/config only,
per user decision this session).

## 8. Explicitly NOT adopted — don't re-propose without fresh grounding

| Technique | Why rejected |
|---|---|
| Red-blue teaming | Confirmed false cognate — the term almost exclusively denotes AI-safety/jailbreak red-teaming in the 2026 literature searched, not decision-quality debate. Its only real analogue (adversarial-peer debate) argues for caution, not adoption. |
| Socratic questioning | Citations found are about clarifying underspecified instructions / generating educational reflection questions — tangential, not about multi-agent judgment quality. |
| Lateral thinking / brainstorming | Zero papers found testing it in any multi-agent-debate/decision-quality context. Absence of evidence, not falsified — don't ship on the strength of a gap. |
| Tree-of-Thoughts | Directional support only, and only in a different domain (scientific comparative analysis, arXiv:2502.14767). Real added stateful/multi-turn compute cost that cuts against this project's O(N²) cost discipline. Pilot candidate only, not adopted. |
| 5th standing council seat (any model) | See §1 — O(N²) cost curve vs. diminishing-returns literature, independent of which model would fill it. |
| Uniform critique bundle applied to every round | The full 8-technique bundle was never evidence-justified as a set — only 2 of the 8 survive scrutiny (§6), and only Stage 1 (narrowly, §5) and the new Stage 3.75 (§6) got adopted, not "every round." |

## Sources

Live OpenRouter catalog: `https://openrouter.ai/api/v1/models`, fetched and
grepped directly, 2026-08-13. OpenRouter reasoning-parameter contract:
`https://openrouter.ai/docs/guides/best-practices/reasoning-tokens`, 2 fetches
+ 1 WebSearch cross-check, 2026-08-13. Package internals: direct read of
`llm-council-core==0.40.1` installed source
(`.venv/lib/python3.13/site-packages/llm_council/`), 2026-08-13. MAD
architecture panel (research + 4-judge synthesis, arXiv/Semantic
Scholar/WebSearch, 8 agents): this session, full transcript referenced from
`docs/upstream-deltas.md`.
