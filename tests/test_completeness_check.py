"""Tests for completeness_check.py, derived from
docs/specs/custom-scripts-contracts.md, Contract 4, AC1-5.
"""
from __future__ import annotations

import asyncio

from scripts.completeness_check import (
    build_completeness_prompt,
    check_fact_completeness,
    parse_completeness_response,
)
from scripts.grounding_pass import Claim, Evidence, TaggedClaim


def _fact(fid: str, text: str, tag: str = "VERIFIED") -> TaggedClaim:
    return TaggedClaim(
        claim=Claim(id=fid, text=text),
        tag=tag,
        evidence=[Evidence(source="src", date="2024-01-01", supports=True)],
    )


class FakeQueryFn:
    def __init__(self, response="[]", cost=0.0):
        self.calls = []
        self.response = response
        self.cost = cost

    async def __call__(self, model: str, prompt: str) -> tuple[str, float]:
        self.calls.append((model, prompt))
        return self.response, self.cost


# --- AC1: empty verified_facts -> no-op, no call ---


def test_ac1_empty_facts_returns_no_op_without_calling_query_fn():
    query_fn = FakeQueryFn()

    dropped, cost, parse_ok = asyncio.run(
        check_fact_completeness([], "synthesis text", "m", query_fn)
    )

    assert dropped == []
    assert cost == 0.0
    assert parse_ok is True
    assert query_fn.calls == []


# --- AC2: non-empty facts -> exactly one batched call ---


def test_ac2_nonempty_facts_calls_query_fn_exactly_once():
    facts = [_fact("1", "fact one"), _fact("2", "fact two")]
    query_fn = FakeQueryFn(response='["2"]', cost=0.02)

    dropped, cost, parse_ok = asyncio.run(
        check_fact_completeness(facts, "synthesis text", "my-model", query_fn)
    )

    assert len(query_fn.calls) == 1
    assert query_fn.calls[0][0] == "my-model"
    assert dropped == ["2"]
    assert cost == 0.02
    assert parse_ok is True


def test_check_fact_completeness_sends_the_real_built_prompt():
    facts = [_fact("1", "fact one")]
    query_fn = FakeQueryFn(response="[]", cost=0.0)

    asyncio.run(check_fact_completeness(facts, "the synthesis", "m", query_fn))

    expected_prompt = build_completeness_prompt(facts, "the synthesis")
    assert query_fn.calls[0][1] == expected_prompt


# --- AC3: parse_completeness_response filters to real ids ---


def test_ac3_parse_returns_only_ids_present_in_verified_facts():
    facts = [_fact("1", "fact one"), _fact("2", "fact two")]

    dropped, parse_ok = parse_completeness_response('["1", "2"]', facts)

    assert dropped == ["1", "2"]
    assert parse_ok is True


def test_ac3_parse_filters_out_hallucinated_id_not_in_verified_facts():
    facts = [_fact("1", "fact one")]

    dropped, parse_ok = parse_completeness_response('["1", "99"]', facts)

    assert dropped == ["1"]
    assert parse_ok is True


def test_ac3_parse_empty_array_means_nothing_dropped():
    facts = [_fact("1", "fact one")]

    dropped, parse_ok = parse_completeness_response("[]", facts)

    assert dropped == []
    assert parse_ok is True


# --- AC4/AC10/AC11: malformed/unparseable response degrades to ([], False)
# without raising - and a well-formed response (even an empty array) must
# always report parse_ok=True, so callers can distinguish "verified clean"
# from "couldn't tell." ---


def test_ac4_malformed_json_returns_empty_list_not_crash():
    facts = [_fact("1", "fact one")]

    dropped, parse_ok = parse_completeness_response("not json at all", facts)

    assert dropped == []
    assert parse_ok is False


def test_ac4_valid_json_but_not_a_list_returns_empty_list():
    facts = [_fact("1", "fact one")]

    dropped, parse_ok = parse_completeness_response('{"1": true}', facts)

    assert dropped == []
    assert parse_ok is False


def test_ac4_markdown_fenced_json_array_still_parses():
    facts = [_fact("1", "fact one")]

    dropped, parse_ok = parse_completeness_response('```json\n["1"]\n```', facts)

    assert dropped == ["1"]
    assert parse_ok is True


