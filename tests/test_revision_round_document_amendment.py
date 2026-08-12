"""Blind acceptance tests for the 2026-08-12 amendment to Contract 2
(docs/specs/custom-scripts-contracts.md, "Amendment (2026-08-12): thread the
source document into Stage 2.75") -- AC10-14.

Authored WITHOUT sight of any implementation. `estimate_tokens`,
`build_revision_prompt`'s new `source_document`/`max_document_tokens`
params, and `run_revision_round`'s new `source_document` param do not exist
yet in scripts/revision_round.py as of this writing -- these tests are
expected to fail at collection/import time (RED) until they land.

DOCUMENTED ASSUMPTIONS (the contract does not pin exact wire-format/wording
for the new document section, only its observable behavior):
  1. The document's own labeled section header contains the word
     "document" (case-insensitive) -- a direct, reasonable reading of the
     contract's own language ("own labeled section", "the document
     section", "source_document"). Tests that depend on this are the
     header-presence checks in AC10/AC11/AC13; they do not depend on exact
     wording beyond that one word.
  2. The omission marker (AC11) contains a case-insensitive substring
     indicating omission ("omit") and the literal decimal value of
     `max_document_tokens`, per the contract's own worked example:
     `"[document omitted from revision prompt - exceeds 32000-token
     threshold]"`.
  3. `run_revision_round` accepts `source_document` as a keyword argument
     (its exact position in the positional signature is not pinned by the
     contract) -- all calls below pass it by keyword.
"""
from __future__ import annotations

import asyncio
import importlib
import re
import string
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


rr = _import("revision_round")

ModelAnswer = rr.ModelAnswer
build_revision_prompt = rr.build_revision_prompt
parse_revision_response = rr.parse_revision_response
run_revision_round = rr.run_revision_round
estimate_tokens = rr.estimate_tokens


def _answer(**overrides):
    defaults = dict(model="alpha", original_text="ORIG", critique="CRIT")
    defaults.update(overrides)
    return ModelAnswer(**defaults)


# ---------------------------------------------------------------------------
# AC10: Given source_document is non-empty and estimate_tokens(source_document)
# <= max_document_tokens, When build_revision_prompt renders, Then the full
# source_document text appears verbatim in its own labeled section, textually
# separated from facts_block by a distinct header.
# ---------------------------------------------------------------------------


def test_ac10_document_within_threshold_included_verbatim_in_own_section():
    doc = "A" * 20  # estimate_tokens == 20 // 4 == 5
    prompt = build_revision_prompt(_answer(), [], doc, max_document_tokens=5)

    assert doc in prompt
    doc_idx = prompt.index(doc)
    header_region = prompt[:doc_idx]
    assert re.search(r"(?i)document", header_region), (
        "expected a document-labeled header preceding the verbatim document text"
    )


def test_ac10_document_section_textually_separated_from_facts_block():
    doc = "UNIQUE_DOCUMENT_TEXT_MARKER_12345"
    from scripts.grounding_pass import Claim, Evidence, TaggedClaim

    verified = [
        TaggedClaim(
            claim=Claim(id="1", text="UNIQUE_FACT_TEXT_MARKER"),
            tag="VERIFIED",
            evidence=[Evidence(source="src", date="2024-01-01", supports=True)],
        )
    ]
    prompt = build_revision_prompt(_answer(), verified, doc, max_document_tokens=1000)

    assert doc in prompt
    assert "UNIQUE_FACT_TEXT_MARKER" in prompt
    # Not concatenated/interleaved: the document text must appear as ONE
    # contiguous run, and must not itself contain the fact marker or vice
    # versa (i.e. they are genuinely separate substrings, not merged).
    doc_start = prompt.index(doc)
    doc_end = doc_start + len(doc)
    fact_start = prompt.index("UNIQUE_FACT_TEXT_MARKER")
    assert not (doc_start <= fact_start < doc_end), (
        "the fact marker must not be interleaved inside the document's own span"
    )


