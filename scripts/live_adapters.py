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

# docs/specs/quantitative-evidence-weighting-contract.md, Contract 1. Sits
# OUTSIDE the claim delimiters (appended after _CLAIM_SECTION_END) so it
# reads as a trusted instruction bracketing the untrusted claim text on both
# sides, same reasoning as _STAGE1_REFERENCE_INSTRUCTION_BLOCK
# (council_adapter.py). Domain-neutral by construction - names no
# subject-matter category (no "revenue"/"market share"/etc, per
# pipeline-architecture-spec.md section 6's domain-neutrality rule).
# Evidence-methodology terms (systematic review, meta-analysis, survey) are
# evidence-TYPE labels, not subject-matter content - same status as
# "input document"/"verified facts" in the Stage 1 reference instruction -
# so naming them stays domain-neutral: they apply identically whether the
# decision under debate is a hire, a fundraise, or a hardware purchase.
#
# Clauses 5-8 (docs/specs/stage-0-5-epistemic-clauses-contract.md): added
# after a 12-agent sweep + adversarial panel (docs/stage-0-5-epistemic-
# clauses-decision-2026-08-13.md) surfaced 23 candidate epistemic checks,
# narrowed to 6 adversarially-judged, narrowed again at synthesis to these
# 4 - each closes a gap none of clauses 1-4 catch (diagnosticity, cost-to-
# fake, proxy validity, production-method diversity). 2 individually-passing
# candidates (base-rate anchoring, absence-of-expected-signal) were
# deliberately NOT shipped - both reopen clause 1's fabrication-risk profile
# for a signal that fires rarely under the one-search-call-per-claim
# architecture; see the decision doc for the full reasoning.
_EVIDENCE_WEIGHTING_INSTRUCTION_BLOCK = (
    "\n\n"
    "Prefer a specific, dated, independently verifiable finding over a "
    "vague or unsourced one when a real one actually exists. Do not invent "
    "a plausible-sounding report, survey, or source name to satisfy this "
    "preference - if no real, checkable source turns up, the verdict must "
    'be "unverifiable". An unverified claim that merely sounds specific or '
    "numeric is LOWER trust than a hedged, transparently-sourced claim, not "
    "higher - never mark something as supporting or contradicting on the "
    "strength of specificity alone. When the finding itself aggregates or "
    "surveys many independent sources - a systematic review, meta-analysis, "
    "or industry-wide survey, rather than one study, one opinion, or one "
    "anecdote - note that explicitly: aggregated evidence is stronger than "
    "an isolated data point, but only when the aggregation is itself real "
    "and cited, never estimated or guessed at. A dated, verifiable action - "
    "a signed agreement, a completed transaction, a public commitment - is "
    "often stronger evidence of an entity's actual direction than a stated "
    "prediction or opinion about that direction; when both are found and "
    "both are real, treat the verified action as at least as weighty as the "
    "stated forecast. When the same underlying direction is independently "
    "corroborated by real, cited sources from more than one sphere of "
    "activity - for example, research literature, commercial or industrial "
    "activity, and observable market behavior - note that convergence "
    "explicitly: independent corroboration across spheres is stronger "
    "signal than any single source. But this only holds when each "
    "corroborating source is itself real, dated, and cited - citing more "
    "sources than actually exist, or treating repeated mentions of the "
    "same underlying source as independent corroboration, is exactly the "
    "fabrication risk this instruction exists to prevent. "
    "Judge a found source not just by how strongly it seems to support the "
    "claim on its own, but by whether it would be unlikely to exist if the "
    "claim were false - specifically, unlikely under the claim's own "
    "negation or the specific rival option the claim names. A finding "
    "equally compatible with the opposite conclusion adds little value even "
    "when well-sourced and specific. This also catches a related failure: a "
    "real, dated, cited source that turns out to address a different, "
    "similar-sounding claim contributes nothing here. Only compare against "
    "the alternative the claim itself implies - its plain negation, or a "
    "rival it explicitly names - never invent a new alternative to test "
    "against, and never assert that a source distinguishes the claim from "
    "its alternative unless the source's own stated content actually does "
    "so; if no source addresses the actual claim, as opposed to a "
    "look-alike neighbor, default to unverifiable. When a source explicitly "
    "discloses that making a statement or taking an action was costly, "
    "risky, or worked against the stating party's own apparent interest - "
    "an explicit penalty, a disclosed conflict of interest, a stated "
    "resource commitment, or a concession that undercuts the party's own "
    "position - weight that finding more heavily than an equivalent "
    "statement or action with no such disclosed cost; a low-cost, "
    "self-serving announcement is easy to make regardless of whether it's "
    "true, and this can outweigh the default action-over-opinion ranking "
    "above. Apply this only when the cost, risk, or against-interest nature "
    "is explicitly stated in the source itself - never estimate a cost, "
    "infer risk, or guess at a party's true incentive from general "
    "knowledge of how such situations usually work; if the source does not "
    "disclose it, this factor does not apply, and the finding is scored on "
    "the other criteria alone. When a finding offers a "
    "continuously-observable stand-in measurement - a count, index, "
    "volume, or rate - as evidence for a separate, not-yet-confirmed "
    "outcome, a precise and well-sourced number for that stand-in does not "
    "by itself establish that it predicts the outcome. Trust the link "
    "between the two only if the source itself states, or cites, an "
    "established relationship between that specific measurement and that "
    "specific outcome - never invent a predictive relationship, "
    "correlation, or lead-time the source does not state. Absent that "
    "grounding, treat the finding as unverified for the outcome it is "
    "cited to support, even though the underlying number is itself real "
    "and dated. When more than one real, cited source agrees on a "
    "direction, treat agreement between sources produced by genuinely "
    "different methods or processes - for example, a recorded transaction, "
    "an independent survey, a firsthand account, a direct measurement - as "
    "stronger evidence than agreement between sources produced the same "
    "way, or that turn out to be restatements of one original report "
    "carried by multiple outlets. Apply this only when each source's "
    "production method is actually stated or evident from the source "
    "itself - never infer, assume, or guess a method that isn't shown, and "
    "never treat two copies or reprints of the same underlying report as "
    "independent methods just because they appear in different places. If "
    "the sources' methods can't be verified as both real and different, "
    "give no diversity bonus and fall back to judging each source on its "
    "own merits."
)


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
        f"{_EVIDENCE_WEIGHTING_INSTRUCTION_BLOCK}"
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


