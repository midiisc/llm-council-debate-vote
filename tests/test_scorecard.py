"""Blind acceptance tests for Contract 3 -- scorecard.py (+ scorecard CLI).

Source of truth: docs/specs/custom-scripts-contracts.md, Contract 3,
Acceptance criteria 1-8. Authored WITHOUT sight of any implementation.

DOCUMENTED ASSUMPTION (contract gives ScorecardRecord's field shapes but not
session_result's exact schema for build_scorecard_record): tests assume
session_result mirrors ScorecardRecord's own field names 1:1 (css,
rubric_scores, ranks, is_outlier, cost_usd, each a model-keyed dict where
applicable) since that is the most direct, contract-grounded mapping and
build_scorecard_record's only stated job is to fold session_result +
topic_label + timestamp into a ScorecardRecord.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _import(name: str):
    try:
        return importlib.import_module(f"scripts.{name}")
    except ModuleNotFoundError:
        return importlib.import_module(name)


sc = _import("scorecard")

ScorecardRecord = sc.ScorecardRecord
ScorecardReport = sc.ScorecardReport
build_scorecard_record = sc.build_scorecard_record
default_scorecard_path = sc.default_scorecard_path
append_record = sc.append_record
load_records = sc.load_records
confidence_tier = sc.confidence_tier
compute_report = sc.compute_report
render_report = sc.render_report

RUBRIC_DIMS = ("accuracy", "relevance", "completeness", "conciseness", "clarity")


def _rubric(seed: float) -> dict:
    return {dim: seed for dim in RUBRIC_DIMS}


def _make_record(
    timestamp="2026-01-01T00:00:00",
    topic="topic",
    css=0.8,
    models=("alpha", "beta"),
    outlier_model=None,
) -> ScorecardRecord:
    return ScorecardRecord(
        timestamp=timestamp,
        topic_label=topic,
        css=css,
        rubric_scores={m: _rubric(4.0) for m in models},
        ranks={m: i + 1 for i, m in enumerate(models)},
        is_outlier={m: (m == outlier_model) for m in models},
        cost_usd={m: 0.01 for m in models},
    )


# ---------------------------------------------------------------------------
# AC1: Given a completed session result with rubric scores/ranks/CSS/cost
# for N models, When build_scorecard_record runs, Then the record includes
# every model present in the session result -- none silently dropped.
# ---------------------------------------------------------------------------


def test_ac1_build_scorecard_record_drops_no_models():
    session_result = {
        "css": 0.62,
        "rubric_scores": {
            "alpha": _rubric(4.5),
            "beta": _rubric(3.0),
            "gamma": _rubric(5.0),
        },
        "ranks": {"alpha": 1, "beta": 3, "gamma": 2},
        "is_outlier": {"alpha": False, "beta": True, "gamma": False},
        "cost_usd": {"alpha": 0.01, "beta": 0.02, "gamma": 0.015},
    }

    record = build_scorecard_record(
        session_result, topic_label="my topic", timestamp="2026-01-01T00:00:00"
    )

    expected_models = {"alpha", "beta", "gamma"}
    assert set(record.rubric_scores.keys()) == expected_models
    assert set(record.ranks.keys()) == expected_models
    assert set(record.is_outlier.keys()) == expected_models
    assert set(record.cost_usd.keys()) == expected_models
    # Mutation-gate hardening: timestamp/topic_label/css must be carried
    # through verbatim, not silently dropped/nulled.
    assert record.timestamp == "2026-01-01T00:00:00"
    assert record.topic_label == "my topic"
    assert record.css == 0.62


@settings(max_examples=50, derandomize=True, deadline=500)
@given(
    model_names=st.lists(
        st.text(alphabet=st.characters(whitelist_categories=("Ll",)), min_size=1, max_size=8),
        min_size=1,
        max_size=6,
        unique=True,
    )
)
def test_ac1_property_every_session_result_model_survives_into_record(model_names):
    session_result = {
        "css": 0.5,
        "rubric_scores": {m: _rubric(3.0) for m in model_names},
        "ranks": {m: i + 1 for i, m in enumerate(model_names)},
        "is_outlier": {m: False for m in model_names},
        "cost_usd": {m: 0.0 for m in model_names},
    }

    record = build_scorecard_record(session_result, topic_label="t", timestamp="ts")

    assert set(record.rubric_scores.keys()) == set(model_names)
    assert set(record.ranks.keys()) == set(model_names)
    assert set(record.is_outlier.keys()) == set(model_names)
    assert set(record.cost_usd.keys()) == set(model_names)


# ---------------------------------------------------------------------------
# AC2: Given default_scorecard_path(cwd) is called, When executed, Then it
# returns cwd/council-runs/scorecard.jsonl -- never any path under
# Path.home().
# ---------------------------------------------------------------------------


def test_ac2_default_scorecard_path_is_folder_scoped_never_under_home(tmp_path):
    result = default_scorecard_path(tmp_path)

    assert result == tmp_path / "council-runs" / "scorecard.jsonl"
    assert Path.home() not in result.parents


# ---------------------------------------------------------------------------
# AC3: Given append_record is called twice against the same path, When the
# file is read back, Then it contains exactly 2 JSON lines, each a valid,
# independently-parseable JSON object.
# ---------------------------------------------------------------------------


def test_ac3_append_record_twice_yields_two_independently_parseable_json_lines(tmp_path):
    path = tmp_path / "council-runs" / "scorecard.jsonl"
    path.parent.mkdir(parents=True)

    append_record(_make_record(timestamp="t1"), path)
    append_record(_make_record(timestamp="t2"), path)

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["timestamp"] == "t1"
    assert parsed[1]["timestamp"] == "t2"


@settings(max_examples=25, derandomize=True, deadline=1000)
@given(n=st.integers(min_value=1, max_value=8))
def test_ac3_property_append_then_load_round_trips_all_records_in_order(tmp_path_factory, n):
    path = tmp_path_factory.mktemp("sc") / "scorecard.jsonl"
    for i in range(n):
        append_record(_make_record(timestamp=f"t{i}"), path)

    loaded = load_records(path)

    assert len(loaded) == n
    assert [r.timestamp for r in loaded] == [f"t{i}" for i in range(n)]


# ---------------------------------------------------------------------------
# AC4: Given load_records(path, cross_folder=False), When called from a
# directory containing other council-runs/ folders elsewhere, Then only the
# exact path given is read -- no implicit aggregation.
# ---------------------------------------------------------------------------


def test_ac4_load_records_reads_only_exact_path_no_implicit_aggregation(tmp_path):
    path_a = tmp_path / "proj_a" / "council-runs" / "scorecard.jsonl"
    path_b = tmp_path / "proj_b" / "council-runs" / "scorecard.jsonl"
    path_a.parent.mkdir(parents=True)
    path_b.parent.mkdir(parents=True)

    append_record(_make_record(timestamp="a1"), path_a)
    append_record(_make_record(timestamp="a2"), path_a)
    append_record(_make_record(timestamp="b1"), path_b)
    append_record(_make_record(timestamp="b2"), path_b)
    append_record(_make_record(timestamp="b3"), path_b)

    loaded = load_records(path_a, cross_folder=False)

    assert len(loaded) == 2
    assert {r.timestamp for r in loaded} == {"a1", "a2"}


# ---------------------------------------------------------------------------
# AC5: Given exactly 9, 10, 19, 20, 49, and 50 records, When confidence_tier
# is called on each count, Then it returns
# insufficient/preliminary/preliminary/moderate/moderate/high respectively.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n, expected",
    [
        (9, "insufficient"),
        (10, "preliminary"),
        (19, "preliminary"),
        (20, "moderate"),
        (49, "moderate"),
        (50, "high"),
    ],
)
def test_ac5_confidence_tier_boundaries_exact(n, expected):
    assert confidence_tier(n) == expected


_TIER_RANK = {"insufficient": 0, "preliminary": 1, "moderate": 2, "high": 3}


@settings(max_examples=50, derandomize=True, deadline=500)
@given(n1=st.integers(min_value=0, max_value=200), n2=st.integers(min_value=0, max_value=200))
def test_ac5_property_confidence_tier_is_monotonic_non_decreasing(n1, n2):
    lo, hi = sorted((n1, n2))
    assert _TIER_RANK[confidence_tier(lo)] <= _TIER_RANK[confidence_tier(hi)]


# ---------------------------------------------------------------------------
# AC6: Given a target model was is_outlier=True in some records, When
# compute_report runs, Then outlier_sessions lists exactly those records'
# timestamp+topic_label, never auto-excluded from the averages.
# ---------------------------------------------------------------------------


def test_ac6_outlier_sessions_lists_exactly_flagged_records():
    r1 = _make_record(timestamp="s1", topic="topic-1", outlier_model="alpha")
    r2 = _make_record(timestamp="s2", topic="topic-2", outlier_model=None)
    r3 = _make_record(timestamp="s3", topic="topic-3", outlier_model="alpha")

    report = compute_report([r1, r2, r3], target_model="alpha")

    assert set(report.outlier_sessions) == {("s1", "topic-1"), ("s3", "topic-3")}
    assert report.session_count == 3


def test_ac6_outlier_flagged_record_still_contributes_to_the_average():
    # alpha always scores strictly higher than beta on every dimension;
    # alpha is flagged as an outlier in session s1. If outlier sessions were
    # silently excluded from averaging, session_count/the diff would not
    # reflect both sessions.
    r1 = ScorecardRecord(
        timestamp="s1",
        topic_label="t1",
        css=0.8,
        rubric_scores={"alpha": _rubric(5.0), "beta": _rubric(1.0)},
        ranks={"alpha": 1, "beta": 2},
        is_outlier={"alpha": True, "beta": False},
        cost_usd={"alpha": 0.01, "beta": 0.01},
    )
    r2 = ScorecardRecord(
        timestamp="s2",
        topic_label="t2",
        css=0.8,
        rubric_scores={"alpha": _rubric(5.0), "beta": _rubric(1.0)},
        ranks={"alpha": 1, "beta": 2},
        is_outlier={"alpha": False, "beta": False},
        cost_usd={"alpha": 0.01, "beta": 0.01},
    )

    report = compute_report([r1, r2], target_model="alpha")

    assert report.session_count == 2
    for dim in RUBRIC_DIMS:
        assert report.model_avg_vs_others[dim] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# AC7: Given render_report is called, When the output is inspected, Then it
# contains no keep/drop/recommend/should-remove language of any kind.
# ---------------------------------------------------------------------------


FORBIDDEN_TERMS = ("keep", "drop", "recommend", "should-remove", "should remove")


def test_ac7_render_report_contains_no_prescriptive_language():
    r1 = _make_record(timestamp="s1", topic="t1", outlier_model="alpha")
    report = compute_report([r1], target_model="alpha")

    output = render_report(report, "alpha")

    lowered = output.lower()
    for term in FORBIDDEN_TERMS:
        assert term not in lowered, f"found forbidden prescriptive term: {term!r}"


# ---------------------------------------------------------------------------
# AC8: Given zero records exist, When render_report runs, Then it returns a
# clear "no sessions recorded yet" message -- not a crash, not a fabricated
# N=0 average line.
# ---------------------------------------------------------------------------


def test_ac8_zero_records_renders_clear_message_not_a_crash():
    report = compute_report([], target_model="alpha")

    output = render_report(report, "alpha")

    assert "no sessions recorded yet" in output.lower()
    assert "nan" not in output.lower()


# ---------------------------------------------------------------------------
# Mutation-gate hardening: append_record's own mkdir(parents=True) must
# create a MULTI-LEVEL missing directory chain (AC3's own test pre-creates
# the parent, which leaves append_record's own mkdir call under-exercised),
# and must write UTF-8 -- not the platform-default encoding -- so non-ASCII
# content round-trips.
# ---------------------------------------------------------------------------


def test_append_record_creates_a_multi_level_missing_directory_chain(tmp_path):
    path = tmp_path / "a" / "b" / "c" / "council-runs" / "scorecard.jsonl"
    assert not path.parent.exists()

    append_record(_make_record(timestamp="t1"), path)

    assert path.exists()
    assert json.loads(path.read_text().splitlines()[0])["timestamp"] == "t1"


def test_append_record_and_load_records_round_trip_non_ascii_utf8_content(tmp_path):
    path = tmp_path / "council-runs" / "scorecard.jsonl"
    record = _make_record(timestamp="t1", topic="Ünïcödé tòpìc — 日本語")

    append_record(record, path)
    loaded = load_records(path)

    assert loaded[0].topic_label == "Ünïcödé tòpìc — 日本語"


def test_append_record_and_load_records_open_files_with_explicit_utf8_encoding(
    tmp_path, monkeypatch
):
    # A content round-trip alone can't distinguish an explicit "utf-8" from
    # an omitted/None encoding on a system whose locale already defaults to
    # UTF-8 (true of this environment). Spy on Path.open instead and pin the
    # `encoding` kwarg each call site actually passes -- catches
    # encoding=None and a dropped encoding kwarg regardless of locale.
    seen_encodings = []
    real_open = Path.open

    def spy_open(self, *args, **kwargs):
        seen_encodings.append(kwargs.get("encoding"))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_open)

    path = tmp_path / "council-runs" / "scorecard.jsonl"
    append_record(_make_record(timestamp="t1"), path)
    load_records(path)

    assert len(seen_encodings) >= 2
    assert all(
        enc is not None and enc.lower().replace("-", "").replace("_", "") == "utf8"
        for enc in seen_encodings
    )


# ---------------------------------------------------------------------------
# Mutation-gate hardening: load_records' cross_folder=True path -- currently
# untested entirely -- must (a) raise ValueError when search_root is None,
# (b) actually aggregate every council-runs/scorecard.jsonl found under
# search_root, (c) skip a listed-but-nonexistent path without aborting the
# scan of the remaining paths, and (d) skip a blank line without aborting
# the scan of the remaining lines in that file.
# ---------------------------------------------------------------------------


def test_load_records_cross_folder_true_without_search_root_raises_value_error(tmp_path):
    path = tmp_path / "council-runs" / "scorecard.jsonl"
    try:
        load_records(path, cross_folder=True, search_root=None)
        assert False, "expected ValueError"
    except ValueError as exc:
        # Exact text, not a substring check -- a wrapped/relettered message
        # ("XX...XX", wrong case) must fail this, not silently pass.
        assert str(exc) == "cross_folder=True requires search_root"


def test_load_records_skips_a_path_removed_after_listing_but_still_reads_the_rest(
    tmp_path, monkeypatch
):
    # `search_root.rglob(...)` only ever returns paths that exist at scan
    # time, so a real "listed-but-now-missing" file can only be forced via
    # the filesystem race it defends against -- simulate that race directly
    # by monkeypatching rglob to also yield a path that was never created.
    # `break` instead of `continue` here would silently drop every path
    # discovered after the missing one.
    ghost_path = tmp_path / "aaa_ghost" / "council-runs" / "scorecard.jsonl"  # sorts first, never created
    real_path = tmp_path / "zzz_real" / "council-runs" / "scorecard.jsonl"
    append_record(_make_record(timestamp="present1"), real_path)

    def fake_rglob(self, pattern):
        assert pattern == "council-runs/scorecard.jsonl"
        return iter([ghost_path, real_path])

    monkeypatch.setattr(Path, "rglob", fake_rglob)

    loaded = load_records(real_path, cross_folder=True, search_root=tmp_path)

    assert [r.timestamp for r in loaded] == ["present1"]


def test_load_records_cross_folder_true_aggregates_every_folder_under_search_root(tmp_path):
    path_a = tmp_path / "proj_a" / "council-runs" / "scorecard.jsonl"
    path_b = tmp_path / "proj_b" / "council-runs" / "scorecard.jsonl"

    append_record(_make_record(timestamp="a1"), path_a)
    append_record(_make_record(timestamp="b1"), path_b)
    append_record(_make_record(timestamp="b2"), path_b)

    loaded = load_records(path_a, cross_folder=True, search_root=tmp_path)

    assert {r.timestamp for r in loaded} == {"a1", "b1", "b2"}


def test_load_records_skips_a_missing_path_but_still_reads_the_rest(tmp_path):
    # Exercises the `if not p.exists(): continue` branch with more than one
    # candidate path -- `break` instead of `continue` would silently drop
    # every path discovered after the missing one.
    missing_dir = tmp_path / "missing_proj"
    present_dir = tmp_path / "present_proj"
    present_path = present_dir / "council-runs" / "scorecard.jsonl"
    append_record(_make_record(timestamp="present1"), present_path)

    # rglob only finds paths that exist, so force the "missing path" case by
    # calling load_records directly against a path list scenario: a search
    # root containing one real project and one empty (no scorecard.jsonl)
    # project directory alongside it.
    (missing_dir / "council-runs").mkdir(parents=True)

    loaded = load_records(present_path, cross_folder=True, search_root=tmp_path)

    assert {r.timestamp for r in loaded} == {"present1"}


def test_load_records_skips_a_blank_line_but_still_reads_lines_after_it(tmp_path):
    path = tmp_path / "council-runs" / "scorecard.jsonl"
    path.parent.mkdir(parents=True)
    r1 = json.dumps({
        "timestamp": "t1", "topic_label": "x", "css": 0.5,
        "rubric_scores": {}, "ranks": {}, "is_outlier": {}, "cost_usd": {},
    })
    r2 = json.dumps({
        "timestamp": "t2", "topic_label": "x", "css": 0.5,
        "rubric_scores": {}, "ranks": {}, "is_outlier": {}, "cost_usd": {},
    })
    # A blank line in the MIDDLE of the file -- `break` instead of
    # `continue` would silently drop every record after it (t2).
    path.write_text(r1 + "\n\n" + r2 + "\n")

    loaded = load_records(path)

    assert [r.timestamp for r in loaded] == ["t1", "t2"]


# ---------------------------------------------------------------------------
# Mutation-gate hardening: compute_report -- a full golden-value test
# pinning session_count, tier, model_avg_vs_others (division not
# multiplication, correct defaults for a model missing from a session
# entirely and for a model missing just one dimension), outlier_sessions
# (is_outlier key genuinely absent, not just False), and cost_share_pct
# (division direction and the exact percentage).
# ---------------------------------------------------------------------------


def test_compute_report_full_golden_values_across_all_fields():
    r1 = ScorecardRecord(
        timestamp="s1",
        topic_label="t1",
        css=0.8,
        rubric_scores={
            "alpha": {"accuracy": 4.0, "clarity": 2.0},
            "beta": {"accuracy": 2.0, "clarity": 6.0},
        },
        ranks={"alpha": 1, "beta": 2},
        is_outlier={"alpha": True, "beta": False},
        cost_usd={"alpha": 1.0, "beta": 3.0},
    )
    r2 = ScorecardRecord(
        timestamp="s2",
        topic_label="t2",
        css=0.8,
        # alpha is entirely absent from this session's rubric_scores/is_outlier
        # (not merely False) -- must default safely, not KeyError/crash.
        rubric_scores={"beta": {"accuracy": 6.0, "clarity": 4.0}},
        ranks={"beta": 1},
        is_outlier={"beta": False},
        cost_usd={"beta": 1.0},
    )

    report = compute_report([r1, r2], target_model="alpha")

    assert report.session_count == 2
    assert report.tier == confidence_tier(2)
    assert report.tier == "insufficient"

    # accuracy: alpha avg = 4.0 (only s1); beta avg over both sessions =
    # (2.0 + 6.0) / 2 = 4.0 -> diff = 0.0
    assert report.model_avg_vs_others["accuracy"] == pytest.approx(0.0)
    # clarity: alpha avg = 2.0 (only s1); beta avg = (6.0 + 4.0) / 2 = 5.0
    # -> diff = -3.0 (division, not multiplication, of the "others" average)
    assert report.model_avg_vs_others["clarity"] == pytest.approx(-3.0)

    assert report.outlier_sessions == [("s1", "t1")]

    # total cost = 1.0 + 3.0 + 1.0 = 5.0; alpha's share = 1.0 / 5.0 * 100 = 20.0
    assert report.cost_share_pct == pytest.approx(20.0)


def test_compute_report_zero_total_cost_yields_zero_cost_share_not_crash():
    r1 = _make_record(models=("alpha",))
    r1.cost_usd = {"alpha": 0.0}

    report = compute_report([r1], target_model="alpha")

    assert report.cost_share_pct == 0.0


def test_compute_report_others_average_divides_not_multiplies_when_two_others_present():
    # A single session with the target model plus TWO other models: with
    # len(others) == 1 division and multiplication coincide, so this needs
    # >= 2 "other" models in the same session to distinguish `/` from `*`.
    r1 = ScorecardRecord(
        timestamp="s1", topic_label="t1", css=0.8,
        rubric_scores={
            "alpha": {"accuracy": 10.0},
            "beta": {"accuracy": 4.0},
            "gamma": {"accuracy": 4.0},
        },
        ranks={"alpha": 1, "beta": 2, "gamma": 3},
        is_outlier={"alpha": False, "beta": False, "gamma": False},
        cost_usd={"alpha": 0.0, "beta": 0.0, "gamma": 0.0},
    )

    report = compute_report([r1], target_model="alpha")

    # others = [4.0, 4.0]; correct avg = 8/2 = 4.0 -> diff = 10-4 = 6.0.
    # A `*` mutant would instead compute 4*4=16.0 for the "other_session_avg"
    # summed into other_session_avgs=[16.0] -> others_avg=16.0 -> diff=-6.0.
    assert report.model_avg_vs_others["accuracy"] == pytest.approx(6.0)


def test_compute_report_target_absent_from_every_session_defaults_to_zero_not_one():
    r1 = ScorecardRecord(
        timestamp="s1", topic_label="t1", css=0.8,
        rubric_scores={"beta": {"accuracy": 3.0}},
        ranks={"beta": 1}, is_outlier={"beta": False}, cost_usd={"beta": 0.0},
    )

    report = compute_report([r1], target_model="ghost")

    # target_scores is empty for every dim -> target_avg must default to
    # 0.0, not a fabricated 1.0. diff = 0.0 - 3.0 = -3.0.
    assert report.model_avg_vs_others["accuracy"] == pytest.approx(-3.0)


def test_compute_report_no_other_models_present_defaults_others_avg_to_zero_not_one():
    r1 = ScorecardRecord(
        timestamp="s1", topic_label="t1", css=0.8,
        rubric_scores={"alpha": {"accuracy": 3.0}},
        ranks={"alpha": 1}, is_outlier={"alpha": False}, cost_usd={"alpha": 0.0},
    )

    report = compute_report([r1], target_model="alpha")

    # other_session_avgs is empty (no other models anywhere) -> others_avg
    # must default to 0.0, not a fabricated 1.0. diff = 3.0 - 0.0 = 3.0.
    assert report.model_avg_vs_others["accuracy"] == pytest.approx(3.0)


def test_compute_report_cost_share_computed_for_total_cost_strictly_between_zero_and_one():
    # Pins the `total_cost > 0` boundary specifically: total_cost=0.5 must
    # still take the computed branch, not the `else 0.0` fallback that a
    # `total_cost > 1` mutant would wrongly take.
    r1 = ScorecardRecord(
        timestamp="s1", topic_label="t1", css=0.8,
        rubric_scores={"alpha": {"accuracy": 1.0}}, ranks={"alpha": 1},
        is_outlier={"alpha": False}, cost_usd={"alpha": 0.5},
    )

    report = compute_report([r1], target_model="alpha")

    assert report.cost_share_pct == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Mutation-gate hardening: render_report's exact template for both the
# with-rubric-data/with-outliers branch and the no-rubric-data/no-outliers
# branch, including the sign boundary at exactly 0.0 (">= 0" must render
# "+0.000", not "" -- ">" would render the zero case unsigned).
# ---------------------------------------------------------------------------


def test_render_report_exact_content_with_data_and_outliers():
    report = ScorecardReport(
        session_count=12,
        tier="preliminary",
        model_avg_vs_others={"accuracy": 0.0, "clarity": -1.5},
        outlier_sessions=[("s1", "topic-1")],
        cost_share_pct=33.333,
    )

    output = render_report(report, "alpha")

    expected = "\n".join(
        [
            "Scorecard for alpha",
            "Sessions recorded: 12 (confidence tier: preliminary)",
            "",
            "Average rubric score vs. mean of other models:",
            "  accuracy: +0.000",
            "  clarity: -1.500",
            "",
            "Cost share: 33.3% of total recorded council spend",
            "",
            "Sessions where this model was flagged as an outlier (for manual review):",
            "  - s1 — topic-1",
        ]
    )
    assert output == expected


def test_render_report_zero_sessions_message_exact_text_and_case():
    report = compute_report([], target_model="alpha")

    # Exact equality, not a lower-cased substring check -- a mutant that
    # wraps or relowers the whole message must fail this.
    assert render_report(report, "alpha") == "No sessions recorded yet."


def test_render_report_exact_content_with_no_rubric_data_and_no_outliers():
    report = ScorecardReport(
        session_count=1,
        tier="insufficient",
        model_avg_vs_others={},
        outlier_sessions=[],
        cost_share_pct=0.0,
    )

    output = render_report(report, "beta")

    expected = "\n".join(
        [
            "Scorecard for beta",
            "Sessions recorded: 1 (confidence tier: insufficient)",
            "",
            "Average rubric score vs. mean of other models:",
            "  (no rubric data)",
            "",
            "Cost share: 0.0% of total recorded council spend",
            "",
            "No outlier-flagged sessions.",
        ]
    )
    assert output == expected


# ---------------------------------------------------------------------------
# Mutation-gate hardening: the `scorecard report` CLI entry point (main())
# had zero test coverage. Cover the default path, an explicit --path, the
# --cross-folder flag (both the success case and the --cross-folder-without
# ---search-root argparse error), and that it prints via render_report.
# ---------------------------------------------------------------------------


def _run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["scorecard"] + argv)
    return sc.main()


def test_main_reports_against_default_scorecard_path_in_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "council-runs" / "scorecard.jsonl"
    append_record(_make_record(timestamp="t1", models=("alpha",)), path)

    _run_main(monkeypatch, ["--target-model", "alpha"])

    out = capsys.readouterr().out
    assert "Scorecard for alpha" in out
    assert "Sessions recorded: 1" in out


def test_main_respects_explicit_path_argument(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    custom_path = tmp_path / "elsewhere" / "scorecard.jsonl"
    append_record(_make_record(timestamp="t1", models=("alpha",)), custom_path)

    _run_main(monkeypatch, ["--target-model", "alpha", "--path", str(custom_path)])

    out = capsys.readouterr().out
    assert "Sessions recorded: 1" in out


def test_main_cross_folder_without_search_root_raises(monkeypatch):
    # Current main() does not pre-validate at the argparse level; it passes
    # --cross-folder straight through to load_records, which is the thing
    # that actually enforces search_root is required.
    monkeypatch.setattr(sys, "argv", ["scorecard", "--target-model", "alpha", "--cross-folder"])
    try:
        sc.main()
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "search_root" in str(exc)


def test_main_cross_folder_aggregates_across_search_root(tmp_path, monkeypatch, capsys):
    path_a = tmp_path / "proj_a" / "council-runs" / "scorecard.jsonl"
    path_b = tmp_path / "proj_b" / "council-runs" / "scorecard.jsonl"
    append_record(_make_record(timestamp="a1", models=("alpha",)), path_a)
    append_record(_make_record(timestamp="b1", models=("alpha",)), path_b)

    _run_main(
        monkeypatch,
        [
            "--target-model", "alpha",
            "--cross-folder",
            "--search-root", str(tmp_path),
            "--path", str(path_a),
        ],
    )

    out = capsys.readouterr().out
    assert "Sessions recorded: 2" in out


def test_main_zero_sessions_prints_no_sessions_message(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    _run_main(monkeypatch, ["--target-model", "nobody"])

    out = capsys.readouterr().out
    assert "no sessions recorded yet" in out.lower()


# ---------------------------------------------------------------------------
# Mutation-gate hardening: --target-model must actually be a *required*
# argparse argument (a `required=None`/`required=False` mutant would let the
# CLI silently proceed instead of erroring), and the parser's own --help
# text -- prog name, description, and per-flag help strings -- is itself
# user-facing output, not just internal wiring, so it is pinned exactly.
# ---------------------------------------------------------------------------


def test_main_missing_required_target_model_is_an_argparse_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["scorecard"])
    with pytest.raises(SystemExit) as exc_info:
        sc.main()
    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "--target-model" in err


def test_main_help_text_exact_prog_description_and_flag_help(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "200")
    # argv[0] deliberately differs from "scorecard": ArgumentParser's own
    # default `prog` (used whenever `prog=` is omitted or explicitly None)
    # is `os.path.basename(sys.argv[0])`. If argv[0] were "scorecard" this
    # test could never distinguish an explicit prog="scorecard" from a
    # dropped/None prog -- both would coincidentally print the same usage
    # line.
    monkeypatch.setattr(sys, "argv", ["/usr/bin/not-scorecard-at-all", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        sc.main()
    assert exc_info.value.code == 0

    out = capsys.readouterr().out
    lines = out.splitlines()

    # prog: usage line must read exactly "usage: scorecard [-h] ..." -- not
    # the derived-from-argv[0] "usage: not-scorecard-at-all ...".
    assert lines[0] == (
        "usage: scorecard [-h] --target-model TARGET_MODEL [--path PATH] "
        "[--cross-folder] [--search-root SEARCH_ROOT] [--show-audition]"
    )
    assert "not-scorecard-at-all" not in out

    # description and every per-flag help string: exact full lines, not
    # substring checks -- a "XX...XX"-wrapped or re-cased mutant contains
    # the original text as a substring and would slip past `in`.
    assert "Report confidence-gated LLM council scorecard statistics." in lines
    assert "                        Model name to report statistics for." in lines
    assert (
        "  --path PATH           Path to scorecard.jsonl "
        "(default: <cwd>/council-runs/scorecard.jsonl)."
        in lines
    )
    assert (
        "  --cross-folder        Aggregate scorecard.jsonl files found under --search-root."
        in lines
    )
    assert (
        "                        Root directory to walk when --cross-folder is set." in lines
    )


def test_main_passes_the_actual_target_model_into_compute_report_not_none(
    tmp_path, monkeypatch, capsys
):
    # Regression for a `compute_report(records, None)` wiring mutant: if the
    # CLI's own --target-model were dropped before reaching compute_report,
    # "alpha" (present in every record) would be treated as an "other"
    # model instead of the target, changing the rendered diff sign/value.
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "council-runs" / "scorecard.jsonl"
    r1 = ScorecardRecord(
        timestamp="s1", topic_label="t1", css=0.8,
        rubric_scores={"alpha": {"accuracy": 9.0}, "beta": {"accuracy": 1.0}},
        ranks={"alpha": 1, "beta": 2},
        is_outlier={"alpha": False, "beta": False},
        cost_usd={"alpha": 0.0, "beta": 0.0},
    )
    append_record(r1, path)

    _run_main(monkeypatch, ["--target-model", "alpha"])

    out = capsys.readouterr().out
    assert "  accuracy: +8.000" in out
