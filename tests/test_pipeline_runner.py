"""Tests for pipeline_runner.py, derived from docs/specs/pipeline-runner-contract.md.

Uses fake fetch_evidence/council_fn/query_model - never real network calls.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from scripts.pipeline_runner import (
    PipelineConfig,
    PipelineResult,
    _build_arg_parser,
    _compute_outliers,
    build_critique_from_rubric,
    exit_code_for_result,
    extract_rubric_scores_for_scorecard,
    make_output_dir,
    run_pipeline,
    slugify,
)
from scripts.grounding_pass import Claim, Evidence
from scripts.scorecard import load_records


def _stage2_results_fixture():
    label_to_model = {
        "Response A": {"model": "model-x", "display_index": 0},
        "Response B": {"model": "model-y", "display_index": 1},
    }
    stage2_results = [
        {
            "model": "model-y",  # reviewer
            "ranking": "raw text",
            "parsed_ranking": {
                "evaluations": {
                    "Response A": {
                        "accuracy": 8,
                        "relevance": 7,
                        "completeness": 6,
                        "conciseness": 9,
                        "clarity": 8,
                    },
                },
                "rubric_scoring": True,
            },
        },
        {
            "model": "model-x",  # reviewer
            "ranking": "raw text",
            "parsed_ranking": {
                "evaluations": {
                    "Response B": {
                        "accuracy": 4,
                        "relevance": 5,
                        "completeness": 3,
                        "conciseness": 6,
                        "clarity": 5,
                    },
                },
                "rubric_scoring": True,
            },
        },
    ]
    return stage2_results, label_to_model


def _council_result_fixture(css=0.8, cost_x=0.01, cost_y=0.02):
    stage1_results = [
        {"model": "model-x", "response": "Answer from X"},
        {"model": "model-y", "response": "Answer from Y"},
    ]
    stage2_results, label_to_model = _stage2_results_fixture()
    aggregate_rankings = [
        {"model": "model-x", "borda_score": 1.0, "rank": 1},
        {"model": "model-y", "borda_score": 0.0, "rank": 2},
    ]
    stage3_result = {"model": "model-x", "response": "Final synthesis text"}
    metadata = {
        "status": "complete",
        "quality_metrics": {"core": {"consensus_strength": css}},
        "aggregate_rankings": aggregate_rankings,
        "label_to_model": label_to_model,
        "usage": {
            "by_model": {
                "model-x": {"cost_usd": cost_x},
                "model-y": {"cost_usd": cost_y},
            },
            "total": {"cost_usd": cost_x + cost_y},
        },
    }
    return stage1_results, stage2_results, stage3_result, metadata


class FakeQueryModel:
    def __init__(self, response_by_model=None, cost_per_call=0.0):
        self.calls = []
        self.response_by_model = response_by_model or {}
        self.cost_per_call = cost_per_call

    async def __call__(self, model: str, prompt: str) -> tuple[str, float]:
        self.calls.append((model, prompt))
        return self.response_by_model.get(model, "no revision"), self.cost_per_call


def _make_council_fn(result):
    calls = []

    async def council_fn(query: str):
        calls.append(query)
        return result

    council_fn.calls = calls
    return council_fn


def _make_fetch_evidence(evidence_by_claim_id=None):
    calls = []

    async def fetch_evidence(claims):
        calls.append(list(claims))
        return evidence_by_claim_id or {}

    fetch_evidence.calls = calls
    return fetch_evidence


def _run(config, fetch_evidence, council_fn, query_model):
    return asyncio.run(run_pipeline(config, fetch_evidence, council_fn, query_model))


# --- AC1: empty raw_claims_text -> grounding skipped entirely ---


def test_ac1_empty_claims_text_skips_grounding_entirely(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(
        topic_label="test topic",
        query="a question",
        raw_claims_text="",
        output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert fetch_evidence.calls == []
    assert not (result.output_dir / "grounding.md").exists()


# --- AC2: non-empty raw_claims_text -> grounding.md written ---


def test_ac2_nonempty_claims_text_writes_grounding_md(tmp_path):
    evidence = {
        "1": [Evidence(source="http://example.com", date="2026-08-09", supports=True)]
    }
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(
        topic_label="test topic",
        query="a question",
        raw_claims_text="1. Some claim.",
        output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert len(fetch_evidence.calls) == 1
    assert fetch_evidence.calls[0] == [Claim(id="1", text="Some claim.")]
    grounding_path = result.output_dir / "grounding.md"
    assert grounding_path.exists()
    assert "VERIFIED" in grounding_path.read_text()


# --- AC3: CSS >= 0.50 -> no revision, query_model never called ---


def test_ac3_high_css_skips_revision(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.6))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="the actual question", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert query_model.calls == []
    assert result.revision_triggered is False
    assert result.css == 0.6
    assert result.synthesis == "Final synthesis text"
    assert council_fn.calls == ["the actual question"]


# --- AC4: CSS < 0.50 and no cost ceiling -> revision triggered ---


def test_ac4_low_css_triggers_revision(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.3))
    query_model = FakeQueryModel()

    config = PipelineConfig(
        topic_label="t", query="q", output_root=tmp_path, max_cost_usd=None,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.revision_triggered is True
    assert result.revision_skipped_for_cost is False
    assert result.css == 0.3
    called_models = {m for m, _ in query_model.calls}
    assert called_models == {"model-x", "model-y"}
    # each model's own original answer text must reach the revision prompt
    prompt_for_x = next(p for m, p in query_model.calls if m == "model-x")
    assert "Answer from X" in prompt_for_x
    prompt_for_y = next(p for m, p in query_model.calls if m == "model-y")
    assert "Answer from Y" in prompt_for_y
    # and never the OTHER model's answer (critique must be model-specific)
    assert "Answer from Y" not in prompt_for_x
    assert "Answer from X" not in prompt_for_y
    # the critique itself (model-x's own rubric scores, accuracy=8) must reach
    # the prompt - proves build_critique_from_rubric was called with the
    # RIGHT model, not None or the wrong model
    assert "accuracy: 8.0/10" in prompt_for_x
    assert "accuracy: 4.0/10" in prompt_for_y


def test_revision_cost_is_real_not_hardcoded_zero(tmp_path):
    # Regression test for the panel-confirmed bug: revision_cost was
    # hardcoded to 0.0 regardless of what query_model actually reported.
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.3, cost_x=0.01, cost_y=0.02))
    query_model = FakeQueryModel(cost_per_call=0.05)

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.revision_triggered is True
    # stage1-3 cost (0.03) + 2 revision calls at 0.05 each = 0.13
    assert result.total_cost_usd == pytest.approx(0.13)


@given(
    cost_x=st.floats(min_value=0.0, max_value=5.0, allow_nan=False),
    cost_y=st.floats(min_value=0.0, max_value=5.0, allow_nan=False),
    revision_cost_per_call=st.floats(min_value=0.0, max_value=5.0, allow_nan=False),
)
@settings(deadline=None)
def test_property_total_cost_always_equals_sum_of_all_stage_costs(
    tmp_path_factory, cost_x, cost_y, revision_cost_per_call
):
    tmp_path = tmp_path_factory.mktemp("cost_prop")
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.1, cost_x=cost_x, cost_y=cost_y))
    query_model = FakeQueryModel(cost_per_call=revision_cost_per_call)

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    expected = cost_x + cost_y + 2 * revision_cost_per_call
    assert result.total_cost_usd == pytest.approx(expected)


# --- cost_ceiling_exceeded: post-revision ceiling re-check (panel finding) ---


def test_cost_ceiling_exceeded_false_when_no_ceiling_set(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path, max_cost_usd=None)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.cost_ceiling_exceeded is False


def test_cost_ceiling_exceeded_true_when_revision_pushes_past_ceiling(tmp_path):
    # The pre-revision check only compares stage1-3 cost (0.03) to the
    # ceiling and lets revision proceed since 0.03 < 0.10. But two revision
    # calls at 0.05 each push the real total to 0.13, past the ceiling -
    # this must be visible in the result even though it can't be prevented
    # after the fact (the pre-check already let revision start).
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.3, cost_x=0.01, cost_y=0.02))
    query_model = FakeQueryModel(cost_per_call=0.05)

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path, max_cost_usd=0.10)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.revision_triggered is True
    assert result.total_cost_usd == pytest.approx(0.13)
    assert result.cost_ceiling_exceeded is True


def test_cost_ceiling_not_exceeded_when_total_exactly_equals_ceiling(tmp_path):
    # Boundary: being exactly AT the ceiling is not "exceeded" (strict >,
    # matching should_trigger_revision's own strict < semantics elsewhere
    # in this pipeline).
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9, cost_x=0.01, cost_y=0.02))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path, max_cost_usd=0.03)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.total_cost_usd == pytest.approx(0.03)
    assert result.cost_ceiling_exceeded is False


def test_cost_ceiling_not_exceeded_when_total_is_under_ceiling(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.3, cost_x=0.01, cost_y=0.02))
    query_model = FakeQueryModel(cost_per_call=0.01)

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path, max_cost_usd=10.0)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.cost_ceiling_exceeded is False


# --- AC5: CSS < 0.50 but cost ceiling already met -> revision skipped for cost ---


def test_ac5_low_css_but_cost_ceiling_met_skips_revision(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    # stage1-3 cost is 0.03 (0.01 + 0.02) in the fixture
    council_fn = _make_council_fn(_council_result_fixture(css=0.3, cost_x=0.01, cost_y=0.02))
    query_model = FakeQueryModel()

    config = PipelineConfig(
        topic_label="t", query="q", output_root=tmp_path, max_cost_usd=0.03,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert query_model.calls == []
    assert result.revision_triggered is False
    assert result.revision_skipped_for_cost is True
    assert "Final synthesis text" in result.synthesis
    assert result.total_cost_usd == pytest.approx(0.03)


# --- AC6: exactly one scorecard record appended ---


def test_ac6_appends_exactly_one_scorecard_record(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9, cost_x=0.011, cost_y=0.022))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="my topic", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.scorecard_appended is True
    scorecard_path = tmp_path / "scorecard.jsonl"
    assert scorecard_path.exists()
    records = load_records(scorecard_path)
    assert len(records) == 1
    record = records[0]
    assert record.topic_label == "my topic"
    assert record.css == 0.9
    assert record.rubric_scores["model-x"]["accuracy"] == 8
    assert record.rubric_scores["model-y"]["accuracy"] == 4
    assert record.ranks == {"model-x": 1, "model-y": 2}
    assert record.is_outlier == {"model-x": False, "model-y": False}
    assert record.cost_usd == {"model-x": 0.011, "model-y": 0.022}
    assert record.timestamp  # non-empty, present


def test_ac6_two_runs_append_two_records_not_overwrite(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t1", query="q", output_root=tmp_path)
    _run(config, fetch_evidence, council_fn, query_model)
    config2 = PipelineConfig(topic_label="t2", query="q", output_root=tmp_path)
    _run(config2, fetch_evidence, council_fn, query_model)

    records = load_records(tmp_path / "scorecard.jsonl")
    assert len(records) == 2


# --- AC7: build_critique_from_rubric with zero reviewers -> no crash ---


def test_ac7_zero_reviewers_returns_clear_message_not_crash():
    critique = build_critique_from_rubric("model-z", [], {})
    assert critique == "No peer scores available for this response."


def test_ac7_model_not_in_populated_label_to_model_returns_clear_message():
    stage2_results, label_to_model = _stage2_results_fixture()
    critique = build_critique_from_rubric("model-not-present", stage2_results, label_to_model)
    assert critique == "No peer scores available for this response."


def test_ac7_build_critique_from_rubric_real_data():
    stage2_results, label_to_model = _stage2_results_fixture()
    critique = build_critique_from_rubric("model-x", stage2_results, label_to_model)
    # model-x's response (Response A) was scored by exactly 1 reviewer
    # (model-y): accuracy=8, relevance=7, completeness=6, conciseness=9, clarity=8
    # -> weakest is completeness at 6
    assert critique == (
        "Reviewers scored your response (1 reviewer(s)) — "
        "accuracy: 8.0/10, relevance: 7.0/10, completeness: 6.0/10, "
        "conciseness: 9.0/10, clarity: 8.0/10. "
        "Weakest dimension: completeness (6.0)."
    )


def test_rubric_scores_for_model_missing_parsed_ranking_key_yields_empty_not_crash():
    stage2_results = [{"model": "model-y", "ranking": "raw text"}]  # no parsed_ranking at all
    label_to_model = {"Response A": {"model": "model-x", "display_index": 0}}
    critique = build_critique_from_rubric("model-x", stage2_results, label_to_model)
    assert critique == "No peer scores available for this response."


def test_rubric_scores_for_model_missing_evaluations_key_yields_empty_not_crash():
    stage2_results = [
        {"model": "model-y", "ranking": "raw text", "parsed_ranking": {"rubric_scoring": True}}
    ]
    label_to_model = {"Response A": {"model": "model-x", "display_index": 0}}
    critique = build_critique_from_rubric("model-x", stage2_results, label_to_model)
    assert critique == "No peer scores available for this response."


# --- AC8: output dir created even if parent missing ---


def test_ac8_creates_missing_parent_directories(tmp_path):
    deep_root = tmp_path / "a" / "b" / "c"
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="My Cool Topic", query="q", output_root=deep_root)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.output_dir.exists()
    assert result.output_dir.is_relative_to(deep_root)
    assert "my-cool-topic" in result.output_dir.name
    # timestamp prefix present, e.g. 2026-08-09T12-00-00
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-", result.output_dir.name)


def test_output_root_none_defaults_to_cwd_council_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=None)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.output_dir.is_relative_to(tmp_path / "council-runs")
    scorecard_path = tmp_path / "council-runs" / "scorecard.jsonl"
    assert scorecard_path.exists()


def test_grounding_leaves_no_stray_intermediate_file(tmp_path):
    evidence = {
        "1": [Evidence(source="http://example.com", date="2026-08-09", supports=True)]
    }
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(
        topic_label="t", query="q", raw_claims_text="1. Some claim.", output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert sorted(p.name for p in result.output_dir.iterdir()) == [
        "grounding.md",
        "run_status.json",
    ]


def test_ac8_make_output_dir_creates_full_path(tmp_path):
    out = make_output_dir(tmp_path / "x" / "y", "My Topic", "2026-08-09T12-00-00")
    assert out.exists()
    assert out.parent == tmp_path / "x" / "y"


def test_make_output_dir_is_idempotent_same_args_does_not_raise(tmp_path):
    out1 = make_output_dir(tmp_path, "Same Topic", "2026-08-09T12-00-00")
    out2 = make_output_dir(tmp_path, "Same Topic", "2026-08-09T12-00-00")
    assert out1 == out2
    assert out1.exists()


# --- AC9: slugify ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Should we adopt X?", "should-we-adopt-x"),
        ("  Leading/trailing  ", "leading-trailing"),
        ("Multiple   spaces---and--dashes", "multiple-spaces-and-dashes"),
        ("MiXeD CaSe", "mixed-case"),
    ],
)
def test_ac9_slugify(raw, expected):
    assert slugify(raw) == expected


def test_ac9_slugify_never_has_leading_or_trailing_hyphen():
    assert not slugify("---weird---").startswith("-")
    assert not slugify("---weird---").endswith("-")


# --- AC10: total_cost_usd reflects stage1-3 + revision cost ---


def test_ac10_total_cost_includes_stage1to3_cost_when_no_revision(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9, cost_x=0.01, cost_y=0.02))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.total_cost_usd == pytest.approx(0.03)


# --- Regression: verified_facts from grounding must actually reach the
# revision round and the Stage 4 completeness check, not be silently
# discarded. pipeline_runner used to hardcode verified_facts=[] and never
# populate it from run_grounding_pass's tagged claims - grounding.md would
# be written correctly to disk, but neither revision_round nor (once added)
# completeness_check ever received a real citable fact even when grounding
# ran and produced a VERIFIED/CONTRADICTED tag. ---


def test_verified_facts_from_grounding_reach_revision_prompt(tmp_path):
    evidence = {"1": [Evidence(source="http://example.com", date="2026-01-01", supports=True)]}
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.3))
    query_model = FakeQueryModel()

    config = PipelineConfig(
        topic_label="t",
        query="q",
        raw_claims_text="1. UNIQUE_GROUNDED_CLAIM_TEXT.",
        output_root=tmp_path,
    )
    _run(config, fetch_evidence, council_fn, query_model)

    revision_prompts = [p for m, p in query_model.calls if m in ("model-x", "model-y")]
    assert len(revision_prompts) == 2
    for prompt in revision_prompts:
        assert "UNIQUE_GROUNDED_CLAIM_TEXT" in prompt
        assert "VERIFIED" in prompt


def test_contradicted_claims_from_grounding_reach_revision_prompt(tmp_path):
    evidence = {
        "1": [Evidence(source="http://example.com", date="2026-01-01", supports=False)]
    }
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.3))
    query_model = FakeQueryModel()

    config = PipelineConfig(
        topic_label="t",
        query="q",
        raw_claims_text="1. UNIQUE_CONTRADICTED_CLAIM_TEXT.",
        output_root=tmp_path,
    )
    _run(config, fetch_evidence, council_fn, query_model)

    revision_prompts = [p for m, p in query_model.calls if m in ("model-x", "model-y")]
    assert len(revision_prompts) == 2
    for prompt in revision_prompts:
        assert "UNIQUE_CONTRADICTED_CLAIM_TEXT" in prompt
        assert "CONTRADICTED" in prompt


def test_unverifiable_claims_from_grounding_do_not_reach_revision_prompt(tmp_path):
    # No evidence supplied -> tag_claim yields UNVERIFIABLE, which must be
    # filtered OUT of verified_facts (only VERIFIED/CONTRADICTED qualify).
    fetch_evidence = _make_fetch_evidence({})
    council_fn = _make_council_fn(_council_result_fixture(css=0.3))
    query_model = FakeQueryModel()

    config = PipelineConfig(
        topic_label="t",
        query="q",
        raw_claims_text="1. UNGROUNDED_CLAIM_TEXT.",
        output_root=tmp_path,
    )
    _run(config, fetch_evidence, council_fn, query_model)

    revision_prompts = [p for m, p in query_model.calls if m in ("model-x", "model-y")]
    for prompt in revision_prompts:
        assert "UNGROUNDED_CLAIM_TEXT" not in prompt


# --- Stage 4 completeness check wiring ---


def test_completeness_check_not_called_when_no_grounding(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.dropped_facts == []
    assert result.completeness_check_skipped_for_cost is False
    assert query_model.calls == []


def test_completeness_check_called_once_when_grounding_ran(tmp_path):
    evidence = {"1": [Evidence(source="http://x.com", date="2026-01-01", supports=True)]}
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))  # no revision
    query_model = FakeQueryModel(response_by_model={"google/gemini-3.6-flash": '["1"]'})

    config = PipelineConfig(
        topic_label="t",
        query="q",
        raw_claims_text="1. UNIQUE_COMPLETENESS_CLAIM_TEXT.",
        output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    completeness_calls = [c for c in query_model.calls if c[0] == "google/gemini-3.6-flash"]
    assert len(completeness_calls) == 1
    # the actual synthesis text must reach the completeness prompt, not be
    # swapped for a placeholder
    assert "Final synthesis text" in completeness_calls[0][1]
    assert "UNIQUE_COMPLETENESS_CLAIM_TEXT" in completeness_calls[0][1]
    assert result.dropped_facts == ["1"]


def test_completeness_check_cost_included_in_total_cost(tmp_path):
    evidence = {"1": [Evidence(source="http://x.com", date="2026-01-01", supports=True)]}
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.9, cost_x=0.01, cost_y=0.02))
    query_model = FakeQueryModel(cost_per_call=0.05)

    config = PipelineConfig(
        topic_label="t", query="q", raw_claims_text="1. Some claim.", output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.total_cost_usd == pytest.approx(0.03 + 0.05)


def test_total_cost_accumulates_revision_and_completeness_cost_additively(tmp_path):
    # Distinguishes cost_so_far += completeness_check_cost from = or -=:
    # with both revision (css<0.50) and completeness check running, total
    # must equal stage1to3 + revision + completeness, not just the last
    # cost written or a subtraction.
    evidence = {"1": [Evidence(source="http://x.com", date="2026-01-01", supports=True)]}
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.3, cost_x=0.01, cost_y=0.02))
    query_model = FakeQueryModel(cost_per_call=0.05)

    config = PipelineConfig(
        topic_label="t",
        query="q",
        raw_claims_text="1. Some claim.",
        output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    # stage1to3 (0.03) + revision (2 calls x 0.05) + completeness (1 call x 0.05)
    assert result.total_cost_usd == pytest.approx(0.03 + 0.10 + 0.05)


def test_cost_so_far_includes_completeness_check_cost_not_overwritten(tmp_path, monkeypatch):
    # Distinguishes cost_so_far += completeness_check_cost from = or -=:
    # total_cost_usd is computed independently (stage1to3 + revision +
    # completeness), so it can't catch this - only cost_so_far, surfaced
    # via run_status.json on a failure after the completeness check ran.
    import scripts.pipeline_runner as pr_module

    def exploding_append_record(record, path):
        raise OSError("disk full")

    monkeypatch.setattr(pr_module, "append_record", exploding_append_record)

    evidence = {"1": [Evidence(source="http://x.com", date="2026-01-01", supports=True)]}
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.1, cost_x=0.01, cost_y=0.02))
    query_model = FakeQueryModel(cost_per_call=0.05)

    config = PipelineConfig(
        topic_label="t", query="q", raw_claims_text="1. Some claim.", output_root=tmp_path,
    )
    with pytest.raises(OSError):
        _run(config, fetch_evidence, council_fn, query_model)

    run_dirs = list(tmp_path.iterdir())
    status = _read_run_status(run_dirs[0])
    # stage1-3 (0.03) + 2 revision calls (0.10) + 1 completeness call (0.05)
    assert status["cost_so_far_usd"] == pytest.approx(0.18)


def test_completeness_check_skipped_when_cost_ceiling_already_met(tmp_path):
    evidence = {"1": [Evidence(source="http://x.com", date="2026-01-01", supports=True)]}
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.9, cost_x=0.05, cost_y=0.05))
    query_model = FakeQueryModel(cost_per_call=0.05)

    config = PipelineConfig(
        topic_label="t",
        query="q",
        raw_claims_text="1. Some claim.",
        output_root=tmp_path,
        max_cost_usd=0.10,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.completeness_check_skipped_for_cost is True
    assert result.dropped_facts == []
    assert query_model.calls == []


# --- _compute_outliers ---


def test_compute_outliers_single_model_never_flagged():
    result = _compute_outliers([{"model": "solo", "borda_score": 1.0}])
    assert result == {"solo": False}


def test_compute_outliers_flags_genuine_statistical_outlier():
    rankings = [
        {"model": "leader", "borda_score": 1.0},
        {"model": "middle", "borda_score": 0.9},
        {"model": "laggard", "borda_score": 0.1},
    ]
    result = _compute_outliers(rankings)
    assert result == {"leader": False, "middle": False, "laggard": True}


def test_compute_outliers_zero_variance_never_flags_anyone():
    # all scores identical -> median==every score==threshold; must use
    # strict "<" so nobody is flagged when there's no real spread at all
    rankings = [
        {"model": "a", "borda_score": 5.0},
        {"model": "b", "borda_score": 5.0},
        {"model": "c", "borda_score": 5.0},
    ]
    result = _compute_outliers(rankings)
    assert result == {"a": False, "b": False, "c": False}


# --- extract_rubric_scores_for_scorecard ---


def test_extract_rubric_scores_for_scorecard_shape():
    stage2_results, label_to_model = _stage2_results_fixture()
    scores = extract_rubric_scores_for_scorecard(stage2_results, label_to_model)
    assert scores["model-x"]["accuracy"] == 8
    assert scores["model-y"]["accuracy"] == 4


# --- AC11-15: run_status.json crash/interrupt-safety lifecycle ---


def _read_run_status(output_dir):
    return json.loads((output_dir / "run_status.json").read_text())


def test_ac11_running_status_written_before_expensive_work(tmp_path):
    # A council_fn that raises immediately still leaves a "running" marker,
    # proving the status is written BEFORE council_fn is even called, not
    # only recorded retroactively on success.
    async def failing_council_fn(query):
        raise RuntimeError("network down")

    fetch_evidence = _make_fetch_evidence()
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="crash test", query="q", output_root=tmp_path)
    with pytest.raises(RuntimeError):
        _run(config, fetch_evidence, failing_council_fn, query_model)

    run_dirs = list(tmp_path.iterdir())
    assert len(run_dirs) == 1
    status = _read_run_status(run_dirs[0])
    assert status["status"] == "failed"


def test_ac11_running_status_literal_string_correct(tmp_path):
    # council_fn peeks at run_status.json WHILE it's still "running" (before
    # this function overwrites it to "complete"), to verify the literal
    # status string content independently of the final complete/failed state.
    seen_status = {}

    async def peeking_council_fn(query):
        result = _council_result_fixture(css=0.9)
        # output_dir isn't available here directly; reconstruct via closure
        # over the known tmp_path root (single run in this test).
        run_dirs = list(tmp_path.iterdir())
        assert len(run_dirs) == 1
        seen_status.update(_read_run_status(run_dirs[0]))
        return result

    fetch_evidence = _make_fetch_evidence()
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    _run(config, fetch_evidence, peeking_council_fn, query_model)

    assert seen_status["status"] == "running"


def test_ac12_complete_status_written_on_success(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9, cost_x=0.01, cost_y=0.02))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    status = _read_run_status(result.output_dir)
    assert status["status"] == "complete"
    assert status["total_cost_usd"] == pytest.approx(0.03)


def test_ac13_failed_status_includes_error_and_cost_so_far_after_council_succeeds(tmp_path):
    # Council succeeds (stage1-3 cost known: 0.03), but revision's query_fn
    # blows up - the run fails AFTER real money was already spent, and that
    # spend must be visible in the failure record, not silently lost.
    async def exploding_query_model(model, prompt):
        raise ConnectionError("revision call failed")

    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.1, cost_x=0.01, cost_y=0.02))

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    with pytest.raises(ConnectionError):
        _run(config, fetch_evidence, council_fn, exploding_query_model)

    run_dirs = list(tmp_path.iterdir())
    assert len(run_dirs) == 1
    status = _read_run_status(run_dirs[0])
    assert status["status"] == "failed"
    assert "revision call failed" in status["error"]
    assert status["cost_so_far_usd"] == pytest.approx(0.03)


def test_ac13_failed_status_cost_so_far_is_zero_when_council_never_returns(tmp_path):
    async def failing_council_fn(query):
        raise RuntimeError("boom")

    fetch_evidence = _make_fetch_evidence()
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    with pytest.raises(RuntimeError):
        _run(config, fetch_evidence, failing_council_fn, query_model)

    run_dirs = list(tmp_path.iterdir())
    status = _read_run_status(run_dirs[0])
    assert status["cost_so_far_usd"] == 0.0


def test_cost_so_far_accumulates_stage1to3_plus_revision_not_overwritten(tmp_path, monkeypatch):
    # Force a failure AFTER a successful revision round but before
    # run_pipeline returns (during scorecard append), to prove cost_so_far
    # is stage1to3_cost + revision_cost at that point, not just
    # revision_cost alone (which would happen if += were = or -=).
    import scripts.pipeline_runner as pr_module

    def exploding_append_record(record, path):
        raise OSError("disk full")

    monkeypatch.setattr(pr_module, "append_record", exploding_append_record)

    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.1, cost_x=0.01, cost_y=0.02))
    query_model = FakeQueryModel(cost_per_call=0.05)

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    with pytest.raises(OSError):
        _run(config, fetch_evidence, council_fn, query_model)

    run_dirs = list(tmp_path.iterdir())
    status = _read_run_status(run_dirs[0])
    # stage1-3 (0.03) + 2 revision calls at 0.05 each (0.10) = 0.13
    assert status["cost_so_far_usd"] == pytest.approx(0.13)


def test_ac14_run_status_written_atomically_no_tmp_file_left_behind(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert (result.output_dir / "run_status.json").exists()
    assert not (result.output_dir / "run_status.json.tmp").exists()


def test_ac15_original_exception_type_and_message_preserved_not_swallowed(tmp_path):
    class CustomError(ValueError):
        pass

    async def failing_council_fn(query):
        raise CustomError("very specific message")

    fetch_evidence = _make_fetch_evidence()
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    with pytest.raises(CustomError, match="very specific message"):
        _run(config, fetch_evidence, failing_council_fn, query_model)


# --- exit_code_for_result: AC16-20 exit-code contract ---


def _result(cost_ceiling_exceeded=False, revision_skipped_for_cost=False):
    return PipelineResult(
        output_dir=Path("/tmp/x"),
        css=0.5,
        revision_triggered=False,
        revision_skipped_for_cost=revision_skipped_for_cost,
        total_cost_usd=1.0,
        scorecard_appended=True,
        synthesis="s",
        cost_ceiling_exceeded=cost_ceiling_exceeded,
    )


def test_ac16_plain_success_exits_zero():
    assert exit_code_for_result(_result()) == 0


def test_ac17_revision_skipped_for_cost_exits_two():
    assert exit_code_for_result(_result(revision_skipped_for_cost=True)) == 2


def test_ac18_cost_ceiling_exceeded_exits_three():
    assert exit_code_for_result(_result(cost_ceiling_exceeded=True)) == 3


def test_ac20_ceiling_exceeded_outranks_revision_skipped():
    assert (
        exit_code_for_result(
            _result(cost_ceiling_exceeded=True, revision_skipped_for_cost=True)
        )
        == 3
    )


# --- _build_arg_parser: CLI surface ---


def test_arg_parser_requires_topic_label_and_query():
    parser = _build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_arg_parser_requires_topic_label_when_only_query_given():
    parser = _build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--query", "q"])


def test_arg_parser_requires_query_when_only_topic_label_given():
    parser = _build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--topic-label", "t"])


def test_arg_parser_prog_and_description_are_set():
    parser = _build_arg_parser()
    assert parser.prog == "llm-council-pipeline"
    assert parser.description == (
        "Run the full grounded council pipeline: "
        "Stage 0.5 -> 1-3.5 -> [2.75] -> 4 -> scorecard."
    )


def test_arg_parser_defaults_optional_flags_to_none():
    parser = _build_arg_parser()
    args = parser.parse_args(["--topic-label", "t", "--query", "q"])
    assert args.claims_file is None
    assert args.max_cost_usd is None
    assert args.output_root is None


def test_arg_parser_parses_all_flags():
    parser = _build_arg_parser()
    args = parser.parse_args(
        [
            "--topic-label",
            "t",
            "--query",
            "q",
            "--claims-file",
            "/tmp/claims.txt",
            "--max-cost-usd",
            "1.5",
            "--output-root",
            "/tmp/out",
        ]
    )
    assert args.claims_file == Path("/tmp/claims.txt")
    assert args.max_cost_usd == 1.5
    assert args.output_root == Path("/tmp/out")
