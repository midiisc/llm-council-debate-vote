"""Blind acceptance tests for Contract 2 (revision_round.py, Stage 2.75),
Amendment (2026-08-12): "thread the source document into Stage 2.75".

Source of truth: docs/specs/custom-scripts-contracts.md, Contract 2, the
"Amendment (2026-08-12)" section, Acceptance criteria 10-14, plus the new
`estimate_tokens` function and the `source_document`/`max_document_tokens`
signature additions to `build_revision_prompt` / `run_revision_round`.
Authored WITHOUT sight of any implementation code, per blind-TDV
(anti-test-hacking.md / this repo's Pillar 3).

TRANSPARENCY NOTE: this isolated test-author task's "CONTRACT:" section
arrived empty. Rather than fabricate a contract or block, this file sources
its acceptance criteria directly from the repo's own spec ledger
(docs/specs/custom-scripts-contracts.md), which is exactly what every
sibling blind test file in tests/ already does (see conftest.py's own
docstring and tests/test_revision_round.py's identical sourcing pattern for
Contract 2's earlier AC1-9). Confirmed via `grep` before writing this file
that `estimate_tokens`/`source_document`/`max_document_tokens` do not yet
appear anywhere in scripts/revision_round.py or tests/test_revision_round.py
-- this is genuinely unimplemented, untested surface, so RED is expected and
correct until the implementation lands.

Hermetic: no real network/DB/clock; `query_fn` is always a local async stub.
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
build_revision_prompt = rr.build_revision_prompt
parse_revision_response = rr.parse_revision_response
run_revision_round = rr.run_revision_round
estimate_tokens = rr.estimate_tokens  # NEW per the 2026-08-12 amendment


def _fact(fid: str, text: str, tag: str = "VERIFIED") -> TaggedClaim:
    return TaggedClaim(
        claim=Claim(id=fid, text=text),
        tag=tag,
        evidence=[Evidence(source="src", date="2024-01-01", supports=(tag != "CONTRADICTED"))],
    )


def _separation_gap(prompt: str, a: str, b: str) -> str:
    """Return the text strictly between two non-overlapping substrings of
    `prompt`, regardless of which one occurs first. Used to prove two
    rendered blocks are not directly concatenated/interleaved (AC10/AC12)."""
    ia, ib = prompt.index(a), prompt.index(b)
    if ia < ib:
        return prompt[ia + len(a):ib]
    return prompt[ib + len(b):ia]


# ---------------------------------------------------------------------------
# estimate_tokens: documented formula is `len(text) // 4` (spec, verbatim).
# Property tests first-class: exact-formula law + monotonicity law.
# ---------------------------------------------------------------------------


@settings(max_examples=50, derandomize=True, deadline=500)
@given(text=st.text(max_size=2000))
def test_estimate_tokens_matches_documented_formula(text):
    """AC (spec, 'Approximation' paragraph): estimate_tokens(text) == len(text) // 4."""
    assert estimate_tokens(text) == len(text) // 4


@settings(max_examples=50, derandomize=True, deadline=500)
@given(text=st.text(max_size=1000), extra=st.text(min_size=0, max_size=200))
def test_estimate_tokens_monotonic_non_decreasing_with_more_text(text, extra):
    """Law: appending text never decreases the estimate (monotonicity)."""
    assert estimate_tokens(text) <= estimate_tokens(text + extra)


def test_estimate_tokens_empty_string_is_zero():
    assert estimate_tokens("") == 0


# ---------------------------------------------------------------------------
# AC10: Given source_document is non-empty and estimate_tokens(source_document)
# <= max_document_tokens, When build_revision_prompt renders, Then the full
# source_document text appears verbatim in its own labeled section,
# textually separated from facts_block by a distinct header (not
# concatenated or interleaved).
# ---------------------------------------------------------------------------


