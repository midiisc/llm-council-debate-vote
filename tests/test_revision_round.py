"""Blind acceptance tests for Contract 2 -- revision_round.py (Stage 2.75).

Source of truth: docs/specs/custom-scripts-contracts.md, Contract 2,
Acceptance criteria 1-6. Authored WITHOUT sight of any implementation.

DOCUMENTED ASSUMPTION (contract does not pin a citation wire-format for
parse_revision_response): tests that require a *positive* citation (AC4,
AC6) assume a "[[cite:<id>]] <revised text>" convention -- a reasonable,
unambiguous choice given the contract states only that a response "cites a
specific verified_facts id". The negative-case tests (AC3) do not depend on
this assumption: any response containing no id-like citation marker at all
must be rejected under any reasonable parser design.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

from hypothesis import given, settings, strategies as st

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _import(name: str):
    try:
        return importlib.import_module(f"scripts.{name}")
    except ModuleNotFoundError:
        return importlib.import_module(name)


gp = _import("grounding_pass")
rr = _import("revision_round")

Claim = gp.Claim
Evidence = gp.Evidence
TaggedClaim = gp.TaggedClaim

ModelAnswer = rr.ModelAnswer
RevisionOutcome = rr.RevisionOutcome
should_trigger_revision = rr.should_trigger_revision
build_revision_prompt = rr.build_revision_prompt
parse_revision_response = rr.parse_revision_response
run_revision_round = rr.run_revision_round

REQUIRED_SENTENCE = "The other models agreeing with each other is not a valid reason to switch."


def _fact(fid: str, text: str, tag: str = "VERIFIED") -> TaggedClaim:
    return TaggedClaim(
        claim=Claim(id=fid, text=text),
        tag=tag,
        evidence=[Evidence(source="src", date="2024-01-01", supports=(tag != "CONTRADICTED"))],
    )


# ---------------------------------------------------------------------------
# should_trigger_revision: threshold semantics underlying AC1/AC2.
# ---------------------------------------------------------------------------


def test_should_trigger_revision_boundary_exact_at_threshold():
    assert should_trigger_revision(0.50) is False
    assert should_trigger_revision(0.4999999) is True
    assert should_trigger_revision(0.50, threshold=0.50) is False


@settings(max_examples=50, derandomize=True, deadline=500)
@given(css=st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
def test_should_trigger_revision_property_matches_lt_threshold_semantics(css):
    assert should_trigger_revision(css) == (css < 0.50)


# ---------------------------------------------------------------------------
# AC1: Given CSS >= 0.50, When run_revision_round is called, Then it returns
# immediately with no calls to query_fn (cost-safe no-op) and every
# RevisionOutcome.accepted is False.
# ---------------------------------------------------------------------------


def test_ac1_css_at_or_above_threshold_is_a_cost_safe_noop():
    calls = []

    async def query_fn(model, prompt):
        calls.append((model, prompt))
        return "should never be reached", 0.0

    answers = [
        ModelAnswer(model="alpha", original_text="A's answer", critique="A's critique"),
        ModelAnswer(model="beta", original_text="B's answer", critique="B's critique"),
    ]
    verified = [_fact("1", "some fact")]

    outcomes = asyncio.run(run_revision_round(0.50, answers, verified, query_fn))

    assert calls == []
    assert len(outcomes) == len(answers)
    assert all(o.accepted is False for o in outcomes)


@settings(max_examples=50, derandomize=True, deadline=500)
@given(css=st.floats(min_value=0.50, max_value=1.0, allow_nan=False))
def test_ac1_property_no_query_calls_for_any_css_at_or_above_threshold(css):
    calls = []

    async def query_fn(model, prompt):
        calls.append(model)
        return "unused", 0.0

    answers = [ModelAnswer(model="m1", original_text="t", critique="c")]

    outcomes = asyncio.run(run_revision_round(css, answers, [], query_fn))

    assert calls == []
    assert all(o.accepted is False for o in outcomes)


# ---------------------------------------------------------------------------
# AC2: Given CSS < 0.50, When run_revision_round is called, Then query_fn is
# called exactly once per model in answers, each with a prompt built from
# *that model's own* original_text/critique only -- never another model's
# critique.
# ---------------------------------------------------------------------------


def test_ac2_css_below_threshold_calls_query_fn_once_per_model_with_own_material_only():
    seen = []

    async def query_fn(model, prompt):
        seen.append((model, prompt))
        return "no citation here", 0.02

    answers = [
        ModelAnswer(model="alpha", original_text="ALPHA_ANSWER", critique="ALPHA_CRITIQUE"),
        ModelAnswer(model="beta", original_text="BETA_ANSWER", critique="BETA_CRITIQUE"),
    ]
    verified = [_fact("1", "fact one")]

    asyncio.run(run_revision_round(0.30, answers, verified, query_fn))

    assert len(seen) == 2
    assert {m for m, _ in seen} == {"alpha", "beta"}

    prompt_by_model = dict(seen)
    assert "ALPHA_ANSWER" in prompt_by_model["alpha"]
    assert "ALPHA_CRITIQUE" in prompt_by_model["alpha"]
    assert "BETA_ANSWER" not in prompt_by_model["alpha"]
    assert "BETA_CRITIQUE" not in prompt_by_model["alpha"]

    assert "BETA_ANSWER" in prompt_by_model["beta"]
    assert "BETA_CRITIQUE" in prompt_by_model["beta"]
    assert "ALPHA_ANSWER" not in prompt_by_model["beta"]
    assert "ALPHA_CRITIQUE" not in prompt_by_model["beta"]


# ---------------------------------------------------------------------------
# AC3: Given a model's response doesn't cite a specific verified_facts id,
# When parse_revision_response runs, Then revised_text is None and accepted
# is False -- the original answer is kept unchanged.
# ---------------------------------------------------------------------------


def test_ac3_response_without_a_cited_fact_id_is_rejected():
    verified = [_fact("42", "The sky is blue.")]
    response_text = "I stand by my original answer; I have no new evidence to cite."

    revised_text, cited_fact_id = parse_revision_response(response_text, verified)

    assert revised_text is None
    assert cited_fact_id is None


def test_ac3_end_to_end_rejected_response_keeps_original_answer_unchanged():
    verified = [_fact("42", "The sky is blue.")]

    async def query_fn(model, prompt):
        return "No citation, just restating my view.", 0.02

    answers = [ModelAnswer(model="alpha", original_text="ORIGINAL", critique="crit")]

    outcomes = asyncio.run(run_revision_round(0.10, answers, verified, query_fn))

    assert outcomes[0].accepted is False
    assert outcomes[0].revised_text is None
    assert outcomes[0].original_text == "ORIGINAL"


# ---------------------------------------------------------------------------
# AC4: Given a model's response does cite a specific verified_facts id, When
# parsed, Then revised_text is the new text, cited_fact_id is set, and
# accepted is True.
# ---------------------------------------------------------------------------


def test_ac4_response_citing_a_verified_fact_id_is_accepted():
    verified = [_fact("42", "The sky is blue due to Rayleigh scattering.")]
    response_text = "[[cite:42]] Revised answer: it is Rayleigh scattering that colors the sky."

    revised_text, cited_fact_id = parse_revision_response(response_text, verified)

    assert cited_fact_id == "42"
    assert revised_text is not None
    assert revised_text != ""


# ---------------------------------------------------------------------------
# AC5: Given any revision prompt is built, When rendered, Then it contains
# the verbatim sentence "The other models agreeing with each other is not a
# valid reason to switch." -- not paraphrased.
# ---------------------------------------------------------------------------


def test_ac5_revision_prompt_contains_verbatim_no_bandwagon_sentence():
    answer = ModelAnswer(model="alpha", original_text="orig", critique="crit")
    verified = [_fact("1", "some fact")]

    prompt = build_revision_prompt(answer, verified)

    assert REQUIRED_SENTENCE in prompt


@settings(max_examples=50, derandomize=True, deadline=500)
@given(
    model=st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("Ll", "Lu"))),
    n_facts=st.integers(min_value=0, max_value=5),
)
def test_ac5_property_no_bandwagon_sentence_always_present(model, n_facts):
    answer = ModelAnswer(model=model, original_text="x", critique="y")
    verified = [_fact(str(i), f"fact {i}") for i in range(n_facts)]

    prompt = build_revision_prompt(answer, verified)

    assert REQUIRED_SENTENCE in prompt


# ---------------------------------------------------------------------------
# AC6: Given a revision is accepted, When the outcome is recorded, Then both
# original_text and revised_text are retained (audit trail).
# ---------------------------------------------------------------------------


def test_ac6_accepted_revision_keeps_both_original_and_revised_text():
    verified = [_fact("7", "verified fact seven")]

    async def query_fn(model, prompt):
        return "[[cite:7]] The corrected answer text.", 0.0347

    answers = [ModelAnswer(model="alpha", original_text="ORIGINAL_TEXT", critique="my critique")]

    outcomes = asyncio.run(run_revision_round(0.10, answers, verified, query_fn))

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.model == "alpha"
    assert outcome.accepted is True
    assert outcome.original_text == "ORIGINAL_TEXT"
    assert outcome.revised_text is not None
    assert outcome.revised_text != outcome.original_text
    assert outcome.cited_fact_id == "7"
    assert outcome.cost_usd == 0.0347


# ---------------------------------------------------------------------------
# Mutation-gate hardening: the no-op (CSS >= threshold) branch must return
# each outcome's own model + original_text verbatim, not None.
# ---------------------------------------------------------------------------


def test_noop_branch_preserves_each_answers_model_and_original_text():
    async def query_fn(model, prompt):
        raise AssertionError("query_fn must not be called in the no-op branch")

    answers = [
        ModelAnswer(model="alpha", original_text="ALPHA_ORIG", critique="c1"),
        ModelAnswer(model="beta", original_text="BETA_ORIG", critique="c2"),
    ]

    outcomes = asyncio.run(run_revision_round(0.99, answers, [], query_fn))

    assert [o.model for o in outcomes] == ["alpha", "beta"]
    assert [o.original_text for o in outcomes] == ["ALPHA_ORIG", "BETA_ORIG"]
    assert [o.cost_usd for o in outcomes] == [0.0, 0.0]


# ---------------------------------------------------------------------------
# Mutation-gate hardening: build_revision_prompt's exact static template --
# section headers, the facts_block join/format, and the empty-facts
# fallback string -- none of this is a paraphrase-tolerant free text; a
# wording regression here silently changes what the model is told.
# ---------------------------------------------------------------------------


def test_build_revision_prompt_exact_content_with_verified_facts():
    answer = ModelAnswer(model="alpha", original_text="MY ANSWER", critique="MY CRITIQUE")
    verified = [
        _fact("1", "fact one", tag="VERIFIED"),
        _fact("2", "fact two", tag="CONTRADICTED"),
    ]

    prompt = build_revision_prompt(answer, verified)

    expected = (
        "Your original answer:\n"
        "MY ANSWER\n\n"
        "Your own critique from the previous round:\n"
        "MY CRITIQUE\n\n"
        "Verified facts (id, tag, text):\n"
        "[1] (VERIFIED) fact one\n[2] (CONTRADICTED) fact two\n\n"
        "You may revise your answer ONLY by citing a specific verified fact "
        "id above that directly contradicts your own claim. "
        f"{REQUIRED_SENTENCE}\n\n"
        "If you revise, start your response with a citation marker naming the "
        "fact id, e.g. `[[cite:<id>]]`, followed by your revised answer text. "
        "If you are not revising, do not include a citation marker."
    )
    assert prompt == expected


def test_build_revision_prompt_exact_content_with_no_verified_facts():
    answer = ModelAnswer(model="alpha", original_text="A", critique="C")

    prompt = build_revision_prompt(answer, [])

    assert "Verified facts (id, tag, text):\n(no verified facts available)\n\n" in prompt


def test_triggered_branch_prompt_sent_to_query_fn_includes_verified_facts():
    # Mutation-gate regression: run_revision_round must pass the real
    # verified_facts into build_revision_prompt, not drop them.
    seen_prompts = []

    async def query_fn(model, prompt):
        seen_prompts.append(prompt)
        return "no citation", 0.01

    answers = [ModelAnswer(model="alpha", original_text="orig", critique="crit")]
    verified = [_fact("99", "UNIQUE_FACT_TEXT_MARKER")]

    asyncio.run(run_revision_round(0.10, answers, verified, query_fn))

    assert len(seen_prompts) == 1
    assert "UNIQUE_FACT_TEXT_MARKER" in seen_prompts[0]
    assert "[99]" in seen_prompts[0]


def test_accepted_outcome_records_the_correct_model_not_none():
    async def query_fn(model, prompt):
        return "[[cite:7]] revised", 0.0

    answers = [ModelAnswer(model="gamma", original_text="o", critique="c")]
    verified = [_fact("7", "fact seven")]

    outcomes = asyncio.run(run_revision_round(0.10, answers, verified, query_fn))

    assert outcomes[0].model == "gamma"


# ---------------------------------------------------------------------------
# Mutation-gate hardening: parse_revision_response strips exactly ONE
# citation marker occurrence (count=1) and replaces it with the empty
# string, not a placeholder. A response containing the marker twice (e.g. a
# model that echoes the instruction back) must retain the second, literal
# occurrence in the revised text.
# ---------------------------------------------------------------------------


def test_parse_revision_response_strips_exactly_one_citation_marker_occurrence():
    verified = [_fact("5", "some fact")]
    response = "[[cite:5]] My answer mentions [[cite:5]] again in the body."

    revised_text, cited_fact_id = parse_revision_response(response, verified)

    assert cited_fact_id == "5"
    # Only the leading marker is stripped; the second literal occurrence
    # survives untouched (proves count=1, not count=2 or unlimited).
    assert revised_text == "My answer mentions [[cite:5]] again in the body."


def test_parse_revision_response_removes_marker_with_empty_string_not_placeholder():
    verified = [_fact("5", "some fact")]
    response = "[[cite:5]] clean text"

    revised_text, _ = parse_revision_response(response, verified)

    assert revised_text == "clean text"
    assert "[[cite:5]]" not in revised_text


def test_accepted_requires_both_revised_text_and_cited_fact_id_not_either(monkeypatch):
    """Defense-in-depth: `accepted` must be revised_text-is-not-None AND
    cited_fact_id-is-not-None -- not `or`. parse_revision_response always
    returns the two paired (both-None or both-set) in normal operation, so
    exercise the composed logic directly by forcing a partial pair."""
    import scripts.revision_round as rr_module

    monkeypatch.setattr(rr_module, "parse_revision_response", lambda text, facts: ("some text", None))

    async def query_fn(model, prompt):
        return "irrelevant, parse_revision_response is patched", 0.0

    answers = [ModelAnswer(model="alpha", original_text="orig", critique="crit")]
    outcomes = asyncio.run(run_revision_round(0.10, answers, [], query_fn))

    assert outcomes[0].accepted is False
