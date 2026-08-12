"""Tests for scripts/debate.py - the one-shot resilient debate CLI.

Covers the CLI's own logic (argument parsing, stdout/stderr routing, exit
codes) with run_council_with_timeouts faked - the underlying function's own
behavior is covered by tests/test_council_adapter.py. metadata's optional
"substitutions"/"shortfall_warning" keys are exercised here per the shape
pinned in scripts/debate.py's own docstring
(docs/specs/debate-resilience-contract.md).
"""
from __future__ import annotations

import sys

import pytest

from scripts import debate as debate_module


def _patch_run_council(monkeypatch, result):
    calls = []

    async def fake(user_query, stage1_timeout=300.0, stage2_timeout=300.0, stage3_timeout=300.0):
        calls.append(
            {
                "user_query": user_query,
                "stage1_timeout": stage1_timeout,
                "stage2_timeout": stage2_timeout,
                "stage3_timeout": stage3_timeout,
            }
        )
        return result

    # main() does `from scripts.council_adapter import run_council_with_timeouts`
    # as a function-local import each call, so the only place worth patching
    # is the source module - patching debate_module's own namespace would be
    # a no-op it never reads from.
    import scripts.council_adapter as ca_module

    monkeypatch.setattr(ca_module, "run_council_with_timeouts", fake)
    return calls


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_query_is_a_required_positional_argument():
    parser = debate_module._build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_stage_timeouts_default_to_300_seconds():
    parser = debate_module._build_arg_parser()
    args = parser.parse_args(["some question"])
    assert args.stage1_timeout == 300.0
    assert args.stage2_timeout == 300.0
    assert args.stage3_timeout == 300.0


def test_stage_timeouts_parse_from_explicit_flags():
    parser = debate_module._build_arg_parser()
    args = parser.parse_args(
        [
            "q",
            "--stage1-timeout",
            "11",
            "--stage2-timeout",
            "22",
            "--stage3-timeout",
            "33",
        ]
    )
    assert args.stage1_timeout == 11.0
    assert args.stage2_timeout == 22.0
    assert args.stage3_timeout == 33.0


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_main_prints_synthesis_and_exits_zero_on_success(monkeypatch, capsys):
    calls = _patch_run_council(
        monkeypatch,
        (
            [],
            [],
            {"model": "anthropic/claude-opus-4.8", "response": "the synthesized answer"},
            {},
        ),
    )
    monkeypatch.setattr(sys, "argv", ["llm-council-debate", "what should we do"])

    with pytest.raises(SystemExit) as exc_info:
        debate_module.main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "the synthesized answer"
    assert captured.err == ""
    assert calls == [
        {
            "user_query": "what should we do",
            "stage1_timeout": 300.0,
            "stage2_timeout": 300.0,
            "stage3_timeout": 300.0,
        }
    ]


def test_main_threads_explicit_stage_timeouts_to_run_council_with_timeouts(monkeypatch):
    calls = _patch_run_council(
        monkeypatch,
        ([], [], {"model": "m", "response": "ok"}, {}),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "llm-council-debate",
            "q",
            "--stage1-timeout",
            "11",
            "--stage2-timeout",
            "22",
            "--stage3-timeout",
            "33",
        ],
    )

    with pytest.raises(SystemExit):
        debate_module.main()

    assert calls == [
        {"user_query": "q", "stage1_timeout": 11.0, "stage2_timeout": 22.0, "stage3_timeout": 33.0}
    ]


# ---------------------------------------------------------------------------
# metadata["shortfall_warning"] / metadata["substitutions"] surfacing
# ---------------------------------------------------------------------------


def test_shortfall_warning_absent_prints_nothing_to_stderr(monkeypatch, capsys):
    _patch_run_council(monkeypatch, ([], [], {"model": "m", "response": "ok"}, {}))
    monkeypatch.setattr(sys, "argv", ["llm-council-debate", "q"])

    with pytest.raises(SystemExit):
        debate_module.main()

    assert capsys.readouterr().err == ""


def test_shortfall_warning_present_is_printed_to_stderr_as_warning(monkeypatch, capsys):
    metadata = {"shortfall_warning": "Only 3 of 4 minimum models responded live."}
    _patch_run_council(monkeypatch, ([], [], {"model": "m", "response": "ok"}, metadata))
    monkeypatch.setattr(sys, "argv", ["llm-council-debate", "q"])

    with pytest.raises(SystemExit) as exc_info:
        debate_module.main()

    assert exc_info.value.code == 0  # a shortfall alone is not a hard failure
    err = capsys.readouterr().err
    assert "WARNING: Only 3 of 4 minimum models responded live." in err


def test_substitutions_present_are_each_printed_as_a_note_to_stderr(monkeypatch, capsys):
    metadata = {
        "substitutions": [
            {
                "slot_model": "z-ai/glm-5.2",
                "backup_model": "x-ai/grok-4.6",
                "reason": "unreachable after 3 attempts (last status=timeout)",
            },
            {
                "slot_model": "google/gemini-3.6-flash",
                "backup_model": "qwen/qwen3.8-max",
                "reason": "unreachable after 1 attempts (last status=auth_error)",
            },
        ]
    }
    _patch_run_council(monkeypatch, ([], [], {"model": "m", "response": "ok"}, metadata))
    monkeypatch.setattr(sys, "argv", ["llm-council-debate", "q"])

    with pytest.raises(SystemExit):
        debate_module.main()

    err = capsys.readouterr().err
    assert (
        "NOTE: z-ai/glm-5.2 was unreachable this session, substituted with "
        "backup x-ai/grok-4.6 (unreachable after 3 attempts (last status=timeout))"
        in err
    )
    assert (
        "NOTE: google/gemini-3.6-flash was unreachable this session, "
        "substituted with backup qwen/qwen3.8-max (unreachable after 1 "
        "attempts (last status=auth_error))"
        in err
    )


def test_substitutions_absent_prints_no_note_lines(monkeypatch, capsys):
    _patch_run_council(monkeypatch, ([], [], {"model": "m", "response": "ok"}, {}))
    monkeypatch.setattr(sys, "argv", ["llm-council-debate", "q"])

    with pytest.raises(SystemExit):
        debate_module.main()

    assert "NOTE:" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_main_exits_1_and_prints_to_stderr_when_stage3_result_is_the_error_sentinel(
    monkeypatch, capsys
):
    stage3_result = {"model": "error", "response": "All models failed to respond. Please try again."}
    _patch_run_council(monkeypatch, ([], [], stage3_result, {}))
    monkeypatch.setattr(sys, "argv", ["llm-council-debate", "q"])

    with pytest.raises(SystemExit) as exc_info:
        debate_module.main()

    assert exc_info.value.code == 1
    assert "All models failed to respond" in capsys.readouterr().out


def test_main_exits_1_when_run_council_with_timeouts_raises(monkeypatch, capsys):
    async def raise_fn(*_args, **_kwargs):
        raise RuntimeError("boom")

    import scripts.council_adapter as ca_module

    monkeypatch.setattr(ca_module, "run_council_with_timeouts", raise_fn)
    monkeypatch.setattr(sys, "argv", ["llm-council-debate", "q"])

    with pytest.raises(SystemExit) as exc_info:
        debate_module.main()

    assert exc_info.value.code == 1
    assert "Debate failed: boom" in capsys.readouterr().err
