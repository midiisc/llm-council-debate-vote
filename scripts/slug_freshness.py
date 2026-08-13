"""Daily slug-freshness precheck - confirms every configured OpenRouter
slug (core roster + backup pool) is still live before the first debate
call of the day, cached so the check runs at most once/day per config.

Guards against the dead-slug-burns-the-retry-budget failure mode: a dead
slug fails deterministically, so retrying it in resilient_query.py's
backoff loop can never succeed - this precheck exists so that's caught
before spending a real API call, not discovered the expensive way.

Contract: docs/specs/slug-freshness-precheck-contract.md.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

FetchModelsFn = Callable[[], Awaitable[list[str]]]
# Returns every live model id from OpenRouter's /api/v1/models (the "id"
# field of each entry in the response's "data" array). No API key required.
# Injected so tests never make a real network call.


def default_slug_freshness_cache_path(cwd: Path) -> Path:
    # docs/specs/pending-stage-wiring-contract.md, Contract 1: folder-scoped,
    # never ~/.llm-council/, matching default_scorecard_path/
    # default_audition_path's existing convention.
    return cwd / "council-runs" / "slug_freshness_cache.json"


@dataclass
class FreshnessResult:
    checked_slugs: list[str]  # every configured slug this check covers, in input order
    missing_slugs: list[str]  # configured slugs NOT found live, in input order (empty = all good)
    warning: Optional[str]  # human-readable, non-None iff missing_slugs is non-empty
    cache_hit: bool  # True if served from same-day/same-config cache, no fetch performed
    fetch_error: Optional[str]  # non-None iff fetch_fn raised - distinct from "checked, found missing"


def _config_hash(configured_slugs: list[str]) -> str:
    # Contract: "sha256 of sorted(configured_slugs) joined" - plain
    # concatenation of the sorted slugs, no separator.
    joined = "".join(sorted(configured_slugs))
    # Mutation-testing note (2026-08-13): "utf-8" vs "UTF-8" is a true
    # equivalent mutant - Python's str.encode() codec lookup is
    # case-insensitive (and hyphen/underscore-insensitive) per the codecs
    # module's alias table, so both produce byte-identical output. Verified
    # by direct execution (mutmut run, 1 survivor, traced by hand).
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _build_warning(missing_slugs: list[str]) -> Optional[str]:
    if not missing_slugs:
        return None
    return (
        "Slug freshness check found configured slug(s) no longer live on "
        f"OpenRouter: {', '.join(missing_slugs)}"
    )


def _read_cache(cache_path: Path) -> Optional[dict]:
    try:
        # Mutation-testing note (2026-08-13): dropping the "r" mode literal
        # here (keeping encoding="utf-8") survives mutmut as a documented
        # equivalent mutant, not a real gap -- Path.open()'s own mode
        # parameter already defaults to "r". Dropping/nulling `encoding`
        # instead IS a real behavior change (falls back to the OS locale
        # encoding) and is covered by
        # test_read_cache_opens_with_explicit_utf8_encoding.
        with cache_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_cache(cache_path: Path, today: str, config_hash: str, missing_slugs: list[str]) -> None:
    payload = {"date": today, "config_hash": config_hash, "missing_slugs": missing_slugs}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f)


async def check_slug_freshness(
    configured_slugs: list[str],
    cache_path: Path,
    fetch_fn: FetchModelsFn,
    today: str,
    force: bool = False,
) -> FreshnessResult:
    config_hash = _config_hash(configured_slugs)

    if not force:
        cached = _read_cache(cache_path)
        if (
            cached is not None
            and cached.get("date") == today
            and cached.get("config_hash") == config_hash
        ):
            cached_missing = cached.get("missing_slugs", [])
            return FreshnessResult(
                checked_slugs=list(configured_slugs),
                missing_slugs=list(cached_missing),
                warning=_build_warning(list(cached_missing)),
                cache_hit=True,
                fetch_error=None,
            )

    try:
        live_ids = await fetch_fn()
    except Exception as exc:  # noqa: BLE001 - any fetch failure must be caught, never propagated
        return FreshnessResult(
            checked_slugs=list(configured_slugs),
            missing_slugs=[],
            warning=None,
            cache_hit=False,
            fetch_error=f"{type(exc).__name__}: {exc}",
        )

    live_id_set = set(live_ids)
    missing_slugs = [slug for slug in configured_slugs if slug not in live_id_set]

    try:
        _write_cache(cache_path, today, config_hash, missing_slugs)
    except OSError:
        pass  # cache-write failure must never invalidate or block the check result

    return FreshnessResult(
        checked_slugs=list(configured_slugs),
        missing_slugs=missing_slugs,
        warning=_build_warning(missing_slugs),
        cache_hit=False,
        fetch_error=None,
    )
