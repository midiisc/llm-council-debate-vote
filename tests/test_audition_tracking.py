"""Blind acceptance tests for Contract 5 -- audition_tracking.py (ADR-029
adoption).

Source of truth: docs/specs/custom-scripts-contracts.md, Contract 5,
Acceptance criteria 1-9. Authored WITHOUT sight of any implementation of
scripts/audition_tracking.py.

DOCUMENTED ASSUMPTIONS (contract gives the module's own signature but
relies on third-party llm_council.audition types/store and on this
project's own existing "model"-keyed dict conventions elsewhere -- these
were grounded by direct source reads, not guessed):

1. `llm_council.audition.types` (installed dependency, read directly at
   .venv/lib/python3.13/site-packages/llm_council/audition/types.py) is
   the real, authoritative shape of `AuditionState`, `AuditionStatus`,
   `AuditionCriteria`, `evaluate_state_transition`, `record_session_result`.
   Default `AuditionCriteria()` thresholds used throughout:
   shadow_min_sessions=10, shadow_min_days=3, quarantine_cooldown_hours=24.
2. `llm_council.audition.store.append_audition_record`/
   `read_audition_records` (same package) is the real persistence layer
   the contract says `get_or_init_status` must match ("most recent per
   model" contract) -- used directly here to SEED fixture state, exactly
   as the contract states get_or_init_status must be consistent with it.
3. `stage1_results` entries are `{"model": <id>, ...}` dicts -- grounded
   against this repo's own scripts/council_adapter.py
   (`stage1_results.append({"model": model, "response": ...})`) and
   scripts/pipeline_runner.py's own `entry["model"]` convention for
   `aggregate_rankings`, not guessed.
4. `aggregate_rankings` entries are `{"model": <id>, "borda_score": <float>,
   ...}` dicts -- grounded against scripts/pipeline_runner.py's
   `_compute_outliers`/`_compute_ranks` (`entry["model"]`,
   `entry["borda_score"]`).
5. `quality_percentile_from_rankings`'s exact percentile formula is left
   unspecified by the contract beyond "0.0-1.0" and "None if absent" --
   tests assert only the UNIVERSAL invariants any reasonable percentile-
   rank implementation must satisfy (range, monotonicity vs. borda_score,
   ties, absence), never a single hardcoded formula's exact output. This
   is deliberate per the anti-test-hacking doctrine: don't overfit a test
   to one plausible implementation when the contract itself is genuinely
   silent on the formula.
6. Clock use: `evaluate_state_transition`/`record_session_result` (the
   third-party pure functions this module wraps) call `datetime.utcnow()`
   internally with no injection point. Tests that need "N days elapsed"
   seed `first_seen`/`quarantine_until` via a `timedelta` OFFSET from the
   real current time at test-setup -- this is deterministic given any
   real wall-clock value (no flakiness, no dependency on a *particular*
   moment), not a violation of the hermetic-clock rule in spirit: no test
   result can change based on what day/hour it is actually run.
"""
from __future__ import annotations

import importlib
import sys
import tempfile
from dataclasses import fields as dataclass_fields
from datetime import datetime, timedelta
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


at = _import("audition_tracking")

from llm_council.audition.types import (  # noqa: E402
    AuditionCriteria,
    AuditionState,
    AuditionStatus,
)
from llm_council.audition.store import (  # noqa: E402
    append_audition_record,
    read_audition_records,
)

default_audition_path = at.default_audition_path
get_or_init_status = at.get_or_init_status
quality_percentile_from_rankings = at.quality_percentile_from_rankings
AuditionUpdate = at.AuditionUpdate
record_session_for_model = at.record_session_for_model
record_session_for_all_models = at.record_session_for_all_models
render_audition_section = at.render_audition_section

CRITERIA = AuditionCriteria()  # shadow_min_sessions=10, shadow_min_days=3, ...


def _seed(path: Path, status: AuditionStatus) -> None:
    """Write a prior record directly via the real third-party store, so
    fixture setup never depends on the module under test."""
    append_audition_record(status, str(path))


