"""Blind acceptance test for docs/specs/stage1-5-normalizer-timeout-contract.md
(AC9) -- `pipeline_runner.py` must surface each model in a council_fn
result's `metadata["normalization_failures"]` as a WARNING debug_log line,
mirroring the existing `shortfall_warning`/`ungrounded_models` "loud
debug_log line, never a silent drop" convention already confirmed live in
`scripts/pipeline_runner.py` (the `ungrounded_models` loop).

Authored WITHOUT sight of any implementation. As of this writing,
`run_pipeline` has no code reading `metadata["normalization_failures"]` at
all -- both tests below are expected to fail RED (the WARNING line is
simply absent from `debug_log`) until the contract lands. (The second test,
asserting ABSENCE, is expected to already pass -- included for symmetry and
regression protection, matching the sibling
`test_debug_log_no_grounding_warning_when_key_absent` pattern in
`tests/test_pipeline_runner.py`.)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from scripts.pipeline_runner import PipelineConfig, run_pipeline


class _FakeQueryModel:
    """No revision round should ever be reached by these fixtures (css=0.9,
    well above any default revision threshold) - included only because
    `run_pipeline`'s signature requires a query_model callable."""

    async def __call__(self, model: str, prompt: str):
        return "no revision", 0.0


def _fetch_evidence(claims):
    async def _inner(_claims):
        return {}

    return _inner


def _council_fn(result):
    async def _inner(query, verified_facts=None):
        return result

    return _inner


def _run(config, fetch_evidence, council_fn, query_model):
    return asyncio.run(run_pipeline(config, fetch_evidence, council_fn, query_model))


def _council_result(models, normalization_failures=None):
    stage1_results = [{"model": m, "response": f"answer from {m} [unverified]"} for m in models]
    stage2_results = []
    aggregate_rankings = [
        {"model": m, "borda_score": 1.0, "rank": i + 1} for i, m in enumerate(models)
    ]
    stage3_result = {"model": models[0], "response": "final synthesis text"}
    metadata = {
        "quality_metrics": {"core": {"consensus_strength": 0.9}},
        "aggregate_rankings": aggregate_rankings,
        "label_to_model": {
            f"Response {chr(65 + i)}": {"model": m, "display_index": i} for i, m in enumerate(models)
        },
        "usage": {
            "by_model": {m: {"cost_usd": 0.0} for m in models},
            "total": {"cost_usd": 0.0},
        },
    }
    if normalization_failures is not None:
        metadata["normalization_failures"] = normalization_failures
    return stage1_results, stage2_results, stage3_result, metadata


def test_ac9_debug_log_contains_warning_line_when_normalization_failures_present(tmp_path):
    council_fn = _council_fn(
        _council_result(["model-x", "model-y"], normalization_failures=["some/model"])
    )
    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)

    result = _run(config, _fetch_evidence([]), council_fn, _FakeQueryModel())

    # Mutation-testing hardening (scoped gate, 2026-08-21): a `startswith`
    # check on only the prefix never observes the literal tail text
    # ("time (Stage 1.5) - Stage 3 may see un-normalized, potentially
    # fingerprinted text for this model"), so string-literal mutants on
    # that tail (case-flipped, XX-wrapped) all survived (scoped mutmut
    # run, 5 survivors, traced by hand). Asserting the exact, full message
    # closes that gap.
    assert (
        "WARNING: some/model's response could not be style-normalized in "
        "time (Stage 1.5) - Stage 3 may see un-normalized, potentially "
        "fingerprinted text for this model"
    ) in result.debug_log, result.debug_log


def test_ac9_debug_log_warns_once_per_model_including_duplicates(tmp_path):
    # Contract text: "deduplication not required - a model can appear once
    # per stage it failed in" -- a model failing both Stage 1.5 and Stage 2
    # normalization must produce two WARNING lines, not one collapsed line.
    council_fn = _council_fn(
        _council_result(["model-x", "model-y"], normalization_failures=["model-x", "model-x"])
    )
    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)

    result = _run(config, _fetch_evidence([]), council_fn, _FakeQueryModel())

    warning_lines = [
        line
        for line in result.debug_log
        if line.startswith("WARNING: model-x's response could not be style-normalized")
    ]
    assert len(warning_lines) == 2


def test_ac9_debug_log_has_no_normalization_warning_when_key_absent(tmp_path):
    council_fn = _council_fn(_council_result(["model-x", "model-y"], normalization_failures=None))
    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)

    result = _run(config, _fetch_evidence([]), council_fn, _FakeQueryModel())

    assert not any("could not be style-normalized" in line for line in result.debug_log)
