"""Regression test for the exact bug class found twice in this project's
early setup (docs/upstream-deltas.md): council.models silently diverging
from tiers.pools.<tier>.models with NO error from the tool itself.

Loads the REAL project llm_council.yaml (not a fixture) so this fails loudly
the moment someone hand-edits the config and breaks the double-nesting
workaround or lets a tier drift out of sync - instead of only being caught
by the next person who happens to run council_health_check AND consult_council
and compares them by hand, as happened here.
"""
from __future__ import annotations

from pathlib import Path

from llm_council.tier_contract import create_tier_contract
from llm_council.unified_config import load_config

CONFIG_PATH = Path(__file__).resolve().parent.parent / "llm_council.yaml"


def _load_real_config():
    assert CONFIG_PATH.exists(), f"expected llm_council.yaml at {CONFIG_PATH}"
    return load_config(CONFIG_PATH)


def test_council_models_and_high_tier_pool_are_in_sync():
    config = _load_real_config()
    council_models = config.council.models
    high_tier_models = config.tiers.pools["high"].models
    assert council_models == high_tier_models, (
        "council.models and tiers.pools.high.models have drifted apart - "
        "council_health_check will report one, a real consult_council() "
        "call will use the other. See docs/upstream-deltas.md."
    )


def test_council_models_and_reasoning_tier_pool_are_in_sync():
    # The 'high' check above does NOT cover this - tiers.default is
    # "reasoning" (see llm_council.yaml's own comment on why: same allowed
    # models as "high", larger timeout/token budget), so a real
    # consult_council() call resolves ITS model list via
    # tiers.pools["reasoning"], not tiers.pools["high"]. Confirmed
    # architecture-stress-test-2026-08-13.md finding: this file's own
    # docstring says it exists to catch exactly this drift class, but only
    # asserted against the tier that isn't actually used in production.
    config = _load_real_config()
    council_models = config.council.models
    reasoning_tier_models = config.tiers.pools["reasoning"].models
    assert council_models == reasoning_tier_models, (
        "council.models and tiers.pools.reasoning.models have drifted apart - "
        "tiers.default is 'reasoning', so THIS is the pool a real "
        "consult_council() call actually resolves against. "
        "See docs/upstream-deltas.md."
    )


def test_tiers_default_is_reasoning():
    # Pins the assumption both sync tests above depend on - if this ever
    # changes, the "which tier is actually live" reasoning in this file's
    # module docstring and in docs/upstream-deltas.md needs re-checking too.
    config = _load_real_config()
    assert config.tiers.default == "reasoning"


def test_chairman_is_a_council_member():
    config = _load_real_config()
    assert config.council.chairman in config.council.models


def test_council_size_is_exactly_four():
    config = _load_real_config()
    assert len(config.council.models) == 4


def test_kimi_k3_present_but_never_chairman():
    # 2026-08-17: 4th seat swapped z-ai/glm-5.2 -> moonshotai/kimi-k3 per
    # docs/specs/core-seat-swap-contract.md (GLM-5.2 never graduated its
    # 20-session ADR-029 bar). Same chairman-exclusion rule carries over
    # unchanged -- it was never GLM-specific, it's "the 4th seat."
    config = _load_real_config()
    kimi_models = [m for m in config.council.models if m.startswith("moonshotai/kimi")]
    assert len(kimi_models) == 1
    assert config.council.chairman not in kimi_models


def test_rubric_scoring_is_enabled():
    # Regression: this was silently off for the entire first dry run.
    config = _load_real_config()
    assert config.evaluation.rubric.enabled is True


def test_gateway_fallback_chain_matches_stored_credentials():
    # Only an OpenRouter key is stored; a chain that includes requesty/direct
    # would silently attempt gateways with no credentials on failure.
    config = _load_real_config()
    assert config.gateways.fallback.chain == ["openrouter"]


def test_create_tier_contract_high_resolves_from_real_config_dir(monkeypatch):
    # The actual bug: create_tier_contract reads config from CWD via
    # _find_config_file(), independent of load_config(explicit_path) above -
    # this must be exercised from the real project directory to mean anything.
    monkeypatch.chdir(CONFIG_PATH.parent)
    contract = create_tier_contract("high")
    config = _load_real_config()
    assert contract.allowed_models == config.council.models