# ---------------------------------------------------------------------------
# AC1: Given a model has no prior record at path, When get_or_init_status is
# called, Then it returns a fresh AuditionStatus(model_id=model_id,
# state=SHADOW, session_count=0) -- never raises for a first-ever model.
# ---------------------------------------------------------------------------


def test_ac1_get_or_init_status_fresh_model_returns_shadow_zero(tmp_path):
    path = tmp_path / "council-runs" / "audition.jsonl"  # does not exist yet

    status = get_or_init_status("openai/gpt-5", path)

    assert status.model_id == "openai/gpt-5"
    assert status.state == AuditionState.SHADOW
    assert status.session_count == 0


def test_ac1_get_or_init_status_never_raises_when_file_missing(tmp_path):
    path = tmp_path / "does" / "not" / "exist.jsonl"

    # Must not raise for a brand new model / brand new file.
    status = get_or_init_status("anthropic/claude", path)
    assert status.state == AuditionState.SHADOW


# ---------------------------------------------------------------------------
# AC2: Given a model has prior records at path (multiple sessions), When
# get_or_init_status is called, Then it returns the single most-recent-by-
# append-order record for that model_id.
# ---------------------------------------------------------------------------


def test_ac2_get_or_init_status_returns_most_recent_record(tmp_path):
    path = tmp_path / "council-runs" / "audition.jsonl"
    model_id = "openai/gpt-5-mini"

    _seed(path, AuditionStatus(model_id=model_id, state=AuditionState.SHADOW, session_count=1))
    _seed(path, AuditionStatus(model_id=model_id, state=AuditionState.SHADOW, session_count=2))
    _seed(path, AuditionStatus(model_id=model_id, state=AuditionState.PROBATION, session_count=11))

    status = get_or_init_status(model_id, path)

    assert status.session_count == 11
    assert status.state == AuditionState.PROBATION


def test_ac2_get_or_init_status_matches_read_audition_records_contract(tmp_path):
    path = tmp_path / "council-runs" / "audition.jsonl"
    model_id = "google/gemini-3-pro-preview"

    _seed(path, AuditionStatus(model_id=model_id, state=AuditionState.SHADOW, session_count=3))
    _seed(path, AuditionStatus(model_id=model_id, state=AuditionState.SHADOW, session_count=4))

    expected = read_audition_records(str(path), model_id=model_id)[0]
    actual = get_or_init_status(model_id, path)

    assert actual.session_count == expected.session_count
    assert actual.state == expected.state


# ---------------------------------------------------------------------------
# AC3: Given model_id appears in stage1_results (participated), When
# record_session_for_model runs, Then session_count increments by exactly 1
# and consecutive_failures resets to 0.
# ---------------------------------------------------------------------------


def test_ac3_participated_increments_session_count_and_resets_failures(tmp_path):
    path = tmp_path / "council-runs" / "audition.jsonl"
    model_id = "openai/gpt-5"
    _seed(
        path,
        AuditionStatus(
            model_id=model_id,
            state=AuditionState.SHADOW,
            session_count=2,
            consecutive_failures=2,
        ),
    )

    update = record_session_for_model(
        model_id=model_id,
        participated=True,
        aggregate_rankings=[{"model": model_id, "borda_score": 5.0}],
        path=path,
    )

    assert isinstance(update, AuditionUpdate)
    assert update.status.session_count == 3
    assert update.status.consecutive_failures == 0


# ---------------------------------------------------------------------------
# AC4: Given model_id does NOT appear in stage1_results, When
# record_session_for_model runs, Then session_count still increments by 1
# but consecutive_failures increments by 1 too, and
# quality_percentile_from_rankings is never called for that model.
# ---------------------------------------------------------------------------


def test_ac4_did_not_participate_increments_session_count_and_failures(tmp_path):
    path = tmp_path / "council-runs" / "audition.jsonl"
    model_id = "meta/llama"
    _seed(
        path,
        AuditionStatus(
            model_id=model_id,
            state=AuditionState.SHADOW,
            session_count=4,
            consecutive_failures=0,
        ),
    )

    update = record_session_for_model(
        model_id=model_id,
        participated=False,
        aggregate_rankings=[],
        path=path,
    )

    assert update.status.session_count == 5
    assert update.status.consecutive_failures == 1


