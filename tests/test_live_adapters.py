"""Tests for the deterministic (non-network) parts of live_adapters.py.

The actual HTTP calls (_post_chat_completion, real_query_model,
real_fetch_evidence) are exercised by a real, cheap smoke test outside the
mutation-tested suite - see docs/pipeline-architecture-spec.md's dry-run log.
"""
from __future__ import annotations

from scripts.grounding_pass import Claim
from scripts.live_adapters import build_evidence_prompt, parse_evidence_response


def test_build_evidence_prompt_exact_content():
    claim = Claim(id="3", text="The sky is blue.")
    prompt = build_evidence_prompt(claim)
    assert prompt == (
        "Research this claim using web search and respond with ONLY a JSON "
        "object (no markdown fences, no other text), in exactly this shape:\n"
        '{"verdict": "supports"|"contradicts"|"unverifiable", '
        '"source": "<url of your best source, or empty string if unverifiable>", '
        '"date": "<retrieval date YYYY-MM-DD, or empty string if unverifiable>"}\n\n'
        "Claim: The sky is blue."
    )


def test_parse_evidence_response_supports():
    raw = '{"verdict": "supports", "source": "http://example.com", "date": "2026-08-09"}'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert len(result) == 1
    assert result[0].supports is True
    assert result[0].source == "http://example.com"
    assert result[0].date == "2026-08-09"


def test_parse_evidence_response_contradicts():
    raw = '{"verdict": "contradicts", "source": "http://example.com", "date": "2026-08-09"}'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert result[0].supports is False


def test_parse_evidence_response_unverifiable_yields_empty_list():
    raw = '{"verdict": "unverifiable", "source": "", "date": ""}'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert result == []


def test_parse_evidence_response_missing_source_yields_empty_list():
    raw = '{"verdict": "supports", "source": "", "date": "2026-08-09"}'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert result == []


def test_parse_evidence_response_malformed_json_yields_empty_list_not_crash():
    result = parse_evidence_response("not json at all", retrieval_date="2026-08-01")
    assert result == []


def test_parse_evidence_response_strips_markdown_fences():
    raw = '```json\n{"verdict": "supports", "source": "http://x.com", "date": "2026-08-09"}\n```'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert len(result) == 1
    assert result[0].source == "http://x.com"


def test_parse_evidence_response_strips_json_tag_with_no_newline_after_it():
    # No newline between "json" and the opening brace - proves the slice
    # strips exactly the 4-char "json" tag, not 5 (which would eat the "{")
    raw = '```json{"verdict": "supports", "source": "http://x.com", "date": "2026-08-09"}```'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert len(result) == 1
    assert result[0].source == "http://x.com"


def test_parse_evidence_response_missing_date_falls_back_to_retrieval_date():
    raw = '{"verdict": "supports", "source": "http://x.com"}'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert result[0].date == "2026-08-01"


def test_parse_evidence_response_unknown_verdict_yields_empty_list():
    raw = '{"verdict": "maybe", "source": "http://x.com", "date": "2026-08-09"}'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert result == []
