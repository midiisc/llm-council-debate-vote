"""Tests for scripts/transcript_writer.py, derived from
docs/specs/durable-persistence-contract.md's 8 Given/When/Then ACs.

Each write_* function is a small, pure, dependency-free write - these tests
exercise the real file content written to a real tmp_path directory, never
mocking Path.write_text, so a mutant that changes what gets rendered (wrong
field, dropped section, wrong fallback) is caught by reading the file back.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.transcript_writer import (
    _outcome_field,
    _peer_review_notes,
    _ranking_score,
    write_revision_outcomes,
    write_stage1_transcripts,
    write_stage2_summary,
    write_synthesis,
)


# ---------------------------------------------------------------------------
# AC1/AC2 - write_stage1_transcripts
# ---------------------------------------------------------------------------


def test_stage1_transcripts_contains_every_model_and_its_full_response_verbatim(tmp_path):
    stage1_results = [
        {"model": "model-x", "response": "Answer from X, with detail."},
        {"model": "model-y", "response": "Answer from Y, totally different."},
    ]

    path = write_stage1_transcripts(tmp_path, stage1_results)

    assert path == tmp_path / "stage1_transcripts.md"
    text = path.read_text()
    assert "model-x" in text
    assert "Answer from X, with detail." in text
    assert "model-y" in text
    assert "Answer from Y, totally different." in text
    # Order preserved - model-x's response appears before model-y's.
    assert text.index("Answer from X") < text.index("Answer from Y")


def test_stage1_transcripts_response_is_not_truncated(tmp_path):
    long_response = "word " * 5000  # far longer than any plausible truncation cutoff
    stage1_results = [{"model": "model-x", "response": long_response}]

    path = write_stage1_transcripts(tmp_path, stage1_results)

    assert long_response.strip() in path.read_text()


def test_stage1_transcripts_missing_model_key_falls_back_to_exactly_unknown(tmp_path):
    stage1_results = [{"response": "an answer with no model key"}]

    path = write_stage1_transcripts(tmp_path, stage1_results)

    assert "## unknown" in path.read_text()


def test_stage1_transcripts_missing_response_key_falls_back_to_empty_not_none(tmp_path):
    stage1_results = [{"model": "model-x"}]  # no "response" key at all

    path = write_stage1_transcripts(tmp_path, stage1_results)

    text = path.read_text()
    assert "None" not in text
    # model-x's section has an empty body, not a "None" literal or dropped
    # blank line - exactly "## model-x" followed by two blank-body lines
    # then the next section marker (none, here) / EOF.
    assert text == "# Stage 1 Transcripts\n\n## model-x\n\n\n\n"


def test_stage1_transcripts_empty_results_still_writes_explicit_no_models_statement(tmp_path):
    path = write_stage1_transcripts(tmp_path, [])

    assert path == tmp_path / "stage1_transcripts.md"
    assert path.exists()
    assert path.read_text() == (
        "# Stage 1 Transcripts\n\n"
        "No models responded to this query (no models responded).\n"
    )


def test_stage1_transcripts_exact_file_content_for_two_models(tmp_path):
    # A single exact byte-for-byte assertion closes the header text, blank
    # line placement, and join/trailing-newline mutants all at once, rather
    # than a substring check that a case/whitespace mutant can slip past.
    stage1_results = [
        {"model": "model-x", "response": "Answer from X"},
        {"model": "model-y", "response": "Answer from Y"},
    ]

    path = write_stage1_transcripts(tmp_path, stage1_results)

    assert path.read_text() == (
        "# Stage 1 Transcripts\n\n"
        "## model-x\n\nAnswer from X\n\n"
        "## model-y\n\nAnswer from Y\n\n"
    )


# ---------------------------------------------------------------------------
# AC3/AC4/AC5 - write_stage2_summary
# ---------------------------------------------------------------------------


def _aggregate_rankings():
    return [
        {"model": "model-x", "borda_score": 1.0, "rank": 1},
        {"model": "model-y", "borda_score": 0.0, "rank": 2},
    ]


def test_stage2_summary_contains_css_formatted_and_every_models_rank_and_score(tmp_path):
    path = write_stage2_summary(
        tmp_path,
        stage2_results=[],
        aggregate_rankings=_aggregate_rankings(),
        css=0.845123,
        is_outlier={"model-x": False, "model-y": False},
    )

    assert path == tmp_path / "stage2_summary.md"
    text = path.read_text()
    # Formatted to a reasonable precision, not the raw unrounded float.
    assert "0.845123" not in text
    assert "0.845" in text
    assert "model-x" in text and "1" in text and "1.0" in text
    assert "model-y" in text and "2" in text and "0.0" in text


def test_stage2_summary_exact_content_with_outlier_and_notes(tmp_path):
    # One exact-match assertion closes every literal-string mutant in this
    # function at once (section headers, table header row, separator row,
    # the outlier marker, and all the blank-line placements).
    stage2_results = [
        {
            "model": "model-y",
            "parsed_ranking": {
                "evaluations": {"Response A": {"notes": "clear and well cited"}}
            },
        }
    ]
    path = write_stage2_summary(
        tmp_path,
        stage2_results=stage2_results,
        aggregate_rankings=_aggregate_rankings(),
        css=0.845123,
        is_outlier={"model-x": True, "model-y": False},
    )

    assert path.read_text() == (
        "# Stage 2 Summary\n\n"
        "Consensus Strength Score (CSS): 0.845\n\n"
        "## Rankings\n\n"
        "| Model | Rank | Score |\n"
        "| --- | --- | --- |\n"
        "| model-x (OUTLIER) | 1 | 1.0 |\n"
        "| model-y | 2 | 0.0 |\n\n"
        "## Outliers\n\n"
        "The following model(s) were flagged as statistical outliers: model-x\n\n"
        "## Peer Review Notes\n\n"
        "### model-y\n"
        "- Response A: clear and well cited\n\n"
    )


def test_stage2_summary_css_none_exact_content(tmp_path):
    path = write_stage2_summary(
        tmp_path,
        stage2_results=[],
        aggregate_rankings=[{"model": "model-c", "rank": 1}],
        css=None,
        is_outlier={},
    )

    assert path.read_text() == (
        "# Stage 2 Summary\n\n"
        "Consensus Strength Score (CSS): N/A - single model, no peer review\n\n"
        "## Rankings\n\n"
        "| Model | Rank | Score |\n"
        "| --- | --- | --- |\n"
        "| model-c | 1 | N/A |\n\n"
    )


def test_stage2_summary_ranking_falls_back_to_average_score_then_score(tmp_path):
    # borda_score absent -> average_score used.
    path = write_stage2_summary(
        tmp_path,
        stage2_results=[],
        aggregate_rankings=[{"model": "model-a", "average_score": 7.5, "rank": 1}],
        css=0.5,
        is_outlier={},
    )
    assert "| model-a | 1 | 7.5 |" in path.read_text()

    # Neither borda_score nor average_score present -> plain score used.
    path2 = write_stage2_summary(
        tmp_path,
        stage2_results=[],
        aggregate_rankings=[{"model": "model-b", "score": 3.2, "rank": 1}],
        css=0.5,
        is_outlier={},
    )
    assert "| model-b | 1 | 3.2 |" in path2.read_text()

    # None of the three present -> exactly "N/A", not a crash, blank cell,
    # or a mutated near-miss like "n/a"/"XXN/AXX".
    path3 = write_stage2_summary(
        tmp_path,
        stage2_results=[],
        aggregate_rankings=[{"model": "model-c", "rank": 1}],
        css=0.5,
        is_outlier={},
    )
    assert "| model-c | 1 | N/A |" in path3.read_text()


def test_ranking_score_direct_fallback_chain_exact_values():
    assert _ranking_score({"borda_score": 1.0, "average_score": 2.0, "score": 3.0}) == 1.0
    assert _ranking_score({"average_score": 2.0, "score": 3.0}) == 2.0
    assert _ranking_score({"score": 3.0}) == 3.0
    assert _ranking_score({}) == "N/A"
    # A None value for the highest-priority key present is treated as
    # absent, not returned verbatim.
    assert _ranking_score({"borda_score": None, "average_score": 2.0}) == 2.0


def test_stage2_summary_ranking_row_missing_model_key_falls_back_to_exactly_unknown(tmp_path):
    path = write_stage2_summary(
        tmp_path,
        stage2_results=[],
        aggregate_rankings=[{"rank": 1, "borda_score": 1.0}],  # no "model" key
        css=0.5,
        is_outlier={},
    )
    assert "| unknown | 1 | 1.0 |" in path.read_text()


def test_stage2_summary_ranking_row_missing_rank_key_falls_back_to_exactly_na(tmp_path):
    path = write_stage2_summary(
        tmp_path,
        stage2_results=[],
        aggregate_rankings=[{"model": "model-a", "borda_score": 1.0}],  # no "rank" key
        css=0.5,
        is_outlier={},
    )
    assert "| model-a | N/A | 1.0 |" in path.read_text()


def test_stage2_summary_flags_outliers_visibly_and_distinctly_from_non_outliers(tmp_path):
    path = write_stage2_summary(
        tmp_path,
        stage2_results=[],
        aggregate_rankings=_aggregate_rankings(),
        css=0.5,
        is_outlier={"model-x": True, "model-y": False},
    )

    text = path.read_text()
    assert "OUTLIER" in text
    # model-x's row is visibly marked...
    x_line = next(line for line in text.splitlines() if line.startswith("| model-x"))
    assert "OUTLIER" in x_line
    # ...and model-y's is not conflated with it.
    y_line = next(line for line in text.splitlines() if line.startswith("| model-y"))
    assert "OUTLIER" not in y_line


def test_stage2_summary_multiple_outliers_are_comma_space_joined(tmp_path):
    # A single-outlier fixture can't distinguish ", " from any other
    # separator (join of one element is separator-invariant) - this needs
    # two-plus outliers to pin the exact join string.
    path = write_stage2_summary(
        tmp_path,
        stage2_results=[],
        aggregate_rankings=[
            {"model": "model-x", "borda_score": 1.0, "rank": 1},
            {"model": "model-y", "borda_score": 0.5, "rank": 2},
            {"model": "model-z", "borda_score": 0.0, "rank": 3},
        ],
        css=0.5,
        is_outlier={"model-x": True, "model-y": True, "model-z": False},
    )

    text = path.read_text()
    assert (
        "The following model(s) were flagged as statistical outliers: "
        "model-x, model-y\n" in text
    )


def test_stage2_summary_no_outliers_omits_outliers_section_entirely(tmp_path):
    path = write_stage2_summary(
        tmp_path,
        stage2_results=[],
        aggregate_rankings=_aggregate_rankings(),
        css=0.5,
        is_outlier={"model-x": False, "model-y": False},
    )

    text = path.read_text()
    assert "## Outliers" not in text


def test_stage2_summary_includes_peer_review_notes_when_present(tmp_path):
    stage2_results = [
        {
            "model": "model-y",
            "parsed_ranking": {
                "evaluations": {
                    "Response A": {"notes": "clear and well cited"},
                }
            },
        }
    ]
    path = write_stage2_summary(
        tmp_path,
        stage2_results=stage2_results,
        aggregate_rankings=_aggregate_rankings(),
        css=0.5,
        is_outlier={},
    )

    text = path.read_text()
    assert "## Peer Review Notes" in text
    assert "model-y" in text
    assert "clear and well cited" in text


def test_stage2_summary_omits_notes_section_when_no_reviewer_has_notes(tmp_path):
    stage2_results = [
        {
            "model": "model-y",
            "parsed_ranking": {
                "evaluations": {
                    "Response A": {"accuracy": 8},  # no "notes" key at all
                }
            },
        }
    ]
    path = write_stage2_summary(
        tmp_path,
        stage2_results=stage2_results,
        aggregate_rankings=_aggregate_rankings(),
        css=0.5,
        is_outlier={},
    )

    assert "## Peer Review Notes" not in path.read_text()


def test_peer_review_notes_direct_exact_rendering():
    stage2_results = [
        {
            "model": "model-y",
            "parsed_ranking": {
                "evaluations": {
                    "Response A": {"notes": "note one"},
                    "Response B": {"notes": "note two"},
                }
            },
        }
    ]
    assert _peer_review_notes(stage2_results) == [
        "### model-y",
        "- Response A: note one",
        "- Response B: note two",
        "",
    ]


def test_peer_review_notes_missing_model_key_falls_back_to_exactly_unknown():
    stage2_results = [
        {"parsed_ranking": {"evaluations": {"Response A": {"notes": "a note"}}}}
    ]
    rendered = _peer_review_notes(stage2_results)
    assert rendered[0] == "### unknown"


def test_stage2_summary_missing_parsed_ranking_does_not_raise(tmp_path):
    stage2_results = [{"model": "model-y"}]  # no parsed_ranking key at all

    path = write_stage2_summary(
        tmp_path,
        stage2_results=stage2_results,
        aggregate_rankings=[],
        css=0.5,
        is_outlier={},
    )
    assert path.exists()
    assert "## Peer Review Notes" not in path.read_text()


# ---------------------------------------------------------------------------
# AC6 - write_synthesis
# ---------------------------------------------------------------------------


def test_synthesis_contains_model_name_and_verbatim_text_unaltered(tmp_path):
    synthesis_text = "Line one.\nLine two, with 'quotes' and punctuation!"

    path = write_synthesis(tmp_path, synthesis_text, "anthropic/claude-opus-4.8")

    assert path == tmp_path / "synthesis.md"
    text = path.read_text()
    assert "anthropic/claude-opus-4.8" in text
    assert synthesis_text in text


# ---------------------------------------------------------------------------
# AC7 - write_revision_outcomes
# ---------------------------------------------------------------------------


def test_revision_outcomes_distinguishes_revised_from_not_revising(tmp_path):
    outcomes = [
        {
            "model": "model-x",
            "accepted": True,
            "cited_fact_id": "fact-42",
            "revised_text": "the revised answer text",
        },
        {"model": "model-y", "accepted": False},
    ]

    path = write_revision_outcomes(tmp_path, outcomes)

    assert path == tmp_path / "revision_outcomes.md"
    text = path.read_text()
    assert "model-x" in text
    assert "fact-42" in text
    assert "the revised answer text" in text
    assert "not revising" in text
    assert "model-y" in text

    # The two models' sections are distinct - model-y's section does not
    # itself contain the revised fact/text that belongs to model-x.
    y_section = text.split("## model-y", 1)[1]
    assert "fact-42" not in y_section
    assert "the revised answer text" not in y_section


def test_revision_outcomes_exact_file_content(tmp_path):
    outcomes = [
        {
            "model": "model-x",
            "accepted": True,
            "cited_fact_id": "fact-42",
            "revised_text": "the revised answer text",
        },
        {"model": "model-y", "accepted": False},
    ]

    path = write_revision_outcomes(tmp_path, outcomes)

    assert path.read_text() == (
        "# Stage 2.75 Revision Outcomes\n\n"
        "## model-x\n\n"
        "Cited fact: fact-42\n\n"
        "the revised answer text\n\n"
        "## model-y\n\n"
        "not revising\n\n"
    )


def test_revision_outcomes_missing_model_key_falls_back_to_exactly_unknown(tmp_path):
    path = write_revision_outcomes(tmp_path, [{"accepted": False}])
    assert "## unknown" in path.read_text()


def test_revision_outcomes_missing_accepted_key_defaults_to_not_revising(tmp_path):
    # "accepted" absent entirely - must default falsy (False), never
    # truthy (which would wrongly render a "Cited fact:"/revised-text
    # section for a model that never even reported accepted/rejected).
    path = write_revision_outcomes(tmp_path, [{"model": "model-q"}])
    text = path.read_text()
    assert "not revising" in text
    assert "Cited fact" not in text


def test_revision_outcomes_accepted_missing_revised_text_key_defaults_to_empty(tmp_path):
    outcomes = [{"model": "model-x", "accepted": True, "cited_fact_id": "fact-1"}]
    path = write_revision_outcomes(tmp_path, outcomes)
    text = path.read_text()
    assert "None" not in text
    assert path.read_text() == (
        "# Stage 2.75 Revision Outcomes\n\n"
        "## model-x\n\n"
        "Cited fact: fact-1\n\n\n\n"
    )


def test_outcome_field_direct_dict_and_object_and_default():
    class FakeOutcome:
        def __init__(self):
            self.present = "object-value"

    assert _outcome_field({"present": "dict-value"}, "present") == "dict-value"
    assert _outcome_field({}, "missing", "fallback") == "fallback"
    assert _outcome_field({}, "missing") is None
    assert _outcome_field(FakeOutcome(), "present") == "object-value"
    assert _outcome_field(FakeOutcome(), "missing", "fallback") == "fallback"
    assert _outcome_field(FakeOutcome(), "missing") is None


def test_revision_outcomes_supports_dataclass_like_objects_not_just_dicts(tmp_path):
    class FakeOutcome:
        def __init__(self, model, accepted, cited_fact_id=None, revised_text=""):
            self.model = model
            self.accepted = accepted
            self.cited_fact_id = cited_fact_id
            self.revised_text = revised_text

    outcomes = [FakeOutcome("model-z", True, "fact-7", "revised via object")]

    path = write_revision_outcomes(tmp_path, outcomes)

    text = path.read_text()
    assert "model-z" in text
    assert "fact-7" in text
    assert "revised via object" in text


# ---------------------------------------------------------------------------
# AC8 - output_dir already exists; no atomic-rename dance required
# ---------------------------------------------------------------------------


def test_all_writers_accept_a_pre_existing_output_dir_without_raising(tmp_path):
    assert tmp_path.exists()  # tmp_path fixture already creates the dir

    p1 = write_stage1_transcripts(tmp_path, [{"model": "m", "response": "r"}])
    p2 = write_stage2_summary(tmp_path, [], [], css=0.5, is_outlier={})
    p3 = write_synthesis(tmp_path, "text", "m")
    p4 = write_revision_outcomes(tmp_path, [{"model": "m", "accepted": False}])

    for p in (p1, p2, p3, p4):
        assert p.exists()