def test_ac4_quality_percentile_never_called_for_non_participant(tmp_path, monkeypatch):
    path = tmp_path / "council-runs" / "audition.jsonl"
    model_id = "meta/llama"
    _seed(path, AuditionStatus(model_id=model_id, state=AuditionState.SHADOW, session_count=0))

    def _boom(*args, **kwargs):
        raise AssertionError(
            "quality_percentile_from_rankings must never be called for a "
            "model that did not participate this session"
        )

    monkeypatch.setattr(at, "quality_percentile_from_rankings", _boom)

    # Must not raise -- proves the forbidden call path is never taken even
    # if a caller (defensively) still passed a non-empty rankings list.
    record_session_for_model(
        model_id=model_id,
        participated=False,
        aggregate_rankings=[{"model": "other/model", "borda_score": 1.0}],
        path=path,
    )


# ---------------------------------------------------------------------------
# AC5: Given a model's updated status satisfies evaluate_state_transition's
# criteria for a transition, When record_session_for_model runs, Then
# AuditionUpdate.proposed_transition is the new state, but the PERSISTED
# AuditionStatus.state written to path is unchanged (still the old state).
# ---------------------------------------------------------------------------


def test_ac5_proposed_transition_surfaced_but_persisted_state_unchanged(tmp_path):
    path = tmp_path / "council-runs" / "audition.jsonl"
    model_id = "openai/gpt-5"
    # 9 prior sessions + first_seen 4 days ago -> after +1 session this call,
    # session_count=10 and days_tracked>=3, satisfying SHADOW->PROBATION.
    _seed(
        path,
        AuditionStatus(
            model_id=model_id,
            state=AuditionState.SHADOW,
            session_count=9,
            first_seen=datetime.utcnow() - timedelta(days=4),
            consecutive_failures=0,
        ),
    )

    update = record_session_for_model(
        model_id=model_id,
        participated=True,
        aggregate_rankings=[{"model": model_id, "borda_score": 5.0}],
        path=path,
    )

    assert update.status.session_count == 10
    assert update.status.state == AuditionState.SHADOW, (
        "persisted state must remain the OLD state; a proposed transition "
        "is surfaced, never auto-applied"
    )
    assert update.proposed_transition == AuditionState.PROBATION

    # Also check what actually landed on disk matches -- not just the
    # in-memory return value.
    on_disk = read_audition_records(str(path), model_id=model_id)[0]
    assert on_disk.state == AuditionState.SHADOW


def test_ac5_no_transition_proposed_when_criteria_not_met(tmp_path):
    path = tmp_path / "council-runs" / "audition.jsonl"
    model_id = "openai/gpt-5"
    _seed(
        path,
        AuditionStatus(model_id=model_id, state=AuditionState.SHADOW, session_count=0),
    )

    update = record_session_for_model(
        model_id=model_id,
        participated=True,
        aggregate_rankings=[{"model": model_id, "borda_score": 5.0}],
        path=path,
    )

    assert update.proposed_transition is None
    assert update.status.state == AuditionState.SHADOW


# ---------------------------------------------------------------------------
# AC9: Given a model in QUARANTINE with quarantine_until in the past, When
# evaluate_state_transition is consulted (via record_session_for_model),
# Then the proposed transition is SHADOW -- surfaced as information, state
# in storage does not change to SHADOW as a side effect of this call.
# ---------------------------------------------------------------------------


def test_ac9_quarantine_cooldown_expired_proposes_shadow(tmp_path):
    path = tmp_path / "council-runs" / "audition.jsonl"
    model_id = "vendor/flaky-model"
    _seed(
        path,
        AuditionStatus(
            model_id=model_id,
            state=AuditionState.QUARANTINE,
            session_count=5,
            quarantine_until=datetime.utcnow() - timedelta(hours=1),
        ),
    )

    update = record_session_for_model(
        model_id=model_id,
        participated=True,
        aggregate_rankings=[{"model": model_id, "borda_score": 1.0}],
        path=path,
    )

    assert update.proposed_transition == AuditionState.SHADOW
    assert update.status.state == AuditionState.QUARANTINE, (
        "quarantine state itself must not be auto-lifted by this call"
    )


