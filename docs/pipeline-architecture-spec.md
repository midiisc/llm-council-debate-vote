# Pipeline Architecture Spec

Status: **RESOLVED — both gaps decided 2026-08-09, see §4.** Written before
any config/code change, per Pillar 2 (Spec-Driven Development). All facts below are grounded
against `llm-council-core==0.40.1` installed source
(`~/.local/share/uv/tools/llm-council-core/`) and the live OpenRouter catalog,
retrieved 2026-08-09 — cited inline, nothing inferred from the original setup
doc without re-verification.

## 1. Runtime architecture — corrected from the original setup doc

**Finding: the doc's `llm_council.yaml` example does not do what it implies.**

The doc's example config sets `council.tiers.pools.high.models`. Reading the
installed source (`unified_config.py`, `council.py`, `mcp_server.py`) shows two
separate, only loosely-related systems:

- **`council.models` (flat list) + `council.chairman` (single string)** — this
  is what `consult_council` actually uses. `mcp_server.py:101` sets
  `COUNCIL_MODELS = _get_council_models()` at module load, which reads
  `config.council.models` directly (`council.py:70`). `council_health_check`'s
  `council_size` is `len(COUNCIL_MODELS)` — this exact field.
- **`council.tiers.pools.<tier>.models`** — read only by the triage/
  complexity-classification layer (`triage.enabled`, default `false` per the
  module docstring) and by `frontier_fallback.py`'s fallback chain. Under
  default settings (triage off), **this block is inert** — it has no effect
  on which models `consult_council` actually queries.

**Consequence:** if we'd followed the doc's example verbatim, `consult_council`
would have silently run against `CouncilConfig.models`'s hard-coded default
(`openai/gpt-5.4`, `google/gemini-3.1-pro-preview`, `anthropic/claude-opus-4.8`,
`deepseek/deepseek-v4-pro`) — not the Claude/GPT/Gemini pool the doc's `yaml`
example appeared to configure, and silently including DeepSeek, which was never
asked for. This is exactly the class of error Pillar 1 exists to catch.

**Decision:** configure via `council.models` + `council.chairman` directly.
Leave `tiers`/`triage` unset (defaults: triage disabled) unless a future need
for complexity-based routing is specced separately.

Recorded in `docs/upstream-deltas.md`.

## 2. Council composition contract (per user's standing design decision)

- **Ceiling: 4 models, never more.** Rationale recorded verbatim from the
  user: past 3 labs, distinctness runs out (documented distillation lineage
  risk among open-weight models → correlated copies, not real diversity);
  review cost is O(N²); interpretability degrades past 3-4 voices for a
  founder reading a synthesized memo; actual decision cadence (2-4/month)
  doesn't need a large standing council.
- **3 permanent core, never rotated out:**
  | Model | Role | Slug status |
  |---|---|---|
  | Claude Opus 4.8 | Core + **chairman** (decided §4) | `anthropic/claude-opus-4.8` — confirmed live on OpenRouter (2026-08-09) |
  | GPT-5.5 | Core | `openai/gpt-5.5` — confirmed live on OpenRouter (2026-08-09) |
  | Gemini 3.6 Flash | Core (interim — decided §4) | `google/gemini-3.6-flash` — confirmed live on OpenRouter (2026-08-09) |
- **1 experimental slot, gated:** GLM-5.2, slug `z-ai/glm-5.2-20260616`
  (confirmed live on OpenRouter, 1.05M context, $0.098/M prompt / $0.308/M
  completion). Stage 1 (independent draft) + Stage 2 (peer review/voting)
  only. **Never chairman, never tie-breaker/judge**, until it clears 20+
  scorecard sessions at moderate-or-higher confidence — see §4.
- **If GLM-5.2 doesn't clear the bar:** drop back to the 3-model core. Do not
  reflexively backfill with a different 4th model (user's explicit
  instruction — a proven 3 beats a padded, unproven 4).
  **Superseded 2026-08-12, see below** — this line governed replacing the
  4th *permanent seat*; it does not apply to the new backup pool, which
  exists purely to cover a transient session-level dropout, not to
  reconsider who holds a seat.

