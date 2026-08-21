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
from pathlib import Path

import pytest

from scripts import debate as debate_module


def _patch_run_council(monkeypatch, result):
    calls = []

    async def fake(
        user_query,
        stage1_timeout=300.0,
        stage2_timeout=300.0,
        stage3_timeout=300.0,
        stage1_5_timeout=300.0,
        overall_wall_clock_seconds=None,
    ):
        calls.append(
            {
                "user_query": user_query,
                "stage1_timeout": stage1_timeout,
                "stage2_timeout": stage2_timeout,
                "stage3_timeout": stage3_timeout,
                "stage1_5_timeout": stage1_5_timeout,
                "overall_wall_clock_seconds": overall_wall_clock_seconds,
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


def test_arg_parser_prog_description_and_query_help_exact_text():
    # One exact-match assertion per literal closes every case/whitespace/
    # dropped-string mutant on the argparse metadata in a single test.
    parser = debate_module._build_arg_parser()
    assert parser.prog == "llm-council-debate"
    assert parser.description == (
        "Run a one-shot council debate with retry-with-backoff and "
        "backup-model substitution - prefer this over the raw "
        "consult_council MCP tool whenever losing a model to a "
        "transient timeout matters."
    )
    query_action = next(a for a in parser._actions if a.dest == "query")
    assert query_action.help == "The question/topic to debate."


def test_output_root_flag_coerces_a_cli_string_to_a_path_instance():
    parser = debate_module._build_arg_parser()
    args = parser.parse_args(["q", "--output-root", "some/relative/dir"])
    assert isinstance(args.output_root, Path)
    assert args.output_root == Path("some/relative/dir")


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


def test_main_prints_synthesis_and_exits_zero_on_success(monkeypatch, capsys, tmp_path):
    # chdir into tmp_path - main() persists to a real ./council-runs/ by
    # default (docs/specs/durable-persistence-contract.md), and this test
    # must never leak real files into the repo's own working directory.
    monkeypatch.chdir(tmp_path)
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
    assert captured.err.strip() == "Total cost: $0.0000"
    assert calls == [
        {
            "user_query": "what should we do",
            "stage1_timeout": 300.0,
            "stage2_timeout": 300.0,
            "stage3_timeout": 300.0,
            "stage1_5_timeout": 300.0,
            "overall_wall_clock_seconds": 1200.0,
        }
    ]


def test_main_threads_explicit_stage_timeouts_to_run_council_with_timeouts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
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
        {
            "user_query": "q",
            "stage1_timeout": 11.0,
            "stage2_timeout": 22.0,
            "stage3_timeout": 33.0,
            "stage1_5_timeout": 300.0,
            "overall_wall_clock_seconds": 1200.0,
        }
    ]


# ---------------------------------------------------------------------------
# metadata["shortfall_warning"] / metadata["substitutions"] surfacing
# ---------------------------------------------------------------------------


def test_shortfall_warning_absent_prints_nothing_to_stderr(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_run_council(monkeypatch, ([], [], {"model": "m", "response": "ok"}, {}))
    monkeypatch.setattr(sys, "argv", ["llm-council-debate", "q"])

    with pytest.raises(SystemExit):
        debate_module.main()

    err = capsys.readouterr().err
    assert "NOTE:" not in err
    assert "WARNING:" not in err


def test_shortfall_warning_present_is_printed_to_stderr_as_warning(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    metadata = {"shortfall_warning": "Only 3 of 4 minimum models responded live."}
    _patch_run_council(monkeypatch, ([], [], {"model": "m", "response": "ok"}, metadata))
    monkeypatch.setattr(sys, "argv", ["llm-council-debate", "q"])

    with pytest.raises(SystemExit) as exc_info:
        debate_module.main()

    assert exc_info.value.code == 0  # a shortfall alone is not a hard failure
    err = capsys.readouterr().err
    assert "WARNING: Only 3 of 4 minimum models responded live." in err


def test_substitutions_present_are_each_printed_as_a_note_to_stderr(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    metadata = {
        "substitutions": [
            {
                "slot_model": "z-ai/glm-5.2",
                "backup_model": "x-ai/grok-4.6",
                "reason": "unreachable after 3 attempts (last status=timeout)",
            },
            {
                "slot_model": "google/gemini-3.7-flash",
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
        "NOTE: google/gemini-3.7-flash was unreachable this session, "
        "substituted with backup qwen/qwen3.8-max (unreachable after 1 "
        "attempts (last status=auth_error))"
        in err
    )


def test_substitutions_absent_prints_no_note_lines(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
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


# ---------------------------------------------------------------------------
# Wall-clock ceiling parity (architecture-stress-test-2026-08-13.md High
# finding: debate.py had no overall ceiling at all, unlike pipeline_runner.py)
# ---------------------------------------------------------------------------


def test_max_wall_clock_seconds_flag_defaults_to_1200():
    parser = debate_module._build_arg_parser()
    args = parser.parse_args(["q"])
    assert args.max_wall_clock_seconds == 1200.0


def test_main_exits_4_and_names_the_ceiling_when_wall_clock_exceeded(monkeypatch, capsys):
    import asyncio

    async def slow_fn(*_args, **_kwargs):
        await asyncio.sleep(0.5)

    import scripts.council_adapter as ca_module

    monkeypatch.setattr(ca_module, "run_council_with_timeouts", slow_fn)
    monkeypatch.setattr(
        sys, "argv", ["llm-council-debate", "q", "--max-wall-clock-seconds", "0.05"]
    )

    with pytest.raises(SystemExit) as exc_info:
        debate_module.main()

    assert exc_info.value.code == 4
    err = capsys.readouterr().err
    assert "max_wall_clock_seconds" in err
    assert "0.05" in err


# ---------------------------------------------------------------------------
# Cost-ceiling parity (architecture-stress-test-2026-08-13.md Medium
# finding: debate.py had zero cost-ceiling enforcement at all)
# ---------------------------------------------------------------------------


def test_max_cost_usd_flag_defaults_to_none():
    parser = debate_module._build_arg_parser()
    args = parser.parse_args(["q"])
    assert args.max_cost_usd is None


def test_main_reports_total_cost_after_a_successful_run(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    stage3_result = {"model": "opus", "response": "the answer"}
    metadata = {"usage": {"total": {"cost_usd": 0.1234}}}
    _patch_run_council(monkeypatch, ([], [], stage3_result, metadata))
    monkeypatch.setattr(sys, "argv", ["llm-council-debate", "q"])

    with pytest.raises(SystemExit) as exc_info:
        debate_module.main()

    assert exc_info.value.code == 0
    err = capsys.readouterr().err
    assert "0.1234" in err


def test_main_exits_3_when_cost_exceeds_configured_ceiling(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    stage3_result = {"model": "opus", "response": "the answer"}
    metadata = {"usage": {"total": {"cost_usd": 5.0}}}
    _patch_run_council(monkeypatch, ([], [], stage3_result, metadata))
    monkeypatch.setattr(
        sys, "argv", ["llm-council-debate", "q", "--max-cost-usd", "1.0"]
    )

    with pytest.raises(SystemExit) as exc_info:
        debate_module.main()

    assert exc_info.value.code == 3
    err = capsys.readouterr().err
    assert "max_cost_usd" in err
    assert "5.0" in err or "5.0000" in err or "5.00" in err


# ---------------------------------------------------------------------------
# Durable persistence (docs/specs/durable-persistence-contract.md) - a
# one-shot debate previously wrote nothing anywhere.
# ---------------------------------------------------------------------------


def test_output_root_flag_defaults_to_council_runs():
    parser = debate_module._build_arg_parser()
    args = parser.parse_args(["q"])
    assert args.output_root == Path("./council-runs")


def test_main_persists_synthesis_md_with_verbatim_text_and_model_name(
    monkeypatch, capsys, tmp_path
):
    _patch_run_council(
        monkeypatch,
        ([], [], {"model": "anthropic/claude-opus-4.8", "response": "the synthesized answer"}, {}),
    )
    output_root = tmp_path / "custom-output"
    monkeypatch.setattr(
        sys,
        "argv",
        ["llm-council-debate", "what should we do", "--output-root", str(output_root)],
    )

    with pytest.raises(SystemExit) as exc_info:
        debate_module.main()

    assert exc_info.value.code == 0
    assert output_root.exists()
    written = list(output_root.glob("*/synthesis.md"))
    assert len(written) == 1
    text = written[0].read_text()
    assert "the synthesized answer" in text
    assert "anthropic/claude-opus-4.8" in text


def test_main_persisted_folder_name_has_a_real_utc_timestamp_and_60char_query_slug(
    monkeypatch, capsys, tmp_path
):
    import re
    from datetime import datetime, timezone

    _patch_run_council(
        monkeypatch,
        ([], [], {"model": "anthropic/claude-opus-4.8", "response": "answer"}, {}),
    )
    output_root = tmp_path / "custom-output"
    # 61 lowercase letters - already valid slug characters, so slugify()
    # changes nothing; any deviation in the folder's slug length pins
    # exactly which slice (`query[:60]`, not `[:61]` or the whole query)
    # produced it.
    long_query = "a" * 61
    monkeypatch.setattr(
        sys, "argv", ["llm-council-debate", long_query, "--output-root", str(output_root)]
    )

    before = datetime.now(timezone.utc)
    with pytest.raises(SystemExit):
        debate_module.main()
    after = datetime.now(timezone.utc)

    entries = list(output_root.iterdir())
    assert len(entries) == 1
    folder_name = entries[0].name

    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(a+)$", folder_name)
    assert m, f"unexpected folder name shape: {folder_name!r}"
    timestamp_str, slug = m.group(1), m.group(2)
    assert len(slug) == 60  # query[:60], not [:61] or unsliced

    parsed = datetime.strptime(timestamp_str, "%Y-%m-%dT%H-%M-%S").replace(tzinfo=timezone.utc)
    from datetime import timedelta

    assert before - timedelta(seconds=2) <= parsed <= after + timedelta(seconds=2)


def test_main_stage3_result_missing_response_key_prints_empty_line_and_persists_empty_body(
    monkeypatch, capsys, tmp_path
):
    _patch_run_council(monkeypatch, ([], [], {"model": "anthropic/claude-opus-4.8"}, {}))
    output_root = tmp_path / "custom-output"
    monkeypatch.setattr(
        sys, "argv", ["llm-council-debate", "q", "--output-root", str(output_root)]
    )

    with pytest.raises(SystemExit) as exc_info:
        debate_module.main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == "\n"  # print("") - not "None"

    written = list(output_root.glob("*/synthesis.md"))
    assert len(written) == 1
    # write_synthesis's own default ("") must have been passed through too -
    # an empty body, not the literal string "None" and not any placeholder.
    assert written[0].read_text() == "# Synthesis (chairman: anthropic/claude-opus-4.8)\n\n\n"


def test_main_stage3_result_missing_model_key_still_persists_as_exactly_unknown(
    monkeypatch, capsys, tmp_path
):
    # "model" key entirely absent - .get("model") is None, which is
    # != "error" so persistence must still run, falling back to "unknown"
    # as the persisted chairman name (mirrors write_synthesis's own default).
    _patch_run_council(monkeypatch, ([], [], {"response": "an answer"}, {}))
    output_root = tmp_path / "custom-output"
    monkeypatch.setattr(
        sys, "argv", ["llm-council-debate", "q", "--output-root", str(output_root)]
    )

    with pytest.raises(SystemExit):
        debate_module.main()

    written = list(output_root.glob("*/synthesis.md"))
    assert len(written) == 1
    assert written[0].read_text() == "# Synthesis (chairman: unknown)\n\nan answer\n"


def test_main_cost_exactly_at_ceiling_does_not_warn_or_change_exit_code(
    monkeypatch, capsys, tmp_path
):
    # Boundary: cost == max_cost_usd must NOT be treated as "exceeded"
    # (strict > semantics) - a >= mutant would wrongly warn/exit 3 here.
    monkeypatch.chdir(tmp_path)
    stage3_result = {"model": "opus", "response": "the answer"}
    metadata = {"usage": {"total": {"cost_usd": 1.0}}}
    _patch_run_council(monkeypatch, ([], [], stage3_result, metadata))
    monkeypatch.setattr(
        sys, "argv", ["llm-council-debate", "q", "--max-cost-usd", "1.0"]
    )

    with pytest.raises(SystemExit) as exc_info:
        debate_module.main()

    assert exc_info.value.code == 0
    assert "max_cost_usd" not in capsys.readouterr().err


def test_main_does_not_persist_when_stage3_result_is_the_error_sentinel(
    monkeypatch, capsys, tmp_path
):
    stage3_result = {"model": "error", "response": "All models failed to respond. Please try again."}
    _patch_run_council(monkeypatch, ([], [], stage3_result, {}))
    output_root = tmp_path / "custom-output"
    monkeypatch.setattr(
        sys, "argv", ["llm-council-debate", "q", "--output-root", str(output_root)]
    )

    with pytest.raises(SystemExit) as exc_info:
        debate_module.main()

    assert exc_info.value.code == 1
    assert not output_root.exists()


def test_main_persist_failure_is_non_fatal_and_warns_but_still_exits_zero(
    monkeypatch, capsys, tmp_path
):
    _patch_run_council(
        monkeypatch,
        ([], [], {"model": "anthropic/claude-opus-4.8", "response": "the synthesized answer"}, {}),
    )

    def _raise(*args, **kwargs):
        raise OSError("disk full")

    import scripts.transcript_writer as tw_module

    monkeypatch.setattr(tw_module, "write_synthesis", _raise)
    monkeypatch.setattr(sys, "argv", ["llm-council-debate", "q"])
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        debate_module.main()

    # A persistence failure must never crash an otherwise-successful debate.
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "the synthesized answer"
    assert "WARNING: failed to persist synthesis.md" in captured.err
    assert "disk full" in captured.err
