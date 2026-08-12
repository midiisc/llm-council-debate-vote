"""Blind acceptance tests for the 2026-08-12 amendment to the pipeline-runner
contract (docs/specs/pipeline-runner-contract.md, "Amendment (2026-08-12):
timeout-aware council_fn + wall-clock ceiling") -- the `max_wall_clock_seconds`
half of that amendment only (AC15-16). The `run_council_with_timeouts`
adapter half (AC11-14) is covered separately in tests/test_council_adapter.py.

Authored WITHOUT sight of any implementation. `PipelineConfig.
max_wall_clock_seconds` and the `--max-wall-clock-seconds` CLI flag do not
exist yet as of this writing -- these tests are expected to fail (RED)
until they land.

DOCUMENTED ASSUMPTIONS (the contract pins observable outcomes -- run_status
fields, exit code -- but not the internal exception type/plumbing used to
get there):
  1. Whatever internal exception mechanism is used, run_pipeline still
     surfaces SOME exception to its caller on timeout (consistent with the
     module's existing "except Exception as e: write failed status; raise"
     pattern demonstrated by every other failure-path test already in
     tests/test_pipeline_runner.py, e.g. test_ac13_failed_status_includes_
     error_and_cost_so_far_after_council_succeeds) -- so these tests assert
     via `pytest.raises(Exception)` around the run_pipeline call, then
     inspect the resulting run_status.json rather than asserting a specific
     exception class name.
  2. main()'s mapping of "wall clock ceiling exceeded" to CLI exit code 4 is
     tested by making the module-level `run_pipeline` function (called by
     name from within main(), an existing, unchanged call site) raise a
     `TimeoutError` whose message names `max_wall_clock_seconds`, matching
     the contract's own example wording ("error: exceeded
     max_wall_clock_seconds (<N>s)"). This does not presume HOW main()
     detects the timeout internally, only that a TimeoutError carrying that
     message maps to exit(4) rather than the pre-existing generic exit(1).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pipeline_runner import PipelineConfig, _build_arg_parser, run_pipeline
import scripts.pipeline_runner as pr_module


def _read_run_status(output_dir):
    return json.loads((output_dir / "run_status.json").read_text())


class FakeQueryModel:
    def __init__(self):
        self.calls = []

    async def __call__(self, model, prompt):
        self.calls.append((model, prompt))
        return "no revision", 0.0


async def _fetch_evidence(claims):
    return {}


# ---------------------------------------------------------------------------
# Decision: PipelineConfig gains max_wall_clock_seconds: float = 1200.0,
# always-on (never None-by-default like max_cost_usd).
# ---------------------------------------------------------------------------


def test_pipelineconfig_max_wall_clock_seconds_defaults_to_1200():
    config = PipelineConfig(topic_label="t", query="q")
    assert config.max_wall_clock_seconds == 1200.0


def test_pipelineconfig_max_wall_clock_seconds_is_overridable():
    config = PipelineConfig(topic_label="t", query="q", max_wall_clock_seconds=42.0)
    assert config.max_wall_clock_seconds == 42.0


# ---------------------------------------------------------------------------
# AC15: Given a full run_pipeline call exceeds max_wall_clock_seconds, When
# it times out, Then run_status.json is written with status: "failed" and a
# message naming the ceiling (not a generic asyncio.TimeoutError string),
# the original spend already committed is still reflected in
# cost_so_far_usd, and the CLI exits 4.
# ---------------------------------------------------------------------------


def test_ac15_wall_clock_ceiling_exceeded_writes_failed_status_naming_the_ceiling(tmp_path):
    async def slow_council_fn(query):
        await asyncio.sleep(0.5)
        raise AssertionError("must never actually complete - the ceiling must fire first")

    config = PipelineConfig(
        topic_label="t",
        query="q",
        output_root=tmp_path,
        max_wall_clock_seconds=0.05,
    )

    with pytest.raises(Exception) as exc_info:
        asyncio.run(run_pipeline(config, _fetch_evidence, slow_council_fn, FakeQueryModel()))

    # The raised exception's OWN message (not just what got written to
    # run_status.json beforehand) must also name the ceiling - the write
    # happens from the same local `error_msg` variable, but the exception
    # object is constructed separately, so a mutation to what's passed into
    # TimeoutError(...) wouldn't otherwise be observable.
    assert "max_wall_clock_seconds" in str(exc_info.value), (
        "the raised exception's own message must name the ceiling; "
        f"got: {exc_info.value!r}"
    )

    run_dirs = list(tmp_path.iterdir())
    assert len(run_dirs) == 1
    status = _read_run_status(run_dirs[0])
    assert status["status"] == "failed"
    assert "max_wall_clock_seconds" in status["error"], (
        "the failure message must name the ceiling, not a bare/generic "
        f"TimeoutError string; got: {status['error']!r}"
    )
    assert "cost_so_far_usd" in status
    assert status["cost_so_far_usd"] == 0.0


def test_ac15_wall_clock_ceiling_message_names_the_configured_value(tmp_path):
    async def slow_council_fn(query):
        await asyncio.sleep(0.5)

    config = PipelineConfig(
        topic_label="t",
        query="q",
        output_root=tmp_path,
        max_wall_clock_seconds=0.07,
    )

    with pytest.raises(Exception):
        asyncio.run(run_pipeline(config, _fetch_evidence, slow_council_fn, FakeQueryModel()))

    run_dirs = list(tmp_path.iterdir())
    status = _read_run_status(run_dirs[0])
    assert "0.07" in status["error"], (
        f"expected the configured ceiling (0.07) named in the error; got: {status['error']!r}"
    )


def test_ac15_raised_exception_itself_carries_the_ceiling_message_not_just_run_status_json(tmp_path):
    # Assumption 1 above deliberately avoids pinning the exception's TYPE,
    # verifying via run_status.json instead - but the exception's own
    # message IS separately, directly user-visible: main() prints it
    # verbatim (f"Pipeline run failed: {e}") on the way to exit(4). A test
    # that only ever fakes run_pipeline when testing main() never connects
    # "run_pipeline constructs message X" to "main() prints message X" with
    # the REAL run_pipeline, so this checks that link explicitly.
    async def slow_council_fn(query):
        await asyncio.sleep(0.5)

    config = PipelineConfig(
        topic_label="t", query="q", output_root=tmp_path, max_wall_clock_seconds=0.05,
    )

    with pytest.raises(Exception) as exc_info:
        asyncio.run(run_pipeline(config, _fetch_evidence, slow_council_fn, FakeQueryModel()))

    message = str(exc_info.value)
    assert "max_wall_clock_seconds" in message
    assert "0.05s" in message


# ---------------------------------------------------------------------------
# AC16: Given a run completes well within max_wall_clock_seconds, When
# run_pipeline finishes normally, Then behavior is byte-identical to before
# this amendment - the ceiling is a backstop, never a normal-path behavior
# change.
# ---------------------------------------------------------------------------


def _council_result_fixture(css=0.9):
    stage1_results = [
        {"model": "model-x", "response": "Answer from X"},
        {"model": "model-y", "response": "Answer from Y"},
    ]
    label_to_model = {
        "Response A": {"model": "model-x", "display_index": 0},
        "Response B": {"model": "model-y", "display_index": 1},
    }
    stage2_results = []
    aggregate_rankings = [
        {"model": "model-x", "borda_score": 1.0, "rank": 1},
        {"model": "model-y", "borda_score": 0.0, "rank": 2},
    ]
    stage3_result = {"model": "model-x", "response": "Final synthesis text"}
    metadata = {
        "quality_metrics": {"core": {"consensus_strength": css}},
        "aggregate_rankings": aggregate_rankings,
        "label_to_model": label_to_model,
        "usage": {
            "by_model": {"model-x": {"cost_usd": 0.01}, "model-y": {"cost_usd": 0.02}},
            "total": {"cost_usd": 0.03},
        },
    }
    return stage1_results, stage2_results, stage3_result, metadata


def test_ac16_fast_run_within_ceiling_completes_normally_regardless_of_ceiling_value(tmp_path):
    async def fast_council_fn(query):
        return _council_result_fixture(css=0.9)

    config = PipelineConfig(
        topic_label="t", query="q", output_root=tmp_path, max_wall_clock_seconds=1200.0,
    )
    result = asyncio.run(run_pipeline(config, _fetch_evidence, fast_council_fn, FakeQueryModel()))

    status = _read_run_status(result.output_dir)
    assert status["status"] == "complete"
    assert result.css == 0.9
    assert result.total_cost_usd == pytest.approx(0.03)
    assert result.revision_triggered is False


def test_ac16_default_max_wall_clock_seconds_does_not_interfere_with_a_normal_fast_run(tmp_path):
    async def fast_council_fn(query):
        return _council_result_fixture(css=0.9)

    # Rely on the dataclass default (1200.0) entirely - not passed explicitly.
    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)
    result = asyncio.run(run_pipeline(config, _fetch_evidence, fast_council_fn, FakeQueryModel()))

    status = _read_run_status(result.output_dir)
    assert status["status"] == "complete"


# ---------------------------------------------------------------------------
# New --max-wall-clock-seconds CLI flag, defaulting to PipelineConfig's own
# default.
# ---------------------------------------------------------------------------


def test_cli_flag_max_wall_clock_seconds_parses_to_float():
    parser = _build_arg_parser()
    args = parser.parse_args(
        ["--topic-label", "t", "--query", "q", "--max-wall-clock-seconds", "42.5"]
    )
    assert args.max_wall_clock_seconds == 42.5


def test_cli_flag_max_wall_clock_seconds_omitted_resolves_to_pipelineconfig_default():
    parser = _build_arg_parser()
    args = parser.parse_args(["--topic-label", "t", "--query", "q"])
    default_config = PipelineConfig(topic_label="t", query="q")
    # Either argparse itself defaults to the same value, or it defaults to
    # None and main() falls back to PipelineConfig's own default - both are
    # legitimate ways to satisfy "defaulting to PipelineConfig's own
    # default" from the contract text.
    assert args.max_wall_clock_seconds in (None, default_config.max_wall_clock_seconds)


# ---------------------------------------------------------------------------
# CLI exit code 4 (extends the existing 0/1/2/3 contract) for a wall-clock
# timeout, distinct from the pre-existing generic exit(1) failure path.
# ---------------------------------------------------------------------------


def test_main_exits_4_when_run_pipeline_raises_wall_clock_timeout(monkeypatch, tmp_path):
    async def _raise_timeout(*args, **kwargs):
        raise TimeoutError("exceeded max_wall_clock_seconds (60s)")

    monkeypatch.setattr(pr_module, "run_pipeline", _raise_timeout)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "llm-council-pipeline",
            "--topic-label",
            "t",
            "--query",
            "q",
            "--output-root",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        pr_module.main()

    assert exc_info.value.code == 4, (
        "a wall-clock-ceiling timeout must exit 4, distinct from the "
        f"pre-existing generic exit(1); got exit({exc_info.value.code})"
    )
