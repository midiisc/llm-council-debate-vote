"""Stage 3.75 devil's-advocate + counterfactual critique: runs once, on
Stage 3's synthesis only, by GPT-5.5 exclusively - never the chairman
(Opus 4.8), since self-critique on one's own output is a documented
failure mode (arXiv:2607.28576), not a hypothetical one. Gated on
CSS < 0.50 OR any model flagged is_outlier.

Output is a labeled critique memo attached to the synthesis for the
still-manual Stage 4 premortem to read - never a rewrite, never an
auto-triggered re-synthesis.

Contract: docs/specs/stage-3-75-critique-contract.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

QueryModelFn = Callable[[str, str], Awaitable[tuple[str, float]]]
# (model, prompt) -> (response_text, cost_usd) - matches Stage 2.75/Stage 4's
# existing injected query_model shape, no new contract needed.

# Hardcoded, deliberately - the same convention live_adapters.py already
# uses for EVIDENCE_MODEL/COMPLETENESS_CHECK_MODEL: a role tied to one
# specific, chosen model, not a config-driven seat. Never Opus-4.8 (the
# chairman) - see module docstring.
CRITIC_MODEL = "openai/gpt-5.5"

_SYNTHESIS_SECTION_BEGIN = "--- BEGIN SYNTHESIS ---"
_SYNTHESIS_SECTION_END = "--- END SYNTHESIS ---"


def should_trigger_critique(
    css: float, is_outlier: dict[str, bool], threshold: float = 0.50
) -> bool:
    return css < threshold or any(is_outlier.values())


def build_critique_prompt(synthesis_text: str) -> str:
    return (
        "Below is a synthesized answer that a council of models already "
        "converged on. Your job is to attack it, not rewrite it.\n\n"
        "Write a critique memo using two techniques:\n"
        "1. Devil's-advocate / adversarial critique: actively argue the "
        "strongest real case AGAINST this conclusion, as if you were "
        "trying to convince someone it's wrong.\n"
        "2. Counterfactual / what-if: state what would have to be true "
        "for this conclusion to be wrong, and how likely that is.\n\n"
        "This is a critique memo for a human to read alongside the "
        "synthesis, not a replacement for it - do not produce a rewritten "
        "or alternative synthesis, and do not claim to supersede the "
        "original answer.\n\n"
        f"{_SYNTHESIS_SECTION_BEGIN}\n"
        f"{synthesis_text}\n"
        f"{_SYNTHESIS_SECTION_END}"
    )


@dataclass
class CritiqueOutcome:
    critique_text: str
    cost_usd: float
    model: str


async def run_critique_round(
    synthesis_text: str, query_fn: QueryModelFn
) -> CritiqueOutcome:
    text, cost = await query_fn(CRITIC_MODEL, build_critique_prompt(synthesis_text))
    return CritiqueOutcome(critique_text=text, cost_usd=cost, model=CRITIC_MODEL)