def test_ac10_document_included_verbatim_and_separated_from_facts_under_threshold():
    doc = "DOCXSTART " + ("lorem ipsum dolor sit amet " * 15) + "DOCXEND"
    answer = ModelAnswer(model="alpha", original_text="MY ANSWER", critique="MY CRITIQUE")
    verified = [_fact("1", "fact one")]

    assert estimate_tokens(doc) <= 100_000  # sanity: well under the threshold used below

    prompt = build_revision_prompt(answer, verified, source_document=doc, max_document_tokens=100_000)

    assert doc in prompt  # verbatim inclusion

    fact_line = "[1] (VERIFIED, source: src) fact one"
    assert fact_line in prompt  # facts block still rendered too

    # Not concatenated/interleaved: nothing glues the two blocks together
    # with zero separator text either direction.
    assert doc + fact_line not in prompt
    assert fact_line + doc not in prompt

    gap = _separation_gap(prompt, doc, fact_line)
    assert gap.strip() != ""  # a real header/label exists between the two blocks


def test_ac10_document_present_at_boundary_default_threshold_32000():
    # estimate_tokens(doc) == exactly 32000 (the documented default) must
    # still count as "<=" -- inclusive boundary.
    doc = "z" * (32000 * 4)
    assert estimate_tokens(doc) == 32000

    answer = ModelAnswer(model="alpha", original_text="a", critique="c")
    prompt = build_revision_prompt(answer, [], source_document=doc)  # default max_document_tokens

    assert doc in prompt


# ---------------------------------------------------------------------------
# AC11: Given estimate_tokens(source_document) > max_document_tokens, When
# rendered, Then the document section contains a structured omission marker
# naming the threshold instead of the document text -- never a silent,
# unmarked absence.
# ---------------------------------------------------------------------------


def test_ac11_document_omitted_with_threshold_named_when_over_limit():
    doc = "x" * 40_000  # estimate_tokens == 10_000
    max_tokens = 5_000
    assert estimate_tokens(doc) > max_tokens

    answer = ModelAnswer(model="alpha", original_text="a", critique="c")
    prompt = build_revision_prompt(answer, [], source_document=doc, max_document_tokens=max_tokens)

    assert doc not in prompt  # never leak the raw text past the threshold
    assert str(max_tokens) in prompt  # threshold is named, not just "omitted"
    assert "omit" in prompt.lower()  # a structured omission marker exists


def test_ac11_document_omitted_at_default_threshold_when_over_32000():
    doc = "y" * ((32_000 * 4) + 400)  # estimate_tokens == 32100 > default 32000
    assert estimate_tokens(doc) > 32_000

    answer = ModelAnswer(model="alpha", original_text="a", critique="c")
    prompt = build_revision_prompt(answer, [], source_document=doc)  # default max_document_tokens

    assert doc not in prompt
    assert "32000" in prompt


@settings(max_examples=50, derandomize=True, deadline=1000)
@given(
    doc=st.text(alphabet=st.characters(whitelist_categories=("L", "N")), min_size=5, max_size=400),
    max_tokens=st.integers(min_value=0, max_value=200),
)
def test_ac10_ac11_property_inclusion_matches_threshold_comparison(doc, max_tokens):
    """Law tying AC10 and AC11 together: document verbatim-inclusion in the
    rendered prompt holds iff estimate_tokens(doc) <= max_document_tokens."""
    answer = ModelAnswer(model="alpha", original_text="orig", critique="crit")

    prompt = build_revision_prompt(answer, [], source_document=doc, max_document_tokens=max_tokens)

    included = doc in prompt
    within_threshold = estimate_tokens(doc) <= max_tokens
    assert included == within_threshold


# ---------------------------------------------------------------------------
# AC12: a source_document crafted with a literal [[cite:<id>]]-shaped
# substring for a real verified_facts id must (a) leave
# parse_revision_response, which only ever looks at the MODEL's response
# text, completely unaffected, and (b) stay confined inside the document's
# own delimited section in the rendered prompt -- distinguishable from a
# real model-authored citation.
# ---------------------------------------------------------------------------


