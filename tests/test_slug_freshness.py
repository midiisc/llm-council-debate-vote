"""Blind acceptance tests for `docs/specs/slug-freshness-precheck-contract.md`
("Daily slug-freshness precheck contract (Pillar 2 -- spec before code)")
-- `scripts/slug_freshness.py`'s `check_slug_freshness` coroutine (AC1-10).

Authored WITHOUT sight of any implementation, design reasoning, or other
agent's work -- ONLY the contract markdown above was read. The import path
below (`scripts.slug_freshness`, with a bare `slug_freshness` fallback)
mirrors the exact pattern every other blind test file in this repo already
uses (see `tests/test_resilient_query.py`, `tests/test_council_adapter.py`)
for resolving `scripts.<module>` package members. If the module is absent
this file fails at collection/import time (RED) -- the correct, expected
blind-TDV state (see `docs/anti-test-hacking.md` / CLAUDE.md Pillar 3): a
missing module is a feature-missing failure, not a typo/import-path bug.

DOCUMENTED ASSUMPTIONS (the contract pins the exact dataclass shapes and
async signature verbatim, but a few wiring/construction details are left
to a reasonable, standard default -- called out here rather than silently
baked in):

  1. **Dataclass construction / access.** `FreshnessResult` is a plain
     `@dataclass` per the contract's literal code block. Tests construct
     via keyword args only where needed and read fields via plain
     attribute access -- the contract shows no alternate accessor.

  2. **`fetch_fn` call signature.** The contract pins
     `FetchModelsFn = Callable[[], Awaitable[list[str]]]` -- a zero-arg
     async callable returning the list of live model ids. Fakes below
     never receive arguments and record only that they were called (call
     count), since the contract gives no per-call input to record.

  3. **`check_slug_freshness` is a coroutine function** ("-> Awaitable
     [FreshnessResult]"). Tests run it via `asyncio.run(...)`, matching
     the exact hermetic-async pattern this repo already established in
     `tests/test_resilient_query.py` (no pytest-asyncio dependency
     installed in this environment).

  4. **Cache file format / config_hash algorithm is NOT pinned precisely
     enough to hand-compute.** The contract only says `"<sha256 of
     sorted(configured_slugs) joined>"` -- the join separator and
     encoding are unstated implementation choices. Tests therefore never
     hand-compute a hash string to pre-seed a "matching" cache and assert
     a resulting cache-hit against it -- doing so would silently pin an
     unstated implementation detail and could produce a false RED against
     a contractually-valid implementation that picks a different
     separator. Instead, every test that needs a genuinely *matching*
     same-day cache **bootstraps it via a real prior call to
     `check_slug_freshness` itself** (round-trip style: call once to let
     the module write its own real cache, then make behavioral
     assertions about the next call). Tests that only need a cache with a
     *mismatching* hash (AC4) or an *irrelevant* hash (AC3, where the date
     alone must force a re-fetch regardless of hash; AC8's second test,
     where the pre-existing file must simply survive untouched) may still
     hand-write a cache file directly, since correctness there does not
     depend on the pre-seeded hash coincidentally matching the real
     algorithm (a wrong-on-purpose or don't-care hash still exercises the
     intended branch). The one hand-written exception that IS safe to
     compute is the empty-list case (`sorted([])` joined by any separator
     is always `""`, so `sha256("".encode()).hexdigest()` is separator-
     independent) -- used for AC10's cache-hit-on-empty-roster test.

  5. **`cache_path`'s parent directory** is created by the test fixture
     (a fresh tmp_path per test) before calling `check_slug_freshness` --
     the contract doesn't say whether the module creates missing parent
     directories, and testing that would bake in an unstated detail.
     Every test passes a `cache_path` whose immediate parent already
     exists (matching the contract's own example,
     `./council-runs/.slug_freshness_cache.json`, where `council-runs/`
     is expected to already exist).

  6. **AC8 "no cache file is written" on `fetch_fn` failure** is tested
     both for the fresh-cache case (no pre-existing file -> still absent
     after the call) and cross-checked against AC9 (a *write* failure
     after a *successful* fetch is a different failure mode -- whether a
     cache file existed beforehand is irrelevant to AC8; AC8 is purely
     about "did a new/overwritten file appear as a side effect of a fetch
     exception").

  7. **AC9 "cache write fails"** is simulated by pointing `cache_path` at
     a location that cannot be written (a directory, or a file inside a
     read-only directory) rather than mocking internal I/O calls --
     keeping the test purely black-box/behavioral. Two concrete
     simulations are used for robustness across sandboxes: (a)
     `cache_path` itself IS an existing directory (so opening it for
     writing raises IsADirectoryError on POSIX) -- the primary, portable
     evidence; (b) `cache_path`'s parent directory is chmod'd read-only --
     supplementary, skipped when running as root (permissions aren't
     enforced) or on platforms without `os.geteuid`.

Hermetic: no real network, no real `datetime.now()` (the contract's own
`today` parameter is caller-supplied precisely to keep this pure/
testable -- tests always pass an explicit literal date string), no real
filesystem outside pytest's `tmp_path` fixture.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import stat
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


sf = _import("slug_freshness")


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _make_fetch_fn(live_ids: list[str] | None = None, exc: Exception | None = None):
    """Build a fake FetchModelsFn. If `exc` is given, the fetch raises it
    (after recording the call) instead of returning `live_ids`."""
    calls = {"count": 0}

    async def fetch_fn() -> list[str]:
        calls["count"] += 1
        if exc is not None:
            raise exc
        return list(live_ids or [])

    return fetch_fn, calls


def _empty_list_hash() -> str:
    # Separator-independent: joining zero elements is "" no matter what
    # separator an implementation picks. Safe to hand-compute (see
    # assumption #4).
    return hashlib.sha256("".encode()).hexdigest()


def _write_cache_raw(path: Path, date: str, config_hash: str, missing_slugs: list[str]) -> None:
    path.write_text(
        json.dumps({"date": date, "config_hash": config_hash, "missing_slugs": missing_slugs})
    )


TODAY = "2026-08-13"
YESTERDAY = "2026-08-12"


# ---------------------------------------------------------------------------
# AC1 -- no cache file exists -> fetch called once, missing_slugs computed,
# cache written with today's date + config hash, cache_hit=False
# ---------------------------------------------------------------------------


def test_ac1_no_cache_file_fetches_once_and_writes_cache(tmp_path):
    cache_path = tmp_path / "cache.json"
    assert not cache_path.exists()
    slugs = ["openai/gpt-5", "moonshotai/kimi-k3", "z-ai/glm-5.2"]
    fetch_fn, calls = _make_fetch_fn(live_ids=["openai/gpt-5", "moonshotai/kimi-k3"])

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert calls["count"] == 1
    assert result.missing_slugs == ["z-ai/glm-5.2"]
    assert result.cache_hit is False
    assert cache_path.exists()
    written = json.loads(cache_path.read_text())
    assert written["date"] == TODAY
    assert isinstance(written["config_hash"], str) and written["config_hash"]
    assert written["missing_slugs"] == ["z-ai/glm-5.2"]


def test_config_hash_is_literally_sha256_of_sorted_slugs_joined_with_no_separator(tmp_path):
    """Revisits assumption #4's deliberate hedge: the contract's own wording
    -- `"<sha256 of sorted(configured_slugs) joined>"` -- names no
    separator at all, and "joined" with no separator specified is the
    standard way to describe plain concatenation (`"".join(...)`, not
    `", ".join(...)` or any other punctuated join, which the contract
    would need to spell out explicitly to mean). This is pinned literally,
    with 3+ slugs (so a wrong separator provably changes the digest,
    unlike the 0/1-element cases used elsewhere): a cache file carrying
    exactly `sha256("".join(sorted(configured_slugs)).encode()).hexdigest()`
    for today must be recognized as a genuine same-day match (cache_hit,
    no re-fetch) -- not treated as a coincidental mismatch."""
    cache_path = tmp_path / "cache.json"
    slugs = ["c/three", "a/one", "b/two"]
    literal_hash = hashlib.sha256("".join(sorted(slugs)).encode("utf-8")).hexdigest()
    _write_cache_raw(cache_path, TODAY, literal_hash, [])
    fetch_fn, calls = _make_fetch_fn(live_ids=["definitely/would-be-missing-if-refetched"])

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert calls["count"] == 0  # recognized as a genuine cache hit, no re-fetch
    assert result.cache_hit is True
    assert result.missing_slugs == []


# ---------------------------------------------------------------------------
# AC2 -- cache exists, date == today, config_hash matches, force=False
# -> fetch NOT called, cached missing_slugs returned verbatim, cache_hit=True
#
# Bootstrapped via a real first call (see assumption #4) rather than a
# hand-computed hash, so this test is agnostic to the exact join/encoding
# scheme the implementation picks for config_hash.
# ---------------------------------------------------------------------------


def test_ac2_matching_same_day_cache_short_circuits_fetch(tmp_path):
    cache_path = tmp_path / "cache.json"
    slugs = ["openai/gpt-5", "anthropic/claude-5"]
    fetch_fn, calls = _make_fetch_fn(live_ids=["openai/gpt-5"])  # claude-5 missing

    first = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs, cache_path=cache_path, fetch_fn=fetch_fn, today=TODAY
        )
    )
    assert calls["count"] == 1
    assert first.cache_hit is False
    assert cache_path.exists()

    second = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
            force=False,
        )
    )

    assert calls["count"] == 1  # NOT called again -- served from cache
    assert second.cache_hit is True
    assert second.missing_slugs == first.missing_slugs  # returned verbatim
    # The cache-hit path must apply the SAME warning contract as the fetch
    # path (AC6): a non-empty cached missing_slugs must still yield a
    # non-None warning naming it, not a silently-dropped/nulled warning
    # just because the result came from cache instead of a live fetch.
    assert first.missing_slugs  # sanity: bootstrap actually has drift to carry
    assert second.warning is not None
    assert "anthropic/claude-5" in second.warning


def test_ac2_cache_hit_with_missing_slugs_key_absent_defaults_to_empty(tmp_path):
    """A cache dict matching date+config_hash but missing the
    'missing_slugs' key entirely (e.g. an older/malformed cache format)
    must be handled gracefully -- defaulting to "no known drift" -- rather
    than crashing on `list(None)`. Bootstrapped via a real prior call (per
    assumption #4) so config_hash is guaranteed genuinely matching, then
    the cache file is rewritten dropping just that one key."""
    cache_path = tmp_path / "cache.json"
    slugs = ["openai/gpt-5"]
    fetch_fn, calls = _make_fetch_fn(live_ids=["openai/gpt-5"])
    _run(
        sf.check_slug_freshness(
            configured_slugs=slugs, cache_path=cache_path, fetch_fn=fetch_fn, today=TODAY
        )
    )
    assert calls["count"] == 1
    written = json.loads(cache_path.read_text())
    del written["missing_slugs"]
    cache_path.write_text(json.dumps(written))

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert calls["count"] == 1  # still a cache hit -- no re-fetch
    assert result.cache_hit is True
    assert result.missing_slugs == []
    assert result.warning is None


# ---------------------------------------------------------------------------
# AC3 -- cache exists with date != today -> fetch IS called (stale-by-date
# cache never short-circuits), cache overwritten with today's date
# ---------------------------------------------------------------------------


def test_ac3_stale_date_cache_forces_fetch_and_overwrites(tmp_path):
    cache_path = tmp_path / "cache.json"
    slugs = ["openai/gpt-5", "moonshotai/kimi-k3"]
    # Hash value is irrelevant here -- date mismatch alone must force a
    # fetch regardless of whether the hash would otherwise match.
    _write_cache_raw(cache_path, YESTERDAY, "irrelevant-hash-value", ["moonshotai/kimi-k3"])
    # Live catalog has changed since yesterday: now both are live.
    fetch_fn, calls = _make_fetch_fn(live_ids=["openai/gpt-5", "moonshotai/kimi-k3"])

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert calls["count"] == 1
    assert result.missing_slugs == []
    written = json.loads(cache_path.read_text())
    assert written["date"] == TODAY


# ---------------------------------------------------------------------------
# AC4 -- cache exists, date == today, but config_hash does NOT match current
# configured_slugs -> fetch IS called (config change invalidates same-day cache)
# ---------------------------------------------------------------------------


def test_ac4_same_day_but_config_hash_mismatch_forces_fetch(tmp_path):
    cache_path = tmp_path / "cache.json"
    new_slugs = ["openai/gpt-5", "moonshotai/kimi-k3"]  # user edited config intraday
    # A hash that provably cannot equal the real hash of new_slugs under any
    # reasonable scheme (wrong length / not hex) -- guaranteed mismatch.
    _write_cache_raw(cache_path, TODAY, "definitely-not-a-real-sha256-digest", [])
    fetch_fn, calls = _make_fetch_fn(live_ids=["openai/gpt-5"])  # kimi-k3 now dead

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=new_slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert calls["count"] == 1
    assert result.missing_slugs == ["moonshotai/kimi-k3"]
    assert result.cache_hit is False


# ---------------------------------------------------------------------------
# AC5 -- force=True always calls fetch_fn regardless of any matching cache
#
# Bootstrapped to a genuinely matching cache first (see assumption #4), so
# this proves force=True bypasses a REAL matching cache, not merely an
# accidentally-mismatched hand-written one.
# ---------------------------------------------------------------------------


def test_ac5_force_true_always_fetches_even_with_matching_cache(tmp_path):
    cache_path = tmp_path / "cache.json"
    slugs = ["openai/gpt-5", "anthropic/claude-5"]
    fetch_fn1, calls1 = _make_fetch_fn(live_ids=["openai/gpt-5", "anthropic/claude-5"])
    bootstrap = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs, cache_path=cache_path, fetch_fn=fetch_fn1, today=TODAY
        )
    )
    assert calls1["count"] == 1
    assert bootstrap.missing_slugs == []  # genuinely matching, valid cache now on disk

    fetch_fn2, calls2 = _make_fetch_fn(live_ids=["openai/gpt-5"])  # claude-5 now dead
    result = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn2,
            today=TODAY,
            force=True,
        )
    )

    assert calls2["count"] == 1  # force=True bypassed the matching cache
    assert result.missing_slugs == ["anthropic/claude-5"]


# ---------------------------------------------------------------------------
# AC6 -- fetch_fn returns a live-id list missing one or more configured_slugs
# -> missing_slugs contains exactly those absent slugs, in original input
# order, and warning is a non-None string naming every one by exact slug string
# ---------------------------------------------------------------------------


def test_ac6_missing_slugs_preserve_input_order_and_warning_names_each(tmp_path):
    cache_path = tmp_path / "cache.json"
    slugs = ["a/one", "b/two", "c/three", "d/four"]
    fetch_fn, _ = _make_fetch_fn(live_ids=["a/one", "c/three"])  # b/two, d/four dead

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert result.missing_slugs == ["b/two", "d/four"]  # original input order
    assert result.warning is not None
    assert "b/two" in result.warning
    assert "d/four" in result.warning


def test_ac6_warning_names_every_missing_slug_even_with_many_missing(tmp_path):
    """Directly encodes the contract's "not a count, not truncated" wording:
    every one of a large batch of missing slugs must appear verbatim in
    `warning`, not summarized as e.g. "12 models missing"."""
    cache_path = tmp_path / "cache.json"
    slugs = [f"vendor/model-{i}" for i in range(12)]
    fetch_fn, _ = _make_fetch_fn(live_ids=[])  # all 12 missing

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert result.missing_slugs == slugs
    assert result.warning is not None
    for s in slugs:
        assert s in result.warning


def test_ac6_warning_uses_comma_space_separator_between_multiple_missing_slugs(tmp_path):
    """AC6 pins the warning as "human-readable" (FreshnessResult's own field
    comment). Checking substring membership of each missing slug alone
    (as the tests above do) can't distinguish a correctly-punctuated
    sentence from one with each slug run together or joined by noise
    characters -- pin the exact ", " separator between two missing slugs
    so a garbled/unreadable join is caught."""
    cache_path = tmp_path / "cache.json"
    slugs = ["a/one", "b/two"]
    fetch_fn, _ = _make_fetch_fn(live_ids=[])  # both missing

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert result.warning is not None
    assert "a/one, b/two" in result.warning
    # And the message must open with the actual sentence prefix the
    # contract's own wording describes -- not e.g. an uppercased,
    # lowercased, or otherwise reworded variant that would still pass a
    # bare substring-of-slugs check.
    assert result.warning.startswith("Slug freshness check found configured slug(s)")


# ---------------------------------------------------------------------------
# AC7 -- every configured_slugs entry present in fetched live-id list ->
# missing_slugs == [] and warning is None
# ---------------------------------------------------------------------------


def test_ac7_all_slugs_live_yields_empty_missing_and_none_warning(tmp_path):
    cache_path = tmp_path / "cache.json"
    slugs = ["openai/gpt-5", "moonshotai/kimi-k3"]
    fetch_fn, _ = _make_fetch_fn(live_ids=["openai/gpt-5", "moonshotai/kimi-k3", "extra/model"])

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert result.missing_slugs == []
    assert result.warning is None


# ---------------------------------------------------------------------------
# AC8 -- fetch_fn raises -> exception caught, fetch_error set (human-readable),
# missing_slugs == [] (never conflated with real drift), cache_hit=False,
# no cache file written
# ---------------------------------------------------------------------------


def test_ac8_fetch_fn_raises_sets_fetch_error_and_writes_no_cache(tmp_path):
    cache_path = tmp_path / "cache.json"
    assert not cache_path.exists()
    slugs = ["openai/gpt-5"]
    fetch_fn, calls = _make_fetch_fn(exc=ConnectionError("catalog unreachable"))

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert calls["count"] == 1
    assert result.fetch_error is not None
    assert isinstance(result.fetch_error, str)
    # "human-readable" (per FreshnessResult's own field comment) means it
    # must name the ACTUAL exception that occurred, not a generic/blank
    # placeholder -- a caller triaging failures needs to distinguish a
    # ConnectionError from a TimeoutError from this string alone.
    assert "ConnectionError" in result.fetch_error
    assert "catalog unreachable" in result.fetch_error
    assert result.missing_slugs == []
    assert result.cache_hit is False
    assert not cache_path.exists()
    # checked_slugs is "every configured slug this check covers" (its own
    # field comment) regardless of path taken -- a fetch failure doesn't
    # exempt the exception branch from populating it correctly.
    assert result.checked_slugs == slugs


def test_ac8_fetch_fn_raises_does_not_overwrite_a_pre_existing_stale_cache(tmp_path):
    """A stale (yesterday's) cache existed before the failed check; per AC8
    a failed check must never poison state -- here read as: it must not
    silently leave behind a cache claiming today's date with a false result.
    The pre-existing file's content (from yesterday) is left untouched.
    Hash value is a don't-care here: correctness of "file left untouched"
    doesn't depend on whether it happens to match."""
    cache_path = tmp_path / "cache.json"
    _write_cache_raw(cache_path, YESTERDAY, "irrelevant-hash-value", [])
    original_bytes = cache_path.read_bytes()
    fetch_fn, _ = _make_fetch_fn(exc=TimeoutError("catalog timed out"))

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=["openai/gpt-5"],
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert result.fetch_error is not None
    assert result.missing_slugs == []
    # File is unchanged from before the failed check (no cache write occurred).
    assert cache_path.read_bytes() == original_bytes


def test_writes_cache_even_when_multiple_levels_of_cache_dir_are_missing(tmp_path):
    """Revisits assumption #5: that assumption only said tests wouldn't
    REQUIRE nested-parent auto-creation, not that the shipped behavior
    (when present) should go unverified. `cache_path`'s parent here is two
    levels deep and does not exist at all before the call -- the cache
    write must still succeed (this is the difference between a working
    precheck on a genuinely fresh checkout, where `council-runs/` may not
    exist yet, and one that silently no-ops on every run)."""
    cache_path = tmp_path / "council-runs" / "nested" / "cache.json"
    assert not cache_path.parent.exists()
    slugs = ["openai/gpt-5"]
    fetch_fn, calls = _make_fetch_fn(live_ids=["openai/gpt-5"])

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert calls["count"] == 1
    assert result.missing_slugs == []
    assert cache_path.exists()
    written = json.loads(cache_path.read_text())
    assert written["date"] == TODAY


# ---------------------------------------------------------------------------
# AC9 -- cache write fails after a successful live check -> the
# already-computed FreshnessResult is still returned (correct
# missing_slugs/warning); write failure must never invalidate the result
# ---------------------------------------------------------------------------


def test_ac9_cache_write_failure_still_returns_correct_result(tmp_path):
    # Point cache_path AT an existing directory so any attempt to open it
    # for writing as a file raises (IsADirectoryError on POSIX) -- a
    # purely black-box way to force a write failure without mocking
    # internals.
    cache_dir_as_path = tmp_path / "cache_target_is_a_dir"
    cache_dir_as_path.mkdir()
    slugs = ["openai/gpt-5", "moonshotai/kimi-k3"]
    fetch_fn, calls = _make_fetch_fn(live_ids=["openai/gpt-5"])  # kimi-k3 missing

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_dir_as_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert calls["count"] == 1
    # The check itself succeeded and computed the real drift, despite the
    # cache write being physically impossible.
    assert result.missing_slugs == ["moonshotai/kimi-k3"]
    assert result.warning is not None
    assert "moonshotai/kimi-k3" in result.warning
    assert result.fetch_error is None


@pytest.mark.skipif(
    os.geteuid() == 0 if hasattr(os, "geteuid") else True,
    reason="chmod-based write-protection is not enforced for root / not "
    "meaningful on this platform",
)
def test_ac9_cache_write_failure_via_readonly_parent_still_returns_result(tmp_path):
    """Supplementary AC9 evidence via a read-only parent directory, per
    assumption #7. Skipped when running as root (permissions are not
    enforced) or on platforms without geteuid, since it would otherwise be
    an unreliable false-positive-prone check rather than a real signal."""
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    cache_path = readonly_dir / "cache.json"
    slugs = ["openai/gpt-5"]
    fetch_fn, _ = _make_fetch_fn(live_ids=[])  # gpt-5 missing

    original_mode = readonly_dir.stat().st_mode
    readonly_dir.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        result = _run(
            sf.check_slug_freshness(
                configured_slugs=slugs,
                cache_path=cache_path,
                fetch_fn=fetch_fn,
                today=TODAY,
            )
        )
        assert result.missing_slugs == ["openai/gpt-5"]
        assert result.fetch_error is None
    finally:
        readonly_dir.chmod(original_mode)


# ---------------------------------------------------------------------------
# AC10 -- configured_slugs is empty -> fetch_fn still called (if cache
# doesn't already cover today), missing_slugs == [], warning is None
# ---------------------------------------------------------------------------


def test_ac10_empty_configured_slugs_still_fetches_when_no_cache(tmp_path):
    cache_path = tmp_path / "cache.json"
    fetch_fn, calls = _make_fetch_fn(live_ids=["openai/gpt-5"])

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=[],
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert calls["count"] == 1
    assert result.missing_slugs == []
    assert result.warning is None


def test_ac10_empty_configured_slugs_with_matching_same_day_cache_skips_fetch(tmp_path):
    # "still called if the cache doesn't already cover today" implies the
    # normal AC2 short-circuit rule applies unchanged to an empty roster --
    # an already-covered empty-roster day should NOT re-fetch. Safe to
    # hand-write here: sorted([]) joined is "" regardless of separator, so
    # this hash is provably correct under any reasonable scheme (assumption
    # #4's one safe exception).
    cache_path = tmp_path / "cache.json"
    _write_cache_raw(cache_path, TODAY, _empty_list_hash(), [])
    fetch_fn, calls = _make_fetch_fn(live_ids=["openai/gpt-5"])

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=[],
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert calls["count"] == 0
    assert result.cache_hit is True
    assert result.missing_slugs == []


# ---------------------------------------------------------------------------
# checked_slugs -- every configured slug this check covers, in input order
# (pinned by the FreshnessResult field comment; exercised across both the
# cache-hit and cache-miss paths since the contract doesn't say it's
# computed differently in either case)
# ---------------------------------------------------------------------------


def test_checked_slugs_matches_configured_slugs_in_order_on_fetch_path(tmp_path):
    cache_path = tmp_path / "cache.json"
    slugs = ["z/last", "a/first", "m/middle"]
    fetch_fn, _ = _make_fetch_fn(live_ids=slugs)

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert result.checked_slugs == slugs


def test_checked_slugs_matches_configured_slugs_in_order_on_cache_hit_path(tmp_path):
    cache_path = tmp_path / "cache.json"
    slugs = ["z/last", "a/first", "m/middle"]
    fetch_fn, calls = _make_fetch_fn(live_ids=slugs)
    _run(
        sf.check_slug_freshness(
            configured_slugs=slugs, cache_path=cache_path, fetch_fn=fetch_fn, today=TODAY
        )
    )
    assert calls["count"] == 1

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert calls["count"] == 1  # second call was a cache hit
    assert result.checked_slugs == slugs


# ---------------------------------------------------------------------------
# Property-based tests -- laws that hold across the whole input space,
# reached for FIRST per doctrine wherever a general law exists.
# ---------------------------------------------------------------------------


_slug_strategy = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Nd"), whitelist_characters="/-_."
    ),
    min_size=1,
    max_size=20,
).map(lambda s: s if "/" in s else f"vendor/{s}")