### Superseding decision (2026-08-12): 3-model backup pool added

Prompted by a reported live incident where a debate ran with only 3 of 4
models because one dropped on timeout with no retry (root-caused in
`docs/upstream-deltas.md`, "Debate resilience" entry — the dropped model
was never actually a configured seat, it was package-default leakage from
an already-fixed config bug, but the underlying "no retry, no backup, silent
drop" gap was real and is fixed by this decision).

**User explicitly reversed the no-backfill line above** (asked directly,
confirmed 2026-08-12): the 3 permanent core + 1 gated-experimental seats are
unchanged, but a session that can't reach 4 live models from those 4 alone
now gets backup candidates to try before giving up a seat, rather than
silently degrading. Substitution only triggers when a primary seat is
confirmed **genuinely unreachable** (retries exhausted or a terminal error
like an auth failure) — never for a merely slow response that would have
succeeded on retry. If every configured backup is also unreachable, the
session proceeds degraded rather than hard-failing, but with a loud,
non-silent shortfall warning (user's explicit choice — silent degradation
is exactly the failure mode that prompted this change). Full behavioral
contract: `docs/specs/debate-resilience-contract.md`.

### 4th-seat diversity panel (2026-08-12): GLM-5.2 kept, backup pool re-ranked and expanded to 3

Separately from the resilience fix above, the user asked a harder question:
of the candidates researched for the backup pool, is GLM-5.2 actually the
*best* 4th seat on the specific criterion this project cares about —
maximizing training-corpus/training-methodology diversity against the 3
Western RLHF-aligned core seats — or would a swap be better, with the
losers becoming backups? Ran as a grounded 3-lens judge panel (corpus
diversity, alignment-methodology diversity, practical debate-capability
fit — deliberately blind to diversity), each lens scoring GLM-5.2, Grok
4.6, Qwen3.8-Max, and Kimi K3 (Moonshot AI, newly researched for this
question). Full grounded research, judge rankings, and synthesis reasoning
recorded in `docs/upstream-deltas.md`, "4th-seat diversity panel" entry —
summary here.

**Panel finding:** 2 of 3 lenses (both diversity-focused) independently
ranked **Kimi K3** first — not on nationality, but on alignment
*methodology*: Kimi's post-training replaces human-preference RLHF almost
entirely with a self-critique rubric-reward loop and trains 9 separate
task-expert models merged by distillation, a genuinely different training
*topology* from the RLHF/RLVR-plus-late-human-preference-stage recipe every
other candidate (including GLM and Qwen, which cluster closely with each
other) still uses. Grok ranked *last* on diversity specifically — its own
model cards describe an RLHF/RLAIF/LLM-judge pipeline built in part on
Anthropic's own Petri audit tooling, structurally the same paradigm as the
3 incumbent seats; its differentiation is deployment-time tone/content-policy,
not training corpus or method. The 3rd lens (practical capability, diversity
set aside) ranked Kimi *last* — real, honestly-disclosed risk: middling
general-purpose debate Elo relative to its coding-specific strength, a
directional (single-source, unconfirmed) hallucination-rate gap, and
Moonshot's own docs suggesting an agentic-harness-optimized design rather
than bare-prompt debate use.

**Decision (user, asked directly, not guessed):** **keep GLM-5.2 as the
primary 4th seat** — the lower-risk, already-verified-quality pick over the
panel's diversity-maximizing recommendation. The panel's own backup ranking
is adopted instead of its primary-seat recommendation, expanding the backup
pool from 2 to 3 (the resilience engine's `backup_models` was always a
plain ordered list, not hardcoded to 2 — see
`docs/specs/debate-resilience-contract.md` — so this required no contract
change, only a config update):

| Backup rank | Model | Lab | Slug | Why this rank |
|---|---|---|---|---|
| 1 | Kimi K3 | Moonshot AI | `moonshotai/kimi-k3-20260715` (dated slug — re-verify before ever promoting out of backup) | Most methodologically distinct candidate overall; the panel's top pick for the seat itself |
| 2 | Qwen3.8-Max | Alibaba | `qwen/qwen3.8-max` | Solid balanced capability, but methodologically redundant with GLM (both RLVR-primary + late human-preference stage) — adds less orthogonal signal as a 2nd backup than a 1st pick would |
| 3 | Grok 4.6 | xAI | `x-ai/grok-4.6` | Last on the diversity axis this pool exists for; kept as pure-resilience fallback only |

**Not treated as closed:** GLM-5.2 still needs its 20-session ADR-029
graduation bar; if it doesn't clear it, this panel's finding — that Kimi
K3 is the strongest diversity-maximizing alternative, not a reflexive
different-4th-model swap — is the grounded starting point for that
follow-up decision, not a fresh unguided search.

This does not change §2's seat-composition rules above (GLM-5.2 still needs
20+ sessions to graduate, still never chairman) — the backup pool is a
resilience mechanism for a single session's dropout, not a change to who
holds a permanent or experimental seat.