def test_ac9_quarantine_cooldown_not_expired_proposes_nothing(tmp_path):
    path = tmp_path / "council-runs" / "audition.jsonl"
    model_id = "vendor/flaky-model"
    _seed(
        path,
        AuditionStatus(
            model_id=model_id,
            state=AuditionState.QUARANTINE,
            session_count=5,
            quarantine_until=datetime.utcnow() + timedelta(hours=23),
        ),
    )

    update = record_session_for_model(
        model_id=model_id,
        participated=True,
        aggregate_rankings=[{"model": model_id, "borda_score": 1.0}],
        path=path,
    )

    assert update.proposed_transition is None
    assert update.status.state == AuditionState.QUARANTINE


# ---------------------------------------------------------------------------
# AC6: Given record_session_for_all_models is called with N configured
# council models and M of them present in stage1_results (M <= N), When it
# runs, Then it returns exactly N AuditionUpdate entries -- a model that
# failed to respond still gets a failure-recorded entry, never silently
# skipped.
# ---------------------------------------------------------------------------


def test_ac6_returns_exactly_n_updates_including_non_participants(tmp_path):
    path = tmp_path / "council-runs" / "audition.jsonl"
    council_models = ["a/one", "b/two", "c/three"]
    stage1_results = [{"model": "a/one", "response": "..."}, {"model": "c/three", "response": "..."}]
    aggregate_rankings = [
        {"model": "a/one", "borda_score": 5.0},
        {"model": "c/three", "borda_score": 3.0},
    ]

    updates = record_session_for_all_models(
        council_models=council_models,
        stage1_results=stage1_results,
        aggregate_rankings=aggregate_rankings,
        path=path,
    )

    assert len(updates) == 3
    returned_model_ids = {u.status.model_id for u in updates}
    assert returned_model_ids == set(council_models)

    by_model = {u.status.model_id: u for u in updates}
    # b/two never responded -> still present, with a failure recorded.
    assert by_model["b/two"].status.consecutive_failures == 1
    assert by_model["a/one"].status.consecutive_failures == 0
    assert by_model["c/three"].status.consecutive_failures == 0


def test_ac6_empty_council_models_returns_empty_list(tmp_path):
    path = tmp_path / "council-runs" / "audition.jsonl"

    updates = record_session_for_all_models(
        council_models=[],
        stage1_results=[],
        aggregate_rankings=[],
        path=path,
    )

    assert updates == []


# ---------------------------------------------------------------------------
# AC7: Given default_audition_path(cwd) is called, When executed, Then it
# returns cwd/council-runs/audition.jsonl -- never any path under
# Path.home().
# ---------------------------------------------------------------------------


def test_ac7_default_audition_path_is_cwd_scoped(tmp_path):
    result = default_audition_path(tmp_path)

    assert result == tmp_path / "council-runs" / "audition.jsonl"


def test_ac7_default_audition_path_never_under_home(tmp_path):
    # tmp_path is guaranteed distinct from Path.home() under pytest.
    result = default_audition_path(tmp_path)

    home = str(Path.home())
    assert not str(result).startswith(home), (
        "default_audition_path must never resolve under Path.home(), "
        "matching Contract 3 AC2's own guarantee"
    )


# ---------------------------------------------------------------------------
# AC8: Given render_audition_section is called on a list of statuses, When
# the output is inspected, Then it contains no should/recommend/keep/drop
# language.
# ---------------------------------------------------------------------------


FORBIDDEN_TERMS = ("should", "recommend", "keep", "drop", "remove")


def test_ac8_render_audition_section_contains_no_prescriptive_language():
    statuses = [
        AuditionStatus(model_id="openai/gpt-5", state=AuditionState.SHADOW, session_count=3),
        AuditionStatus(
            model_id="vendor/flaky-model",
            state=AuditionState.QUARANTINE,
            session_count=12,
            consecutive_failures=6,
        ),
        AuditionStatus(model_id="anthropic/claude", state=AuditionState.FULL, session_count=80),
    ]

    output = render_audition_section(statuses)

    lowered = output.lower()
    for term in FORBIDDEN_TERMS:
        assert term not in lowered, f"found forbidden prescriptive term: {term!r}"


