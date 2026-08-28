"""Tests for scripts.length_control (amiable-dev/llm-council#675 local
mitigation). Pure module, no network calls, no config file reads."""
from __future__ import annotations

import math
from typing import Optional

from scripts.length_control import (
    LengthControlConfig,
    apply_length_control,
    response_lengths_from_texts,
)


def _entry(model: str, average_score: Optional[float], borda_score: float = 0.5, rank: int = 0) -> dict:
    return {"model": model, "average_score": average_score, "borda_score": borda_score, "rank": rank}


def test_disabled_returns_input_unchanged_same_object():
    rankings = [_entry("model-a", 0.9), _entry("model-b", 0.7)]
    config = LengthControlConfig(enabled=False)

    result = apply_length_control(rankings, {"model-a": 500, "model-b": 100}, config)

    assert result is rankings  # exact same object, not even a copy, when disabled


def test_empty_response_lengths_returns_input_unchanged():
    rankings = [_entry("model-a", 0.9)]
    config = LengthControlConfig(enabled=True)

    result = apply_length_control(rankings, {}, config)

    assert result is rankings


def test_longer_response_gets_discounted_shorter_gets_boosted():
    # Equal raw scores, very different lengths -- length control should
    # separate them even though calculate_aggregate_rankings originally
    # scored them identically.
    rankings = [_entry("verbose-model", 0.8), _entry("terse-model", 0.8)]
    lengths = {"verbose-model": 4000, "terse-model": 250}
    config = LengthControlConfig(enabled=True, sensitivity=0.15)

    result = apply_length_control(rankings, lengths, config)

    by_model = {r["model"]: r for r in result}
    assert by_model["terse-model"]["average_score_length_controlled"] > 0.8
    assert by_model["verbose-model"]["average_score_length_controlled"] < 0.8
    # terse model now outranks verbose model despite identical raw scores
    assert by_model["terse-model"]["rank"] < by_model["verbose-model"]["rank"]


def test_can_flip_a_ranking_shorter_but_slightly_lower_raw_score_can_win():
    # This is the actual claim of "actually adjust rankings", not just
    # visibility: a shorter response with a slightly lower raw score should
    # be able to outrank a much longer one that only won on raw score.
    rankings = [_entry("long-winner", 0.85), _entry("short-runnerup", 0.80)]
    lengths = {"long-winner": 6000, "short-runnerup": 300}
    config = LengthControlConfig(enabled=True, sensitivity=0.2)

    result = apply_length_control(rankings, lengths, config)

    assert result[0]["model"] == "short-runnerup"
    assert result[0]["rank"] == 1


def test_exact_adjustment_formula():
    rankings = [_entry("model-a", 0.7)]
    lengths = {"model-a": 200}  # mean length == 200 (only one model) -> log(1) == 0, no adjustment
    config = LengthControlConfig(enabled=True, sensitivity=0.15)

    result = apply_length_control(rankings, lengths, config)

    assert result[0]["average_score_length_controlled"] == 0.7


def test_missing_length_data_for_a_model_passes_through_unadjusted():
    rankings = [_entry("model-a", 0.7), _entry("model-b", 0.6)]
    lengths = {"model-a": 500}  # no entry for model-b

    result = apply_length_control(rankings, lengths, LengthControlConfig(enabled=True))

    by_model = {r["model"]: r for r in result}
    assert by_model["model-b"]["average_score_length_controlled"] == 0.6
    assert by_model["model-b"]["length_control_applied"] is True


def test_none_average_score_passes_through_as_none():
    rankings = [_entry("model-a", None)]  # e.g. zero effective votes
    lengths = {"model-a": 500}

    result = apply_length_control(rankings, lengths, LengthControlConfig(enabled=True))

    assert result[0]["average_score_length_controlled"] is None


def test_min_length_chars_floor_prevents_log_zero():
    rankings = [_entry("model-a", 0.7), _entry("model-b", 0.7)]
    lengths = {"model-a": 0, "model-b": 100}
    config = LengthControlConfig(enabled=True, min_length_chars=1)

    result = apply_length_control(rankings, lengths, config)  # must not raise

    by_model = {r["model"]: r for r in result}
    assert by_model["model-a"]["average_score_length_controlled"] is not None
    assert math.isfinite(by_model["model-a"]["average_score_length_controlled"])


def test_response_lengths_from_texts_basic():
    responses = [
        {"model": "model-a", "response": "hello world"},
        {"model": "model-b", "response": "a much longer response text here"},
    ]

    lengths = response_lengths_from_texts(responses)

    assert lengths == {"model-a": 11, "model-b": 32}


def test_response_lengths_from_texts_skips_missing_or_non_string_response():
    responses = [
        {"model": "model-a", "response": "ok"},
        {"model": "model-b", "response": None},
        {"model": "model-c"},
    ]

    lengths = response_lengths_from_texts(responses)

    assert lengths == {"model-a": 2}
