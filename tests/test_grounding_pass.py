"""Blind acceptance tests for Contract 1 -- grounding_pass.py (Stage 0.5).

Source of truth: docs/specs/custom-scripts-contracts.md, Contract 1,
Acceptance criteria 1-6. Authored WITHOUT sight of any implementation --
only the contract's dataclass/function signatures and Given/When/Then ACs.

Input-format assumption (not specified verbatim by the contract, but
directly implied by AC5's own example "'3.' then '7.'"): a raw claims file
is one numbered claim per line, formatted as "<id>. <text>".
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from hypothesis import given, settings, strategies as st

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _import(name: str):
    """Import `scripts.<name>` if present, else a bare top-level `<name>`.

    A ModuleNotFoundError here is a legitimate RED (feature not yet built),
    not a test-authoring bug.
    """
    try:
        return importlib.import_module(f"scripts.{name}")
    except ModuleNotFoundError:
        return importlib.import_module(name)


gp = _import("grounding_pass")

Evidence = gp.Evidence
Claim = gp.Claim
TaggedClaim = gp.TaggedClaim
parse_claims = gp.parse_claims
tag_claim = gp.tag_claim
render_output = gp.render_output
run_grounding_pass = gp.run_grounding_pass

ALLOWED_TAGS = {"VERIFIED", "CONTRADICTED", "UNVERIFIABLE"}


def _numbered_raw(pairs: list[tuple[str, str]]) -> str:
    """pairs: [(id, text), ...] -> raw numbered-claims text, one per line."""
    return "\n".join(f"{cid}. {text}" for cid, text in pairs) + "\n"


# ---------------------------------------------------------------------------
# AC1: Given a raw file with N numbered claims and evidence for each, When
# run_grounding_pass runs, Then it writes <output_dir>/grounding.md with
# every claim tagged exactly one of the three tags, original numbering and
# text preserved verbatim.
# ---------------------------------------------------------------------------


def test_ac1_run_grounding_pass_writes_grounding_md_verbatim_and_tagged(tmp_path):
    raw = _numbered_raw(
        [
            ("1", "The sky is blue."),
            ("2", "Water boils at 100C at sea level."),
            ("3", "The moon is made of cheese."),
        ]
    )
    input_path = tmp_path / "context.txt"
    input_path.write_text(raw)

    evidence = {
        "1": [Evidence(source="NIST optics survey", date="2023-01-01", supports=True)],
        "2": [Evidence(source="CRC Handbook", date="2020-05-05", supports=True)],
        "3": [],
    }
    output_dir = tmp_path / "out"

    result_path = run_grounding_pass(input_path, evidence, output_dir)

    assert result_path == output_dir / "grounding.md"
    assert result_path.exists()
    content = result_path.read_text()

    # original numbering + text preserved verbatim
    assert "The sky is blue." in content
    assert "Water boils at 100C at sea level." in content
    assert "The moon is made of cheese." in content

    # claims with support-only evidence render VERIFIED; empty-evidence claim
    # is demoted to ASSUMPTION per AC4.
    assert content.count("VERIFIED") >= 2
    assert "ASSUMPTION" in content


def test_ac1_parse_then_tag_every_claim_gets_exactly_one_allowed_tag(tmp_path):
    raw = _numbered_raw(
        [("1", "Claim one."), ("2", "Claim two."), ("3", "Claim three.")]
    )
    claims = parse_claims(raw)
    assert len(claims) == 3

    evidence = {
        "1": [Evidence(source="a", date="2020-01-01", supports=True)],
        "2": [Evidence(source="b", date="2020-01-01", supports=False)],
        "3": [],
    }
    tagged = [tag_claim(c, evidence[c.id]) for c in claims]
    assert all(t.tag in ALLOWED_TAGS for t in tagged)
    assert len(tagged) == len(claims)


# ---------------------------------------------------------------------------
# AC1/AC2/AC3/AC4 combined decision-table property: whatever the mix of
# supporting/contradicting evidence, the resulting tag is always exactly one
# of the three allowed values, and follows the conservative rule (any
# contradiction wins; else any support wins; else UNVERIFIABLE).
# ---------------------------------------------------------------------------


@settings(max_examples=50, derandomize=True, deadline=500)
@given(
    n_support=st.integers(min_value=0, max_value=4),
    n_contradict=st.integers(min_value=0, max_value=4),
)
def test_ac1to4_property_tag_decision_table_is_always_one_of_three_and_conservative(
    n_support, n_contradict
):
    claim = Claim(id="z", text="claim text")
    evidence = [
        Evidence(source=f"s{i}", date="2021-01-01", supports=True) for i in range(n_support)
    ] + [
        Evidence(source=f"c{i}", date="2021-01-01", supports=False) for i in range(n_contradict)
    ]

    tagged = tag_claim(claim, evidence)

    assert tagged.tag in ALLOWED_TAGS
    assert tagged.claim == claim
    if n_contradict > 0:
        assert tagged.tag == "CONTRADICTED"
    elif n_support > 0:
        assert tagged.tag == "VERIFIED"
    else:
        assert tagged.tag == "UNVERIFIABLE"


# ---------------------------------------------------------------------------
# AC2: Given a claim with >=1 supporting evidence item and 0 contradicting
# items, When tagged, Then the result is VERIFIED citing that evidence's
# source and date.
# ---------------------------------------------------------------------------


def test_ac2_supporting_only_evidence_yields_verified_with_citation():
    claim = Claim(id="5", text="Paris is the capital of France.")
    ev = Evidence(source="Britannica", date="2022-03-01", supports=True)

    tagged = tag_claim(claim, [ev])

    assert tagged.tag == "VERIFIED"
    assert ev in tagged.evidence
    assert any(e.source == "Britannica" and e.date == "2022-03-01" for e in tagged.evidence)


# ---------------------------------------------------------------------------
# AC3: Given a claim with >=1 contradicting evidence item (regardless of any
# supporting items also present), When tagged, Then the result is
# CONTRADICTED -- contradiction always wins over support (conservative).
# ---------------------------------------------------------------------------


def test_ac3_any_contradicting_evidence_wins_even_with_supporting_evidence_present():
    claim = Claim(id="6", text="The Great Wall of China is visible from space.")
    evidence = [
        Evidence(source="popular myth compilation", date="2010-01-01", supports=True),
        Evidence(source="NASA", date="2019-06-01", supports=False),
    ]

    tagged = tag_claim(claim, evidence)

    assert tagged.tag == "CONTRADICTED"


@settings(max_examples=50, derandomize=True, deadline=500)
@given(
    n_support=st.integers(min_value=0, max_value=5),
    n_contradict=st.integers(min_value=1, max_value=5),
)
def test_ac3_property_contradiction_always_wins_regardless_of_support_count(
    n_support, n_contradict
):
    claim = Claim(id="x", text="some claim")
    evidence = [
        Evidence(source=f"sup{i}", date="2020-01-01", supports=True) for i in range(n_support)
    ] + [
        Evidence(source=f"con{i}", date="2020-01-01", supports=False) for i in range(n_contradict)
    ]

    tagged = tag_claim(claim, evidence)

    assert tagged.tag == "CONTRADICTED"


# ---------------------------------------------------------------------------
# AC4: Given a claim with an empty evidence list, When tagged, Then the
# result is UNVERIFIABLE, and the rendered output shows it demoted to
# ASSUMPTION.
# ---------------------------------------------------------------------------


def test_ac4_empty_evidence_yields_unverifiable_and_renders_as_assumption():
    claim = Claim(id="9", text="Unfalsifiable claim.")

    tagged = tag_claim(claim, [])
    assert tagged.tag == "UNVERIFIABLE"

    rendered = render_output([tagged], _numbered_raw([("9", "Unfalsifiable claim.")]))
    assert "ASSUMPTION" in rendered
    assert "Unfalsifiable claim." in rendered


# ---------------------------------------------------------------------------
# AC5: Given claims numbered non-sequentially in the input (e.g. "3." then
# "7."), When parsed, Then parse_claims preserves the original id strings --
# never renumbers.
# ---------------------------------------------------------------------------


def test_ac5_nonsequential_ids_preserved_not_renumbered():
    raw = _numbered_raw([("3", "First claim text."), ("7", "Second claim text.")])

    claims = parse_claims(raw)

    assert [c.id for c in claims] == ["3", "7"]
    assert [c.text for c in claims] == ["First claim text.", "Second claim text."]


_id_strategy = st.from_regex(r"[1-9][0-9]{0,2}", fullmatch=True)
_text_strategy = (
    st.text(
        alphabet=st.characters(blacklist_categories=("Cc", "Cs"), blacklist_characters="\n."),
        min_size=1,
        max_size=40,
    )
    .map(str.strip)
    .filter(lambda s: len(s) > 0)
)


@settings(max_examples=50, derandomize=True, deadline=500)
@given(
    st.lists(
        st.tuples(_id_strategy, _text_strategy),
        min_size=1,
        max_size=6,
        unique_by=lambda t: t[0],
    )
)
def test_ac5_property_parse_claims_never_renumbers_ids(pairs):
    raw = _numbered_raw(pairs)

    claims = parse_claims(raw)

    assert [c.id for c in claims] == [p[0] for p in pairs]


# ---------------------------------------------------------------------------
# AC6: Given output_dir doesn't exist, When run_grounding_pass runs, Then it
# creates the directory (folder-scoped output, no writes outside it).
# ---------------------------------------------------------------------------


def test_ac6_run_grounding_pass_creates_missing_output_dir(tmp_path):
    raw = _numbered_raw([("1", "A claim.")])
    input_path = tmp_path / "in.txt"
    input_path.write_text(raw)

    output_dir = tmp_path / "does" / "not" / "exist"
    assert not output_dir.exists()

    result_path = run_grounding_pass(input_path, {"1": []}, output_dir)

    assert output_dir.exists()
    assert output_dir.is_dir()
    assert result_path.exists()
    assert result_path.parent == output_dir


# ---------------------------------------------------------------------------
# Mutation-hardening tests (scoped mutation gate follow-up). These pin exact
# behaviour that the blind acceptance tests above only checked loosely
# (substring "in" checks, always-present evidence keys), so precise output
# regressions -- wrong casing, wrong punctuation, wrong dict-default, wrong
# mkdir flags -- are caught as failures instead of silently passing.
# ---------------------------------------------------------------------------


def test_render_output_exact_text_for_verified_and_contradicted_with_evidence():
    tagged = [
        TaggedClaim(
            claim=Claim(id="1", text="Sky is blue."),
            tag="VERIFIED",
            evidence=[Evidence(source="NASA", date="2020-01-01", supports=True)],
        ),
        TaggedClaim(
            claim=Claim(id="2", text="Moon is cheese."),
            tag="CONTRADICTED",
            evidence=[Evidence(source="NASA", date="2020-01-01", supports=False)],
        ),
        TaggedClaim(claim=Claim(id="3", text="Unfalsifiable."), tag="UNVERIFIABLE", evidence=[]),
    ]

    rendered = render_output(tagged, "irrelevant original text")

    expected = (
        "# Grounding Pass\n"
        "\n"
        "## Claim 1\n"
        "Sky is blue.\n"
        "**Tag:** VERIFIED\n"
        "**Evidence:**\n"
        "- NASA (2020-01-01) — supports\n"
        "\n"
        "## Claim 2\n"
        "Moon is cheese.\n"
        "**Tag:** CONTRADICTED\n"
        "**Evidence:**\n"
        "- NASA (2020-01-01) — contradicts\n"
        "\n"
        "## Claim 3\n"
        "Unfalsifiable.\n"
        "**Tag:** UNVERIFIABLE (demoted to ASSUMPTION)\n"
        "**Evidence:** none\n"
    )
    assert rendered == expected


def test_run_grounding_pass_uses_empty_list_default_for_claim_missing_from_evidence_dict(tmp_path):
    """A claim id absent from the evidence mapping must fall back to an empty
    evidence list (-> UNVERIFIABLE), not crash and not silently look up the
    wrong key."""
    raw = _numbered_raw([("1", "Has evidence."), ("2", "No evidence entry at all.")])
    input_path = tmp_path / "in.txt"
    input_path.write_text(raw)

    # Deliberately omit claim "2" from the evidence dict entirely.
    evidence = {"1": [Evidence(source="src", date="2020-01-01", supports=True)]}
    output_dir = tmp_path / "out"

    result_path = run_grounding_pass(input_path, evidence, output_dir)
    content = result_path.read_text()

    assert "## Claim 1" in content
    claim2_section = content.split("## Claim 2")[1]
    assert "UNVERIFIABLE (demoted to ASSUMPTION)" in claim2_section
    assert "**Evidence:** none" in claim2_section


def test_run_grounding_pass_is_idempotent_against_a_preexisting_output_dir(tmp_path):
    """Calling run_grounding_pass twice against the same (already-existing)
    output_dir must not raise -- output_dir.mkdir must be called with
    exist_ok=True, not exist_ok=False/None."""
    raw = _numbered_raw([("1", "A claim.")])
    input_path = tmp_path / "in.txt"
    input_path.write_text(raw)
    output_dir = tmp_path / "out"

    first = run_grounding_pass(input_path, {"1": []}, output_dir)
    # Second call: output_dir already exists.
    second = run_grounding_pass(input_path, {"1": []}, output_dir)

    assert first == second
    assert second.exists()


def test_ac6_run_grounding_pass_is_rerunnable_against_an_existing_output_dir(tmp_path):
    # Mutation-gate regression: output_dir.mkdir must be called with
    # exist_ok=True. If exist_ok were False (or falsy), a second run against
    # an already-created output_dir would raise FileExistsError instead of
    # simply overwriting grounding.md.
    raw = _numbered_raw([("1", "A claim.")])
    input_path = tmp_path / "in.txt"
    input_path.write_text(raw)
    output_dir = tmp_path / "out"

    first = run_grounding_pass(input_path, {"1": []}, output_dir)
    second = run_grounding_pass(input_path, {"1": []}, output_dir)

    assert first == second
    assert second.exists()


# ---------------------------------------------------------------------------
# Mutation-gate hardening: a claim id absent from the evidence dict must
# default to an empty evidence list (UNVERIFIABLE), not crash. tag_claim's
# `evidence=list(evidence)` would raise TypeError on a None default.
# ---------------------------------------------------------------------------


def test_run_grounding_pass_claim_missing_from_evidence_dict_defaults_to_unverifiable(tmp_path):
    raw = _numbered_raw([("1", "Has evidence."), ("2", "Missing from evidence dict.")])
    input_path = tmp_path / "in.txt"
    input_path.write_text(raw)

    evidence = {"1": [Evidence(source="s", date="2020-01-01", supports=True)]}
    output_dir = tmp_path / "out"

    result_path = run_grounding_pass(input_path, evidence, output_dir)

    content = result_path.read_text()
    lines = content.split("## Claim 2", 1)
    assert len(lines) == 2
    claim_2_section = lines[1]
    assert "UNVERIFIABLE" in claim_2_section
    assert "**Evidence:** none" in claim_2_section


# ---------------------------------------------------------------------------
# Mutation-gate hardening: render_output's exact markdown structure -- header,
# per-claim heading/text/tag line, the evidence block (both the
# with-evidence and no-evidence branches, and the supports/contradicts
# verdict wording), and the blank-line join separator.
# ---------------------------------------------------------------------------


def test_render_output_exact_markdown_for_all_branches():
    verified_claim = TaggedClaim(
        claim=Claim(id="1", text="Claim one text."),
        tag="VERIFIED",
        evidence=[
            Evidence(source="Source A", date="2020-01-01", supports=True),
            Evidence(source="Source B", date="2021-02-02", supports=False),
        ],
    )
    unverifiable_claim = TaggedClaim(
        claim=Claim(id="2", text="Claim two text."),
        tag="UNVERIFIABLE",
        evidence=[],
    )

    rendered = render_output([verified_claim, unverifiable_claim], "unused original text")

    expected = "\n".join(
        [
            "# Grounding Pass",
            "",
            "## Claim 1",
            "Claim one text.",
            "**Tag:** VERIFIED",
            "**Evidence:**",
            "- Source A (2020-01-01) — supports",
            "- Source B (2021-02-02) — contradicts",
            "",
            "## Claim 2",
            "Claim two text.",
            "**Tag:** UNVERIFIABLE (demoted to ASSUMPTION)",
            "**Evidence:** none",
            "",
        ]
    )
    assert rendered == expected
