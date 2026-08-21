# Upstream issue — FILED as amiable-dev/llm-council#593

Filed 2026-08-16: https://github.com/amiable-dev/llm-council/issues/593

Drafted in a different project's session (the xSpecies deck project) that hit
this while hardening its own `llm_council.yaml` against rate-limit degradation.
Follows this project's own precedent (`#591`, `#592` in `docs/upstream-deltas.md`)
— filed with explicit user go-ahead. Consider adding a matching entry to
`docs/upstream-deltas.md` in this project's own ledger format, since that file
wasn't touched directly from the other session (cross-project edit boundary).

---

**Title:** `gateways.fallback` config is unreachable dead code — `_use_gateway_layer()` checks a config field that doesn't exist on `GatewayConfig`

**Version:** llm-council-core 0.40.1 (PyPI)

**Summary:**

The gateway abstraction layer (`llm_council/gateway/`, ADR-023) — which provides
provider fallback chains (e.g. `openrouter` → `direct` → `requesty`) — can never
actually activate, for any configuration, because the feature-gate checks a
config field that the config schema doesn't define.

**Root cause:**

`llm_council/gateway_adapter.py`:

```python
def _use_gateway_layer() -> bool:
    """Check if gateway layer is enabled from unified config."""
    config = get_config()
    return getattr(config.gateways, "enabled", False) if hasattr(config, "gateways") else False


USE_GATEWAY_LAYER = _use_gateway_layer()
```

`USE_GATEWAY_LAYER` is evaluated once, at module import time, and gates every
`gateway_adapter.query_model`/`query_model_with_status`/`query_models_parallel`/
`query_models_with_progress` call — when `False`, all of these silently delegate
to the raw OpenRouter-only functions imported from `llm_council/openrouter.py`
(`_direct_query_model` etc.), bypassing the gateway/fallback layer entirely.

`config.gateways` is a `GatewayConfig` instance (`llm_council/unified_config.py`,
~line 325):

```python
class GatewayConfig(BaseModel):
    """Configuration for gateway routing (ADR-023, Layer 4)."""
    model_config = ConfigDict(protected_namespaces=())
    default: str = Field(default="openrouter")
    providers: Dict[str, Union[GatewayProviderConfig, OllamaProviderConfig]] = Field(default_factory=dict)
    model_routing: Dict[str, str] = Field(default_factory=dict)
    model_name_map: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)
```

There is no `enabled` field on this model — only `default`, `providers`,
`model_routing`, `model_name_map`, `fallback`. So `getattr(config.gateways,
"enabled", False)` always falls through to the `False` default, no matter what
a user puts in `gateways.fallback.enabled` or `gateways.fallback.chain`.
`USE_GATEWAY_LAYER` is permanently `False` for every possible config.

**Impact:**

- `gateways.fallback.chain` (e.g. `[openrouter, direct]` to add a direct-provider
  fallback when a user has a matching `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/
  `GOOGLE_API_KEY`) has no effect — every request always goes through
  OpenRouter only, regardless of configuration.
- `gateways.providers.<name>.enabled` (per-provider toggles) and
  `gateways.model_routing`/`model_name_map` are equally unreachable for the same
  reason — they're all read inside the `if USE_GATEWAY_LAYER:` branches.
- This is silent: no error, no warning, `council_health_check` reports
  `ready: true` regardless (it doesn't inspect this code path at all), and the
  config loads and validates successfully. A user configuring a fallback chain
  has every reason to believe it's active.

**Reproduction:**

```python
from llm_council.gateway_adapter import USE_GATEWAY_LAYER
print(USE_GATEWAY_LAYER)  # False, even with gateways.fallback.enabled: true set
```

Or: set `gateways.fallback.chain: [openrouter, direct]` with a real provider key
present in env for one of the configured models, force that model's OpenRouter
route to fail (or just trace with a debugger/print), and confirm the direct
gateway is never invoked.

**Suggested fix (one of):**

1. Add `enabled: bool = True` (or `False`, whichever matches the intended
   default) to `GatewayConfig`, and have `_use_gateway_layer()` read it as
   intended.
2. Or, if gating on a single top-level flag was never the intent and
   `gateways.fallback.enabled` alone should suffice: change
   `_use_gateway_layer()` to check `config.gateways.fallback.enabled` instead
   of a non-existent `config.gateways.enabled`.
3. Either way, add a regression test asserting `USE_GATEWAY_LAYER` reflects a
   real config value, and that `gateway_adapter.query_model` actually routes
   through the configured fallback chain when a provider fails — the current
   test suite apparently didn't catch this (checked: no test in the installed
   package's own repo asserts this behavior, though this was checked against
   the installed wheel, not a full clone of the test suite).

**How this was found:** grounding a rate-limit hardening pass for a
`google/gemini-3.6-flash` direct-provider fallback, then re-verifying by tracing
the actual call path (not just confirming the config loaded) after being asked
whether the same fallback should exist for `anthropic/claude-opus-4.8` too.
