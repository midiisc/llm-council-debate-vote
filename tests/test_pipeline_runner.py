"""Tests for pipeline_runner.py, derived from docs/specs/pipeline-runner-contract.md.

Uses fake fetch_evidence/council_fn/query_model - never real network calls.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

import scripts.council_adapter as _ca_module
import scripts.live_adapters as _la_module
import scripts.pipeline_runner as _pr_module
from scripts.pipeline_runner import (
    DEFAULT_MAX_WALL_CLOCK_SECONDS,
    PipelineConfig,
    PipelineResult,
    _build_arg_parser,
    _compute_outliers,
    _rubric_scores_for_model,
    _write_run_status,
    build_critique_from_rubric,
    exit_code_for_result,
    extract_rubric_scores_for_scorecard,
    main,
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

    async def council_fn(query: str, verified_facts=None):
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


def _make_fetch_evidence_with_cost(evidence_by_claim_id, cost_usd, truncated=False):
    # docs/specs/wallclock-cost-budget-contract.md, Contract 2: a REAL
    # EvidenceMap (not a plain dict), matching what live_adapters.py's
    # real_fetch_evidence actually returns - _make_fetch_evidence above
    # deliberately stays a plain-dict fake (the common case every other
    # existing test needs, mirroring getattr(x, "cost_usd", 0.0)'s default).
    from scripts.live_adapters import EvidenceMap

    async def fetch_evidence(claims):
        result = EvidenceMap(evidence_by_claim_id)
        result.cost_usd = cost_usd
        result.truncated = truncated
        return result

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


# --- Contract 2 (docs/specs/wallclock-cost-budget-contract.md): Stage 0.5
# cost tracking + truncation warning, closes Critical #5 ---


def test_stage05_real_cost_is_added_to_total_cost_usd(tmp_path):
    evidence = {"1": [Evidence(source="http://example.com", date="2026-08-09", supports=True)]}
    fetch_evidence = _make_fetch_evidence_with_cost(evidence, cost_usd=0.42)
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(
        topic_label="test topic",
        query="a question",
        raw_claims_text="1. Some claim.",
        output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.total_cost_usd >= 0.42


def test_stage05_truncation_produces_a_debug_log_line(tmp_path):
    evidence = {"1": [Evidence(source="http://example.com", date="2026-08-09", supports=True)]}
    fetch_evidence = _make_fetch_evidence_with_cost(evidence, cost_usd=0.0, truncated=True)
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(
        topic_label="test topic",
        query="a question",
        raw_claims_text="1. Some claim.",
        output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert any(
        "Stage 0.5" in line and ("truncat" in line.lower() or "cap" in line.lower())
        for line in result.debug_log
    )


def test_stage05_no_truncation_produces_no_truncation_debug_line(tmp_path):
    evidence = {"1": [Evidence(source="http://example.com", date="2026-08-09", supports=True)]}
    fetch_evidence = _make_fetch_evidence_with_cost(evidence, cost_usd=0.0, truncated=False)
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(
        topic_label="test topic",
        query="a question",
        raw_claims_text="1. Some claim.",
        output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert not any("truncat" in line.lower() for line in result.debug_log)


def test_stage05_plain_dict_fetch_evidence_costs_nothing(tmp_path):
    # A plain-dict FetchEvidenceFn (every pre-existing fake in this file)
    # must still work exactly as before - getattr defaults to 0.0/False.
    evidence = {"1": [Evidence(source="http://example.com", date="2026-08-09", supports=True)]}
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

    assert not any("truncat" in line.lower() for line in result.debug_log)


def test_raw_claims_temp_input_file_uses_exact_expected_filename(tmp_path, monkeypatch):
    # The temp file passed to run_grounding_pass is deleted immediately
    # after (input_path.unlink()), so its exact name is otherwise
    # unobservable post-hoc - intercept the call to pin it.
    captured = {}

    def fake_run_grounding_pass(input_path, evidence_map, output_dir):
        captured["input_path"] = input_path
        (output_dir / "grounding.md").write_text("VERIFIED\n")

    monkeypatch.setattr(_pr_module, "run_grounding_pass", fake_run_grounding_pass)

    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()
    config = PipelineConfig(
        topic_label="t", query="q", raw_claims_text="1. Some claim.", output_root=tmp_path,
    )
    _run(config, fetch_evidence, council_fn, query_model)

    assert captured["input_path"].name == "_raw_claims.txt"


def test_output_dir_timestamp_uses_utc_not_local_wall_clock(tmp_path):
    from datetime import datetime, timedelta, timezone

    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()
    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)

    before = datetime.now(timezone.utc)
    result = _run(config, fetch_evidence, council_fn, query_model)
    after = datetime.now(timezone.utc)

    timestamp_str = result.output_dir.name[: len("YYYY-MM-DDTHH-MM-SS")]
    parsed = datetime.strptime(timestamp_str, "%Y-%m-%dT%H-%M-%S").replace(tzinfo=timezone.utc)

    # A couple of seconds of slack for strftime's 1-second resolution - a
    # naive-local-time implementation would be off by this sandbox's
    # Asia/Kolkata UTC+05:30 offset, vastly outside this window.
    assert before - timedelta(seconds=2) <= parsed <= after + timedelta(seconds=2)


def test_revision_prompt_receives_config_query_as_source_document(tmp_path):
    # docs/specs/pipeline-runner-contract.md, "Amendment (2026-08-12)":
    # run_revision_round is now called with source_document=config.query -
    # verify that exact text (not None, not omitted) reaches the actual
    # revision prompt sent to query_model.
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.3))  # triggers revision
    query_model = FakeQueryModel()

    config = PipelineConfig(
        topic_label="t",
        query="UNIQUE_SOURCE_DOCUMENT_MARKER_TEXT",
        output_root=tmp_path,
    )
    _run(config, fetch_evidence, council_fn, query_model)

    assert query_model.calls, "revision must have actually run for this to be meaningful"
    for _model, prompt in query_model.calls:
        assert "UNIQUE_SOURCE_DOCUMENT_MARKER_TEXT" in prompt
        assert "--- BEGIN SOURCE DOCUMENT ---" in prompt


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


# --- Contract 5 wiring: council_models=None skips audition tracking; a
# real list records every configured model, including one that never
# responded (per audition_tracking.py's own AC6) ---


def test_council_models_none_skips_audition_tracking_entirely(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = asyncio.run(
        run_pipeline(config, fetch_evidence, council_fn, query_model, council_models=None)
    )

    assert result.css == 0.9
    assert not (tmp_path / "audition.jsonl").exists()
    assert "Audition tracking" not in "\n".join(result.debug_log)


def test_council_models_given_records_every_configured_model_including_absent_one(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    # _council_result_fixture's stage1_results only has model-x/model-y -
    # "model-z" is configured but never responds this session.
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = asyncio.run(
        run_pipeline(
            config,
            fetch_evidence,
            council_fn,
            query_model,
            council_models=["model-x", "model-y", "model-z"],
        )
    )

    assert "Audition tracking: recorded" in result.debug_log
    audition_path = tmp_path / "audition.jsonl"
    assert audition_path.exists()
    lines = [json.loads(l) for l in audition_path.read_text().splitlines() if l.strip()]
    recorded_models = {rec["model_id"] for rec in lines}
    assert recorded_models == {"model-x", "model-y", "model-z"}
    z_record = next(rec for rec in lines if rec["model_id"] == "model-z")
    assert z_record["consecutive_failures"] == 1  # did not participate this session
    x_record = next(rec for rec in lines if rec["model_id"] == "model-x")
    assert x_record["consecutive_failures"] == 0  # participated


def test_output_root_none_resolves_audition_path_via_cwd_and_threads_real_rankings(
    monkeypatch, tmp_path
):
    """output_root=None must resolve the audition path via
    default_audition_path(Path.cwd()) (never a hardcoded/omitted cwd
    argument), and record_session_for_all_models must receive the run's
    own real aggregate_rankings (never a placeholder) - both silently
    unobserved by every other audition test above, which always sets
    output_root=tmp_path and never inspects quality_percentile."""
    monkeypatch.chdir(tmp_path)
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=None)
    result = asyncio.run(
        run_pipeline(
            config,
            fetch_evidence,
            council_fn,
            query_model,
            council_models=["model-x", "model-y"],
        )
    )

    assert "Audition tracking: recorded" in result.debug_log
    audition_path = tmp_path / "council-runs" / "audition.jsonl"
    assert audition_path.exists()
    lines = [json.loads(l) for l in audition_path.read_text().splitlines() if l.strip()]
    x_record = next(rec for rec in lines if rec["model_id"] == "model-x")
    y_record = next(rec for rec in lines if rec["model_id"] == "model-y")
    # _council_result_fixture's aggregate_rankings gives model-x the
    # highest borda_score (1.0) and model-y the lowest (0.0) - real
    # rankings produce distinct, non-None percentiles; a None/omitted
    # aggregate_rankings argument collapses both to None instead.
    assert x_record["quality_percentile"] == 1.0
    assert y_record["quality_percentile"] == 0.5


def test_audition_tracking_write_failure_is_non_fatal_to_the_run(tmp_path, monkeypatch):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    def _boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(_pr_module, "record_session_for_all_models", _boom)

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = asyncio.run(
        run_pipeline(
            config, fetch_evidence, council_fn, query_model, council_models=["model-x"]
        )
    )

    assert result.css == 0.9  # the run itself still succeeded
    assert any("Audition tracking: failed non-fatally" in line for line in result.debug_log)


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


def test_rubric_scores_for_model_returns_empty_dict_not_all_dims_when_model_absent():
    # The internal `label_for_model = None` sentinel (distinct from "") is
    # only observable when the model is genuinely absent from
    # label_to_model - build_critique_from_rubric's own "No peer scores"
    # early-return masks the difference (both an empty {} and an
    # all-empty-lists dict yield no truthy `averages`), so this must call
    # _rubric_scores_for_model directly.
    result = _rubric_scores_for_model(
        "absent-model", [], {"Response A": {"model": "other-model", "display_index": 0}}
    )
    assert result == {}


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

    # The actual invariant this test protects: the grounding pass's own
    # temp input file (_raw_claims.txt) must never linger after the run.
    # It does NOT enumerate every legitimate output file - that set grows
    # as durable-persistence writes land (docs/specs/durable-persistence-
    # contract.md) - so this checks for the specific stray file's absence
    # plus the two files this test always expected to exist, rather than
    # an exhaustive allowlist that would go stale on every new legitimate
    # output.
    names = {p.name for p in result.output_dir.iterdir()}
    assert "_raw_claims.txt" not in names
    assert {"grounding.md", "run_status.json"} <= names


def test_raw_claims_temp_file_cleaned_up_even_when_grounding_pass_raises(tmp_path, monkeypatch):
    # architecture-stress-test-2026-08-13.md, Medium finding: the temp file
    # write -> run_grounding_pass -> unlink sequence assumed the middle call
    # never raises. If it does, the file must still be cleaned up (a
    # try/finally around the unlink), not orphaned.
    import scripts.pipeline_runner as pr_module

    def exploding_grounding_pass(input_path, evidence_map, output_dir):
        raise RuntimeError("grounding pass blew up")

    monkeypatch.setattr(pr_module, "run_grounding_pass", exploding_grounding_pass)

    fetch_evidence = _make_fetch_evidence({"1": []})
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(
        topic_label="t", query="q", raw_claims_text="1. Some claim.", output_root=tmp_path,
    )
    with pytest.raises(RuntimeError):
        _run(config, fetch_evidence, council_fn, query_model)

    run_dirs = list(tmp_path.iterdir())
    assert len(run_dirs) == 1
    assert not (run_dirs[0] / "_raw_claims.txt").exists()


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
    assert result.completeness_check_parse_failed is False


def test_completeness_check_parse_failure_is_surfaced_not_hidden(tmp_path):
    evidence = {"1": [Evidence(source="http://x.com", date="2026-01-01", supports=True)]}
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))  # no revision
    # A malformed (non-JSON) response for the completeness check.
    query_model = FakeQueryModel(
        response_by_model={"google/gemini-3.6-flash": "not json at all"}
    )

    config = PipelineConfig(
        topic_label="t", query="q", raw_claims_text="1. Some claim.", output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    # dropped_facts==[] alone would look identical to "verified clean" -
    # the parse_failed flag is what tells the two apart.
    assert result.dropped_facts == []
    assert result.completeness_check_parse_failed is True


def test_completeness_check_parse_failed_stays_false_when_check_never_runs(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.completeness_check_parse_failed is False


# --- debug_log: per-stage transparency ---


def test_debug_log_records_grounding_skipped_and_stage_summaries(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert "Stage 0.5: skipped (no raw_claims_text)" in result.debug_log
    assert "Stage 1-3.5: council returned 2 model response(s)" in result.debug_log
    assert "Stage 2.5: CSS=0.900" in result.debug_log
    assert "Stage 2.75: skipped (CSS 0.900 >= threshold)" in result.debug_log
    assert "Stage 4: skipped (no verified facts)" in result.debug_log


def test_debug_log_records_grounding_and_revision_when_they_run(tmp_path):
    evidence = {"1": [Evidence(source="http://x.com", date="2026-01-01", supports=True)]}
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.2))  # triggers revision
    query_model = FakeQueryModel(response_by_model={"google/gemini-3.6-flash": "[]"})

    config = PipelineConfig(
        topic_label="t", query="q", raw_claims_text="1. Some claim.", output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert (
        "Stage 0.5: grounding ran, 1 claim(s) (1 verified, 0 contradicted, 0 unverifiable), "
        "cost=$0.0000"
        in result.debug_log
    )
    assert "Stage 2.75: revision triggered, 2 model(s) responded, 0 accepted" in result.debug_log
    assert "Stage 4: ran, parse succeeded, 0 fact(s) dropped" in result.debug_log


def test_debug_log_grounding_counts_each_tag_correctly(tmp_path):
    # Distinguishes each of n_verified/n_contradicted/n_unverifiable's own
    # count from the others - a single-VERIFIED-only fixture can't tell
    # "counts CONTRADICTED correctly" from "always reports 0 contradicted".
    evidence = {
        "1": [Evidence(source="http://a.com", date="2026-01-01", supports=True)],   # VERIFIED
        "2": [Evidence(source="http://b.com", date="2026-01-01", supports=False)],  # CONTRADICTED
        # claim 3: no evidence entry -> UNVERIFIABLE
    }
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel(response_by_model={"google/gemini-3.6-flash": "[]"})

    config = PipelineConfig(
        topic_label="t",
        query="q",
        raw_claims_text="1. Claim one.\n2. Claim two.\n3. Claim three.",
        output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert (
        "Stage 0.5: grounding ran, 3 claim(s) (1 verified, 1 contradicted, 1 unverifiable), "
        "cost=$0.0000"
        in result.debug_log
    )


def test_debug_log_flags_single_model_as_not_mad(tmp_path):
    fetch_evidence = _make_fetch_evidence()

    async def single_model_council_fn(query, verified_facts=None):
        stage1_results = [{"model": "model-x", "response": "Answer from X"}]
        stage2_results = []
        aggregate_rankings = [{"model": "model-x", "borda_score": 1.0, "rank": 1}]
        stage3_result = {"model": "model-x", "response": "Final synthesis text"}
        metadata = {
            "quality_metrics": {"core": {"consensus_strength": 0.9}},
            "aggregate_rankings": aggregate_rankings,
            "label_to_model": {"Response A": {"model": "model-x", "display_index": 0}},
            "usage": {
                "by_model": {"model-x": {"cost_usd": 0.01}},
                "total": {"cost_usd": 0.01},
            },
        }
        return stage1_results, stage2_results, stage3_result, metadata

    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, single_model_council_fn, query_model)

    assert (
        "WARNING: only 1 model(s) participated - this is not multi-agent debate"
        in result.debug_log
    )


def test_debug_log_no_mad_warning_with_exactly_two_models(tmp_path):
    # Boundary: 2 is the minimum for "multi-agent" - must NOT warn at
    # exactly 2 (distinguishes < 2 from <= 2 or < 3).
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))  # 2 models
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert not any("not multi-agent debate" in line for line in result.debug_log)


def test_debug_log_revision_skipped_for_cost_exact_message(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.1, cost_x=0.05, cost_y=0.05))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path, max_cost_usd=0.05)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert "Stage 2.75: skipped (would exceed max_cost_usd)" in result.debug_log


def test_debug_log_revision_accepted_count_is_accurate_not_just_len(tmp_path):
    # Distinguishes n_accepted's real count from a mutant that counts every
    # outcome as 2 (or otherwise ignores .accepted) - needs a MIX of
    # accepted/rejected outcomes to be observable.
    evidence = {"1": [Evidence(source="http://x.com", date="2026-01-01", supports=True)]}
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.1))  # triggers revision, 2 models
    query_model = FakeQueryModel(
        response_by_model={"model-x": "[[cite:1]] revised text", "model-y": "no citation"}
    )

    config = PipelineConfig(
        topic_label="t", query="q", raw_claims_text="1. Some claim.", output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert "Stage 2.75: revision triggered, 2 model(s) responded, 1 accepted" in result.debug_log


def test_debug_log_stage3_names_the_actual_chairman_model(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    # _council_result_fixture's stage3_result = {"model": "model-x", ...}
    assert "Stage 3: synthesis produced by model-x" in result.debug_log


def test_debug_log_stage3_falls_back_to_unknown_when_model_key_missing(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    stage1_results, stage2_results, stage3_result, metadata = _council_result_fixture(css=0.9)
    stage3_result = {"response": "text with no model key"}  # 'model' key deliberately absent
    council_fn = _make_council_fn((stage1_results, stage2_results, stage3_result, metadata))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert "Stage 3: synthesis produced by unknown" in result.debug_log


def test_debug_log_completeness_skipped_for_cost_exact_message(tmp_path):
    evidence = {"1": [Evidence(source="http://x.com", date="2026-01-01", supports=True)]}
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.9, cost_x=0.05, cost_y=0.05))
    query_model = FakeQueryModel()

    config = PipelineConfig(
        topic_label="t",
        query="q",
        raw_claims_text="1. Some claim.",
        output_root=tmp_path,
        max_cost_usd=0.10,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert "Stage 4: skipped (would exceed max_cost_usd)" in result.debug_log


def test_debug_log_completeness_parse_failed_exact_message(tmp_path):
    evidence = {"1": [Evidence(source="http://x.com", date="2026-01-01", supports=True)]}
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel(response_by_model={"google/gemini-3.6-flash": "not json"})

    config = PipelineConfig(
        topic_label="t", query="q", raw_claims_text="1. Some claim.", output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert (
        "Stage 4: ran, parse FAILED - completeness is UNDETERMINED, not verified"
        in result.debug_log
    )


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


def test_compute_outliers_boundary_exactly_two_scores_still_reaches_stats_computation(monkeypatch):
    # For n==2, computing median/stdev vs. short-circuiting to
    # {model: False} happen to produce the SAME returned dict (provably:
    # with exactly 2 points the "< threshold" check is always False either
    # way) - so the "< 2" vs "<= 2" vs "< 3" boundary can only be
    # distinguished by whether the real computation was actually reached,
    # not by the output value.
    calls = {"median_called_with": None}
    real_median = _pr_module.statistics.median

    def spy_median(data):
        calls["median_called_with"] = list(data)
        return real_median(data)

    monkeypatch.setattr(_pr_module.statistics, "median", spy_median)

    rankings = [
        {"model": "a", "borda_score": 3.0},
        {"model": "b", "borda_score": 1.0},
    ]
    result = _compute_outliers(rankings)

    assert calls["median_called_with"] == [3.0, 1.0]
    assert result == {"a": False, "b": False}


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


def test_write_run_status_exact_pretty_printed_json_content(tmp_path):
    # The intermediate ".tmp" filename is never observed post-call (it's
    # always renamed away before this function returns - covered instead by
    # the AC11-15 lifecycle tests below that just check the FINAL file), but
    # the exact final content - including 2-space indent - is a real,
    # directly observable contract this function's own docstring implies
    # ("never a half-written run_status.json").
    _write_run_status(tmp_path, "running", foo="bar", n=3)

    final_path = tmp_path / "run_status.json"
    assert final_path.exists()
    assert not (tmp_path / "run_status.json.tmp").exists()

    expected_payload = {"status": "running", "foo": "bar", "n": 3}

    assert final_path.read_text() == json.dumps(expected_payload, indent=2)


def test_write_run_status_uses_exact_tmp_filename_before_rename(tmp_path, monkeypatch):
    # The comment above deliberately skips checking the intermediate ".tmp"
    # name since it's gone by the time the function returns - intercept
    # Path.rename itself (still letting it actually run) to observe the
    # exact source/target names the atomic-write pattern uses.
    from pathlib import Path

    captured = {}
    real_rename = Path.rename

    def fake_rename(self, target):
        captured["source_name"] = self.name
        captured["target_name"] = Path(target).name
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", fake_rename)

    _write_run_status(tmp_path, "running")

    assert captured["source_name"] == "run_status.json.tmp"
    assert captured["target_name"] == "run_status.json"


def test_ac11_running_status_written_before_expensive_work(tmp_path):
    # A council_fn that raises immediately still leaves a "running" marker,
    # proving the status is written BEFORE council_fn is even called, not
    # only recorded retroactively on success.
    async def failing_council_fn(query, verified_facts=None):
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

    async def peeking_council_fn(query, verified_facts=None):
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


def test_debug_log_is_persisted_on_failure_not_dropped(tmp_path):
    # architecture-stress-test-2026-08-13.md, High finding: the accumulated
    # debug_log lines (e.g. "Stage 1-3.5: council returned...") must survive
    # into the written failure record - losing them on the exact path where
    # diagnosing a failure matters most defeats the whole point of
    # debug_log existing.
    async def exploding_query_model(model, prompt):
        raise ConnectionError("revision call failed")

    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.1, cost_x=0.01, cost_y=0.02))

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    with pytest.raises(ConnectionError):
        _run(config, fetch_evidence, council_fn, exploding_query_model)

    run_dirs = list(tmp_path.iterdir())
    status = _read_run_status(run_dirs[0])
    assert "debug_log" in status
    assert any("Stage 1-3.5" in line for line in status["debug_log"])
    assert status["cost_so_far_usd"] == pytest.approx(0.03)


def test_ac13_failed_status_cost_so_far_is_zero_when_council_never_returns(tmp_path):
    async def failing_council_fn(query, verified_facts=None):
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

    async def failing_council_fn(query, verified_facts=None):
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


def test_arg_parser_max_wall_clock_seconds_defaults_to_module_constant():
    parser = _build_arg_parser()
    args = parser.parse_args(["--topic-label", "t", "--query", "q"])
    assert args.max_wall_clock_seconds == DEFAULT_MAX_WALL_CLOCK_SECONDS


def test_arg_parser_max_wall_clock_seconds_overridable():
    parser = _build_arg_parser()
    args = parser.parse_args(
        ["--topic-label", "t", "--query", "q", "--max-wall-clock-seconds", "42.5"]
    )
    assert args.max_wall_clock_seconds == 42.5


# ---------------------------------------------------------------------------
# main(): the CLI entrypoint, end to end. Was entirely untested (0 direct
# assertions on it) before this mutation-gate hardening pass - every literal
# print format, exit code, and PipelineConfig-construction argument was
# unobserved. run_pipeline/run_council_with_timeouts are faked; real_fetch_
# evidence/real_query_model are only ever passed through by identity here,
# never called - so this stays hermetic (no network, no credentials).
# ---------------------------------------------------------------------------


def _make_pipeline_result(**overrides):
    defaults = dict(
        output_dir=Path("/tmp/out-dir"),
        css=0.812,
        revision_triggered=False,
        revision_skipped_for_cost=False,
        total_cost_usd=1.2345,
        scorecard_appended=True,
        synthesis="THE SYNTHESIS TEXT",
        cost_ceiling_exceeded=False,
        dropped_facts=[],
        completeness_check_skipped_for_cost=False,
        completeness_check_parse_failed=False,
        debug_log=["line one", "line two"],
    )
    defaults.update(overrides)
    return PipelineResult(**defaults)


def _run_main(monkeypatch, capsys, argv_tail, result=None, exc=None):
    calls = {"run_pipeline_args": None, "council_fn_query": None}

    async def fake_run_pipeline(config, fetch_evidence, council_fn, query_model, council_models=None):
        calls["run_pipeline_args"] = (config, fetch_evidence, council_fn, query_model)
        calls["council_models"] = council_models
        if exc is not None:
            raise exc
        return result

    async def fake_run_council_with_timeouts(query, verified_facts=None, overall_wall_clock_seconds=None):
        calls["council_fn_query"] = query
        calls["council_fn_verified_facts"] = verified_facts
        calls["council_fn_overall_wall_clock_seconds"] = overall_wall_clock_seconds
        return ([], [], {}, {})

    monkeypatch.setattr(_pr_module, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(_ca_module, "run_council_with_timeouts", fake_run_council_with_timeouts)
    monkeypatch.setattr(sys, "argv", ["llm-council-pipeline"] + argv_tail)

    with pytest.raises(SystemExit) as exc_info:
        main()

    out = capsys.readouterr()
    return exc_info.value.code, calls, out


def test_main_happy_path_prints_output_and_exits_0(monkeypatch, capsys, tmp_path):
    result = _make_pipeline_result(
        output_dir=tmp_path / "run1",
        css=0.755,
        total_cost_usd=0.4321,
        synthesis="FINAL SYNTHESIS",
        debug_log=["stage1 ok", "stage2 ok"],
    )
    code, calls, out = _run_main(
        monkeypatch, capsys, ["--topic-label", "T", "--query", "Q"], result=result
    )

    assert code == 0
    assert f"Output: {result.output_dir}" in out.out
    assert "CSS: 0.755" in out.out
    assert "Total cost: $0.4321" in out.out
    assert "FINAL SYNTHESIS" in out.out
    # Exact-line match, not substring "in" - a substring check would still
    # pass against a mutated "XXDebug log:XX" (mutmut's own string-mutation
    # scheme wraps literals rather than replacing them), since the original
    # text remains present as a substring either way.
    err_lines = out.err.splitlines()
    assert err_lines[0] == "Debug log:"
    assert "  stage1 ok" in out.err
    assert "  stage2 ok" in out.err

    config = calls["run_pipeline_args"][0]
    assert config.topic_label == "T"
    assert config.query == "Q"
    assert config.raw_claims_text == ""
    assert config.max_cost_usd is None
    assert config.output_root is None
    assert config.max_wall_clock_seconds == DEFAULT_MAX_WALL_CLOCK_SECONDS


def test_main_reads_claims_file_when_given(monkeypatch, capsys, tmp_path):
    claims_file = tmp_path / "claims.txt"
    claims_file.write_text("claim text here")
    result = _make_pipeline_result()

    code, calls, out = _run_main(
        monkeypatch,
        capsys,
        ["--topic-label", "T", "--query", "Q", "--claims-file", str(claims_file)],
        result=result,
    )

    config = calls["run_pipeline_args"][0]
    assert config.raw_claims_text == "claim text here"


def test_main_threads_max_cost_output_root_and_wall_clock(monkeypatch, capsys, tmp_path):
    result = _make_pipeline_result()

    code, calls, out = _run_main(
        monkeypatch,
        capsys,
        [
            "--topic-label", "T", "--query", "Q",
            "--max-cost-usd", "2.5",
            "--output-root", str(tmp_path),
            "--max-wall-clock-seconds", "99.0",
        ],
        result=result,
    )

    config = calls["run_pipeline_args"][0]
    assert config.max_cost_usd == 2.5
    assert config.output_root == tmp_path
    assert config.max_wall_clock_seconds == 99.0


def test_main_council_fn_wraps_run_council_with_timeouts(monkeypatch, capsys):
    result = _make_pipeline_result()

    code, calls, out = _run_main(
        monkeypatch, capsys, ["--topic-label", "T", "--query", "Q"], result=result
    )

    council_fn = calls["run_pipeline_args"][2]
    ret = asyncio.run(council_fn("probe-query", []))
    assert calls["council_fn_query"] == "probe-query"
    assert ret == ([], [], {}, {})


def test_main_council_fn_threads_verified_facts_through_unchanged(monkeypatch, capsys):
    result = _make_pipeline_result()

    code, calls, out = _run_main(
        monkeypatch, capsys, ["--topic-label", "T", "--query", "Q"], result=result
    )

    council_fn = calls["run_pipeline_args"][2]
    # A genuinely non-empty, non-None sentinel - distinguishes "threaded
    # through unchanged" from both a None-replaced and an omitted
    # (default-valued) verified_facts argument.
    sentinel_facts = ["not-empty-verified-facts-marker"]
    asyncio.run(council_fn("probe-query", sentinel_facts))
    assert calls["council_fn_verified_facts"] == sentinel_facts


def test_main_council_fn_threads_max_wall_clock_seconds_to_council_adapter(monkeypatch, capsys):
    # docs/specs/wallclock-cost-budget-contract.md, Contract 1, AC7: the
    # real, configured ceiling must reach Stage 1's deadline computation,
    # not a hardcoded default.
    result = _make_pipeline_result()

    code, calls, out = _run_main(
        monkeypatch,
        capsys,
        ["--topic-label", "T", "--query", "Q", "--max-wall-clock-seconds", "77.0"],
        result=result,
    )

    council_fn = calls["run_pipeline_args"][2]
    asyncio.run(council_fn("probe-query", []))
    assert calls["council_fn_overall_wall_clock_seconds"] == 77.0


def test_main_council_models_sourced_from_config_and_threaded_to_run_pipeline(
    monkeypatch, capsys
):
    import llm_council.unified_config as _uc_module
    from types import SimpleNamespace

    fake_models = ["config-model-a", "config-model-b"]
    monkeypatch.setattr(
        _uc_module,
        "get_config",
        lambda: SimpleNamespace(council=SimpleNamespace(models=fake_models)),
    )
    result = _make_pipeline_result()

    code, calls, out = _run_main(
        monkeypatch, capsys, ["--topic-label", "T", "--query", "Q"], result=result
    )

    assert calls["council_models"] == fake_models


def test_main_passes_the_real_fetch_evidence_and_query_model_adapters(monkeypatch, capsys):
    result = _make_pipeline_result()

    code, calls, out = _run_main(
        monkeypatch, capsys, ["--topic-label", "T", "--query", "Q"], result=result
    )

    _, fetch_evidence, _, query_model = calls["run_pipeline_args"]
    assert fetch_evidence is _la_module.real_fetch_evidence
    assert query_model is _la_module.real_query_model


def test_main_cost_ceiling_exceeded_exits_3_with_exact_warning(monkeypatch, capsys):
    result = _make_pipeline_result(cost_ceiling_exceeded=True, total_cost_usd=5.6789)

    code, calls, out = _run_main(
        monkeypatch,
        capsys,
        ["--topic-label", "T", "--query", "Q", "--max-cost-usd", "5.0"],
        result=result,
    )

    assert code == 3
    assert "WARNING: total cost $5.6789 exceeded --max-cost-usd 5.0" in out.err


def test_main_revision_skipped_for_cost_exits_2_with_exact_warning(monkeypatch, capsys):
    result = _make_pipeline_result(revision_skipped_for_cost=True)

    code, calls, out = _run_main(
        monkeypatch,
        capsys,
        ["--topic-label", "T", "--query", "Q", "--max-cost-usd", "1.0"],
        result=result,
    )

    assert code == 2
    assert (
        "WARNING: revision round was skipped because starting it would "
        "have exceeded --max-cost-usd 1.0"
    ) in out.err


def test_main_cost_ceiling_exceeded_outranks_revision_skipped_for_cost(monkeypatch, capsys):
    result = _make_pipeline_result(
        cost_ceiling_exceeded=True, revision_skipped_for_cost=True, total_cost_usd=9.0
    )

    code, calls, out = _run_main(
        monkeypatch,
        capsys,
        ["--topic-label", "T", "--query", "Q", "--max-cost-usd", "1.0"],
        result=result,
    )

    assert code == 3


def test_main_success_does_not_exit_with_ceiling_or_revision_codes(monkeypatch, capsys):
    result = _make_pipeline_result(cost_ceiling_exceeded=False, revision_skipped_for_cost=False)

    code, calls, out = _run_main(
        monkeypatch, capsys, ["--topic-label", "T", "--query", "Q"], result=result
    )

    assert code == 0


def test_main_dropped_facts_warning_joins_exact_ids(monkeypatch, capsys):
    result = _make_pipeline_result(dropped_facts=["fact-1", "fact-2"])

    code, calls, out = _run_main(
        monkeypatch, capsys, ["--topic-label", "T", "--query", "Q"], result=result
    )

    assert code == 0
    assert (
        "WARNING: the final synthesis does not appear to address these "
        "verified facts: fact-1, fact-2"
    ) in out.err


def test_main_no_dropped_facts_warning_when_list_empty(monkeypatch, capsys):
    result = _make_pipeline_result(dropped_facts=[])

    code, calls, out = _run_main(
        monkeypatch, capsys, ["--topic-label", "T", "--query", "Q"], result=result
    )

    assert "does not appear to address" not in out.err


def test_main_completeness_check_parse_failed_exact_warning(monkeypatch, capsys):
    result = _make_pipeline_result(completeness_check_parse_failed=True)

    code, calls, out = _run_main(
        monkeypatch, capsys, ["--topic-label", "T", "--query", "Q"], result=result
    )

    assert code == 0
    assert (
        "WARNING: the Stage 4 completeness check ran but its response "
        "could not be understood - completeness is UNDETERMINED, not "
        "verified. dropped_facts=[] here does NOT mean nothing is missing."
    ) in out.err


def test_main_completeness_check_skipped_for_cost_exact_warning(monkeypatch, capsys):
    result = _make_pipeline_result(completeness_check_skipped_for_cost=True)

    code, calls, out = _run_main(
        monkeypatch,
        capsys,
        ["--topic-label", "T", "--query", "Q", "--max-cost-usd", "3.0"],
        result=result,
    )

    assert code == 0
    assert (
        "WARNING: the Stage 4 completeness check was skipped because "
        "running it would have exceeded --max-cost-usd 3.0"
    ) in out.err


def test_main_timeout_error_exits_4_with_exact_message(monkeypatch, capsys):
    code, calls, out = _run_main(
        monkeypatch,
        capsys,
        ["--topic-label", "T", "--query", "Q"],
        exc=TimeoutError("wall clock exceeded"),
    )

    assert code == 4
    assert "Pipeline run failed: wall clock exceeded" in out.err


def test_main_generic_exception_exits_1_with_exact_message(monkeypatch, capsys):
    code, calls, out = _run_main(
        monkeypatch,
        capsys,
        ["--topic-label", "T", "--query", "Q"],
        exc=ValueError("boom"),
    )

    assert code == 1
    assert "Pipeline run failed: boom" in out.err


# ---------------------------------------------------------------------------
# Durable persistence integration (docs/specs/durable-persistence-contract.md)
# - transcript_writer.py's own rendering is covered directly by
# tests/test_transcript_writer.py; these tests cover only the *wiring*:
# each write_* call fires at the right point in _run_stages, real files
# land in the real output_dir, and a write failure is non-fatal.
# ---------------------------------------------------------------------------


def test_durable_writes_land_in_the_real_output_dir_with_real_content(tmp_path):
    evidence = {"1": [Evidence(source="http://x.com", date="2026-01-01", supports=True)]}
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.1))  # triggers revision
    query_model = FakeQueryModel(
        response_by_model={"model-x": "[[cite:1]] revised text", "model-y": "no citation"}
    )

    config = PipelineConfig(
        topic_label="t", query="q", raw_claims_text="1. Some claim.", output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    names = {p.name for p in result.output_dir.iterdir()}
    assert {
        "stage1_transcripts.md",
        "stage2_summary.md",
        "synthesis.md",
        "revision_outcomes.md",
    } <= names

    stage1_text = (result.output_dir / "stage1_transcripts.md").read_text()
    assert "Answer from X" in stage1_text and "Answer from Y" in stage1_text

    # css=0.1 (the real value threaded through, not a placeholder/None) and
    # the real chairman model name from stage3_result - not omitted/default.
    stage2_text = (result.output_dir / "stage2_summary.md").read_text()
    assert "0.100" in stage2_text

    synthesis_text = (result.output_dir / "synthesis.md").read_text()
    assert "Final synthesis text" in synthesis_text
    assert "model-x" in synthesis_text  # _council_result_fixture's chairman

    revision_text = (result.output_dir / "revision_outcomes.md").read_text()
    assert "not revising" in revision_text  # model-y didn't cite


def test_write_synthesis_uses_exactly_unknown_when_stage3_result_lacks_model_key(tmp_path):
    # stage3_result.get("model", "unknown") - the "model" key genuinely
    # absent (not just falsy) is the only scenario where this default
    # fallback is ever exercised; every other fixture always sets "model".
    fetch_evidence = _make_fetch_evidence()

    async def council_fn_no_model_key(query, verified_facts=None):
        stage1_results, stage2_results, _stage3_result, metadata = _council_result_fixture(
            css=0.9
        )
        stage3_result = {"response": "Final synthesis text"}  # no "model" key
        return stage1_results, stage2_results, stage3_result, metadata

    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn_no_model_key, query_model)

    synthesis_text = (result.output_dir / "synthesis.md").read_text()
    assert synthesis_text == "# Synthesis (chairman: unknown)\n\nFinal synthesis text\n"


def test_write_synthesis_runs_even_when_revision_is_not_triggered(tmp_path):
    # write_synthesis fires unconditionally right after Stage 3, not gated
    # on Stage 2.75 having run at all.
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))  # no revision
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.revision_triggered is False
    assert (result.output_dir / "synthesis.md").exists()
    # revision never ran, so there is nothing to write - no stray file.
    assert not (result.output_dir / "revision_outcomes.md").exists()


def test_revision_outcomes_file_absent_when_revision_skipped_for_cost(tmp_path):
    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.1, cost_x=0.05, cost_y=0.05))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path, max_cost_usd=0.05)
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert result.revision_skipped_for_cost is True
    assert not (result.output_dir / "revision_outcomes.md").exists()


@pytest.mark.parametrize(
    "target_name,filename",
    [
        ("write_stage1_transcripts", "stage1_transcripts.md"),
        ("write_stage2_summary", "stage2_summary.md"),
        ("write_synthesis", "synthesis.md"),
    ],
)
def test_transcript_write_failure_is_non_fatal_and_logged(monkeypatch, tmp_path, target_name, filename):
    def _raise(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(_pr_module, target_name, _raise)

    fetch_evidence = _make_fetch_evidence()
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    query_model = FakeQueryModel()

    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = _run(config, fetch_evidence, council_fn, query_model)

    # The run as a whole must still succeed - a transcript-write failure
    # must never crash an otherwise-successful pipeline run.
    assert _read_run_status(result.output_dir)["status"] == "complete"
    assert any(
        f"Transcript write ({filename}): failed non-fatally" in line and "disk full" in line
        for line in result.debug_log
    )
    assert not (result.output_dir / filename).exists()


def test_revision_outcomes_write_failure_is_non_fatal_and_logged(monkeypatch, tmp_path):
    def _raise(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(_pr_module, "write_revision_outcomes", _raise)

    evidence = {"1": [Evidence(source="http://x.com", date="2026-01-01", supports=True)]}
    fetch_evidence = _make_fetch_evidence(evidence)
    council_fn = _make_council_fn(_council_result_fixture(css=0.1))  # triggers revision
    query_model = FakeQueryModel(
        response_by_model={"model-x": "[[cite:1]] revised text", "model-y": "no citation"}
    )

    config = PipelineConfig(
        topic_label="t", query="q", raw_claims_text="1. Some claim.", output_root=tmp_path,
    )
    result = _run(config, fetch_evidence, council_fn, query_model)

    assert _read_run_status(result.output_dir)["status"] == "complete"
    assert result.revision_triggered is True
    assert any(
        "Transcript write (revision_outcomes.md): failed non-fatally" in line
        and "disk full" in line
        for line in result.debug_log
    )
    assert not (result.output_dir / "revision_outcomes.md").exists()
