# Tool selection: amiable-dev/llm-council

Decided: 2026-08-09. Re-open only on a verified, material capability gap.

## Requirement bar (9 pipeline stages)
1. Independent parallel drafts, zero cross-visibility
2. Anonymization + optional style normalization
3. Peer review + quantitative consensus score, self-vote exclusion
4. Conditional debate gating tied to measured (dis)agreement
5. Chairman/synthesizer with an explicit disagreement-surfacing mode
6. Automatic minority-report / dissent extraction
7. Persistent, cross-session bias audit (length bias, reviewer calibration, position bias)
8. MCP-native (plugs into Claude Code as a tool, not just a CLI)
9. Gateway flexibility (OpenRouter/Requesty/direct, switchable via config)

## Verdict
`amiable-dev/llm-council` (PyPI `llm-council-core`) natively covers all 9. It is
a from-scratch derivative of Karpathy's original `llm-council` (which itself is
essentially a frozen one-day hack — no MCP, no scoring, no gating, no bias
audit, no license file) — not the same project, despite the shared name.

Confirmed native mechanisms (source: ADR-010, ADR-036, ADR-044 at
github.com/amiable-dev/llm-council, retrieved 2026-08-09):
- CSS formula: `CSS = (winner_margin × 0.6) + ((1 − variance) × 0.4)`, bands
  0.85-1.0 strong / 0.70-0.84 moderate / 0.50-0.69 weak → `include_dissent` /
  <0.50 → debate mode (ADR-036).
- Self-vote exclusion confirmed in code (ADR-010).
- Escalation signal is CSS-driven (ADR-044) — functionally matches this
  project's "don't always debate" requirement, but ADR-044 frames it around
  compute-optimal test-time scaling / RouteLLM-style routing, NOT the
  Choi/Zhu/Li "Debate or Vote" NeurIPS 2025 paper (arXiv:2508.17536) this
  project's pipeline design is grounded in. The mechanism aligns; the stated
  rationale differs. Worth re-checking if amiable-dev ever cites that paper
  directly, since it would tighten the alignment.

## Alternatives considered and rejected
| Tool | Native coverage | Why rejected |
|---|---|---|
| karpathy/llm-council (original) | ~2 of 9 | Frozen since creation, no MCP, no scoring/gating/dissent/bias-audit |
| az9713/llm-council (fork) | unclear, low | ~3 commits, license unclear, no confirmed MCP |
| blueman82/ai-counsel | ~3 of 9 | No anonymization (opposite of requirement 2 by design), no self-vote exclusion, no chairman, no dissent extraction, no bias audit |
| raiyanyahya/ensemble | ~4 of 9 | Gating is exit-only not entry-gated on disagreement, no bias-audit module, no OpenRouter |
| Consilium (hackathon MCP) | ~2 of 9 | No scoring formalism, no gating, no dissent extraction, no bias audit, stale model roster |
| zen/pal-mcp-server `consensus` tool | ~1 of 9 | Shallow "gather opinions" tool, no scoring/anonymization/gating/dissent/bias-audit |
| LangGraph / AutoGen-AG2 / CrewAI | 0 of 9 native | Orchestration frameworks only — every scoring/gating/dissent/bias-audit stage would be hand-built, defeating the point |
| Mixture-of-Agents (Together AI / SMoA) | 0 of 9 native | Research reference code, fusion-based not anonymized-peer-review-based, not MCP |
| deeplearning-wisc/debate-or-vote (the cited paper's own repo) | 0 of 9 | This is benchmark evaluation code for the paper itself, not a deployable tool |

## Known risk to weigh, not disqualifying
- Young/low-adoption relative to Karpathy's viral original (38 stars vs ~24k
  as of 2026-08-09) — smaller surface of independent scrutiny.
- **Security advisory**: versions 0.22.0-0.38.2 could leak credential files to
  LLM providers during verify/gate runs, fixed in 0.39.0+. This repo pins
  `>=0.39.0` as a hard floor (see CLAUDE.md).
- Last confirmed push: 2026-07-13 (~4 weeks before this decision) — active,
  not stale, but re-check activity before any future re-adoption decision.

## Sources (retrieved 2026-08-09)
- https://github.com/amiable-dev/llm-council
- https://pypi.org/project/llm-council-core/
- https://llm-council.dev/guides/mcp/
- https://llm-council.dev/adr/ADR-010-consensus-mechanisms/
- https://llm-council.dev/adr/ADR-036-output-quality-quantification/
- https://llm-council.dev/adr/ADR-044-compute-optimal-deliberation/
- https://github.com/karpathy/llm-council
- https://arxiv.org/abs/2508.17536
- https://github.com/deeplearning-wisc/debate-or-vote
- https://github.com/blueman82/ai-counsel
- https://github.com/raiyanyahya/ensemble
