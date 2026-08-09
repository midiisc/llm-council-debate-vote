"""Tests for pipeline_runner.py, derived from docs/specs/pipeline-runner-contract.md.

Uses fake fetch_evidence/council_fn/query_model - never real network calls.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from scripts.pipeline_runner import (
    PipelineConfig,
    _compute_outliers,
    build_critique_from_rubric,
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
    stage3_result = {"synthesis": "Final synthesis text"}
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
    def __init__(self, response_by_model=None):
        self.calls = []
        self.response_by_model = response_by_model or {}

    async def __call__(self, model: str, prompt: str) -> str:
        self.calls.append((model, prompt))
        return self.response_by_model.get(model, "no revision")


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

    assert sorted(p.name for p in result.output_dir.iterdir()) == ["grounding.md"]


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