async def _source_is_reachable(url: str, timeout: float = 5.0) -> bool:
    """docs/specs/quantitative-evidence-weighting-contract.md, Contract 3.

    Guards VERIFIED/CONTRADICTED status behind an actual resolvability
    check on the evidence model's self-reported source, so a fabricated
    "McKinsey 2026 State of the Market Report" can't pass as identically
    trustworthy to a real one. Conservative by design (false-positive
    "verified" is worse than a missed one, per the grounding decision this
    closes): any exception, any non-2xx/3xx status, or a non-http(s) string
    all return False, never raise. HEAD request via asyncio.to_thread -
    same non-blocking pattern as _post_chat_completion_async, so this
    doesn't reopen the wall-clock-preemption gap that pattern already
    closed.
    """
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return False

    def _check() -> bool:
        req = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return 200 <= resp.status < 400
        except Exception:
            return False

    try:
        return await asyncio.to_thread(_check)
    except Exception:
        return False


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

    docs/specs/quantitative-evidence-weighting-contract.md, Contract 3: a
    claim whose self-reported source doesn't actually resolve has its
    evidence dropped to [] here, before grounding_pass.tag_claim ever sees
    it - an empty evidence list is what tag_claim already treats as
    UNVERIFIABLE, so this needs no change to tag_claim itself.
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
            claim_evidence = parse_evidence_response(content, retrieval_date)
            if claim_evidence and not await _source_is_reachable(claim_evidence[0].source):
                claim_evidence = []
        return claim.id, claim_evidence, cost

    results = await asyncio.gather(*(_fetch_one(claim) for claim in claims_to_fetch))

    evidence = EvidenceMap()
    total_cost = 0.0
    for claim_id, claim_evidence, cost in results:
        evidence[claim_id] = claim_evidence
        total_cost += cost

    evidence.cost_usd = total_cost
    evidence.truncated = truncated
    return evidence
