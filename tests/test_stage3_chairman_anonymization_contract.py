"""Blind acceptance tests for docs/specs/stage3-chairman-anonymization-
contract.md (Stage 3 chairman-identity anonymization, Contract 1: three new
pure functions in `scripts/council_adapter.py`).

Authored WITHOUT sight of any implementation, design reasoning, or other
agent's work -- ONLY the contract file above was read. As of authoring,
`scripts/council_adapter.py` defines none of `_build_stage3_identity_map`,
`_anonymize_for_stage3`, `_resolve_response_labels` (confirmed by grep and
`hasattr` before writing a single test below) -- every test in this file is
expected to fail at collection (AttributeError on `ca.<name>`) until the
three functions land. That RED is correct and expected per blind-TDV.

DOCUMENTED ASSUMPTIONS (things the contract does not pin, decided here so
tests are runnable without leaking implementation guesses back into the
contract):

  1. **Import path.** The contract's signatures live in
     `scripts/council_adapter.py` (stated explicitly in "Contract 1 -- three
     new functions in `scripts/council_adapter.py`"). Imported the same way
     every existing council_adapter test in this repo does (`scripts.
     council_adapter`, falling back to a bare `council_adapter` -- see the
     `_import` helper duplicated across this repo's existing test files,
     e.g. tests/test_council_adapter_resilient_stage1.py).
  2. **"New label continuing the sequence" (AC2).** The contract says a
     reviewer-only model gets "a genuinely new label, continuing the
     sequence -- e.g. `Response C`" but does not pin the exact letter when
     `label_to_model` already has gaps or unusual keys -- tests for those
     open-ended cases assert only the invariants the contract actually
     states: not one of the labels already used, and unique among the
     result's values (AC7's bare injectivity check). For the specific,
     gap-free scenarios AC2/AC7 themselves use (label_to_model contiguous
     A, B with no gaps), the "continuing the sequence" requirement IS
     unambiguous, and AC2's own example pins it -- so those tests now
     assert the exact resulting label(s) too (added 2026-08-14, scoped
     mutation gate: the looser "just not A/B" / "just injective" checks
     let a wrong letter-offset or increment-stride formula survive
     undetected while staying spec-compliant on every original ac).
  3. **Purity check (AC8).** "mutate ... (compare each argument's
     identity-preserving content before/after the call)" is implemented via
     deep-copy-and-compare against snapshots taken before the call, plus an
     identity check that mutable containers passed in are not the same
     object as anything returned (belt-and-suspenders, not over-asserting
     beyond "unchanged after the call").
"""
from __future__ import annotations

import copy
import importlib
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _import(name: str):
    try:
        return importlib.import_module(f"scripts.{name}")
    except ModuleNotFoundError:
        return importlib.import_module(name)


ca = _import("council_adapter")


# ---------------------------------------------------------------------------
# Function A: _build_stage3_identity_map
# ---------------------------------------------------------------------------


def test_ac1_stage1_drafters_keep_their_exact_existing_label():
    """AC1: every Stage 1 drafter keeps the exact label label_to_model
    already assigned it -- never reassigned to a different label."""
    label_to_model = {
        "Response A": {"model": "m1", "display_index": 0},
        "Response B": {"model": "m2", "display_index": 1},
    }
    stage1_results = [{"model": "m1", "response": "x"}, {"model": "m2", "response": "y"}]
    stage2_results = [{"model": "m1", "ranking": "..."}, {"model": "m2", "ranking": "..."}]

    result = ca._build_stage3_identity_map(stage1_results, stage2_results, label_to_model)

    assert result == {"m1": "Response A", "m2": "Response B"}


def test_ac2_reviewer_with_no_stage1_draft_gets_a_genuinely_new_label():
    """AC2: a reviewer with no Stage 1 draft of its own gets a new label
    (not Response A/B), while m1/m2 stay unchanged."""
    label_to_model = {
        "Response A": {"model": "m1", "display_index": 0},
        "Response B": {"model": "m2", "display_index": 1},
    }
    stage1_results = [{"model": "m1"}, {"model": "m2"}]
    stage2_results = [{"model": "m3", "ranking": "..."}]

    result = ca._build_stage3_identity_map(stage1_results, stage2_results, label_to_model)

    assert result["m1"] == "Response A"
    assert result["m2"] == "Response B"
    assert "m3" in result
    assert result["m3"] not in ("Response A", "Response B")
    # This scenario has no gaps in label_to_model (exactly A, B already
    # used), so AC2's own example ("continuing the sequence -- e.g.
    # Response C") is unambiguous here: pin the exact value, not just
    # "some new label" -- mutation testing (2026-08-14) found the looser
    # assertion above lets a wrong letter-offset formula survive
    # undetected.
    assert result["m3"] == "Response C"


