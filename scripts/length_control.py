"""Length-controlled score adjustment for Stage 2 aggregate rankings.

Inspired by Dubois et al., "Length-Controlled AlpacaEval: A Simple Way to
Debias Automatic Evaluators" (arXiv:2404.04475) - verbosity bias survives
this repo's existing tone/style normalization (`stage1_5_normalize_styles`
explicitly preserves length: "Do NOT add or remove any substantive
content"), because normalization targets stylistic fingerprinting, not
length. See amiable-dev/llm-council#675 (filed 2026-08-28).

IMPORTANT SCOPE LIMITATION, stated up front rather than glossed over:
AlpacaEval-LC fits a generalized linear model predicting the auto-
annotator's preference from length difference across a large benchmark
(thousands of comparisons), then reports the counterfactual preference at
zero length difference. A single council call only ever has as many data
points as configured models (typically 4-5) - nowhere near enough to fit a
GLM with any statistical confidence. This module does NOT attempt to
replicate that method. It applies a much simpler, transparent, per-call
heuristic instead: discount (or boost) each response's raw average_score
by a fixed, configurable coefficient times the log-ratio of its length to
the batch's own mean length for that call. This is an approximation
motivated by the same underlying concern (don't let length alone move the
score), not a validated statistical debiasing method - treat it as such
when interpreting results.

A second, disclosed limitation: this only adjusts `average_score` (the
direct per-response 1-10 rating). It does NOT adjust `borda_score`, which
is derived from each reviewer's holistic ranking, not the individual
scores - length bias baked into a reviewer's relative ordering can't be
un-baked after the fact without re-deriving the ranking from length-
adjusted per-response comparisons, which this module does not attempt.

Pure, dependency-injected: no network calls, no config file reads, no
mutation of the input list (returns a new list). Disabled by default
(`LengthControlConfig.enabled=False`) until real usage shows the default
sensitivity is reasonable for this operator's actual query mix.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class LengthControlConfig:
    enabled: bool = False
    sensitivity: float = 0.15
    # Weight applied to the log-length-ratio discount. 0.15 is a starting
    # point, not a calibrated value - no real usage data exists yet to tune
    # this against (see docs/specs/length-control-contract.md).
    min_length_chars: int = 1
    # Floor applied to every length before computing ratios, so a
    # response's length is never zero (guards log(0) -> -inf).


def apply_length_control(
    aggregate_rankings: List[Dict[str, Any]],
    response_lengths: Dict[str, int],
    config: LengthControlConfig,
) -> List[Dict[str, Any]]:
    """Return a NEW list, re-sorted by length-controlled score if enabled.

    `aggregate_rankings`: the exact return value of
    `llm_council.council_rankings.calculate_aggregate_rankings` (each entry
    has at least "model", "average_score", "borda_score").
    `response_lengths`: model -> character count of the response text that
    model's Stage 2 reviewers actually saw (post Stage-1.5-normalization,
    NOT the raw Stage 1 draft, if normalization is enabled for this run).

    When `config.enabled` is False, or `response_lengths` is empty, returns
    `aggregate_rankings` unchanged (same object, no copy) - a no-op cheap
    enough to call unconditionally at every aggregation call site.
    """
    if not config.enabled or not response_lengths:
        return aggregate_rankings

    lengths = {
        model: max(length, config.min_length_chars)
        for model, length in response_lengths.items()
    }
    mean_length = sum(lengths.values()) / len(lengths)

    adjusted: List[Dict[str, Any]] = []
    for entry in aggregate_rankings:
        new_entry = dict(entry)
        model = entry.get("model")
        raw_score = entry.get("average_score")
        length = lengths.get(model) if isinstance(model, str) else None

        if length is not None and isinstance(raw_score, (int, float)):
            discount = config.sensitivity * math.log(length / mean_length)
            new_entry["average_score_length_controlled"] = round(raw_score - discount, 3)
        else:
            # No length data for this model, or no raw score to adjust
            # (e.g. a model with zero effective votes) - pass through
            # unadjusted rather than guessing.
            new_entry["average_score_length_controlled"] = raw_score

        new_entry["length_control_applied"] = True
        adjusted.append(new_entry)

    adjusted.sort(
        key=lambda x: (
            -(x["average_score_length_controlled"] if x["average_score_length_controlled"] is not None else -999),
            -(x.get("borda_score") or 0),
        )
    )
    for i, entry in enumerate(adjusted, start=1):
        entry["rank"] = i

    return adjusted


def response_lengths_from_texts(responses: List[Dict[str, Any]]) -> Dict[str, int]:
    """Build the `response_lengths` dict `apply_length_control` needs, from
    a Stage-1/1.5-shaped list of {"model": ..., "response": ...} dicts.
    Missing or non-string "response" values are skipped (that model
    contributes no length signal rather than a fabricated zero)."""
    lengths: Dict[str, int] = {}
    for item in responses:
        model = item.get("model")
        text = item.get("response")
        if model and isinstance(text, str):
            lengths[model] = len(text)
    return lengths
