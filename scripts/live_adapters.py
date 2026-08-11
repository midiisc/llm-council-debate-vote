"""Real OpenRouter-backed implementations of the dependency-injected
functions pipeline_runner.py expects: fetch_evidence (Stage 0.5, via
OpenRouter's web search plugin) and query_model (Stage 2.75 revision calls).

Kept separate from pipeline_runner.py itself so the orchestration logic
stays testable without live network calls - see
docs/specs/pipeline-runner-contract.md's design note.
"""
from __future__ import annotations

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


async def real_query_model(model: str, prompt: str) -> tuple[str, float]:
    """query_model for revision_round.run_revision_round.

    Returns (response_text, cost_usd). cost_usd comes from OpenRouter's own
    usage.cost field; treated as 0.0 if the provider didn't report it rather
    than raising, since a missing cost figure shouldn't crash a revision
    round that otherwise succeeded.
    """
    data = _post_chat_completion(model, prompt)
    text = data["choices"][0]["message"]["content"]
    cost_usd = data.get("usage", {}).get("cost") or 0.0
    return text, cost_usd


def build_evidence_prompt(claim: Claim) -> str:
    return (
        "Research this claim using web search and respond with ONLY a JSON "
        "object (no markdown fences, no other text), in exactly this shape:\n"
        '{"verdict": "supports"|"contradicts"|"unverifiable", '
        '"source": "<url of your best source, or empty string if unverifiable>", '
        '"date": "<retrieval date YYYY-MM-DD, or empty string if unverifiable>"}\n\n'
        f"Claim: {claim.text}"
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

    verdict = data.get("verdict")
    source = data.get("source") or ""
    if verdict not in ("supports", "contradicts") or not source:
        return []

    date = data.get("date") or retrieval_date
    return [Evidence(source=source, date=date, supports=(verdict == "supports"))]


async def real_fetch_evidence(claims: list[Claim]) -> dict[str, list[Evidence]]:
    """fetch_evidence for pipeline_runner.run_pipeline - one OpenRouter
    web-search call per claim via the :online plugin."""
    from datetime import datetime, timezone

    retrieval_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    evidence: dict[str, list[Evidence]] = {}
    for claim in claims:
        prompt = build_evidence_prompt(claim)
        data = _post_chat_completion(EVIDENCE_MODEL, prompt, max_tokens=500)
        content = data["choices"][0]["message"]["content"]
        evidence[claim.id] = parse_evidence_response(content, retrieval_date)
    return evidence
