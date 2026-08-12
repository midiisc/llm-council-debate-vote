"""Blind acceptance tests for the `pipeline_runner.py` half of the
2026-08-12 "resilient Stage 1" amendment to docs/specs/pipeline-runner-
contract.md -- AC22 (shortfall_warning surfaced as a WARNING debug_log
line) plus the substitution-NOTE-line behavior specified in the same
amendment's "pipeline_runner.py companion change" paragraph (no separate
AC number, but directly quoted/specified prose in the contract, not an
inferred assumption).

The `council_adapter.py` half (AC17-21, AC23 -- how `metadata[
"shortfall_warning"]`/`metadata["substitutions"]` get produced in the
first place) is covered separately in
tests/test_council_adapter_resilient_stage1.py. This file only exercises
`run_pipeline`'s existing, already-established debug_log mechanism
(unchanged infrastructure, verified real by the pre-existing, passing
tests/test_pipeline_runner.py suite) against a fake `council_fn` that
returns metadata already carrying these two keys -- exactly the black-box
boundary the contract itself draws ("no new PipelineResult field or new
main() print branch... reuses the pre-existing mechanism").

Authored WITHOUT sight of any implementation. As of this writing,
`run_pipeline`'s debug_log construction has no shortfall_warning/
substitutions handling (confirmed by reading the current file's `debug_log`
call sites before authoring, same "recover accurate call signatures from
the pre-feature file" allowance already exercised elsewhere in this repo's
blind-TDV test files) -- every test below is expected to fail (RED) until
the amendment lands.

DOCUMENTED ASSUMPTIONS:
  1. Exact line templates are quoted directly from the contract, not
     inferred: `f"WARNING: {metadata['shortfall_warning']}"` and
     `f"NOTE: {slot_model} was unreachable this session, substituted with
     backup {backup_model} ({reason})"`. Tests assert these exact strings
     appear in `result.debug_log`, not merely a substring/prefix match,
     except where the AC's own wording explicitly only requires a prefix
     ("a line starting with 'WARNING: '").
  2. `metadata["substitutions"]` entries are plain dicts with
     slot_model/backup_model/reason keys (matches AC20's own contract,
     exercised independently in the other test file) -- this file builds
     that fixture shape directly rather than depending on
     council_adapter.py at all.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pipeline_runner import PipelineConfig, run_pipeline


def _stage2_results_fixture():
    label_to_model = {
        "Response A": {"model": "model-p", "display_index": 0},
        "Response B": {"model": "model-q", "display_index": 1},
    }
    stage2_results = [
        {
            "model": "model-q",
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
            "model": "model-p",
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


def _council_result_fixture(css=0.9, extra_metadata=None):
    stage1_results = [
        {"model": "model-p", "response": "Answer from P"},
        {"model": "model-q", "response": "Answer from Q"},
    ]
    stage2_results, label_to_model = _stage2_results_fixture()
    aggregate_rankings = [
        {"model": "model-p", "borda_score": 1.0, "rank": 1},
        {"model": "model-q", "borda_score": 0.0, "rank": 2},
    ]
    stage3_result = {"model": "model-p", "response": "Final synthesis text"}
    metadata = {
        "quality_metrics": {"core": {"consensus_strength": css}},
        "aggregate_rankings": aggregate_rankings,
        "label_to_model": label_to_model,
        "usage": {
            "by_model": {
                "model-p": {"cost_usd": 0.01},
                "model-q": {"cost_usd": 0.02},
            },
            "total": {"cost_usd": 0.03},
        },
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return stage1_results, stage2_results, stage3_result, metadata


def _make_council_fn(result):
    async def council_fn(query: str):
        return result

    return council_fn


async def _fetch_evidence(claims):
    return {}


class _FakeQueryModel:
    async def __call__(self, model: str, prompt: str):
        return "no revision", 0.0


def _run(config, council_fn):
    return asyncio.run(run_pipeline(config, _fetch_evidence, council_fn, _FakeQueryModel()))


# ---------------------------------------------------------------------------
# AC22: Given Stage 1's metadata carries a shortfall_warning, When
# run_pipeline runs, Then debug_log contains a line starting with
# "WARNING: " and including the warning text verbatim, printed to stderr by
# main()'s existing "Debug log:" loop -- unchanged from today's mechanism,
# no new field.
# ---------------------------------------------------------------------------


def test_ac22_shortfall_warning_surfaced_as_warning_line_in_debug_log(tmp_path):
    warning_text = "Only 3 of the required minimum 4 council models responded; unreachable: model-z"
    council_fn = _make_council_fn(
        _council_result_fixture(css=0.9, extra_metadata={"shortfall_warning": warning_text})
    )
    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)

    result = _run(config, council_fn)

    matching = [line for line in result.debug_log if line.startswith("WARNING: ")]
    assert any(line == f"WARNING: {warning_text}" for line in matching)
    assert any(warning_text in line for line in matching)


def test_ac22_no_shortfall_warning_line_when_key_absent(tmp_path):
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)

    result = _run(config, council_fn)

    assert not any("required minimum" in line for line in result.debug_log)


# ---------------------------------------------------------------------------
# pipeline_runner.py companion change (substitution NOTE lines, quoted
# directly from the contract's own f-string template): one
# "NOTE: {slot_model} was unreachable this session, substituted with backup
# {backup_model} ({reason})" line per entry in
# metadata.get("substitutions", []).
# ---------------------------------------------------------------------------


def test_substitution_note_line_uses_exact_contract_template(tmp_path):
    substitutions = [
        {
            "slot_model": "model-x",
            "backup_model": "model-y",
            "reason": "unreachable after 3 attempts (last status=timeout)",
        }
    ]
    council_fn = _make_council_fn(
        _council_result_fixture(css=0.9, extra_metadata={"substitutions": substitutions})
    )
    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)

    result = _run(config, council_fn)

    expected_line = (
        "NOTE: model-x was unreachable this session, substituted with backup "
        "model-y (unreachable after 3 attempts (last status=timeout))"
    )
    assert expected_line in result.debug_log


def test_multiple_substitutions_each_produce_their_own_note_line_in_order(tmp_path):
    substitutions = [
        {"slot_model": "model-x", "backup_model": "model-y", "reason": "reason-one"},
        {"slot_model": "model-w", "backup_model": "model-z", "reason": "reason-two"},
    ]
    council_fn = _make_council_fn(
        _council_result_fixture(css=0.9, extra_metadata={"substitutions": substitutions})
    )
    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)

    result = _run(config, council_fn)

    note_lines = [line for line in result.debug_log if line.startswith("NOTE: ")]
    expected_first = "NOTE: model-x was unreachable this session, substituted with backup model-y (reason-one)"
    expected_second = "NOTE: model-w was unreachable this session, substituted with backup model-z (reason-two)"
    assert expected_first in result.debug_log
    assert expected_second in result.debug_log
    assert note_lines.index(expected_first) < note_lines.index(expected_second)


def test_no_note_lines_when_substitutions_key_absent(tmp_path):
    council_fn = _make_council_fn(_council_result_fixture(css=0.9))
    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)

    result = _run(config, council_fn)

    assert not any("substituted with backup" in line for line in result.debug_log)


def test_no_note_lines_when_substitutions_is_empty_list(tmp_path):
    council_fn = _make_council_fn(
        _council_result_fixture(css=0.9, extra_metadata={"substitutions": []})
    )
    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)

    result = _run(config, council_fn)

    assert not any("substituted with backup" in line for line in result.debug_log)


# ---------------------------------------------------------------------------
# Property test: the general law behind both behaviors above -- debug_log
# always contains exactly one derived WARNING line iff a shortfall_warning
# is present, and exactly one derived NOTE line per substitution entry
# (order-preserving, content-faithful) -- never more, never fewer, never
# malformed by whatever text the warning/reason strings happen to contain.
# ---------------------------------------------------------------------------


@settings(max_examples=50, derandomize=True, deadline=3000, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    shortfall_text=st.one_of(
        st.none(), st.text(alphabet="abcdefghij 0123456789:;,", min_size=1, max_size=40)
    ),
    substitution_rows=st.lists(
        st.tuples(
            st.text(alphabet="abcdefghij-", min_size=1, max_size=8),
            st.text(alphabet="abcdefghij-", min_size=1, max_size=8),
            st.text(alphabet="abcdefghij ()=", min_size=1, max_size=20),
        ),
        max_size=3,
    ),
)
def test_property_debug_log_faithfully_mirrors_shortfall_and_substitution_counts(
    tmp_path, shortfall_text, substitution_rows
):
    extra_metadata = {}
    if shortfall_text is not None:
        extra_metadata["shortfall_warning"] = shortfall_text
    if substitution_rows:
        extra_metadata["substitutions"] = [
            {"slot_model": s, "backup_model": b, "reason": r} for s, b, r in substitution_rows
        ]

    council_fn = _make_council_fn(_council_result_fixture(css=0.9, extra_metadata=extra_metadata))
    config = PipelineConfig(topic_label="t", query="q", output_root=tmp_path)

    result = _run(config, council_fn)

    if shortfall_text is not None:
        matching = [line for line in result.debug_log if line == f"WARNING: {shortfall_text}"]
        assert len(matching) == 1
    else:
        # No shortfall_warning key at all in metadata -- no line derived
        # from one can exist (the pre-existing "not multi-agent debate"
        # WARNING line, unrelated to this feature, is not generated for
        # this fixture's 2-model happy-path council result).
        assert not any(line.startswith("WARNING: ") for line in result.debug_log)

    expected_notes = [
        f"NOTE: {s} was unreachable this session, substituted with backup {b} ({r})"
        for s, b, r in substitution_rows
    ]
    for note in expected_notes:
        assert note in result.debug_log
    actual_note_lines = [line for line in result.debug_log if line.startswith("NOTE: ") and "substituted with backup" in line]
    assert len(actual_note_lines) == len(expected_notes)