_slugs_list_strategy = st.lists(_slug_strategy, min_size=0, max_size=8, unique=True)


@given(configured=_slugs_list_strategy, live=_slugs_list_strategy)
@settings(max_examples=50, derandomize=True, deadline=None)
def test_property_missing_slugs_is_exactly_configured_minus_live_preserving_order(
    configured, live, tmp_path_factory
):
    """AC1/AC6/AC7 generalized as one law: for ANY configured/live
    combination on a fresh (no-cache) run, missing_slugs is exactly the
    subset of configured_slugs absent from the live set, in original
    configured_slugs order -- and warning's non-None-ness is exactly the
    inverse of "missing_slugs is empty" (AC6 vs AC7)."""
    tmp_path = tmp_path_factory.mktemp("prop")
    cache_path = tmp_path / "cache.json"
    fetch_fn, _ = _make_fetch_fn(live_ids=live)

    result = _run(
        sf.check_slug_freshness(
            configured_slugs=configured,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    live_set = set(live)
    expected_missing = [s for s in configured if s not in live_set]
    assert result.missing_slugs == expected_missing
    if expected_missing:
        assert result.warning is not None
        for s in expected_missing:
            assert s in result.warning
    else:
        assert result.warning is None


@given(slugs=_slugs_list_strategy)
@settings(max_examples=50, derandomize=True, deadline=None)
def test_property_cache_round_trip_is_idempotent_when_config_and_day_unchanged(
    slugs, tmp_path_factory
):
    """AC1 + AC2 generalized as a round-trip/idempotence law: running the
    check twice in a row with the same configured_slugs, same today, and
    fetch_fn returning the same live set produces the SAME missing_slugs
    both times, but the underlying live catalog is only actually fetched
    once -- the second call must be served entirely from the cache the
    first call wrote (fetch_fn call count stays at 1, cache_hit flips from
    False to True)."""
    tmp_path = tmp_path_factory.mktemp("prop2")
    cache_path = tmp_path / "cache.json"
    # Deterministically mark a stable subset of slugs "dead" based on the
    # slug's own text (not Python's randomized str hash()), so the live set
    # is a genuine, reproducible subset -- not always empty or full, and
    # not dependent on PYTHONHASHSEED.
    live = [s for s in slugs if len(s) % 2 == 0]
    fetch_fn, calls = _make_fetch_fn(live_ids=live)

    first = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )
    second = _run(
        sf.check_slug_freshness(
            configured_slugs=slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert calls["count"] == 1  # fetch only happened on the first call
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert first.missing_slugs == second.missing_slugs


@given(slugs=_slugs_list_strategy)
@settings(max_examples=50, derandomize=True, deadline=None)
def test_property_config_hash_is_order_and_duplicate_insensitive(slugs, tmp_path_factory):
    """The contract pins config_hash as 'sha256 of sorted(configured_slugs)
    joined' -- sorting is explicit, so shuffling the input list's order
    must still hit the SAME cache entry (a reordering-only edit to
    llm_council.yaml's model list must not spuriously invalidate the
    cache). Verified behaviorally, with NO hand-computed hash (see
    assumption #4): bootstrap a real cache via one genuine call, then query
    with a reversed ordering of the identical slug SET on the same day --
    must still be a cache hit, with no second fetch."""
    if len(slugs) < 2:
        return  # need at least 2 elements for a meaningful reordering
    tmp_path = tmp_path_factory.mktemp("prop3")
    cache_path = tmp_path / "cache.json"
    fetch_fn, calls = _make_fetch_fn(live_ids=slugs)

    _run(
        sf.check_slug_freshness(
            configured_slugs=slugs, cache_path=cache_path, fetch_fn=fetch_fn, today=TODAY
        )
    )
    assert calls["count"] == 1

    reversed_slugs = list(reversed(slugs))
    result = _run(
        sf.check_slug_freshness(
            configured_slugs=reversed_slugs,
            cache_path=cache_path,
            fetch_fn=fetch_fn,
            today=TODAY,
        )
    )

    assert calls["count"] == 1  # still just once -- reorder didn't bust cache
    assert result.cache_hit is True


# ---------------------------------------------------------------------------
# Mutation-gate hardening (2026-08-13) -- scoped mutmut run on this file
# surfaced that _read_cache/_write_cache's explicit encoding="utf-8" was
# unverified: no prior test distinguished it from an omitted/None encoding
# (which falls back to locale.getpreferredencoding(), a real cross-platform
# behavior change -- not every OS/locale defaults to UTF-8). These two tests
# spy on Path.open to assert the exact kwarg, closing that gap directly
# rather than relying on cache content (JSON's default ensure_ascii=True
# would make a content-based round-trip test blind to the encoding used).
# ---------------------------------------------------------------------------


def test_read_cache_opens_with_explicit_utf8_encoding(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps({"date": TODAY, "config_hash": "irrelevant", "missing_slugs": []})
    )

    captured_kwargs = {}
    real_open = Path.open

    def spy_open(self, *args, **kwargs):
        if self == cache_path:
            captured_kwargs.update(kwargs)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_open)

    sf._read_cache(cache_path)

    assert captured_kwargs.get("encoding") == "utf-8"


def test_write_cache_opens_with_explicit_utf8_encoding(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"

    captured_kwargs = {}
    real_open = Path.open

    def spy_open(self, *args, **kwargs):
        if self == cache_path:
            captured_kwargs.update(kwargs)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_open)

    sf._write_cache(cache_path, TODAY, "irrelevant", [])

    assert captured_kwargs.get("encoding") == "utf-8"