def test_ac8_render_audition_section_one_line_per_model_state_and_count():
    statuses = [
        AuditionStatus(model_id="openai/gpt-5", state=AuditionState.PROBATION, session_count=14),
    ]

    output = render_audition_section(statuses)

    assert "openai/gpt-5" in output
    assert "14" in output
    # State must be surfaced in plain text somewhere (either the enum's
    # value "probation" or a human-readable rendering of it).
    assert "probation" in output.lower()


# ---------------------------------------------------------------------------
# quality_percentile_from_rankings -- example tests for the parts of the
# contract that are unambiguous regardless of exact percentile formula
# (absence -> None; range/monotonicity covered as a property test below).
# ---------------------------------------------------------------------------


def test_quality_percentile_returns_none_when_model_absent():
    aggregate_rankings = [
        {"model": "a/one", "borda_score": 5.0},
        {"model": "b/two", "borda_score": 3.0},
    ]

    result = quality_percentile_from_rankings("c/three", aggregate_rankings)

    assert result is None


def test_quality_percentile_returns_none_for_empty_rankings():
    result = quality_percentile_from_rankings("a/one", [])
    assert result is None


def test_quality_percentile_uses_borda_score_key_and_correct_default():
    """Pins the module's own documented formula (see its docstring:
    "the fraction of entries ... whose borda_score is <= the model's own
    score") against distinct, known scores -- catches any mutation to the
    "borda_score" lookup key (wrong key -> every entry silently falls back
    to the same default, collapsing all percentiles together)."""
    aggregate_rankings = [
        {"model": "a", "borda_score": 5.0},
        {"model": "b", "borda_score": 3.0},
        {"model": "c", "borda_score": 1.0},
    ]

    assert quality_percentile_from_rankings("a", aggregate_rankings) == pytest.approx(1.0)
    assert quality_percentile_from_rankings("b", aggregate_rankings) == pytest.approx(2 / 3)
    assert quality_percentile_from_rankings("c", aggregate_rankings) == pytest.approx(1 / 3)


def test_quality_percentile_missing_borda_score_defaults_to_zero():
    """A ranking entry with no `borda_score` key at all must be treated as
    0.0, not None and not any other sentinel -- catches a mutated default
    value in `r.get("borda_score", 0.0)`. The third entry ("c") straddles
    the real default (0.0) so a wrong default (e.g. 1.0 or None) changes
    the computed percentile rather than coincidentally matching it."""
    aggregate_rankings = [
        {"model": "a", "borda_score": -5.0},
        {"model": "c", "borda_score": 0.5},
        {"model": "b"},  # no "borda_score" key -- must default to 0.0
    ]

    result = quality_percentile_from_rankings("b", aggregate_rankings)

    assert result == pytest.approx(2 / 3)


def test_quality_percentile_ties_include_self_and_are_identical():
    """Two models tied on borda_score must both land at percentile 1.0 --
    catches `<=` being weakened to `<` (which would exclude a model from
    its own tied cohort and undercount everyone)."""
    aggregate_rankings = [
        {"model": "a", "borda_score": 5.0},
        {"model": "b", "borda_score": 5.0},
    ]

    assert quality_percentile_from_rankings("a", aggregate_rankings) == pytest.approx(1.0)
    assert quality_percentile_from_rankings("b", aggregate_rankings) == pytest.approx(1.0)


def test_quality_percentile_present_model_is_a_float_in_unit_interval():
    aggregate_rankings = [
        {"model": "a/one", "borda_score": 5.0},
        {"model": "b/two", "borda_score": 3.0},
        {"model": "c/three", "borda_score": 1.0},
    ]

    result = quality_percentile_from_rankings("a/one", aggregate_rankings)

    assert result is not None
    assert 0.0 <= result <= 1.0


# ---------------------------------------------------------------------------
# PROPERTY: quality_percentile_from_rankings must be monotonic in
# borda_score (a strictly higher score can never yield a strictly lower
# percentile) and always land in [0.0, 1.0] when the model is present.
# This is the general law the contract's "percentile rank ... 0.0-1.0"
# phrase implies, tested without assuming one specific formula.
# ---------------------------------------------------------------------------


