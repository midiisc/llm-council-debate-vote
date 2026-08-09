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

## Confirmed-correct (no action needed)
- Install extras `[mcp,secure]`, keychain-silent-ignore behavior — confirmed verbatim in README.
- `llm-council setup-key`, `llm-council serve`, `council_health_check` fields (`api_key_configured`, `key_source`, `council_size`, `estimated_duration`, `ready`) — confirmed.
- `consult_council` metadata path `metadata["quality_metrics"]["core"]["consensus_strength"]` — confirmed exactly.
- `llm_council.yaml` schema (`council.tiers.default`, `council.tiers.pools.<tier>.models`, `timeout_seconds`, `council.gateways.default`) — confirmed.
- `claude mcp add --transport stdio llm-council --scope user -- llm-council` — confirmed exact registration command.
- `anthropic/claude-opus-4.8`, `openai/gpt-5.5` model slugs — confirmed live on OpenRouter.
- OpenRouter 5.5% non-crypto fee, Requesty 5% markup + EU residency — confirmed against pricing pages.
- Requesty BYOK removing the 5% markup — **not confirmed**, pricing page doesn't state this; treat as unverified, don't assume it in cost planning.

## Check log
- 2026-08-09 — initial grounding pass (2 parallel research checks: package/CLI/config verification, competitive tool survey). Populated this ledger for the first time.
