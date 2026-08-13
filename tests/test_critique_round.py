"""Blind-spec-style tests for scripts/critique_round.py (Stage 3.75).

Contract: docs/specs/stage-3-75-critique-contract.md.
"""
from __future__ import annotations

import asyncio

from scripts.critique_round import (
    CRITIC_MODEL,
    CritiqueOutcome,
    build_critique_prompt,
    run_critique_round,
    should_trigger_critique,
)


# --- AC1-3: should_trigger_critique gating ---


def test_low_css_triggers_regardless_of_outlier():
    assert should_trigger_critique(css=0.3, is_outlier={"model-a": False, "model-b": False}) is True


def test_high_css_with_outlier_still_triggers():
    assert should_trigger_critique(css=0.9, is_outlier={"model-a": True, "model-b": False}) is True


def test_high_css_no_outlier_does_not_trigger():
    assert should_trigger_critique(css=0.9, is_outlier={"model-a": False, "model-b": False}) is False


def test_high_css_empty_outlier_dict_does_not_trigger():
    assert should_trigger_critique(css=0.9, is_outlier={}) is False


def test_css_exactly_at_threshold_does_not_trigger_alone():
    # css < threshold, not <=  - boundary must not trigger on equality.
    assert should_trigger_critique(css=0.50, is_outlier={}) is False


def test_css_just_below_threshold_triggers():
    assert should_trigger_critique(css=0.49, is_outlier={}) is True


def test_custom_threshold_respected():
    assert should_trigger_critique(css=0.6, is_outlier={}, threshold=0.7) is True
    assert should_trigger_critique(css=0.6, is_outlier={}, threshold=0.5) is False


# --- AC4-6: build_critique_prompt content ---


def test_prompt_instructs_devils_advocate():
    prompt = build_critique_prompt("The synthesis text.")
    assert "devil's-advocate" in prompt.lower() or "adversarial" in prompt.lower()
    assert "strongest" in prompt.lower()


def test_prompt_instructs_counterfactual():
    prompt = build_critique_prompt("The synthesis text.")
    assert "counterfactual" in prompt.lower() or "what-if" in prompt.lower() or "what if" in prompt.lower()
    assert "what would have to be true" in prompt.lower()


def test_prompt_forbids_rewrite():
    prompt = build_critique_prompt("The synthesis text.")
    assert "not a replacement" in prompt.lower()
    assert "do not produce a rewritten" in prompt.lower()
    assert "do not claim to supersede" in prompt.lower()


def test_prompt_delimits_synthesis_text():
    prompt = build_critique_prompt("SPECIFIC_SYNTHESIS_MARKER")
    assert "--- BEGIN SYNTHESIS ---" in prompt
    assert "--- END SYNTHESIS ---" in prompt
    begin = prompt.find("--- BEGIN SYNTHESIS ---")
    marker = prompt.find("SPECIFIC_SYNTHESIS_MARKER")
    end = prompt.find("--- END SYNTHESIS ---")
    assert begin < marker < end


def test_prompt_names_no_subject_matter_category():
    prompt = build_critique_prompt("anything")
    lowered = prompt.lower()
    for banned_word in ("market share", "revenue", "acquisition", "merger"):
        assert banned_word not in lowered


# --- AC7: CRITIC_MODEL is the hardcoded GPT-5.5 slug ---


def test_critic_model_is_gpt_5_5():
    assert CRITIC_MODEL == "openai/gpt-5.5"


# --- AC8: run_critique_round ---


def test_run_critique_round_calls_critic_model_exactly_once():
    calls = []

    async def fake_query_fn(model, prompt):
        calls.append((model, prompt))
        return "critique text here", 0.02

    outcome = asyncio.run(run_critique_round("The synthesis.", fake_query_fn))

    assert len(calls) == 1
    assert calls[0][0] == "openai/gpt-5.5"
    assert "The synthesis." in calls[0][1]


def test_run_critique_round_never_calls_the_chairman_model():
    calls = []

    async def fake_query_fn(model, prompt):
        calls.append(model)
        return "critique", 0.0

    asyncio.run(run_critique_round("synthesis", fake_query_fn))

    assert "anthropic/claude-opus-4.8" not in calls


def test_run_critique_round_returns_the_real_text_and_cost():
    async def fake_query_fn(model, prompt):
        return "This is a real critique of the synthesis.", 0.0347

    outcome = asyncio.run(run_critique_round("synthesis", fake_query_fn))

    assert isinstance(outcome, CritiqueOutcome)
    assert outcome.critique_text == "This is a real critique of the synthesis."
    assert outcome.cost_usd == 0.0347
    assert outcome.model == "openai/gpt-5.5"


def test_run_critique_round_cost_is_not_hardcoded_zero():
    async def fake_query_fn(model, prompt):
        return "critique", 0.099

    outcome = asyncio.run(run_critique_round("synthesis", fake_query_fn))

    assert outcome.cost_usd == 0.099