def test_ac3_same_reviewer_appearing_twice_gets_exactly_one_label_no_crash():
    """AC3: the same reviewer appearing twice in stage2_results must not
    crash and must not mint a second/duplicate label for that model."""
    label_to_model = {
        "Response A": {"model": "m1", "display_index": 0},
        "Response B": {"model": "m2", "display_index": 1},
    }
    stage1_results = [{"model": "m1"}, {"model": "m2"}]
    stage2_results = [
        {"model": "m3", "ranking": "..."},
        {"model": "m3", "ranking": "..."},
    ]

    result = ca._build_stage3_identity_map(stage1_results, stage2_results, label_to_model)

    # m3 appears exactly once as a key, mapped to exactly one label.
    assert list(result.keys()).count("m3") == 1
    assert isinstance(result["m3"], str)


def test_ac4_stage1_drafter_also_reviewing_keeps_existing_stage1_label():
    """AC4: a Stage 1 drafter that also reviews in Stage 2 maps to its
    existing Stage 1 label, not a newly minted one."""
    label_to_model = {
        "Response A": {"model": "m1", "display_index": 0},
        "Response B": {"model": "m2", "display_index": 1},
    }
    stage1_results = [{"model": "m1"}, {"model": "m2"}]
    stage2_results = [{"model": "m1", "ranking": "..."}]

    result = ca._build_stage3_identity_map(stage1_results, stage2_results, label_to_model)

    assert result["m1"] == "Response A"


def test_ac5_empty_stage2_results_is_exact_inversion_of_label_to_model():
    """AC5: single-model degraded mode (no Stage 2 round) -- result is
    exactly the inversion of label_to_model, no crash on empty list."""
    label_to_model = {"Response A": {"model": "m1", "display_index": 0}}
    stage1_results = [{"model": "m1"}]
    stage2_results = []

    result = ca._build_stage3_identity_map(stage1_results, stage2_results, label_to_model)

    assert result == {"m1": "Response A"}


def test_ac6_fully_empty_inputs_return_empty_dict_no_crash():
    """AC6: label_to_model={} and stage2_results=[] -> result is {}."""
    result = ca._build_stage3_identity_map([], [], {})
    assert result == {}


def test_ac7_result_is_injective_no_two_models_share_a_label():
    """AC7: no two distinct real model names ever map to the same label --
    every value in the returned dict is unique."""
    label_to_model = {
        "Response A": {"model": "m1", "display_index": 0},
        "Response B": {"model": "m2", "display_index": 1},
    }
    stage1_results = [{"model": "m1"}, {"model": "m2"}]
    stage2_results = [
        {"model": "m3", "ranking": "..."},
        {"model": "m4", "ranking": "..."},
    ]

    result = ca._build_stage3_identity_map(stage1_results, stage2_results, label_to_model)

    values = list(result.values())
    assert len(values) == len(set(values))
    # No gaps in label_to_model (A, B already used) and two new reviewers
    # in a row -- pin the exact, sequentially-continuing values for both.
    # A single-new-reviewer test (AC2) can't distinguish a correct += 1
    # continuation from a stride-2 (or other) bug in the running index,
    # since the first new label is unaffected by how it's incremented
    # afterward; only a *second* new label in the same call exposes that.
    # Mutation testing (2026-08-14) found exactly this survivor -- the
    # bare injectivity check above doesn't catch a wrong-but-still-unique
    # letter.
    assert result["m3"] == "Response C"
    assert result["m4"] == "Response D"


def test_ac8_function_is_pure_does_not_mutate_any_argument():
    """AC8: does not mutate stage1_results, stage2_results, or
    label_to_model."""
    label_to_model = {"Response A": {"model": "m1", "display_index": 0}}
    stage1_results = [{"model": "m1", "response": "x"}]
    stage2_results = [{"model": "m2", "ranking": "y"}]

    label_to_model_before = copy.deepcopy(label_to_model)
    stage1_before = copy.deepcopy(stage1_results)
    stage2_before = copy.deepcopy(stage2_results)

    ca._build_stage3_identity_map(stage1_results, stage2_results, label_to_model)

    assert label_to_model == label_to_model_before
    assert stage1_results == stage1_before
    assert stage2_results == stage2_before


