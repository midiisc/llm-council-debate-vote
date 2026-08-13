"""Real OpenRouter-backed implementations of the dependency-injected
functions pipeline_runner.py expects: fetch_evidence (Stage 0.5, via
OpenRouter's web search plugin) and query_model (Stage 2.75 revision calls).

Kept separate from pipeline_runner.py itself so the orchestration logic
stays testable without live network calls - see
docs/specs/pipeline-runner-contract.md's design note.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import Any

import keyring

from scripts.grounding_pass import Claim, Evidence

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Verified live on OpenRouter, 2026-08-09 (see docs/upstream-deltas.md) -
# used as the evidence-fetching model because it's cheap and web-search
# capable via the :online suffix, not because it's a council member.
EVIDENCE_MODEL = "google/gemini-3.6-flash:online"

# Same base model as EVIDENCE_MODEL, minus the :online search plugin - the
# Stage 3.5 completeness check (scripts/completeness_check.py) reasons over
# text already provided in the prompt, no web search needed.
COMPLETENESS_CHECK_MODEL = "google/gemini-3.6-flash"

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0


def _get_openrouter_key() -> str:
    key = keyring.get_password("llm-council", "openrouter_api_key")
    if not key:
        raise RuntimeError(
            "No OpenRouter key in keychain - run `llm-council setup-key --stdin` first."
        )
    return key


def _is_retryable_error(exc: BaseException) -> bool:
    """Server/network errors are worth retrying; client errors (4xx, e.g.
    bad request or auth failure) will just fail identically again."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return False


class EvidenceMap(dict):
    """dict[str, list[Evidence]] subclass carrying cost/truncation metadata
    alongside the evidence itself (docs/specs/wallclock-cost-budget-contract.md,
    Contract 2). Preserves FetchEvidenceFn's existing dict-shaped contract
    exactly - isinstance(x, dict) is True, every dict operation works
    identically - so no existing FetchEvidenceFn fake across this repo's
    test suite needs to change. Callers must read via
    getattr(x, "cost_usd", 0.0) / getattr(x, "truncated", False), never
    assume these attributes exist (a plain dict, e.g. any test fake, won't
    have them)."""

    cost_usd: float = 0.0
    truncated: bool = False


def _post_chat_completion(
    model: str,
    prompt: str,
    max_tokens: int = 2000,
    max_retries: int = MAX_RETRIES,
    sleep_fn=time.sleep,
) -> dict[str, Any]:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
    ).encode()
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {_get_openrouter_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read())
        except Exception as e:
            if not _is_retryable_error(e) or attempt == max_retries:
                raise
            sleep_fn(BACKOFF_BASE_SECONDS * (2**attempt))
    raise AssertionError("unreachable")  # pragma: no cover


async def _post_chat_completion_async(
    model: str, prompt: str, max_tokens: int = 2000, max_retries: int = MAX_RETRIES,
) -> dict[str, Any]:
    """Runs the existing synchronous _post_chat_completion (urllib +
    blocking time.sleep backoff) in a worker thread via asyncio.to_thread,
    so the event loop stays responsive to asyncio.wait_for cancellation
    during the call (docs/specs/wallclock-cost-budget-contract.md,
    Contract 2 - architecture-stress-test-2026-08-13.md's "the always-on
    wall-clock ceiling cannot actually preempt Stage 0.5/Stage 2.75 network
    calls" finding). No migration off urllib, minimal-diff fix - the
    synchronous _post_chat_completion itself is unchanged."""
    return await asyncio.to_thread(
        _post_chat_completion, model, prompt, max_tokens=max_tokens, max_retries=max_retries
    )


async def real_query_model(model: str, prompt: str) -> tuple[str, float]:
    """query_model for revision_round.run_revision_round.

    Returns (response_text, cost_usd). cost_usd comes from OpenRouter's own
    usage.cost field; treated as 0.0 if the provider didn't report it rather
    than raising, since a missing cost figure shouldn't crash a revision
    round that otherwise succeeded.
    """
    data = await _post_chat_completion_async(model, prompt)
    text = data["choices"][0]["message"]["content"]
    cost_usd = data.get("usage", {}).get("cost") or 0.0
    return text, cost_usd


