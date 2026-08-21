"""Blind acceptance test for docs/specs/stage1-5-normalizer-timeout-contract.md
(AC10) -- `scripts/debate.py` must expose a `--stage1-5-timeout` CLI flag
(`type=float, default=300.0`), threaded into `run_council_with_timeouts`
alongside the existing three `--stageN-timeout` flags.

Authored WITHOUT sight of any implementation. As of this writing,
`debate.py`'s arg parser has no `--stage1-5-timeout` flag at all -- every
test below is expected to fail RED (`SystemExit` from an unrecognized flag,
or an `args` namespace with no `stage1_5_timeout` attribute, or a fake
`run_council_with_timeouts` call missing the kwarg) until the contract
lands.

Harness mirrors `tests/test_debate.py`'s own established
`_patch_run_council` pattern (function-local import inside `main()` means
the only patchable target is `scripts.council_adapter`'s own namespace,
not `debate_module`'s).
"""
from __future__ import annotations

import sys

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

    import scripts.council_adapter as ca_module

    monkeypatch.setattr(ca_module, "run_council_with_timeouts", fake)
    return calls


def test_ac10_stage1_5_timeout_flag_defaults_to_300(monkeypatch):
    parser = debate_module._build_arg_parser()
    args = parser.parse_args(["some question"])
    assert args.stage1_5_timeout == 300.0


def test_ac10_stage1_5_timeout_flag_parses_from_explicit_value(monkeypatch):
    parser = debate_module._build_arg_parser()
    args = parser.parse_args(["q", "--stage1-5-timeout", "45"])
    assert args.stage1_5_timeout == 45.0
    assert isinstance(args.stage1_5_timeout, float)


def test_ac10_stage1_5_timeout_threads_through_to_run_council_with_timeouts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = _patch_run_council(
        monkeypatch,
        ([], [], {"model": "m", "response": "ok"}, {}),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["llm-council-debate", "q", "--stage1-5-timeout", "45"],
    )

    with pytest.raises(SystemExit):
        debate_module.main()

    assert len(calls) == 1
    assert calls[0]["stage1_5_timeout"] == 45.0


def test_ac10_stage1_5_timeout_defaults_to_300_when_flag_omitted(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = _patch_run_council(
        monkeypatch,
        ([], [], {"model": "m", "response": "ok"}, {}),
    )
    monkeypatch.setattr(sys, "argv", ["llm-council-debate", "q"])

    with pytest.raises(SystemExit):
        debate_module.main()

    assert len(calls) == 1
    assert calls[0]["stage1_5_timeout"] == 300.0