## 3. Custom scripts still needing their own spec (Pillar 2/3 — not yet written)

Per Pillar 2, none of these get code until each has its own
Given/When/Then spec and a blind-TDV pass (isolated test author from contract
only → RED → minimal GREEN → mutation gate, 0 survivors, `mutmut` — lightest
maintained option for this stack, `cosmic-ray` is heavier setup for equivalent
coverage on a script this size; flag if you'd rather use `cosmic-ray`):

1. `grounding_pass.py` (Stage 0.5 — fact verification)
2. `revision_round.py` (Stage 2.75 — correction-biased revision)
3. Scorecard wrapper + `scorecard` report command (per-session model
   performance tracking for the GLM-5.2 gate) — appends to
   `~/.llm-council/model_scorecard.jsonl`, reads confidence tiers (<10
   insufficient / 10-19 preliminary / 20-49 moderate / 50+ high) from the
   installed package's own bias-audit module (need to confirm those exact
   thresholds live in `unified_config.py`'s `BiasConfig`/`AuditionConfig`
   rather than assuming the doc's numbers — another grounding check before
   the spec is final)

Each gets its own spec doc under `docs/specs/` before implementation starts.

## 4. Gap resolutions (decided 2026-08-09)

- **Gap 1 (Gemini seat):** no "Pro"-tier Gemini 3 text/chat model currently
  exists on OpenRouter (only `gemini-3.6-flash`, `gemini-3.5-flash-lite`,
  `gemini-3-pro-image` [multimodal, wrong shape for this]). **Decision: use
  `google/gemini-3.6-flash`** as the interim 3rd-lab core seat — current
  Google flagship, just not badged "Pro." Pillar 5's self-update routine
  watches for a Pro-tier slug reappearing; revisit then, not before.
- **Gap 2 (chairman):** **Decision: Claude Opus 4.8** holds chairman/synthesis
  duty — matches the user's own stated rationale for including it ("strongest
  track record on nuanced judgment calls with tradeoffs, not just
  correctness"), which is precisely the chairman's job.

## 5. Final council configuration (ready to implement)

```yaml
council:
  models:
    - anthropic/claude-opus-4.8
    - openai/gpt-5.5
    - google/gemini-3.6-flash
    - z-ai/glm-5.2-20260616
  chairman: anthropic/claude-opus-4.8
  chairman_disabled: false
  synthesis_mode: debate
  exclude_self_votes: true
  style_normalization: true
  gateways:
    default: openrouter
```

`normalizer_model` and `max_reviewers` are left unset — inherit the package's
own current defaults rather than pinning a possibly-stale slug (the original
doc's `google/gemini-2.0-flash-001` predates this entire model generation and
was never re-verified).