_CLAIM_SECTION_BEGIN = "--- BEGIN CLAIM ---"
_CLAIM_SECTION_END = "--- END CLAIM ---"


def build_evidence_prompt(claim: Claim) -> str:
    # Delimited (docs/specs/proposal-a-reference-grounding-contract.md,
    # Contract 2 completion) - the highest-risk of the three unguarded
    # sites the stress test found: claim.text goes directly to a live
    # web-search-enabled model (EVIDENCE_MODEL, :online), so a crafted
    # claim could otherwise forge text that reads as prompt instructions
    # to that model, opening an indirect-prompt-injection chain into live
    # external search.
    return (
        "Research this claim using web search and respond with ONLY a JSON "
        "object (no markdown fences, no other text), in exactly this shape:\n"
        '{"verdict": "supports"|"contradicts"|"unverifiable", '
        '"source": "<url of your best source, or empty string if unverifiable>", '
        '"date": "<retrieval date YYYY-MM-DD, or empty string if unverifiable>"}\n\n'
        f"{_CLAIM_SECTION_BEGIN}\n"
        f"{claim.text}\n"
        f"{_CLAIM_SECTION_END}"
    )


def parse_evidence_response(raw_content: str, retrieval_date: str) -> list[Evidence]:
    """Parse the model's JSON verdict into an Evidence list for grounding_pass.

    Returns an empty list (-> UNVERIFIABLE) on any parse failure, verdict of
    "unverifiable", or a missing/empty source - never raises, since a
    malformed model response must degrade to "couldn't verify," not crash
    the whole grounding pass.
    """
    try:
        stripped = raw_content.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.startswith("json"):
                stripped = stripped[4:]
        data = json.loads(stripped)
    except (json.JSONDecodeError, AttributeError):
        return []
    if not isinstance(data, dict):
        return []

    verdict = data.get("verdict")
    source = data.get("source") or ""
    if verdict not in ("supports", "contradicts") or not source:
        return []

    date = data.get("date") or retrieval_date
    return [Evidence(source=source, date=date, supports=(verdict == "supports"))]


async def real_fetch_evidence(
    claims: list[Claim], max_claims: int = 50, max_concurrency: int = 5,
) -> EvidenceMap:
    """fetch_evidence for pipeline_runner.run_pipeline - one OpenRouter
    web-search call per claim via the :online plugin.

    docs/specs/wallclock-cost-budget-contract.md, Contract 2 (closes
    architecture-stress-test-2026-08-13.md's Critical #5 + the "fully
    sequential, no cap" High finding): claims are fetched CONCURRENTLY
    (bounded by max_concurrency, not one-at-a-time), real cost is tracked
    and returned via EvidenceMap.cost_usd, and total claims are capped at
    max_claims with EvidenceMap.truncated=True set - never a silent drop.
    """
    from datetime import datetime, timezone

    retrieval_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    truncated = len(claims) > max_claims
    claims_to_fetch = claims[:max_claims]

    semaphore = asyncio.Semaphore(max_concurrency)

    async def _fetch_one(claim: Claim) -> tuple[str, list[Evidence], float]:
        async with semaphore:
            prompt = build_evidence_prompt(claim)
            data = await _post_chat_completion_async(EVIDENCE_MODEL, prompt, max_tokens=500)
        content = data["choices"][0]["message"]["content"]
        cost = data.get("usage", {}).get("cost") or 0.0
        return claim.id, parse_evidence_response(content, retrieval_date), cost

    results = await asyncio.gather(*(_fetch_one(claim) for claim in claims_to_fetch))

    evidence = EvidenceMap()
    total_cost = 0.0
    for claim_id, claim_evidence, cost in results:
        evidence[claim_id] = claim_evidence
        total_cost += cost

    evidence.cost_usd = total_cost
    evidence.truncated = truncated
    return evidence
