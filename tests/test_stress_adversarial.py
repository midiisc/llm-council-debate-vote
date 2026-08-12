"""Stress tests for the full reasoning chain (Stage 0.5 -> 1 -> 2 -> 2.5 ->
[2.75] -> 3 -> 4 -> scorecard): adversarial/malformed model responses at
every parse boundary, cost-accounting invariants under randomized inputs,
and a combined worst-case end-to-end scenario. Complements (does not
duplicate) each module's own targeted unit tests - this file's job is to
throw inputs no one specifically thought of at the seams between stages.
"""
from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings, strategies as st

from scripts.completeness_check import parse_completeness_response
from scripts.grounding_pass import Claim, Evidence, TaggedClaim
from scripts.live_adapters import parse_evidence_response
from scripts.pipeline_runner import PipelineConfig, run_pipeline
from scripts.revision_round import parse_revision_response

# A broad text strategy including control chars, surrogates-adjacent ranges,
# empty string, and very long strings - the kind of thing a real LLM could
# plausibly emit (or a hostile response could deliberately contain).
_ADVERSARIAL_TEXT = st.text(min_size=0, max_size=500)


def _fact(fid: str, text: str = "fact text", tag: str = "VERIFIED") -> TaggedClaim:
    return TaggedClaim(
        claim=Claim(id=fid, text=text),
        tag=tag,
        evidence=[Evidence(source="src", date="2024-01-01", supports=True)],
    )


# ---------------------------------------------------------------------------
# Fuzz: every parse_* boundary function must never raise, for any input.
# These are the three points where a real (possibly malformed, possibly
# adversarial) LLM response text crosses into this codebase's control flow.
# ---------------------------------------------------------------------------


@settings(max_examples=200, derandomize=True, deadline=1000)
@given(raw=_ADVERSARIAL_TEXT)
def test_parse_completeness_response_never_raises(raw):
    facts = [_fact("1"), _fact("2")]
    result, parse_ok = parse_completeness_response(raw, facts)
    assert isinstance(result, list)
    assert all(isinstance(x, str) for x in result)
    assert isinstance(parse_ok, bool)


@settings(max_examples=200, derandomize=True, deadline=1000)
@given(raw=_ADVERSARIAL_TEXT)
def test_parse_revision_response_never_raises(raw):
    facts = [_fact("1"), _fact("2")]
    revised_text, cited_fact_id = parse_revision_response(raw, facts)
    assert revised_text is None or isinstance(revised_text, str)
    assert cited_fact_id is None or isinstance(cited_fact_id, str)


@settings(max_examples=200, derandomize=True, deadline=1000)
@given(raw=_ADVERSARIAL_TEXT)
def test_parse_evidence_response_never_raises(raw):
    result = parse_evidence_response(raw, retrieval_date="2026-01-01")
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Property: dropped_facts / cited ids are ALWAYS a subset of the ids that
# were actually offered - never a hallucinated id smuggled through from a
# malicious or malformed model response.
# ---------------------------------------------------------------------------


@settings(max_examples=100, derandomize=True, deadline=1000)
@given(
    real_ids=st.lists(st.text(alphabet="0123456789", min_size=1, max_size=3), min_size=0, max_size=5, unique=True),
    claimed_ids=st.lists(st.text(min_size=0, max_size=5), min_size=0, max_size=8),
)
def test_parse_completeness_response_never_returns_id_not_in_verified_facts(real_ids, claimed_ids):
    facts = [_fact(fid) for fid in real_ids]
    raw = str(claimed_ids).replace("'", '"')  # best-effort JSON-array-ish string

    dropped, _parse_ok = parse_completeness_response(raw, facts)

    assert all(fid in real_ids for fid in dropped)


# ---------------------------------------------------------------------------
# Cost-accounting invariant, extended to cover ALL THREE cost sources at
# once (stage1-3, revision, completeness) under randomized inputs - the
# existing property test in test_pipeline_runner.py covers stage1-3 +
# revision only (grounding was never enabled in that test).
# ---------------------------------------------------------------------------


