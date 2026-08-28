"""Tests for scripts.council_adapter._load_length_control_config
(amiable-dev/llm-council#675 local mitigation config loading)."""
from __future__ import annotations

from pathlib import Path

import scripts.council_adapter as ca


def test_defaults_when_no_config_file(tmp_path: Path):
    missing = tmp_path / "does-not-exist.yaml"

    config = ca._load_length_control_config(config_path=missing)

    assert config.enabled is False
    assert config.sensitivity == 0.15
    assert config.min_length_chars == 1


def test_defaults_when_block_absent(tmp_path: Path):
    path = tmp_path / "llm_council.yaml"
    path.write_text("council:\n  models: [a, b]\n")

    config = ca._load_length_control_config(config_path=path)

    assert config.enabled is False


def test_parses_enabled_block(tmp_path: Path):
    path = tmp_path / "llm_council.yaml"
    path.write_text(
        "length_control:\n"
        "  enabled: true\n"
        "  sensitivity: 0.25\n"
        "  min_length_chars: 10\n"
    )

    config = ca._load_length_control_config(config_path=path)

    assert config.enabled is True
    assert config.sensitivity == 0.25
    assert config.min_length_chars == 10


def test_partial_block_keeps_other_defaults(tmp_path: Path):
    path = tmp_path / "llm_council.yaml"
    path.write_text("length_control:\n  enabled: true\n")

    config = ca._load_length_control_config(config_path=path)

    assert config.enabled is True
    assert config.sensitivity == 0.15  # unspecified -> dataclass default
    assert config.min_length_chars == 1


def test_this_repos_real_config_file_parses_as_enabled():
    """Not hermetic on purpose - confirms the actual llm_council.yaml this
    repo ships (with a real length_control: block added 2026-08-28) parses
    to enabled=True, so a future edit to that file that silently breaks the
    block's shape gets caught here."""
    real_config = Path(__file__).parent.parent / "llm_council.yaml"

    config = ca._load_length_control_config(config_path=real_config)

    assert config.enabled is True
    assert config.sensitivity == 0.15
