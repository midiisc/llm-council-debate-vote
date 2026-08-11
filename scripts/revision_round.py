"""Stage 2.75 correction-biased revision round: triggered only when CSS is
below threshold, each model may revise only by citing a specific verified
fact that contradicts its own claim. "Others agree" is explicitly disallowed
as a reason to switch.

Contract: docs/specs/custom-scripts-contracts.md, Contract 2.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from scripts.grounding_pass import TaggedClaim

_NO_SWITCH_SENTENCE = (
    "The other models agreeing with each other is not a valid reason to switch."
)

# A verified-fact citation is a `[[cite:<id>]]` marker anywhere in the
# response. Whatever remains after stripping the marker is the revised text.
_CITE_MARKER_RE = re.compile(r"\[\[cite:\s*(\S+?)\s*\]\]", re.IGNORECASE)


@dataclass
class ModelAnswer:
    model: str
    original_text: str
    critique: str  # this model's own Stage 2 critique, not another model's


@dataclass
class RevisionOutcome:
    model: str
    original_text: str
    revised_text: Optional[str]  # None if revision rejected/not offered
    cited_fact_id: Optional[str]  # the verified_facts id it cited, if any
    accepted: bool  # True only if a valid citation was found
    cost_usd: float = 0.0  # real cost of the query_fn call that produced this outcome


def should_trigger_revision(css: float, threshold: float = 0.50) -> bool:
    return css < threshold


def build_revision_prompt(
    answer: ModelAnswer,
    verified_facts: list[TaggedClaim],  # only VERIFIED/CONTRADICTED tagged claims
) -> str:
    if verified_facts:
        facts_block = "\n".join(
            f"[{tc.claim.id}] ({tc.tag}) {tc.claim.text}" for tc in verified_facts
        )
    else:
        facts_block = "(no verified facts available)"

    return (
        "Your original answer:\n"
        f"{answer.original_text}\n\n"
        "Your own critique from the previous round:\n"
        f"{answer.critique}\n\n"
        "Verified facts (id, tag, text):\n"
        f"{facts_block}\n\n"
        "You may revise your answer ONLY by citing a specific verified fact "
        "id above that directly contradicts your own claim. "
        f"{_NO_SWITCH_SENTENCE}\n\n"
        "If you revise, start your response with a citation marker naming the "
        "fact id, e.g. `[[cite:<id>]]`, followed by your revised answer text. "
        "If you are not revising, do not include a citation marker."
    )


def parse_revision_response(
    response_text: str, verified_facts: list[TaggedClaim]
) -> tuple[Optional[str], Optional[str]]:
    valid_ids = {tc.claim.id for tc in verified_facts}

    cite_match = _CITE_MARKER_RE.search(response_text)
    if not cite_match:
        return None, None

    cited_id = cite_match.group(1).strip()
    if cited_id not in valid_ids:
        return None, None

    revised_text = _CITE_MARKER_RE.sub("", response_text, count=1).strip()
    if not revised_text:
        return None, None

    return revised_text, cited_id


async def run_revision_round(
    css: float,
    answers: list[ModelAnswer],
    verified_facts: list[TaggedClaim],
    # (model, prompt) -> (response_text, cost_usd). cost_usd must be a real,
    # non-negative figure from the caller (e.g. OpenRouter's reported spend) -
    # never a placeholder, since pipeline_runner.py sums this into
    # total_cost_usd and the cost ceiling re-checks against it.
    query_fn: Callable[[str, str], Awaitable[tuple[str, float]]],
) -> list[RevisionOutcome]:
    if not should_trigger_revision(css):
        return [
            RevisionOutcome(
                model=answer.model,
                original_text=answer.original_text,
                revised_text=None,
                cited_fact_id=None,
                accepted=False,
                cost_usd=0.0,
            )
            for answer in answers
        ]

    outcomes: list[RevisionOutcome] = []
    for answer in answers:
        prompt = build_revision_prompt(answer, verified_facts)
        response, cost_usd = await query_fn(answer.model, prompt)
        revised_text, cited_fact_id = parse_revision_response(response, verified_facts)
        outcomes.append(
            RevisionOutcome(
                model=answer.model,
                original_text=answer.original_text,
                revised_text=revised_text,
                cited_fact_id=cited_fact_id,
                accepted=revised_text is not None and cited_fact_id is not None,
                cost_usd=cost_usd,
            )
        )
    return outcomes