def _stress_council_result(css, cost_x, cost_y):
    stage1_results = [
        {"model": "model-x", "response": "Answer from X"},
        {"model": "model-y", "response": "Answer from Y"},
    ]
    stage2_results = [
        {
            "model": "model-y",
            "ranking": "raw",
            "parsed_ranking": {
                "evaluations": {"Response A": {"accuracy": 5, "relevance": 5, "completeness": 5, "conciseness": 5, "clarity": 5}},
                "rubric_scoring": True,
            },
        },
    ]
    label_to_model = {
        "Response A": {"model": "model-x", "display_index": 0},
        "Response B": {"model": "model-y", "display_index": 1},
    }
    aggregate_rankings = [
        {"model": "model-x", "borda_score": 1.0, "rank": 1},
        {"model": "model-y", "borda_score": 0.0, "rank": 2},
    ]
    stage3_result = {"model": "model-x", "response": "Final synthesis text"}
    metadata = {
        "quality_metrics": {"core": {"consensus_strength": css}},
        "aggregate_rankings": aggregate_rankings,
        "label_to_model": label_to_model,
        "usage": {
            "by_model": {"model-x": {"cost_usd": cost_x}, "model-y": {"cost_usd": cost_y}},
            "total": {"cost_usd": cost_x + cost_y},
        },
    }
    return stage1_results, stage2_results, stage3_result, metadata


@settings(max_examples=25, derandomize=True, deadline=2000)
@given(
    cost_x=st.floats(min_value=0.0, max_value=2.0, allow_nan=False),
    cost_y=st.floats(min_value=0.0, max_value=2.0, allow_nan=False),
    per_call_cost=st.floats(min_value=0.0, max_value=2.0, allow_nan=False),
)
def test_total_cost_always_equals_all_three_cost_sources_combined(tmp_path_factory, cost_x, cost_y, per_call_cost):
    tmp_path = tmp_path_factory.mktemp("stress")

    async def fetch_evidence(claims):
        return {"1": [Evidence(source="s", date="d", supports=True)]}

    result_fixture = _stress_council_result(css=0.1, cost_x=cost_x, cost_y=cost_y)  # low CSS -> revision fires

    async def council_fn(query):
        return result_fixture

    calls = {"n": 0}

    async def query_model(model, prompt):
        calls["n"] += 1
        return "no citation", per_call_cost

    config = PipelineConfig(
        topic_label="stress",
        query="q",
        raw_claims_text="1. Some claim.",
        output_root=tmp_path,
    )
    result = asyncio.run(run_pipeline(config, fetch_evidence, council_fn, query_model))

    expected = (cost_x + cost_y) + per_call_cost * calls["n"]
    assert result.total_cost_usd == pytest.approx(expected)
    assert result.total_cost_usd >= 0.0


# ---------------------------------------------------------------------------
# Combined worst-case end-to-end scenario: mixed VERIFIED/CONTRADICTED/
# UNVERIFIABLE claims, a cost ceiling landing exactly between stages, and
# malformed responses at both the revision and completeness call sites -
# the pipeline must complete without raising and every invariant on the
# result must still hold.
# ---------------------------------------------------------------------------


def test_end_to_end_adversarial_scenario_completes_without_crashing(tmp_path):
    evidence = {
        "1": [Evidence(source="http://a.com", date="2026-01-01", supports=True)],   # VERIFIED
        "2": [Evidence(source="http://b.com", date="2026-01-01", supports=False)],  # CONTRADICTED
        # claim 3 has no evidence entry -> UNVERIFIABLE
    }

    async def fetch_evidence(claims):
        return evidence

    result_fixture = _stress_council_result(css=0.2, cost_x=0.05, cost_y=0.05)

    async def council_fn(query):
        return result_fixture

    async def malformed_query_model(model, prompt):
        # Every call gets adversarial garbage: not JSON, contains a stray
        # citation-looking token, unicode, and a null-ish substring.
        return "not json \x00 [[cite:999]] ☃ <script>evil</script>", 0.01

    config = PipelineConfig(
        topic_label="adversarial",
        query="q",
        raw_claims_text="1. Claim one.\n2. Claim two.\n3. Claim three.",
        output_root=tmp_path,
        max_cost_usd=0.20,  # lands between stage1-3 (0.10) and full spend
    )

    result = asyncio.run(
        run_pipeline(config, fetch_evidence, council_fn, malformed_query_model)
    )

    # Must complete and return a structurally valid result - no crash, no
    # hang, no silently-corrupted state.
    assert result.total_cost_usd >= 0.10  # at least stage1-3 cost was spent
    assert isinstance(result.dropped_facts, list)
    assert all(fid in ("1", "2") for fid in result.dropped_facts)  # only VERIFIED/CONTRADICTED ids possible, never "3"
    assert result.css == 0.2
