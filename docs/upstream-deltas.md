# Upstream deltas ledger

Every verified drift between what this repo assumes (setup doc, config, scripts)
and what's actually live in `amiable-dev/llm-council`, dated and sourced. Updated
by the Pillar 5 self-update check (every 2-3 days) and by any ad hoc grounding
pass. Never edit `llm_council.yaml`/`.env`/scripts based on a claim that isn't
recorded here or freshly re-verified.

Last upstream check: **2026-08-09**. Checked against: PyPI `llm-council-core`
v0.40.1, GitHub `amiable-dev/llm-council` @ `master` (README + ADR-010/-036/-044),
live OpenRouter `/api/v1/models` catalog.

## Open deltas (setup doc still says the old thing — needs a doc/config fix)

| Item | Setup doc says | Actually true (2026-08-09) | Source | Status |
|---|---|---|---|---|
| `LLM_COUNCIL_ACCURACY_CEILING` | Real env var | Does not exist — accuracy ceiling is YAML-only (`evaluation.rubric`), no env var | github.com/amiable-dev/llm-council README | **Fix in setup doc/config** |
| `LLM_COUNCIL_DEADLOCK_THRESHOLD` | Real env var | Does not exist — deadlock detection is a fixed internal 0.1 Borda-spread constant, not configurable | same | **Fix in setup doc/config** |
| `google/gemini-3-pro` model slug | Used in `llm_council.yaml` example | Not a real OpenRouter slug. Use `google/gemini-3-pro-preview` or `google/gemini-3.1-pro-preview`. (Note: this typo exists in upstream's own README too — worth an upstream issue.) | live OpenRouter `/api/v1/models`, github.com/amiable-dev/llm-council README | **Fix in setup doc/config** |
| Install command has no version floor | `pip install "llm-council-core[mcp,secure]"` | Must pin `>=0.39.0` — 0.22.0-0.38.2 has a confirmed credential-leak advisory during verify/gate runs | GitHub security advisory, amiable-dev/llm-council | **Fix in setup doc — security, do first** |
| `claude mcp add --transport stdio llm-council --scope user -- llm-council` | Doc's registration command | Wrong in v0.40.1: the `llm-council` console script (`cli:main`) is an argparse CLI with no bare stdio-serve behavior; `llm-council serve` is HTTP-only (`--host`/`--port`, no stdio option). The actual stdio MCP entrypoint is a separate module never wired to a console script: `mcp_server.py`'s own `main()` (`mcp.run()`). Correct command: `claude mcp add --transport stdio llm-council --scope user -- <tool-venv>/bin/python -m llm_council.mcp_server`. Verified by running it directly (clean exit, no traceback) before registering. | Read `cli.py` entry_points + `mcp_server.py` source directly, `llm-council serve --help` output, 2026-08-09 | **Fixed — registered correctly, wrong version removed first** |
| `council.tiers.pools.<tier>.models` in `llm_council.yaml` | Doc's example config | Inert under default settings (`triage.enabled: false`) — only read by the triage/complexity-classification layer and `frontier_fallback.py`. The actually-active council is `council.models` (flat list) + `council.chairman` (single string) — confirmed via `mcp_server.py:101` (`COUNCIL_MODELS = _get_council_models()`) and `council.py:70`. Following the doc's example verbatim would have silently run the package's hardcoded default 4 models (including `deepseek/deepseek-v4-pro`, never requested) instead of the intended pool. | Read `unified_config.py`/`council.py`/`mcp_server.py` source directly, 2026-08-09 | **Fixed — `llm_council.yaml` uses `council.models`/`council.chairman` directly** |
| "Gemini 3 Pro" as a core model | Setup doc / earlier upstream README | No Gemini 3 "Pro"-tier text/chat model exists on OpenRouter as of 2026-08-09 (only `gemini-3.6-flash`, `gemini-3.5-flash-lite`, `gemini-3-pro-image` [multimodal]). Both previously-cited slugs (`gemini-3-pro-preview`, `gemini-3.1-pro-preview`) are gone from the live catalog entirely. | Live OpenRouter `/api/v1/models` query, 2026-08-09 | **Resolved — using `google/gemini-3.6-flash` as interim 3rd-core seat, see pipeline-architecture-spec.md §4** |

## Confirmed-correct (no action needed)
- Install extras `[mcp,secure]`, keychain-silent-ignore behavior — confirmed verbatim in README.
- `llm-council setup-key`, `llm-council serve`, `council_health_check` fields (`api_key_configured`, `key_source`, `council_size`, `estimated_duration`, `ready`) — confirmed.
- `consult_council` metadata path `metadata["quality_metrics"]["core"]["consensus_strength"]` — confirmed exactly.
- `llm_council.yaml` schema (`council.tiers.default`, `council.tiers.pools.<tier>.models`, `timeout_seconds`, `council.gateways.default`) — confirmed.
- `claude mcp add --transport stdio llm-council --scope user -- llm-council` — confirmed exact registration command.
- `anthropic/claude-opus-4.8`, `openai/gpt-5.5` model slugs — confirmed live on OpenRouter.
- OpenRouter 5.5% non-crypto fee, Requesty 5% markup + EU residency — confirmed against pricing pages.
- Requesty BYOK removing the 5% markup — **not confirmed**, pricing page doesn't state this; treat as unverified, don't assume it in cost planning.

| `load_config()` in `unified_config.py` (~line 1095) | Any `llm_council.yaml` following the doc's/README's nesting example | **Confirmed live bug in v0.40.1**: extracts the inner contents of the top-level `council:` key and passes them as `UnifiedConfig(**council_config)` kwargs directly, instead of re-nesting under `.council`. Since `UnifiedConfig` has no top-level `models`/`chairman`/`synthesis_mode`/etc. fields, they're silently dropped (pydantic default `extra="ignore"`) and the package's hardcoded defaults apply instead — **with no error, no warning, `ready: true`**. Only `gateways:` survives, because it happens to also be a real top-level `UnifiedConfig` field. First caught by actually running `council_health_check` and finding `deepseek/deepseek-v4-pro` in the model list despite never being configured. Workaround (verified working): wrap council-level fields in one EXTRA `council:` layer — see comment block at the top of `llm_council.yaml`. Worth an upstream GitHub issue; not filed yet. | Direct execution of `load_config()` against both the buggy and workaround YAML shapes, 2026-08-09 | **Fixed via workaround in `llm_council.yaml`; upstream bug not yet reported** |

## Check log
- 2026-08-09 — initial grounding pass (2 parallel research checks: package/CLI/config verification, competitive tool survey). Populated this ledger for the first time.
- 2026-08-09 — MCP registration + live `council_health_check` execution caught 2 further live bugs beyond the initial grounding pass: wrong stdio entrypoint in the doc's registration command, and the `load_config()` council-nesting bug above. Both confirmed by direct execution, not just source reading — reinforces that even grounded source-reading isn't a substitute for actually running the thing.