def test_ac9_deterministic_across_repeated_calls_with_same_inputs():
    """AC9: calling twice with the same arguments returns an equal result
    both times."""
    label_to_model = {
        "Response A": {"model": "m1", "display_index": 0},
        "Response B": {"model": "m2", "display_index": 1},
    }
    stage1_results = [{"model": "m1"}, {"model": "m2"}]
    stage2_results = [{"model": "m3", "ranking": "..."}]

    result1 = ca._build_stage3_identity_map(stage1_results, stage2_results, label_to_model)
    result2 = ca._build_stage3_identity_map(stage1_results, stage2_results, label_to_model)

    assert result1 == result2


# Property-based test for Function A: injectivity + Stage-1-label-preservation
# hold for arbitrary well-formed inputs, not just the contract's fixed
# examples -- AC7 (injective) and AC1/AC4 (Stage 1 labels preserved) as a
# general law over randomized model/label sets.
@settings(max_examples=50, derandomize=True, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    n_stage1=st.integers(min_value=0, max_value=6),
    n_extra_reviewers=st.integers(min_value=0, max_value=4),
)
def test_property_identity_map_is_always_injective_and_preserves_stage1_labels(n_stage1, n_extra_reviewers):
    """Property (AC1, AC4, AC7): for any number of Stage 1 drafters and any
    number of reviewer-only models, the resulting map (a) preserves every
    Stage 1 drafter's existing label and (b) is injective (no two models
    share a label)."""
    stage1_models = [f"stage1-model-{i}" for i in range(n_stage1)]
    label_to_model = {
        f"Response {chr(65 + i)}": {"model": m, "display_index": i}
        for i, m in enumerate(stage1_models)
    }
    stage1_results = [{"model": m} for m in stage1_models]
    extra_reviewer_models = [f"reviewer-only-{i}" for i in range(n_extra_reviewers)]
    stage2_results = [{"model": m, "ranking": "..."} for m in stage1_models + extra_reviewer_models]

    result = ca._build_stage3_identity_map(stage1_results, stage2_results, label_to_model)

    # (a) Stage 1 labels preserved exactly.
    for label, entry in label_to_model.items():
        assert result[entry["model"]] == label

    # (b) injective.
    values = list(result.values())
    assert len(values) == len(set(values))

    # every model referenced ends up with a label.
    for m in stage1_models + extra_reviewer_models:
        assert m in result


# ---------------------------------------------------------------------------
# Function B: _anonymize_for_stage3
# ---------------------------------------------------------------------------


def test_ac10_stage1_model_field_replaced_other_keys_preserved():
    """AC10: returned stage1 element has "model" replaced by its label,
    every other key/value preserved exactly."""
    stage1_results = [{"model": "m1", "response": "text"}]
    model_to_label = {"m1": "Response A"}

    out1, out2, out3 = ca._anonymize_for_stage3(stage1_results, [], None, model_to_label)

    assert out1 == [{"model": "Response A", "response": "text"}]


def test_ac11_stage1_original_input_not_mutated():
    """AC11: original stage1_results list/dicts are not mutated."""
    stage1_results = [{"model": "m1", "response": "text"}]
    model_to_label = {"m1": "Response A"}

    ca._anonymize_for_stage3(stage1_results, [], None, model_to_label)

    assert stage1_results[0]["model"] == "m1"


def test_ac12_stage2_model_field_replaced_ranking_fields_preserved_no_mutation():
    """AC12: stage2 "model" replaced, "ranking"/"parsed_ranking" preserved
    byte-identical, input not mutated."""
    stage2_results = [{"model": "m2", "ranking": "text", "parsed_ranking": {"a": 1}}]
    model_to_label = {"m2": "Response B"}
    stage2_before = copy.deepcopy(stage2_results)

    _, out2, _ = ca._anonymize_for_stage3([], stage2_results, None, model_to_label)

    assert out2 == [{"model": "Response B", "ranking": "text", "parsed_ranking": {"a": 1}}]
    assert stage2_results == stage2_before


