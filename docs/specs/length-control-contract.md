# Length-control contract (amiable-dev/llm-council#675)

Status: implemented 2026-08-28. Local mitigation, not an upstream code change — see
`docs/specs/upstream-issue-draft-length-control-2026-08-28.md` for why the underlying gap was
filed there instead of forked.

## The gap this closes

`stage1_5_normalize_styles`'s rewrite prompt preserves length by design ("Do NOT add or remove
any substantive content") — it targets tone/formatting fingerprinting for anonymization, not
verbosity bias. Verbosity bias is heterogeneous by judge-model family (Dubois et al.,
"Length-Controlled AlpacaEval," arXiv:2404.04475, verified via arXiv metadata) and survives
style normalization untouched.

## Design decision — a per-call heuristic, not a real AlpacaEval-LC replication

AlpacaEval-LC fits a generalized linear model predicting an auto-annotator's preference from
length difference across a large benchmark (thousands of comparisons), then reports the
counterfactual preference at zero length difference. A single council call only ever has as
many data points as configured models (4-5 here) — nowhere near enough to fit a GLM with any
statistical confidence. `scripts/length_control.py` applies a much simpler, transparent
per-call heuristic instead: discount/boost each response's `average_score` by
`sensitivity * log(length / batch_mean_length)` for that call's own batch, using the length of
the post-Stage-1.5-normalized text (what reviewers actually saw), not the raw Stage 1 draft.

**Disclosed limitation, not glossed over:** only `average_score` is adjusted. `borda_score`
(derived from each reviewer's holistic ranking, not the individual numeric score) is left
unadjusted — length bias baked into a reviewer's relative ordering can't be un-baked after the
fact without re-deriving the ranking from length-adjusted per-response comparisons, which this
module does not attempt.

## Contract — `scripts/length_control.py`

```python
@dataclass
class LengthControlConfig:
    enabled: bool = False
    sensitivity: float = 0.15   # UNCALIBRATED starting point, see below
    min_length_chars: int = 1

def apply_length_control(
    aggregate_rankings: list[dict],   # calculate_aggregate_rankings' own return value
    response_lengths: dict[str, int], # model -> len(text reviewers actually saw)
    config: LengthControlConfig,
) -> list[dict]: ...

def response_lengths_from_texts(responses: list[dict]) -> dict[str, int]: ...
```

Pure, dependency-injected: no network calls, no config file reads, no mutation of the input
list. A no-op (`enabled=False`, or `response_lengths` empty) returns the input unchanged —
cheap enough to call unconditionally at every aggregation call site.

Wired into `scripts/council_adapter.py`'s single call site for `calculate_aggregate_rankings`,
immediately after it returns, using `responses_for_review` (post-Stage-1.5 text) to compute
lengths via `response_lengths_from_texts`. Config loaded via
`_load_length_control_config` (mirrors `_load_debate_resilience_config`'s file-location and
`get_config()`-bypass rationale — see `docs/upstream-deltas.md`'s config-placement rule) from a
new `length_control:` block in `llm_council.yaml`.

## Calibration status — genuinely unknown, stated plainly

`sensitivity: 0.15` is a starting point, not a measured value. No real usage data exists yet on
this operator's actual query mix. The `high-stakes-research-pipeline` skill's own divergence-log
pattern (a cross-run JSONL log of council verdicts) is the natural place to eventually judge
whether 0.15 over- or under-corrects in practice — not built here, since it requires real runs
to accumulate first.

## Verification

15 new tests (`tests/test_length_control.py`: the adjustment math, including a case that flips a
ranking outcome, the actual claim of "adjust rankings" rather than just surface a second score;
`tests/test_length_control_config_loading.py`: config-loading defaults, partial blocks, and this
repo's own real `llm_council.yaml` parses to `enabled=True`). Full suite re-run: 916 passed (901
baseline + 15 new), zero regressions — including one existing test
(`test_council_adapter_resilient_stage1.py::test_stage3_synthesize_final_receives_correct_aggregate_rankings`)
that needed its shared fixture updated to explicitly neutralize `_load_length_control_config`
(disabled), the same way that fixture already neutralizes `_load_debate_resilience_config` —
that file's own concern is Stage 1/2/3 resilience, not length control, so it shouldn't be
coupled to `llm_council.yaml`'s real (enabled) default. Not a real-money change — hermetic
dependency-injected fakes only, no live API call made.

## Considered and rejected — editing the Stage 1.5 rewrite prompt itself (2026-08-28)

Two prompt-level alternatives to the score-level adjustment above were considered, both
rejected before any code was written:

**Target an expected/reference length directly.** Rejected: fundamentally conflicts with Stage
1.5's own core guarantee, "preserving ALL content and meaning exactly." Forcing two responses of
genuinely different substance toward the same length means either compressing real content out
of the more thorough one or padding the shorter one with fabricated filler — both violate the
"Do NOT add or remove any substantive content" rule this normalizer exists to uphold. It also
conflates length-as-bias with length-as-legitimate-signal: sometimes a longer answer is longer
because it's actually more complete, and that's real information Stage 2 should see, not noise
to erase. This is why Dubois et al.'s actual AlpacaEval-LC method never rewrites text at all — it
statistically corrects the *score*, leaving the response itself untouched.

**Strip padding/filler (redundant restatement, unnecessary preamble) without targeting a
length.** A narrower, more defensible version of the same idea — rejected anyway. A rewriter
model asked to trim "restatement that adds no new information" cannot reliably tell genuine
repetition apart from step-by-step reasoning scaffolding ("given X, we know Y, which means Z"
looks like restatement on the surface but is load-bearing derivation). A miscall silently
compresses a response's shown work into a bare conclusion — penalizing exactly the responses
Stage 2/3 are supposed to credit for inference quality, with no verification step to catch that
it happened. This is the same failure shape as the Deliberative Illusion risk this project
already guards against elsewhere (silent information loss during an LLM-mediated rewrite/
consensus pass), just relocated to Stage 1.5. The marginal bias reduction wasn't judged worth a
hard-to-detect correctness regression on the one thing this normalizer explicitly promises never
to lose.

**Decision:** leave `_build_style_normalize_prompt` untouched. The score-level adjustment above
remains the only local mitigation for #675 — it never touches the text reviewers actually judge.
