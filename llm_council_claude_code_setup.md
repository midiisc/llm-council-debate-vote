# LLM Council Setup — Paste This Into Claude Code

## HOW TO USE THIS FILE
Paste everything under "SETUP INSTRUCTIONS FOR CLAUDE CODE" as a message into Claude
Code. It has enough context to install, configure, and validate the council end to
end, and to write the two custom scripts that close the gaps the base tool doesn't
cover. The sections above that are for you to read first — they explain the pipeline,
what's native vs. custom, and the gateway decision — so you know what you're asking
for and why.

---

## THE FULL PIPELINE (what happens, in order, and why)

```
STAGE 0 — Pre-registration                         [MANUAL, outside the tool]
  You write: decision, options, weighted criteria, kill-switches.
  WHY: blocks post-hoc rationalization. Nothing in the tool does this — it has
  to happen before any model sees the question, or the whole run is theater.

STAGE 0.5 — Grounding / fact verification            [CUSTOM SCRIPT — added below]
  Raw context claims get checked against live web search before entering the
  council. Unverifiable claims get demoted to ASSUMPTION.
  WHY: llm-council has no fact-checking step at all — it goes straight from
  your prompt to Stage 1. Without this, all four independent models can
  hallucinate the same wrong "fact" and the council will confidently agree
  on something false. (Citation fabrication runs 18-55% even in single-model
  use, per prior research in this project — grounding is not optional.)

STAGE 1 — Independent parallel drafts                [NATIVE]
  Each council model (Claude, GPT, Gemini, ...) answers the SAME prompt with
  ZERO visibility into the others. This is the ensembling substrate — most of
  MAD's real benefit lives here, per Choi, Zhu & Li, "Debate or Vote: Which
  Yields Better Decisions in Multi-Agent LLMs?" (NeurIPS 2025 Spotlight,
  arXiv:2508.17536) — majority voting alone accounts for most of the gain
  typically attributed to full debate.

STAGE 1.5 — Style normalization                       [NATIVE, opt-in]
  export LLM_COUNCIL_STYLE_NORMALIZATION=true
  Rewrites every response in neutral style before anyone sees anyone else's.
  WHY: strips stylistic fingerprints (a Claude answer "sounds like Claude")
  so anonymization in Stage 2 actually holds, instead of models silently
  recognizing each other by prose style and giving deference.

STAGE 2 — Anonymous peer review + Consensus Strength Score  [NATIVE]
  Responses relabeled A/B/C, self-votes excluded, JSON-structured rankings.
  Produces a Consensus Strength Score (CSS) 0.0-1.0.
  WHY: this is your vote-aggregation mechanism, and it doubles as the gate
  that decides whether expensive debate is even worth running — see Stage 2.5.

STAGE 2.5 — Conditional debate trigger                [NATIVE, via CSS + config]
  CSS >= 0.85  -> trust the synthesis, skip further debate
  CSS 0.50-0.84 -> enable include_dissent=true, note minority view
  CSS < 0.50    -> run correction-biased revision (Stage 2.75) before synthesis
  Deadlock (top scores within LLM_COUNCIL_DEADLOCK_THRESHOLD) -> auto tie_breaker
  WHY: the same NeurIPS paper proves debate induces a "martingale" over
  belief trajectories — plain debate has NO systematic drift toward
  correctness, it just moves opinions around. Paying for a debate round only
  when the vote signal shows real disagreement avoids buying noise.

STAGE 2.75 — Correction-biased revision round          [CUSTOM SCRIPT — added below]
  Only runs if CSS < 0.50. Each model sees the critique of its own answer and
  may revise ONLY by naming a specific verified fact (from Stage 0.5) that
  contradicts its load-bearing claim. Agreement-because-the-others-agree is
  explicitly disallowed as a reason to switch.
  WHY: this is the paper's own prescription — "targeted interventions that
  bias the belief update toward correction can meaningfully enhance debate
  effectiveness." Ungrounded persuasion is exactly the martingale; grounded,
  fact-triggered switching is the intervention that breaks it usefully. This
  round is not in the base tool at all.

STAGE 3 — Chairman synthesis                          [NATIVE]
  export LLM_COUNCIL_MODE=debate
  One model reads everything (original answers, rankings, revisions if any)
  and writes the final answer. Debate mode surfaces disagreement explicitly
  instead of papering over it with a single confident answer.

STAGE 3.5 — Dissent extraction                        [NATIVE]
  include_dissent=true statistically extracts outlier reviews (score < median
  - 1.5 std) and formats them as a minority perspective, even when the
  council reached a verdict.
  WHY: this is your mandatory minority report, done automatically.

STAGE 4 — Premortem                                    [MANUAL / one extra prompt]
  Not in the tool. Run separately: "Assume this decision failed in 18 months
  — what are the 5 most plausible failure paths?"
  WHY: decision-science practice (Mitchell, Russo & Pennington, 1989 —
  prospective hindsight) not automatable inside a debate tool; needs its own
  clean-context call.

STAGE 5 — Bias audit (runs automatically alongside the above)  [NATIVE, opt-in]
  export LLM_COUNCIL_BIAS_AUDIT=true
  Tracks length-score correlation, reviewer harshness/leniency, position
  bias, per session, with cross-session statistical confidence once you've
  run enough sessions (20+ for moderate confidence, 50+ for high).
  WHY: turns "is this tool actually unbiased?" from a one-time trust
  decision into an ongoing, measured question with real numbers behind it.
```