def test_ac13_aggregate_rankings_model_field_replaced_other_fields_preserved_no_mutation():
    """AC13: aggregate_rankings "model" replaced, rank/average_score/
    vote_count preserved exactly, input not mutated."""
    aggregate_rankings = [{"model": "m1", "rank": 1, "average_score": 8.5, "vote_count": 3}]
    model_to_label = {"m1": "Response A"}
    aggregate_before = copy.deepcopy(aggregate_rankings)

    _, _, out3 = ca._anonymize_for_stage3([], [], aggregate_rankings, model_to_label)

    assert out3 == [{"model": "Response A", "rank": 1, "average_score": 8.5, "vote_count": 3}]
    assert aggregate_rankings == aggregate_before


def test_ac14_aggregate_rankings_none_returns_none_not_empty_list():
    """AC14: aggregate_rankings=None -> returned third element is None."""
    _, _, out3 = ca._anonymize_for_stage3([], [], None, {})
    assert out3 is None


def test_ac15_model_not_in_map_left_unchanged_no_keyerror():
    """AC15: a "model" value not present in model_to_label is left as the
    original real model string, unchanged -- never a KeyError."""
    stage1_results = [{"model": "unmapped-model", "response": "x"}]
    stage2_results = [{"model": "another-unmapped", "ranking": "y"}]
    model_to_label = {"m1": "Response A"}

    out1, out2, _ = ca._anonymize_for_stage3(stage1_results, stage2_results, None, model_to_label)

    assert out1[0]["model"] == "unmapped-model"
    assert out2[0]["model"] == "another-unmapped"


def test_ac16_fully_empty_inputs_return_empty_empty_empty():
    """AC16: fully empty inputs -> ([], [], [])."""
    result = ca._anonymize_for_stage3([], [], [], {})
    assert result == ([], [], [])


# Property-based test for Function B: round-trip invariant -- for arbitrary
# lists of {"model": ..., **opaque_payload} dicts and an injective
# model_to_label, every entry's non-"model" keys survive untouched and the
# "model" key becomes exactly the label (or is left alone if unmapped).
@settings(max_examples=50, derandomize=True, deadline=None)
@given(
    entries=st.lists(
        st.fixed_dictionaries(
            {
                "model": st.sampled_from(["m1", "m2", "unmapped"]),
                "payload": st.integers(),
            }
        ),
        max_size=8,
    )
)
def test_property_anonymize_preserves_non_model_keys_and_maps_model_field(entries):
    """Property (AC10/AC12/AC13, AC15): for any list of dict entries with a
    "model" key plus arbitrary opaque payload, anonymization replaces only
    "model" (via the map, or leaves it if absent from the map) and leaves
    every other key untouched."""
    model_to_label = {"m1": "Response A", "m2": "Response B"}
    entries_before = copy.deepcopy(entries)

    out1, _, _ = ca._anonymize_for_stage3(entries, [], None, model_to_label)

    assert entries == entries_before  # no mutation
    assert len(out1) == len(entries)
    for original, transformed in zip(entries, out1):
        assert transformed["payload"] == original["payload"]
        expected_model = model_to_label.get(original["model"], original["model"])
        assert transformed["model"] == expected_model


# ---------------------------------------------------------------------------
# Function C: _resolve_response_labels
# ---------------------------------------------------------------------------


def test_ac17_single_label_occurrence_replaced_with_real_model_name():
    """AC17: a label occurring once in text is replaced with the real
    model name."""
    result = ca._resolve_response_labels("Response A said this.", {"real-model-x": "Response A"})
    assert result == "real-model-x said this."


def test_ac18_multiple_occurrences_of_same_label_all_replaced():
    """AC18: both occurrences of a repeated label are replaced; no
    remaining literal "Response A" in the output."""
    text = "Response A agrees with Response A on this point."
    result = ca._resolve_response_labels(text, {"real-model-x": "Response A"})

    assert result.count("real-model-x") == 2
    assert "Response A" not in result


def test_ac19_multiple_distinct_labels_resolve_without_cross_contamination():
    """AC19: two distinct labels resolve to their own respective real model
    names, no cross-contamination."""
    model_to_label = {"m1": "Response A", "m2": "Response B"}
    result = ca._resolve_response_labels("Response A and Response B disagree.", model_to_label)
    assert result == "m1 and m2 disagree."