def test_ac4_strips_only_backticks_not_other_chars():
    # strip("`") must remove only backtick characters from the ends, not a
    # broader charset - a mutant widening it to strip("XX`XX") would also
    # strip leading 'X' characters, turning this malformed input valid.
    facts = [_fact("1", "fact one")]

    dropped, parse_ok = parse_completeness_response('```XX["1"]', facts)

    assert dropped == []
    assert parse_ok is False


def test_ac4_json_tag_slice_is_exactly_four_chars():
    # "json" is 4 chars; a mutant slicing [5:] instead of [4:] eats one
    # extra character and corrupts otherwise-valid JSON.
    facts = [_fact("1", "fact one")]

    dropped, parse_ok = parse_completeness_response('```json["1"]```', facts)

    assert dropped == ["1"]
    assert parse_ok is True


def test_ac4_check_fact_completeness_never_raises_on_malformed_response():
    facts = [_fact("1", "fact one")]
    query_fn = FakeQueryFn(response="garbage, not json", cost=0.01)

    dropped, cost, parse_ok = asyncio.run(
        check_fact_completeness(facts, "synthesis", "m", query_fn)
    )

    assert dropped == []
    assert cost == 0.01
    assert parse_ok is False


def test_ac10_well_formed_empty_array_reports_parse_ok_true():
    facts = [_fact("1", "fact one")]

    dropped, parse_ok = parse_completeness_response("[]", facts)

    assert dropped == []
    assert parse_ok is True


def test_ac11_malformed_response_never_silently_looks_like_success():
    # The exact failure mode this contract change closes: dropped==[] must
    # NEVER coincide with parse_ok==True for a genuinely malformed input -
    # that combination would look identical to "verified, nothing missing."
    facts = [_fact("1", "fact one")]

    dropped, parse_ok = parse_completeness_response("<<<not json>>>", facts)

    assert not (dropped == [] and parse_ok is True)


# --- AC5: build_completeness_prompt contains every fact + the synthesis verbatim ---


def test_ac5_prompt_contains_every_fact_id_tag_and_text():
    facts = [
        _fact("1", "UNIQUE_FACT_ONE", tag="VERIFIED"),
        _fact("2", "UNIQUE_FACT_TWO", tag="CONTRADICTED"),
    ]

    prompt = build_completeness_prompt(facts, "the final answer")

    assert "[1]" in prompt
    assert "(VERIFIED)" in prompt
    assert "UNIQUE_FACT_ONE" in prompt
    assert "[2]" in prompt
    assert "(CONTRADICTED)" in prompt
    assert "UNIQUE_FACT_TWO" in prompt


def test_ac5_prompt_contains_synthesis_verbatim():
    facts = [_fact("1", "fact one")]

    prompt = build_completeness_prompt(facts, "UNIQUE_SYNTHESIS_MARKER_TEXT")

    assert "UNIQUE_SYNTHESIS_MARKER_TEXT" in prompt


# Mutation-gate hardening: exact static template, mirroring the same
# treatment given to revision_round.build_revision_prompt.


def test_build_completeness_prompt_exact_content():
    facts = [
        _fact("1", "fact one", tag="VERIFIED"),
        _fact("2", "fact two", tag="CONTRADICTED"),
    ]

    prompt = build_completeness_prompt(facts, "FINAL ANSWER TEXT")

    expected = (
        "Below is a list of research findings that were established before "
        "a final answer was synthesized, and the final synthesized answer "
        "itself. Identify which finding ids, if any, are NOT reflected or "
        "addressed anywhere in the final answer - not necessarily verbatim, "
        "but the substance of the finding must be genuinely absent.\n\n"
        "Findings (id, tag, text):\n"
        "[1] (VERIFIED) fact one\n[2] (CONTRADICTED) fact two\n\n"
        "Final answer:\n"
        "FINAL ANSWER TEXT\n\n"
        "Respond with ONLY a JSON array of the ids that are NOT addressed, "
        'e.g. ["3","7"], or [] if every finding is addressed. No other text.'
    )
    assert prompt == expected