**Superseded 2026-08-14 (`normalizer_model` only, `max_reviewers` unchanged):**
the "inherit the package default" reasoning above turned out to be wrong in
practice — the package's inherited default (`google/gemini-3.1-flash-lite-
preview`) went dead on live OpenRouter (confirmed 2026-08-14, same drift
pattern as glm-5.2/kimi-k3's dated slugs above), which would have silently
broken Stage 1.5 the next time it actually ran. Now pinned explicitly to
`google/gemini-3.6-flash`, grounded against live rewriter-model evidence
("Voice Under Revision," arXiv:2604.22142v1) — full reasoning and citation
in `llm_council.yaml`'s own comment block and `docs/upstream-deltas.md`.

GLM-5.2 is in `council.models` (Stage 1 + Stage 2 eligible) but never
referenced as `chairman` — satisfies "4th voice, never chairman/judge."

**Known enforcement gap (see §8):** the flat `council.models` list alone does
NOT stop GLM-5.2 from being selected in a debate/tie-breaker round if
`synthesis_mode: debate` triggers one — `chairman` only pins synthesis, not
every downstream role. Needs a stage-eligibility wrapper (§8.2) before this is
actually safe to run for real, not just configured to look safe.

## 5b. Design decision: DeepSeek considered, not swapped in (2026-08-09)

Question raised: should DeepSeek replace GLM-5.2, or run alongside it, in the
experimental slot? Researched via WebSearch (DeepSeek-V4's actual training
architecture vs. GLM-5's), not assumed.

- **"Keep both" is rejected outright** — it would make a 5-model council,
  directly contradicting the standing ceiling decision in §2 ("never more
  than four... don't reflexively backfill with a different 4th model").
- **Lineage comparison, honestly reported:** what's actually documented is
  each model's *own* internal training pipeline — DeepSeek-V4 unifies
  domain-specific experts (math/code/agent/instruction) via multi-teacher
  on-policy distillation (MOPD) into one model; GLM-5 uses a "progressive
  alignment" pipeline (multi-task SFT → multi-stage RL → cross-stage
  distillation) trained independently on Huawei Ascend/MindSpore rather than
  NVIDIA/CUDA. **Neither search turned up documented evidence of either
  model specifically distilling from GPT/Claude outputs** — that risk
  (raised in §2's original ceiling rationale) is a real, general pattern in
  the ecosystem, but it is NOT independently confirmed for DeepSeek-V4 or
  GLM-5.2 specifically. Don't overclaim what wasn't verified.
- **Decision: keep GLM-5.2, don't swap.** GLM-5.2 is already configured,
  health-checked, and live in `llm_council.yaml` — swapping now, before a
  single real session, would restart the evaluation clock for no evidenced
  gain. DeepSeek-V4 (interesting note: it's the package's own hardcoded
  *default* 4th model, upstream's own diversity pick) is recorded here as
  the natural next candidate **if GLM-5.2 doesn't clear the scorecard bar**
  — decided with the same 20+-session evidence-gated process already
  established, not swapped on vibes.

Sources: [DeepSeek V4 GA architecture](https://huggingface.co/blog/ResterChed/deepseek-v4-ga-architecture), [DeepSeek-V4 training data strategy](https://kili-technology.com/blog/data-story-deepseek-v4), [GLM-5 2026 architecture](https://webscraft.org/blog/glm5-2026-arhitektura-benchmarki-mozhlivosti-ta-obmezhennya?lang=en), [GLM-5.1 vs DeepSeek comparison](https://wavespeed.ai/blog/posts/glm-5-1-vs-claude-gpt-gemini-deepseek-llm-comparison/)

**Follow-up (2026-08-09, same day): direct capability benchmark check.** No
clean winner — genuinely mixed, reported honestly rather than picking a side:
- GLM-5.2 leads general reasoning (5 of 7 benchmarks): AIME 2026 99.2% vs
  94.6%, HLE-with-tools 54.7% vs 48.2%, GPQA Diamond 91.2% vs 90.1%. Also the
  stronger agentic/tool-use model (near-frontier MCP Atlas, better CLI-agent
  performance).
- DeepSeek V4 Pro leads competitive/algorithmic coding: LiveCodeBench 93.5%
  (#1 globally, any model), SWE-bench Verified 80.6%. Also ~5x cheaper
  ($0.87/M vs $4.40/M input tokens).
- **Decision: keep GLM-5.2.** This pipeline's job is judgment on real
  decisions, not competitive/algorithmic coding — the relevant benchmark
  category is general reasoning (AIME/GPQA/HLE), where GLM-5.2 leads.
  DeepSeek's edges (SWE-bench, LiveCodeBench, price) are in a category this
  pipeline doesn't exercise. Combined with GLM-5.2 already being configured
  and health-checked, no config change made.

Sources: [DeepSeek V4 vs Kimi K3 vs GLM-5.2](https://www.buildfastwithai.com/blogs/deepseek-v4-vs-kimi-k3-vs-glm-5-2-coding), [GLM 5.2 vs DeepSeek V4 Pro full comparison](https://emergent.sh/learn/glm-5-2-vs-deepseek-v4-pro), [GLM-5.2 vs DeepSeek V4 coding showdown](https://codersera.com/blog/glm-5-2-vs-deepseek-v4-coding-2026/), [GLM-5.2 vs DeepSeek V4 score vs price](https://theplanettools.ai/compare/glm-5-2-vs-deepseek-v4)

## 6. Design decision: no per-model personas in Stage 1

Decided 2026-08-09, per user directive with citation to this project's own
earlier research pass. **Do not** assign Claude/GPT/Gemini/GLM distinct
personas (e.g. "VC partner," "technical advisor," "design critic") in Stage 1
independent drafting.

**Why:** "Multi-Persona" (angel/devil) was the worst-performing MAD method
across nearly all datasets in the research this pipeline is grounded in — the
mechanism is that role identities don't create genuine separation, they're
just linguistic context inside one chat; persona drift/collapse compounds
this over a session regardless of which model wears the persona. Applied
here, persona-assignment is architecturally worse than in single-model MAD:
it breaks the Consensus Strength Score's core assumption. CSS only means
something if every model answered the *same* question independently — if
each model is scoped to a different lens, "consensus" between three partial,
differently-scoped analyses isn't a meaningful number. Stage 2.5's entire
conditional-debate-gating logic depends on CSS being real.

**Where structured lens-checking actually belongs:** downstream, paired with
a verifier — matches the pattern that *did* work in the cited research
(devil's-advocate roles paired with a calibrated verifier/ground-truth, not
free-floating). Concretely: **Stage 3.75 (post-synthesis — note: not "3.5",
which is already `run_full_council`'s own internal aggregate-rankings step;
see `docs/agent-model-reasoning-config.md` §6 and
`scripts/completeness_check.py`'s docstring for why this project reserves
"3.5" for the package's internal meaning) and the Stage 4 premortem** are
where lens questions go, answered by whichever model already runs that
stage — never by reassigning the core models into role-play.

**Domain-neutrality requirement (added after a user catch — nothing had
shipped yet, but an early illustrative example used fundraising-specific
lenses):** the lens set must never be hardcoded (no fixed "capital
efficiency / technical risk / narrative coherence" list baked into a
template). Every session, lenses and premortem failure categories are
**derived from that session's own Stage 0 pre-registered criteria and
kill-switches** — Stage 0's file is the only source of domain content this
pipeline ever reads. Same rule applies to every other prompt template in this
repo (grounding pass, cross-exam, judge rubric, whatever comes later): audit
each one for hardcoded domain language before it ships. This is what makes
the toolkit portable across arbitrary decisions instead of being a
renamed single-project tool.

## 7. Design decision: folder-scoped, portable invocation

Decided 2026-08-09. This toolkit is not project-specific — it must work
correctly when invoked from any directory, for any decision.

- **All session output is folder-scoped**, written to
  `./council-runs/<timestamp>-<decision-slug>/` inside whatever directory
  the pipeline is invoked from — grounding pass results, Stage 1-3
  transcripts, CSS, scorecard data, dissent flags, premortem, final memo.
- **No writes to `~/.llm-council/` or any shared home-directory path by
  default.** The global/home directory stays clean regardless of how many
  projects invoke this pipeline.
- If `LLM_COUNCIL_BIAS_PERSISTENCE` is enabled, it must be scoped per-folder
  too (`./council-runs/bias_metrics.jsonl`). Whether the installed package
  supports a configurable path natively, or needs a thin wrapper that
  redirects post-hoc, is checked in §8.3 below — do not fight the tool's
  internals if it's hardcoded, wrap it instead.
- The scorecard script (§3) reads the local folder by default; cross-folder
  aggregation is opt-in via an explicit flag, never the default.
- Before trusting this with any real decision: a dry run from a throwaway
  test directory, unrelated to any real project, showing the resulting local
  folder structure. Not yet done — blocks task completion, not spec approval.

## 8. Independent external review (GPT-5.5 + Gemini-3.6-flash, via direct OpenRouter — 2026-08-09)

Sanity-checked this spec against two of the council's own future members,
called directly via OpenRouter (not through llm-council — MCP wasn't
registered yet at review time). Real cost: ~$0.086 across 4 calls (2
truncated at first pass, re-run with headroom). Both models independently
converged on the same two structural gaps, which is itself a useful signal.

### 8.1 What both models called sound
- The `council.models`/`tiers.pools` config-trap catch (§1) — "high-value,"
  "prevents silent misconfiguration."
- The 3-core + 1-gated ceiling and its O(N²)/interpretability rationale.
- Deferring `grounding_pass.py`/`revision_round.py`/scorecard until specced.

### 8.2 Gap — RESOLVED, was a false alarm (2026-08-09 follow-up)
Both external models flagged this and it was a reasonable thing to check, but
direct source verification closes it: `council.py` routes every synthesis/
tie-breaker call through `_get_chairman_model()` exclusively (grep confirms
no alternate model-selection path for debate/tie-breaking exists anywhere in
the module). Since `chairman: anthropic/claude-opus-4.8` is pinned in config,
GLM-5.2 is **structurally incapable** of becoming chairman or tie-breaker
regardless of `synthesis_mode`/`council.models` membership — there was never
anything to enforce beyond the `chairman:` field already set. No wrapper
logic needed for this specific concern; §8.3's orchestration gap (grounding
+ revision stages) is the only real remaining gap.

### 8.3 Gap — no orchestration wrapper for the custom stages (Gemini's sharpest point)
`llm-council-core`'s standard run doesn't know about Stage 0.5 (grounding) or
Stage 2.75 (correction-biased revision) — those are this project's own
additions and need something that actually chains: Stage 0 → 0.5 (grounding)
→ 1 → 1.5 → 2 → 2.5 (gate) → [2.75 if CSS<0.50] → 3 (chairman) → 3.5
(dissent + lens questions) → [4 premortem, manual]. Nothing in `llm_council.yaml`
alone does this chaining — it needs an explicit runner.
**Action:** this becomes one more spec under `docs/specs/` (§3) —
`pipeline_runner` — that owns: calling `consult_council` for Stages 1-3.5,
invoking `grounding_pass.py` before it, invoking `revision_round.py`
conditionally, enforcing GLM-5.2's stage eligibility (§8.2) before each call,
and writing everything to the folder-scoped output path (§7). This is the
single wrapper that resolves both flagged gaps at once — not two separate
patches.

### 8.4 Gap — Gemini Flash as an equal-weight peer reviewer (Gemini's own self-critique, worth taking seriously precisely because it's self-directed)
With `exclude_self_votes: true` and 4 models, `gemini-3.6-flash` holds a 33%
vote share reviewing Opus/GPT/GLM drafts despite being a lighter, faster
model than the frontier tier it's supposed to be peer to. Two options on the
table, not yet decided:
1. Weight Stage 2 review scores by model class (e.g., frontier=1.0,
   Flash-tier=0.5) before they feed CSS.
2. Leave unweighted, but track this explicitly in the scorecard system (§3) —
   the same infrastructure already built for GLM-5.2's probation applies
   here, just pointed at a different question ("does the interim Gemini seat
   under-perform its vote share, not just its draft quality?").
**Not resolved yet — flagging for a decision, not silently picking one.**

### 8.6 Hardening pass (2026-08-09, grounded against `gateway_adapter.py`, `unified_config.py`)
- **Truncation seen in §8's smoke test was our own bug, not the package's.**
  `gateway_adapter.py` sets no `max_tokens` cap on model calls at all — the
  ad hoc review script capped it too low for reasoning-heavy models (465-670
  reasoning tokens consumed before any visible answer). `consult_council`
  itself won't hit this; no fix needed there.
- **Already-inherited hardening, verified present, nothing to add:**
  per-model circuit breakers (`CircuitBreakerConfig`: 25% failure threshold
  trips, 30min cooldown, half-open probing), tiered timeouts scaled for
  reasoning models (`TimeoutsConfig.reasoning`: 300s/model, 600s total), and
  a gateway fallback chain (`FallbackConfig`, retries on
  timeout/rate_limit/server_error, not on auth/invalid-request/content-filter).
- **Real gap found and fixed:** default fallback chain is
  `[openrouter, requesty, direct]`, but only an OpenRouter key exists.
  Pinned `gateways.fallback.chain: [openrouter]` in `llm_council.yaml` so a
  failure surfaces as a clear OpenRouter error instead of silently cascading
  into two gateways with no stored credentials. Revisit once/if direct keys
  are ever added.

### 8.5 Note — not adopted
GPT-5.5 suggested pinning `normalizer_model`/`max_reviewers` into a resolved
runtime lockfile for reproducibility, trading off against §1's "don't pin
possibly-stale slugs" reasoning. Worth revisiting once the pipeline_runner
spec exists (it's a natural place to snapshot the resolved config per run
anyway, folder-scoped per §7) — not a standalone action now.

## 9. Quality-metric interpretation reference — read before citing CSS as evidence

Added 2026-08-14 after a real mistake this session: a dry-run comparing
Stage 1 `reasoning_effort` settings (docs/specs/reasoning-effort-wiring-
contract.md, Contract 4) showed CSS drop from 0.721 to 0.572, and that was
initially reported as "a real signal against shipping high effort" — a
quality claim CSS cannot actually support. Corrected same session on user
challenge. **Before treating any CSS number as a quality signal, re-read
this section.**

**What CSS actually measures** (`llm_council.quality.consensus.
consensus_strength_score`'s own docstring, direct source read): how
strongly Stage 2 reviewers AGREE on the ranking of Stage 1 answers — clear
rank separation and a clear winner = high CSS; ties and scattered rankings
= low CSS. **It is a ranking-agreement metric, not a correctness or
quality metric.** A high-CSS run is not necessarily "the answers were
better" — it's "the reviewers converged on an ordering." A low-CSS run is
not necessarily "the answers were worse" — it could equally mean the
models reasoned about the question in genuinely different, defensible
ways and reviewers split on which was best.

**The package's own interpretation bands** (`get_consensus_interpretation`):

| CSS range | Label | What this pipeline does about it |
|---|---|---|
| 0.85–1.0 | strong_consensus | proceeds straight to Stage 3 synthesis |
| 0.70–0.84 | moderate_consensus | proceeds; synthesis notes minority views |
| 0.50–0.69 | weak_consensus | still proceeds; still a normal operating band |
| <0.50 | significant_disagreement | **triggers** Stage 2.75 revision and (with an outlier flag) Stage 3.75 critique |

**The point most likely to be misread**: low CSS is not this pipeline's
failure state — it's the pipeline's own TRIGGER for more deliberation
(Stage 2.75/3.75 exist specifically to act on low CSS productively). A
debate/MAD architecture whose whole premise is independent reasoning
should not be judged by how *little* the panelists disagree. Both 0.721
and 0.572 from the Contract 4 dry-run landed in the two middle,
fully-normal bands above — neither was "significant disagreement," and
the drop between them was never valid standalone evidence about answer
quality.

**What a CSS comparison CAN legitimately support**: an operational/cost
claim — a setting that pushes CSS lower more often will trigger Stage
2.75/2.75-adjacent stages more often, which is a real latency/cost
consequence worth knowing, separate from any quality claim. If a real
quality comparison is needed (e.g. "does effort X actually produce better
Stage 1 drafts"), it requires reading the actual draft content against
this project's own Stage 2 rubric (accuracy/relevance/completeness/
conciseness/clarity, `llm_council.yaml`'s `evaluation.rubric.weights`) —
CSS alone cannot answer that question, no matter which direction it moves.
See `docs/upstream-deltas.md`'s 2026-08-14 "CSS correction" entry for the
full incident this section is grounded in.