def test_ac20_text_with_no_matching_label_returned_unchanged():
    """AC20: text containing no occurrence of any label -> unchanged
    no-op."""
    text = "Plain prose with no matching substring at all."
    result = ca._resolve_response_labels(text, {"m1": "Response A"})
    assert result == text


def test_ac21_empty_model_to_label_returns_text_unchanged():
    """AC21: model_to_label={} -> result equals text unchanged, for any
    text."""
    text = "Response A said something."
    result = ca._resolve_response_labels(text, {})
    assert result == text


def test_ac22_longer_label_not_corrupted_by_shorter_label_prefix_match():
    """AC22: "Response AA" (contains "Response A" as a prefix) must resolve
    correctly to m2, not be partially corrupted by "Response A" -> m1
    running first."""
    model_to_label = {"m1": "Response A", "m2": "Response AA"}
    result = ca._resolve_response_labels("Response AA said X", model_to_label)
    assert result == "m2 said X"


def test_ac23_does_not_mutate_map_and_never_raises_on_malformed_text():
    """AC23: does not mutate model_to_label; never raises, including on
    empty string and unicode text."""
    model_to_label = {"m1": "Response A"}
    model_to_label_before = copy.deepcopy(model_to_label)

    # Empty string.
    assert ca._resolve_response_labels("", model_to_label) == ""
    # Unicode / emoji text with no label present.
    unicode_text = "éèê 🎉 no labels here 中文"
    assert ca._resolve_response_labels(unicode_text, model_to_label) == unicode_text

    assert model_to_label == model_to_label_before


# Property-based test for Function C: resolving is idempotent once no
# labels remain, and every occurrence of every label is fully replaced
# (round-trip law: label -> real name -> no more raw labels left in text).
@settings(max_examples=50, derandomize=True, deadline=None)
@given(
    n_a=st.integers(min_value=0, max_value=4),
    n_b=st.integers(min_value=0, max_value=4),
)
def test_property_all_label_occurrences_fully_resolved_and_result_is_idempotent(n_a, n_b):
    """Property (AC17-19): for text built from an arbitrary interleaving of
    "Response A"/"Response B" occurrences plus filler, after resolution no
    raw label substring remains, and re-running resolution on the output
    (now containing only real model names, not labels) is a no-op --
    resolution is idempotent once applied."""
    model_to_label = {"m1": "Response A", "m2": "Response B"}
    parts = ["Response A"] * n_a + ["Response B"] * n_b
    text = " filler ".join(parts) if parts else "no labels at all"

    resolved_once = ca._resolve_response_labels(text, model_to_label)

    assert "Response A" not in resolved_once
    assert "Response B" not in resolved_once
    assert resolved_once.count("m1") == n_a
    assert resolved_once.count("m2") == n_b

    # Idempotence: resolving already-resolved text (which now contains real
    # model names m1/m2, not labels) is a no-op.
    resolved_twice = ca._resolve_response_labels(resolved_once, model_to_label)
    assert resolved_twice == resolved_once


# ---------------------------------------------------------------------------
# Human-legibility carve-out (design decision, not a numbered AC, but stated
# as a hard requirement in the contract's "Design decision" section: the
# chairman's synthesis text must never leak a raw internal label to a human
# reader once Function C has run over it).
# ---------------------------------------------------------------------------


def test_human_legibility_carveout_full_pipeline_of_ab_leaves_no_raw_labels():
    """Given/When/Then encoding the contract's "Human-legibility carve-out":
    Given a chairman synthesis mentioning two anonymized responses by label,
    When _resolve_response_labels is applied using the same model_to_label
    that anonymized the prompt, Then the human-facing text contains zero
    raw "Response X" labels and only real model identities."""
    model_to_label = {"anthropic/claude-opus-4.8": "Response A", "openai/gpt-5.2": "Response B"}
    chairman_text = (
        "After reviewing the drafts, Response A provided the most complete "
        "analysis, while Response B raised a valid counterpoint that "
        "Response A did not fully address."
    )

    resolved = ca._resolve_response_labels(chairman_text, model_to_label)

    assert "Response A" not in resolved
    assert "Response B" not in resolved
    assert "anthropic/claude-opus-4.8" in resolved
    assert "openai/gpt-5.2" in resolved