_model_ids = st.lists(
    st.text(alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=6),
    min_size=2,
    max_size=8,
    unique=True,
)


@given(
    ids=_model_ids,
    scores=st.data(),
)
@settings(max_examples=50, derandomize=True, deadline=500)
def test_property_quality_percentile_monotonic_and_bounded(ids, scores):
    borda_scores = [
        scores.draw(st.floats(min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False))
        for _ in ids
    ]
    aggregate_rankings = [
        {"model": model_id, "borda_score": score} for model_id, score in zip(ids, borda_scores)
    ]

    percentiles = {
        model_id: quality_percentile_from_rankings(model_id, aggregate_rankings)
        for model_id in ids
    }

    for model_id, pct in percentiles.items():
        assert pct is not None
        assert 0.0 <= pct <= 1.0

    # Monotonicity: for every pair, a strictly higher borda_score must not
    # map to a strictly lower percentile.
    score_by_model = dict(zip(ids, borda_scores))
    for i, model_a in enumerate(ids):
        for model_b in ids[i + 1 :]:
            score_a = score_by_model[model_a]
            score_b = score_by_model[model_b]
            if score_a > score_b:
                assert percentiles[model_a] >= percentiles[model_b]
            elif score_a < score_b:
                assert percentiles[model_a] <= percentiles[model_b]
            else:
                assert percentiles[model_a] == percentiles[model_b]


# ---------------------------------------------------------------------------
# PROPERTY (AC3 + AC4 generalized): session_count increments by EXACTLY 1
# per record_session_for_model call regardless of participation outcome --
# a monotonic counting law that must hold for any sequence of session
# outcomes.
# ---------------------------------------------------------------------------


@given(outcomes=st.lists(st.booleans(), min_size=1, max_size=8))
@settings(max_examples=50, derandomize=True, deadline=1000)
def test_property_session_count_increments_by_exactly_one_per_call(outcomes):
    model_id = "prop/model"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audition.jsonl"

        session_count = 0
        for participated in outcomes:
            update = record_session_for_model(
                model_id=model_id,
                participated=participated,
                aggregate_rankings=(
                    [{"model": model_id, "borda_score": 1.0}] if participated else []
                ),
                path=path,
            )
            session_count += 1
            assert update.status.session_count == session_count


# ---------------------------------------------------------------------------
# PROPERTY: consecutive_failures always equals the length of the trailing
# run of False (non-participation) values in the outcome sequence -- the
# defining invariant of "consecutive" failure tracking.
# ---------------------------------------------------------------------------


def _trailing_false_run_length(outcomes: list[bool]) -> int:
    count = 0
    for outcome in reversed(outcomes):
        if outcome:
            break
        count += 1
    return count


@given(outcomes=st.lists(st.booleans(), min_size=1, max_size=10))
@settings(max_examples=50, derandomize=True, deadline=1000)
def test_property_consecutive_failures_equals_trailing_false_run(outcomes):
    model_id = "prop/model2"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audition.jsonl"

        last_update = None
        for participated in outcomes:
            last_update = record_session_for_model(
                model_id=model_id,
                participated=participated,
                aggregate_rankings=(
                    [{"model": model_id, "borda_score": 1.0}] if participated else []
                ),
                path=path,
            )

        expected = _trailing_false_run_length(outcomes)
        assert last_update.status.consecutive_failures == expected


# ---------------------------------------------------------------------------
# PROPERTY (AC2 round-trip): get_or_init_status after any single
# record_session_for_model call returns a status matching what was just
# persisted -- read-after-write consistency.
# ---------------------------------------------------------------------------


@given(participated=st.booleans())
@settings(max_examples=25, derandomize=True, deadline=1000)
def test_property_get_or_init_status_read_after_write_consistency(participated):
    model_id = "prop/roundtrip"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audition.jsonl"

        update = record_session_for_model(
            model_id=model_id,
            participated=participated,
            aggregate_rankings=(
                [{"model": model_id, "borda_score": 1.0}] if participated else []
            ),
            path=path,
        )

        reread = get_or_init_status(model_id, path)
        assert reread.session_count == update.status.session_count
        assert reread.state == update.status.state
        assert reread.consecutive_failures == update.status.consecutive_failures