def test_ac12_parse_revision_response_never_looks_at_the_prompt_or_document():
    verified = [_fact("3", "real fact")]
    # No prompt is built at all here -- parse_revision_response's contract
    # (Contract 2, AC3/AC4) only ever consumes a model's response string.
    response_without_citation = "I have no citation in my response."
    revised, cited = parse_revision_response(response_without_citation, verified)
    assert revised is None
    assert cited is None


def test_ac12_fake_citation_marker_in_document_stays_within_document_section():
    fact_id = "3"
    fake_marker = f"[[cite:{fact_id}]]"
    doc = f"Intro text. {fake_marker} Malicious injected instruction. End of doc."
    verified = [_fact(fact_id, "real fact")]
    answer = ModelAnswer(model="alpha", original_text="orig", critique="crit")

    prompt = build_revision_prompt(answer, verified, source_document=doc, max_document_tokens=1_000_000)

    # The marker appears exactly once in the whole prompt (as part of the
    # verbatim document), never duplicated into the instructions area.
    assert prompt.count(fake_marker) == 1

    # It is only reachable as a substring of the document block itself --
    # i.e. it cannot be isolated from the document without also removing
    # the document's own surrounding text.
    doc_start = prompt.index(doc)
    doc_end = doc_start + len(doc)
    marker_pos = prompt.index(fake_marker)
    assert doc_start <= marker_pos < doc_end


# ---------------------------------------------------------------------------
# AC13: Given source_document is empty string, When rendered, Then no
# document section is rendered at all (not an empty-but-present section) --
# mirrors verified_facts' own empty-list handling stance.
# ---------------------------------------------------------------------------


def test_ac13_empty_source_document_renders_no_document_section():
    answer = ModelAnswer(model="alpha", original_text="A", critique="C")
    verified = [_fact("1", "fact one")]

    prompt_explicit_empty = build_revision_prompt(answer, verified, source_document="")
    prompt_omitted_entirely = build_revision_prompt(answer, verified)  # backward-compatible default

    # No omission marker either -- omission markers are only for the
    # over-threshold case (AC11), not "no document was ever supplied".
    assert "omit" not in prompt_explicit_empty.lower()

    # Identical to the pre-amendment (no source_document at all) rendering:
    # proves nothing extra -- not even an empty labeled section -- was added.
    assert prompt_explicit_empty == prompt_omitted_entirely


# ---------------------------------------------------------------------------
# Amendment prose (not independently numbered, but part of the contract):
# run_revision_round gains source_document, threaded straight through to
# every build_revision_prompt call -- same document for every model, no
# per-model variation.
# ---------------------------------------------------------------------------


def test_run_revision_round_threads_source_document_identically_to_every_model():
    seen_prompts = []

    async def query_fn(model, prompt):
        seen_prompts.append(prompt)
        return "no citation here", 0.01

    answers = [
        ModelAnswer(model="alpha", original_text="a", critique="ca"),
        ModelAnswer(model="beta", original_text="b", critique="cb"),
    ]
    doc = "UNIQUE_SOURCE_DOCUMENT_MARKER_CONTENT " * 5

    asyncio.run(run_revision_round(0.10, answers, [], query_fn, source_document=doc))

    assert len(seen_prompts) == 2
    assert all(doc in p for p in seen_prompts)  # identical document, no per-model variation


def test_run_revision_round_noop_branch_still_accepts_source_document_kwarg():
    # AC1's cost-safe no-op (CSS >= threshold) must not break just because
    # the new parameter is present -- no crash, still zero query_fn calls.
    calls = []

    async def query_fn(model, prompt):
        calls.append(model)
        return "unused", 0.0

    answers = [ModelAnswer(model="alpha", original_text="a", critique="c")]

    outcomes = asyncio.run(
        run_revision_round(0.75, answers, [], query_fn, source_document="some document text")
    )

    assert calls == []
    assert all(o.accepted is False for o in outcomes)
