"""Stage 4: post-synthesis completeness check - flags VERIFIED/CONTRADICTED
facts from Stage 0.5 that the chairman's final synthesis doesn't address,
guarding against the "Deliberative Illusion" failure mode (arXiv:2606.03032)
where apparent consensus masks factual attrition. Diagnostic only - never
edits the synthesis or re-triggers revision. Named Stage 4 (not 3.5) since
run_full_council's own internal aggregate-rankings step already uses "3.5".

Contract: docs/specs/custom-scripts-contracts.md, Contract 4.
"""
from __future__ import annotations

import json
from typing import Awaitable, Callable

from scripts.grounding_pass import TaggedClaim

QueryFn = Callable[[str, str], Awaitable[tuple[str, float]]]


_FINDINGS_SECTION_BEGIN = "--- BEGIN FINDINGS ---"
_FINDINGS_SECTION_END = "--- END FINDINGS ---"


def build_completeness_prompt(verified_facts: list[TaggedClaim], synthesis: str) -> str:
    # Delimited (docs/specs/proposal-a-reference-grounding-contract.md,
    # Contract 2 completion) so a crafted claim.text can't forge text that
    # reads as prompt instructions - mirrors revision_round.py's
    # _build_facts_section pattern; kept as a local, independently-shaped
    # wrapper rather than a shared import since this module's facts_block
    # format (no "source:" field) intentionally differs from
    # revision_round.py's.
    facts_block = "\n".join(
        f"[{tc.claim.id}] ({tc.tag}) {tc.claim.text}" for tc in verified_facts
    )
    return (
        "Below is a list of research findings that were established before "
        "a final answer was synthesized, and the final synthesized answer "
        "itself. Identify which finding ids, if any, are NOT reflected or "
        "addressed anywhere in the final answer - not necessarily verbatim, "
        "but the substance of the finding must be genuinely absent.\n\n"
        "Findings (id, tag, text):\n"
        f"{_FINDINGS_SECTION_BEGIN}\n"
        f"{facts_block}\n"
        f"{_FINDINGS_SECTION_END}\n\n"
        "Final answer:\n"
        f"{synthesis}\n\n"
        "Respond with ONLY a JSON array of the ids that are NOT addressed, "
        'e.g. ["3","7"], or [] if every finding is addressed. No other text.'
    )


def parse_completeness_response(
    raw_content: str, verified_facts: list[TaggedClaim]
) -> tuple[list[str], bool]:
    """Never raises. Returns (dropped_ids, parse_ok).

    parse_ok=False means the response could not be understood at all -
    dropped_ids is always [] in that case, but that [] means "undetermined,"
    NOT "verified nothing was dropped." Callers must check parse_ok before
    treating an empty dropped_ids list as good news - silently equating a
    parse failure with a clean result would hide a real failure behind an
    apparently-successful check.
    """
    valid_ids = {tc.claim.id for tc in verified_facts}
    try:
        stripped = raw_content.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.startswith("json"):
                stripped = stripped[4:]
        data = json.loads(stripped)
    except (json.JSONDecodeError, AttributeError):
        return [], False
    if not isinstance(data, list):
        return [], False
    return [str(fid) for fid in data if str(fid) in valid_ids], True


async def check_fact_completeness(
    verified_facts: list[TaggedClaim],
    synthesis: str,
    model: str,
    query_fn: QueryFn,
) -> tuple[list[str], float, bool]:
    """Returns (dropped_ids, cost_usd, parse_ok). A no-op ([], 0.0, True)
    when there's nothing to check - parse_ok=True there because there was
    no response to fail to parse, not because anything was verified."""
    if not verified_facts:
        return [], 0.0, True
    prompt = build_completeness_prompt(verified_facts, synthesis)
    response, cost_usd = await query_fn(model, prompt)
    dropped, parse_ok = parse_completeness_response(response, verified_facts)
    return dropped, cost_usd, parse_ok