# ---------------------------------------------------------------------------
# AC11: Given estimate_tokens(source_document) > max_document_tokens, When
# rendered, Then the document section contains a structured omission marker
# naming the threshold instead of the document text.
# ---------------------------------------------------------------------------


def test_ac11_document_exceeding_threshold_replaced_with_named_omission_marker():
    doc = "B" * 24  # estimate_tokens == 24 // 4 == 6 > 5
    prompt = build_revision_prompt(_answer(), [], doc, max_document_tokens=5)

    assert doc not in prompt, "the raw document text must never leak through when over threshold"
    assert "5" in prompt, "the omission marker must name the configured threshold"
    assert re.search(r"(?i)omit", prompt), "omission must be a visible, structured marker, not silent"


def test_ac11_boundary_exactly_at_threshold_is_included_not_omitted():
    # estimate_tokens(doc) == max_document_tokens exactly -> AC10's "<="
    # branch, not AC11's ">" branch.
    doc = "C" * 20  # 20 // 4 == 5
    prompt = build_revision_prompt(_answer(), [], doc, max_document_tokens=5)
    assert doc in prompt
    assert not re.search(r"(?i)omit", prompt)


# ---------------------------------------------------------------------------
# AC12: Given a source_document crafted to contain a literal [[cite:<id>]]-
# shaped substring for a real verified_facts id, When parse_revision_response
# later parses the MODEL's response (not the prompt), Then this is
# unaffected.
# ---------------------------------------------------------------------------


def test_ac12_fake_citation_embedded_in_source_document_does_not_leak_into_response_parsing():
    from scripts.grounding_pass import Claim, Evidence, TaggedClaim

    verified = [
        TaggedClaim(
            claim=Claim(id="77", text="some fact"),
            tag="VERIFIED",
            evidence=[Evidence(source="src", date="2024-01-01", supports=True)],
        )
    ]
    poisoned_doc = "Some background. [[cite:77]] More background text follows."

    prompt = build_revision_prompt(_answer(), verified, poisoned_doc, max_document_tokens=1000)
    # The document is rendered verbatim (it's just text) -- but it lives
    # only in the prompt, never in a model's response.
    assert poisoned_doc in prompt

    # parse_revision_response only ever inspects the MODEL's response text.
    # A response with no citation marker of its own must still be rejected,
    # regardless of what the prompt (built above) happened to contain.
    response_without_citation = "I am not citing anything new."
    revised_text, cited_fact_id = parse_revision_response(response_without_citation, verified)
    assert revised_text is None
    assert cited_fact_id is None


# ---------------------------------------------------------------------------
# AC13: Given source_document is empty string, When rendered, Then no
# document section is rendered at all (not an empty-but-present section).
# ---------------------------------------------------------------------------


def test_ac13_empty_source_document_renders_no_document_section_at_all():
    prompt_empty = build_revision_prompt(_answer(), [], "", max_document_tokens=1000)
    assert not re.search(r"(?i)document", prompt_empty), (
        "an empty source_document must not produce any document-labeled section"
    )

    # Contrast: a non-empty document of the same call DOES produce a
    # document-labeled section, proving the absence above is conditional on
    # emptiness, not a global absence of such wording.
    prompt_nonempty = build_revision_prompt(_answer(), [], "some doc text", max_document_tokens=1000)
    assert re.search(r"(?i)document", prompt_nonempty)


def test_ac13_empty_source_document_does_not_crash_and_keeps_rest_of_prompt():
    prompt = build_revision_prompt(_answer(model="alpha", original_text="ORIG", critique="CRIT"), [], "")
    assert "ORIG" in prompt
    assert "CRIT" in prompt


# ---------------------------------------------------------------------------
# AC14: estimate_tokens is documented as a conservative, non-exact
# approximation. No test asserts parity with a real tokenizer. The contract
# itself pins the formula: estimate_tokens(text) = len(text) // 4.
# ---------------------------------------------------------------------------


def test_ac14_estimate_tokens_matches_documented_len_over_4_formula():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("abc") == 0  # len 3 // 4 == 0