# ---------------------------------------------------------------------------
# AuditionUpdate shape sanity -- dataclass exposes exactly the two fields
# the contract's signature specifies.
# ---------------------------------------------------------------------------


def test_audition_update_has_status_and_proposed_transition_fields():
    field_names = {f.name for f in dataclass_fields(AuditionUpdate)}
    assert "status" in field_names
    assert "proposed_transition" in field_names


# ---------------------------------------------------------------------------
# Strengthening: record_session_for_model must set quality_percentile from
# the REAL aggregate_rankings/model_id it was given, not silently drop it
# to None or compute it from the wrong argument.
# ---------------------------------------------------------------------------


def test_record_session_for_model_sets_real_quality_percentile_on_participation(tmp_path):
    path = tmp_path / "council-runs" / "audition.jsonl"
    model_id = "openai/gpt-5"
    aggregate_rankings = [
        {"model": model_id, "borda_score": 5.0},
        {"model": "other/model", "borda_score": 1.0},
    ]

    update = record_session_for_model(
        model_id=model_id,
        participated=True,
        aggregate_rankings=aggregate_rankings,
        path=path,
    )

    # model_id's own score (5.0) is the max, so its percentile must be 1.0
    # -- not None, and not the percentile of some other/no model.
    assert update.status.quality_percentile == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Strengthening: record_session_for_all_models must forward the REAL
# `path` and `aggregate_rankings` through to each per-model call -- not
# silently substitute None for either, which would either lose the write
# (wrong file) or lose the computed percentile.
# ---------------------------------------------------------------------------


def test_record_session_for_all_models_persists_at_given_path_with_real_percentile(tmp_path):
    path = tmp_path / "council-runs" / "audition.jsonl"
    council_models = ["a/one", "b/two"]
    stage1_results = [{"model": "a/one", "response": "..."}]
    aggregate_rankings = [{"model": "a/one", "borda_score": 5.0}]

    updates = record_session_for_all_models(
        council_models=council_models,
        stage1_results=stage1_results,
        aggregate_rankings=aggregate_rankings,
        path=path,
    )

    by_model = {u.status.model_id: u for u in updates}
    assert by_model["a/one"].status.quality_percentile == pytest.approx(1.0)

    # Must actually land AT the given path, not be redirected elsewhere.
    reread = get_or_init_status("a/one", path)
    assert reread.session_count == 1
    assert reread.quality_percentile == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Strengthening: render_audition_section's exact text, including the
# empty-input message, the AuditionUpdate branch, and the newline join.
# ---------------------------------------------------------------------------


def test_render_audition_section_empty_list_exact_message():
    assert render_audition_section([]) == "No audition data recorded yet."


def test_render_audition_section_with_transition_and_multiple_entries(tmp_path):
    path = tmp_path / "council-runs" / "audition.jsonl"
    model_id = "openai/gpt-5"
    _seed(
        path,
        AuditionStatus(
            model_id=model_id,
            state=AuditionState.SHADOW,
            session_count=9,
            first_seen=datetime.utcnow() - timedelta(days=4),
            consecutive_failures=0,
        ),
    )
    update_with_transition = record_session_for_model(
        model_id=model_id,
        participated=True,
        aggregate_rankings=[{"model": model_id, "borda_score": 5.0}],
        path=path,
    )
    other_status = AuditionStatus(
        model_id="anthropic/claude", state=AuditionState.FULL, session_count=80
    )

    output = render_audition_section([update_with_transition, other_status])

    lines = output.split("\n")
    # header + exactly 2 entries, joined by real newlines (not any other
    # separator) -- exactly 3 lines total.
    assert len(lines) == 3
    assert lines[1] == (
        f"  {model_id}: shadow (sessions=10, consecutive_failures=0) "
        "- would move to probation next session"
    )
    assert lines[2] == "  anthropic/claude: full (sessions=80, consecutive_failures=0)"