---

## WHAT'S NATIVE vs. WHAT YOU'RE ADDING

| Piece | Status | Notes |
|---|---|---|
| Independent parallel drafts | Native | Core Stage 1 |
| Anonymization + style normalization | Native | Opt-in flags |
| Vote aggregation (CSS) | Native | Replaces manual "clustering script" |
| Conditional debate gating | Native | Via CSS thresholds + deadlock detection |
| Minority report | Native | `include_dissent=true` |
| Self-preference / bias mitigation | Native | Self-vote exclusion + bias audit module |
| Hallucination guard on scoring | Native | Accuracy ceiling in rubric scoring |
| **Fact-grounding / citation verification** | **You add (Stage 0.5)** | Not present anywhere in the base tool |
| **Correction-biased revision round** | **You add (Stage 2.75)** | Base tool ranks but never lets a model revise its own answer |
| Pre-registration of criteria | You (Stage 0) | Decision-specific, can't be generic |
| Premortem | You (Stage 4) | One extra clean-context prompt |

---

## IS OPENROUTER THE BEST GATEWAY? Short answer: start there, then reconsider.

The tool natively supports three gateways — `openrouter` (default), `requesty`, and
`direct` — switchable with one environment variable, so this isn't a lock-in decision.

- **OpenRouter**: widest catalog (400+ models, 70+ providers), fastest to set up,
  optional Zero Data Retention with no extra per-request fee. The real cost is a
  **5.5% fee on credit purchases** (non-crypto) — meaning every dollar of model usage
  effectively costs $1.055.
- **Requesty**: comparable catalog (300-600+ models), adds **EU data residency**
  (relevant if any syndicate/investor data has EU nexus) and BYOK support, but still
  charges roughly a 5% markup on base provider rates once you're past its free-tier
  cap.
- **Direct** (your own Anthropic/OpenAI/Google keys): **zero gateway markup**, and
  the tool supports it natively via `LLM_COUNCIL_DEFAULT_GATEWAY=direct`. The tradeoff
  is you manage three separate billing relationships and API keys instead of one.

**Recommendation:** set up with OpenRouter first — it's the path of least friction
and the tool's default, so you validate the whole pipeline fastest. Once you've run
a handful of real decisions and know your actual per-decision token spend, flip to
`direct` with your own provider keys to drop the 5.5% fee — you already hold Claude
Max, ChatGPT Plus, and Gemini Pro, so getting API keys from the same three vendors is
a small additional step, not a new relationship. This is a config flag change, not a
rebuild.

---

## FUTURE IMPROVEMENTS WORTH TRACKING

1. **Automate Stage 0 pre-registration as a structured template file** the council
   reads before every run, rather than re-typing criteria per decision — reduces the
   chance of quietly skipping it under time pressure.
2. **Wire Stage 0.5 grounding as an MCP tool call** rather than a separate manual
   step, once you're comfortable with the pipeline — the council already runs as an
   MCP server, so a companion verification MCP tool could sit in the same chain.
3. **Track calibration over time using `LLM_COUNCIL_BIAS_PERSISTENCE`** — after
   20-50 real decisions, you'll have actual data on which model in your council runs
   systematically harsh/generous or verbose, which should inform whether to keep it
   in the pool.