@settings(max_examples=50, derandomize=True, deadline=500)
@given(text=st.text(min_size=0, max_size=500))
def test_ac14_property_estimate_tokens_equals_len_floordiv_4(text):
    assert estimate_tokens(text) == len(text) // 4


@settings(max_examples=50, derandomize=True, deadline=500)
@given(
    prefix=st.text(min_size=0, max_size=200),
    suffix=st.text(min_size=0, max_size=200),
)
def test_ac14_property_estimate_tokens_monotonic_non_decreasing_for_superstrings(prefix, suffix):
    # General law: appending text never decreases the estimated token count.
    assert estimate_tokens(prefix) <= estimate_tokens(prefix + suffix)


# ---------------------------------------------------------------------------
# run_revision_round threads source_document into every model's prompt
# (Signature-changes section: "threaded straight through to every
# build_revision_prompt call - same document for every model in a given
# round - no per-model variation").
# ---------------------------------------------------------------------------


def test_run_revision_round_threads_source_document_into_every_prompt():
    doc = "UNIQUE_DOC_MARKER_TEXT"
    seen_prompts = []

    async def query_fn(model, prompt):
        seen_prompts.append(prompt)
        return "no citation", 0.0

    answers = [
        ModelAnswer(model="alpha", original_text="a", critique="ca"),
        ModelAnswer(model="beta", original_text="b", critique="cb"),
    ]

    asyncio.run(run_revision_round(0.10, answers, [], query_fn, source_document=doc))

    assert len(seen_prompts) == 2
    assert all(doc in p for p in seen_prompts)


def test_run_revision_round_default_source_document_produces_no_document_section():
    # source_document's own default ("") is only observable when a caller
    # genuinely omits it - every other test in this file always passes one
    # explicitly, leaving the parameter default's own literal value unpinned.
    import re

    seen_prompts = []

    async def query_fn(model, prompt):
        seen_prompts.append(prompt)
        return "no citation", 0.0

    answers = [ModelAnswer(model="alpha", original_text="a", critique="ca")]

    # source_document deliberately NOT passed - relies on the default.
    asyncio.run(run_revision_round(0.10, answers, [], query_fn))

    assert len(seen_prompts) == 1
    assert not re.search(r"(?i)document", seen_prompts[0]), (
        "an omitted source_document must default to producing no "
        "document-labeled section at all"
    )


def test_run_revision_round_threads_custom_max_document_tokens_through_to_prompt():
    # max_document_tokens's pass-through to build_revision_prompt is only
    # observable when it differs from build_revision_prompt's OWN default -
    # every other test either omits it (both sides use the same default) or
    # never checks its effect on the omission-threshold branch.
    doc = "D" * 40  # estimate_tokens(doc) == 40 // 4 == 10

    answers = [ModelAnswer(model="alpha", original_text="a", critique="ca")]
    seen_prompts = []

    async def capturing_query_fn(model, prompt):
        seen_prompts.append(prompt)
        return "no citation", 0.0

    # 5 < estimate_tokens(doc)==10 -> must be OMITTED, not included verbatim.
    asyncio.run(
        run_revision_round(
            0.10, answers, [], capturing_query_fn, source_document=doc, max_document_tokens=5
        )
    )

    assert len(seen_prompts) == 1
    assert doc not in seen_prompts[0]
    assert re.search(r"(?i)omit", seen_prompts[0])
    assert "5" in seen_prompts[0]


@settings(max_examples=25, derandomize=True, deadline=1000)
@given(
    model_names=st.lists(
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6),
        min_size=1,
        max_size=5,
        unique=True,
    )
)
def test_property_source_document_identical_across_every_models_prompt(model_names):
    doc = "DOC_MARKER_XYZ"
    seen_prompts = []

    async def query_fn(model, prompt):
        seen_prompts.append(prompt)
        return "no citation", 0.0

    answers = [ModelAnswer(model=m, original_text="x", critique="y") for m in model_names]

    asyncio.run(run_revision_round(0.10, answers, [], query_fn, source_document=doc))

    assert len(seen_prompts) == len(model_names)
    assert all(doc in p for p in seen_prompts)