4. **Consider adding a 4th heterogeneous model (e.g., DeepSeek or Grok)** specifically
   to further decorrelate errors — the correlated-errors research (Kim et al., ICML
   2025) found same-provider/same-architecture models are the most correlated;
   adding a genuinely different lineage model strengthens the independence assumption
   Stage 1 relies on.
5. **An Analysis-of-Competing-Hypotheses (ACH) matrix as an optional Stage 3.5b** —
   structuring the chairman's synthesis around explicit hypotheses x evidence rather
   than free-form debate summary, for the highest-stakes decisions only.
6. **Watch the MCP Server Card / registry submission work already in progress** on
   this repo — as MCP tool discovery standardizes, this could make it trivial to
   invoke the council from other agents/tools without bespoke config.

---

## SETUP INSTRUCTIONS FOR CLAUDE CODE
*(Paste everything below this line into Claude Code)*

I want to set up amiable-dev/llm-council (an MCP server implementing Karpathy's
LLM Council pattern) for structured multi-model business decision deliberation,
plus two custom additions it doesn't natively support. Please do the following:

### 1. Install
```bash
pip install "llm-council-core[mcp,secure]"
```
Use `[mcp,secure]` specifically — without `secure` the OS keychain support is
silently unavailable and a stored key gets silently ignored.

### 2. Set up OpenRouter as the initial gateway
Walk me through creating an OpenRouter account and API key if I don't have one, then:
```bash
llm-council setup-key
```
(prompts for the key, stores it in OS keychain, no plaintext).

### 3. Create `llm_council.yaml` in this project directory
```yaml
council:
  tiers:
    default: high
    pools:
      high:
        models:
          - anthropic/claude-opus-4.8
          - openai/gpt-5.5
          - google/gemini-3-pro
        timeout_seconds: 180
  gateways:
    default: openrouter
```

### 4. Set these environment variables in `.env` (confirm it's gitignored)
```bash
LLM_COUNCIL_STYLE_NORMALIZATION=true
LLM_COUNCIL_NORMALIZER_MODEL=google/gemini-2.0-flash-001
LLM_COUNCIL_RUBRIC_SCORING=true
LLM_COUNCIL_ACCURACY_CEILING=true
LLM_COUNCIL_BIAS_AUDIT=true
LLM_COUNCIL_BIAS_PERSISTENCE=true
LLM_COUNCIL_BIAS_CONSENT=1
LLM_COUNCIL_EXCLUDE_SELF_VOTES=true
LLM_COUNCIL_MODE=debate
LLM_COUNCIL_DEADLOCK_THRESHOLD=0.15
```

### 5. Verify it works before spending real money
```bash
llm-council serve &
```
Then run `council_health_check` and confirm `ready: true`, `council_size: 3`,
`key_source` shows the keychain (not a plaintext env fallback).

### 6. Register with Claude Code
```bash
claude mcp add --transport stdio llm-council --scope user -- llm-council
```

### 7. Write the two custom scripts this project needs (not in the base tool)

**`grounding_pass.py`** (Stage 0.5): takes a raw context file, extracts numbered
factual claims, verifies each via web search, outputs the same file with each
claim tagged `VERIFIED (source, date)`, `CONTRADICTED (source)`, or
`UNVERIFIABLE -> demoted to ASSUMPTION`. This becomes the context block fed into
Stage 1 prompts.

**`revision_round.py`** (Stage 2.75): after a `consult_council` run, check
`metadata["quality_metrics"]["core"]["consensus_strength"]`. If CSS < 0.50, take
each model's original Stage 1 answer plus its Stage 2 critique plus the verified
facts from `grounding_pass.py`, and re-query that same model with:
"You may revise your position ONLY if you can name a specific verified fact that
contradicts your original load-bearing claim. The other models agreeing with each
other is not a valid reason to switch." Feed the revised answers back into Chairman
synthesis instead of the originals.

### 8. Do a dry run
Use a real but low-stakes test decision (not a live term-sheet call) end to end:
grounding pass -> council -> check CSS -> conditional revision if triggered ->
synthesis -> dissent extraction. Show me the full output including the Cost & Tokens
summary so I can see real per-decision spend before I run this on anything that
matters.

Please implement all of this, explain any deviations you need to make from what's
specified above, and flag anything in the actual repo (config options, defaults,
model name syntax) that's changed from what's described here — the repo is under
active development and specifics may have moved.
