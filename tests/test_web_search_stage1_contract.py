"""Blind acceptance tests for the "Stage-1 optional web-search" contract
(see task description: add `enable_web_search: bool = False` to
`scripts.live_adapters.query_model_with_status_and_effort`, wire it through
`scripts.council_adapter._stage1_query_fn` for exactly 3 of the 4 roster
models via a new `_STAGE1_WEB_SEARCH_ENABLED_MODELS` constant).

Authored BLIND: from the contract text only (signature/objective/I-O types/
Given-When-Then ACs/environment) -- no implementation code, design notes, or
other agent's reasoning was consulted. This file intentionally duplicates
the HTTP-mocking harness pattern already used by
tests/test_reasoning_effort_stage1_contract.py (per tests/conftest.py's own
documented convention: "duplicated in each test file") rather than importing
it, to stay isolated from that file's assumptions.

Each test is named after (or a docstring cites) the AC it encodes. Hermetic:
no real network -- httpx.AsyncClient, resolve_endpoint, resolve_model_name,
and build_openrouter_payload are all monkeypatched at their DEFINING module,
matching the existing pattern (these are function-local imports inside
query_model_with_status_and_effort, so patching the importing module's
namespace would not take effect).

AC10 ("every existing test in tests/ passes unmodified after this contract's
changes") is NOT a unit test inside this file -- it is verified by running
the full suite (`pytest tests/`) as the project's testCmd, which is the only
way to genuinely check "every existing test still passes". This file's own
`test_ac10_existing_reasoning_effort_only_call_path_unaffected_by_new_param`
gives a narrow, in-file regression guard for the one code path this contract
directly touches (the function gaining a new parameter), but the AC itself
is a whole-suite property, not something one file can assert alone.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _import(name: str):
    try:
        return importlib.import_module(f"scripts.{name}")
    except ModuleNotFoundError:
        return importlib.import_module(name)


la = _import("live_adapters")
ca = _import("council_adapter")

import llm_council.gateway.openrouter as _gw_openrouter_module
import llm_council.gateway.resolver as _gw_resolver_module


# ---------------------------------------------------------------------------
# Shared HTTP-mocking harness (duplicated from
# tests/test_reasoning_effort_stage1_contract.py's pattern, intentionally --
# see module docstring).
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text="", headers=None, raise_exc=None):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.headers = headers or {}
        self._raise_exc = raise_exc

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc


class _FakeAsyncClient:
    captured: Dict[str, Any] = {}
    constructor_kwargs: Dict[str, Any] = {}
    to_return: Any = None

    def __init__(self, *a, **kw):
        type(self).constructor_kwargs = dict(kw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        type(self).captured = {"url": url, "headers": headers, "json": json}
        result = type(self).to_return
        if isinstance(result, BaseException):
            raise result
        return result


def _install_http_mocks(
    monkeypatch,
    response_or_exc,
    *,
    api_url="https://openrouter.ai/api/v1/chat/completions",
    api_key="sk-test-key",
    route="openrouter",
    resolved_model="resolved/model-name",
):
    _FakeAsyncClient.captured = {}
    _FakeAsyncClient.constructor_kwargs = {}
    _FakeAsyncClient.to_return = response_or_exc
    monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        _gw_resolver_module, "resolve_endpoint", lambda: (api_url, api_key, route)
    )
    monkeypatch.setattr(
        _gw_resolver_module, "resolve_model_name", lambda model, route: resolved_model
    )

    def _fake_build_payload(*, model, messages):
        return {"model": model, "messages": messages, "_base_marker": True}

    monkeypatch.setattr(_gw_openrouter_module, "build_openrouter_payload", _fake_build_payload)


def _run(
    model="anthropic/claude-opus-4.8",
    messages=None,
    timeout=30.0,
    reasoning_effort=None,
    enable_web_search=None,
):
    messages = messages if messages is not None else [{"role": "user", "content": "hi"}]
    if enable_web_search is None:
        # Omit the kwarg entirely -- exercises the true default.
        return asyncio.run(
            la.query_model_with_status_and_effort(
                model, messages, timeout, reasoning_effort=reasoning_effort
            )
        )
    return asyncio.run(
        la.query_model_with_status_and_effort(
            model,
            messages,
            timeout,
            reasoning_effort=reasoning_effort,
            enable_web_search=enable_web_search,
        )
    )


def _ok_response(annotations=None, web_search_requests=None, include_annotations_key=True):
    message: Dict[str, Any] = {"content": "the answer"}
    if include_annotations_key:
        message["annotations"] = annotations if annotations is not None else []
    usage: Dict[str, Any] = {}
    if web_search_requests is not None:
        usage["server_tool_use_details"] = {"web_search_requests": web_search_requests}
    return _FakeResponse(200, json_data={"choices": [{"message": message}], "usage": usage})


def _citation(url: str, title: str, start: int = 0, end: int = 1) -> Dict[str, Any]:
    return {
        "type": "url_citation",
        "url_citation": {"url": url, "title": title, "start_index": start, "end_index": end},
    }


# ---------------------------------------------------------------------------
# AC1 -- enable_web_search=False (default) omits tools/max_tool_calls,
# byte-identical to today's request body.
# ---------------------------------------------------------------------------


def test_ac1_default_omitted_enable_web_search_has_no_tools_or_max_tool_calls_keys(monkeypatch):
    _install_http_mocks(monkeypatch, _ok_response())

    _run(enable_web_search=None)  # kwarg entirely omitted -- true default

    payload = _FakeAsyncClient.captured["json"]
    assert "tools" not in payload
    assert "max_tool_calls" not in payload


def test_ac1_explicit_false_has_no_tools_or_max_tool_calls_keys(monkeypatch):
    _install_http_mocks(monkeypatch, _ok_response())

    _run(enable_web_search=False)

    payload = _FakeAsyncClient.captured["json"]
    assert "tools" not in payload
    assert "max_tool_calls" not in payload


def test_ac1_omitted_and_explicit_false_produce_byte_identical_request_bodies(monkeypatch):
    _install_http_mocks(monkeypatch, _ok_response())
    _run(enable_web_search=None)
    payload_omitted = _FakeAsyncClient.captured["json"]

    _install_http_mocks(monkeypatch, _ok_response())
    _run(enable_web_search=False)
    payload_explicit_false = _FakeAsyncClient.captured["json"]

    assert payload_omitted == payload_explicit_false


# ---------------------------------------------------------------------------
# AC2 -- enable_web_search=True adds the exact tools/max_tool_calls shape.
# ---------------------------------------------------------------------------


def test_ac2_enable_web_search_true_adds_exact_tools_and_max_tool_calls(monkeypatch):
    _install_http_mocks(monkeypatch, _ok_response())

    _run(enable_web_search=True)

    payload = _FakeAsyncClient.captured["json"]
    assert payload["tools"] == [
        {"type": "openrouter:web_search", "parameters": {"max_uses": 1}}
    ]
    assert payload["max_tool_calls"] == 1


def test_ac2_enable_web_search_true_is_additive_to_the_base_payload(monkeypatch):
    _install_http_mocks(monkeypatch, _ok_response())

    _run(enable_web_search=True)

    payload = _FakeAsyncClient.captured["json"]
    assert payload["_base_marker"] is True
    assert payload["model"] == "resolved/model-name"


# ---------------------------------------------------------------------------
# AC3 -- 2xx + enable_web_search=True + >=1 url_citation annotations ->
# enabled_searched, deduplicated by url.
# ---------------------------------------------------------------------------


def test_ac3_single_citation_yields_enabled_searched_with_one_source(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _ok_response(
            annotations=[_citation("https://real.example/a", "Title A")],
            web_search_requests=2,
        ),
    )

    result = _run(enable_web_search=True)

    assert result["web_search_provenance"] == {
        "state": "enabled_searched",
        "queries_count": 2,
        "sources": [{"url": "https://real.example/a", "title": "Title A"}],
    }


def test_ac3_duplicate_url_citations_are_deduplicated_to_one_source(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _ok_response(
            annotations=[
                _citation("https://real.example/a", "Title A", start=0, end=5),
                _citation("https://real.example/a", "Title A", start=10, end=15),
                _citation("https://real.example/b", "Title B"),
            ],
            web_search_requests=3,
        ),
    )

    result = _run(enable_web_search=True)

    provenance = result["web_search_provenance"]
    assert provenance["state"] == "enabled_searched"
    assert provenance["queries_count"] == 3
    urls = [s["url"] for s in provenance["sources"]]
    assert sorted(urls) == ["https://real.example/a", "https://real.example/b"]
    assert len(urls) == len(set(urls))  # no duplicate url entries


_url_alphabet = list("abcdefghijklmnopqrstuvwxyz0123456789")
_url_strategy = st.text(alphabet=_url_alphabet, min_size=1, max_size=10).map(
    lambda s: f"https://example.com/{s}"
)
_title_alphabet = list("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_title_strategy = st.text(alphabet=_title_alphabet, min_size=1, max_size=12)


@st.composite
def _dedup_scenario(draw):
    pairs = draw(
        st.lists(
            st.tuples(_url_strategy, _title_strategy),
            min_size=1,
            max_size=6,
            unique_by=lambda p: p[0],
        )
    )
    annotations = []
    for url, title in pairs:
        repeat = draw(st.integers(min_value=1, max_value=3))
        for _ in range(repeat):
            annotations.append(_citation(url, title))
    annotations = list(draw(st.permutations(annotations)))
    queries_count = draw(st.integers(min_value=0, max_value=999))
    return annotations, pairs, queries_count


@settings(
    max_examples=50,
    derandomize=True,
    deadline=2000,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(scenario=_dedup_scenario())
def test_ac3_property_sources_deduplicated_by_url_regardless_of_order_or_repeat_count(
    monkeypatch, scenario
):
    """AC3's dedup law: however many times a url appears (in whatever
    order), and however many total server-side searches were made
    (queries_count is an independent pass-through from
    usage.server_tool_use_details.web_search_requests, not derived from
    annotation count), `sources` collapses to exactly one entry per unique
    url with its title, and `queries_count` is exactly the configured
    upstream number.

    suppress_health_check=[function_scoped_fixture] is safe here: each
    example calls _install_http_mocks fresh (which resets _FakeAsyncClient
    class state and re-applies every monkeypatch.setattr target itself), so
    reusing the same monkeypatch fixture instance across examples does not
    leak state between them -- there is no accumulation, only replacement.
    """
    annotations, pairs, queries_count = scenario
    _install_http_mocks(
        monkeypatch,
        _ok_response(annotations=annotations, web_search_requests=queries_count),
    )

    result = _run(enable_web_search=True)
    provenance = result["web_search_provenance"]

    assert provenance["state"] == "enabled_searched"
    assert provenance["queries_count"] == queries_count
    assert len(provenance["sources"]) == len(pairs)
    assert {(s["url"], s["title"]) for s in provenance["sources"]} == {
        (url, title) for url, title in pairs
    }


# ---------------------------------------------------------------------------
# AC4 -- 2xx + enable_web_search=True + no url_citation annotations ->
# enabled_no_search.
# ---------------------------------------------------------------------------


def test_ac4_empty_annotations_list_yields_enabled_no_search(monkeypatch):
    _install_http_mocks(
        monkeypatch, _ok_response(annotations=[], web_search_requests=0)
    )

    result = _run(enable_web_search=True)

    assert result["web_search_provenance"] == {"state": "enabled_no_search"}


def test_ac4_missing_annotations_key_entirely_yields_enabled_no_search(monkeypatch):
    _install_http_mocks(
        monkeypatch, _ok_response(include_annotations_key=False)
    )

    result = _run(enable_web_search=True)

    assert result["web_search_provenance"] == {"state": "enabled_no_search"}


def test_ac4_no_citations_but_nonzero_web_search_requests_still_enabled_no_search(monkeypatch):
    # The model had the tool available and the server recorded a search
    # attempt, but no url_citation annotations came back -- AC4 keys off
    # annotations, not the queries_count value.
    _install_http_mocks(
        monkeypatch, _ok_response(annotations=[], web_search_requests=1)
    )

    result = _run(enable_web_search=True)

    assert result["web_search_provenance"] == {"state": "enabled_no_search"}


# ---------------------------------------------------------------------------
# AC5 -- enable_web_search=False (or omitted) -> always not_enabled,
# regardless of response outcome; every other field unaffected.
# ---------------------------------------------------------------------------


def test_ac5_false_with_2xx_response_yields_not_enabled(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _ok_response(annotations=[_citation("https://x.example", "X")], web_search_requests=5),
    )

    result = _run(enable_web_search=False)

    assert result["web_search_provenance"] == {"state": "not_enabled"}


def test_ac5_false_with_429_response_yields_not_enabled_and_correct_status(monkeypatch):
    _install_http_mocks(monkeypatch, _FakeResponse(429, headers={"Retry-After": "17"}))

    result = _run(enable_web_search=False)

    assert result["web_search_provenance"] == {"state": "not_enabled"}
    assert result["status"] == "rate_limited"


def test_ac5_omitted_kwarg_yields_not_enabled_same_as_explicit_false(monkeypatch):
    _install_http_mocks(monkeypatch, _ok_response())

    result = _run(enable_web_search=None)

    assert result["web_search_provenance"] == {"state": "not_enabled"}


def test_ac5_other_status_dict_fields_unaffected_by_the_new_field(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(
            200,
            json_data={
                "choices": [{"message": {"content": "the answer"}}],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 22,
                    "total_tokens": 33,
                    "cost": 0.045,
                },
            },
        ),
    )

    result = _run(enable_web_search=False)

    assert result["status"] == "ok"
    assert result["content"] == "the answer"
    assert result["usage"]["prompt_tokens"] == 11
    assert result["usage"]["completion_tokens"] == 22
    assert result["usage"]["total_tokens"] == 33
    assert result["usage"]["cost"] == 0.045
    assert isinstance(result["latency_ms"], int)


@settings(
    max_examples=50,
    derandomize=True,
    deadline=2000,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(idx=st.integers(min_value=0, max_value=4))
def test_ac5_property_not_enabled_holds_across_every_response_outcome(monkeypatch, idx):
    """AC5's invariant: web_search_provenance is not_enabled for EVERY
    possible response outcome (success, rate-limited, auth-error, generic
    error, timeout) whenever enable_web_search is False. Each example
    re-installs a fresh set of HTTP mocks, so the shared monkeypatch
    fixture instance across examples carries no state forward."""
    import httpx as _real_httpx

    factories = [
        lambda: _ok_response(),
        lambda: _FakeResponse(429, headers={"Retry-After": "5"}),
        lambda: _FakeResponse(401),
        lambda: _FakeResponse(400, text="bad request"),
        lambda: _real_httpx.TimeoutException("timed out"),
    ]
    _install_http_mocks(monkeypatch, factories[idx]())

    result = _run(enable_web_search=False)

    assert result["web_search_provenance"] == {"state": "not_enabled"}


# ---------------------------------------------------------------------------
# Direct unit coverage for _extract_web_search_provenance's own defensive
# .get(..., default) fallbacks -- these guard against a genuinely malformed
# 2xx OpenRouter body (a key missing outright, not merely empty), a shape
# none of the AC3/AC4 fixtures above ever produce since _ok_response always
# builds a fully-populated body. Exercised by calling the function directly
# (it's already imported as part of `la`) rather than through the full
# HTTP-mock chain, since these are internal-shape edge cases, not
# request/response-plumbing ones.
# ---------------------------------------------------------------------------


def test_direct_data_none_returns_the_exact_enabled_no_search_shape():
    assert la._extract_web_search_provenance(True, None) == {"state": "enabled_no_search"}


def test_direct_missing_choices_key_degrades_to_enabled_no_search_no_crash():
    # No "choices" key at all -- a genuinely malformed 2xx body, distinct
    # from data=None (which is already covered by the branch above).
    result = la._extract_web_search_provenance(True, {})
    assert result == {"state": "enabled_no_search"}


def test_direct_missing_message_key_in_first_choice_degrades_no_crash():
    result = la._extract_web_search_provenance(True, {"choices": [{}]})
    assert result == {"state": "enabled_no_search"}


def test_direct_missing_usage_key_entirely_defaults_queries_count_to_zero():
    # citations present (so the function reaches the usage/queries_count
    # computation) but the body has no top-level "usage" key at all -- every
    # nested .get(..., default) in the queries_count chain must fall back to
    # its documented default (0), not None and not crash.
    data = {
        "choices": [
            {
                "message": {
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url_citation": {"url": "https://real.example/a", "title": "A"},
                        }
                    ]
                }
            }
        ]
        # "usage" key intentionally absent
    }
    result = la._extract_web_search_provenance(True, data)
    assert result == {
        "state": "enabled_searched",
        "queries_count": 0,
        "sources": [{"url": "https://real.example/a", "title": "A"}],
    }


def test_direct_missing_server_tool_use_details_key_defaults_queries_count_to_zero():
    data = {
        "choices": [
            {
                "message": {
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url_citation": {"url": "https://real.example/a", "title": "A"},
                        }
                    ]
                }
            }
        ],
        "usage": {},  # present, but no "server_tool_use_details" key
    }
    result = la._extract_web_search_provenance(True, data)
    assert result["queries_count"] == 0


def test_direct_citation_missing_url_citation_key_is_skipped_not_crashed():
    data = {
        "choices": [
            {
                "message": {
                    "annotations": [
                        {"type": "url_citation"},  # no "url_citation" sub-key at all
                        {
                            "type": "url_citation",
                            "url_citation": {"url": "https://real.example/b", "title": "B"},
                        },
                    ]
                }
            }
        ],
        "usage": {"server_tool_use_details": {"web_search_requests": 1}},
    }
    result = la._extract_web_search_provenance(True, data)
    assert result["state"] == "enabled_searched"
    assert result["sources"] == [{"url": "https://real.example/b", "title": "B"}]


# ---------------------------------------------------------------------------
# AC6 -- non-2xx / transport-error classification is unaffected by
# enable_web_search=True; no new status value.
# ---------------------------------------------------------------------------

_EXISTING_STATUS_VALUES = {"ok", "timeout", "rate_limited", "auth_error", "error"}


def test_ac6_429_with_web_search_true_still_maps_to_rate_limited(monkeypatch):
    _install_http_mocks(monkeypatch, _FakeResponse(429, headers={"Retry-After": "17"}))

    result = _run(enable_web_search=True)

    assert result["status"] == "rate_limited"
    assert result["status"] in _EXISTING_STATUS_VALUES
    # No response body was ever parsed on this path -- enable_web_search's
    # own True value (not a dropped/replaced None) must still reach
    # _extract_web_search_provenance so the "tool was available but no
    # outcome is known" state is reported rather than silently downgrading
    # to "not_enabled".
    assert result["web_search_provenance"] == {"state": "enabled_no_search"}


@pytest.mark.parametrize("status_code", [401, 403])
def test_ac6_401_403_with_web_search_true_still_maps_to_auth_error(monkeypatch, status_code):
    _install_http_mocks(monkeypatch, _FakeResponse(status_code))

    result = _run(enable_web_search=True)

    assert result["status"] == "auth_error"
    assert result["status"] in _EXISTING_STATUS_VALUES
    assert result["web_search_provenance"] == {"state": "enabled_no_search"}


def test_ac6_httpx_timeout_with_web_search_true_still_maps_to_timeout(monkeypatch):
    import httpx as _real_httpx

    _install_http_mocks(monkeypatch, _real_httpx.TimeoutException("timed out"))

    result = _run(enable_web_search=True, timeout=5.0)

    assert result["status"] == "timeout"
    assert result["status"] in _EXISTING_STATUS_VALUES
    assert result["web_search_provenance"] == {"state": "enabled_no_search"}


def test_ac6_asyncio_timeout_error_with_web_search_true_still_maps_to_timeout(monkeypatch):
    _install_http_mocks(monkeypatch, asyncio.TimeoutError())

    result = _run(enable_web_search=True, timeout=9.0)

    assert result["status"] == "timeout"
    assert result["web_search_provenance"] == {"state": "enabled_no_search"}


def test_ac6_400_with_web_search_true_still_maps_to_error(monkeypatch):
    _install_http_mocks(monkeypatch, _FakeResponse(400, text="bad request"))

    result = _run(enable_web_search=True)

    assert result["status"] == "error"
    assert result["status"] in _EXISTING_STATUS_VALUES
    assert result["web_search_provenance"] == {"state": "enabled_no_search"}


def test_ac6_generic_exception_with_web_search_true_still_maps_to_error_no_crash(monkeypatch):
    _install_http_mocks(
        monkeypatch, _FakeResponse(500, raise_exc=RuntimeError("upstream 500"))
    )

    result = _run(enable_web_search=True)

    assert result["status"] == "error"
    assert result["status"] in _EXISTING_STATUS_VALUES
    # Exercises the exact dict key ("web_search_provenance", not a
    # case-mutated variant) and the un-dropped enable_web_search value on
    # the generic-Exception catch-all branch specifically.
    assert "web_search_provenance" in result
    assert result["web_search_provenance"] == {"state": "enabled_no_search"}


def test_ac6_no_new_status_value_is_ever_introduced_for_any_outcome(monkeypatch):
    import httpx as _real_httpx

    outcomes = [
        _ok_response(annotations=[_citation("https://x.example", "X")], web_search_requests=1),
        _FakeResponse(429, headers={}),
        _FakeResponse(401),
        _FakeResponse(403),
        _FakeResponse(400, text="bad"),
        _FakeResponse(500, raise_exc=RuntimeError("boom")),
        _real_httpx.TimeoutException("timed out"),
        asyncio.TimeoutError(),
    ]
    for outcome in outcomes:
        _install_http_mocks(monkeypatch, outcome)
        result = _run(enable_web_search=True)
        assert result["status"] in _EXISTING_STATUS_VALUES, result["status"]


# ---------------------------------------------------------------------------
# AC7 -- _STAGE1_WEB_SEARCH_ENABLED_MODELS constant: exact contents, and
# the 4th seat (moonshotai/kimi-k3 as of 2026-08-17, was z-ai/glm-5.2) is
# permanently excluded -- both lack a native `web_search` price on live
# OpenRouter /api/v1/models pricing, re-verified across the swap rather
# than assumed. See docs/specs/core-seat-swap-contract.md.
# ---------------------------------------------------------------------------


def test_ac7_stage1_web_search_enabled_models_exact_contents():
    assert ca._STAGE1_WEB_SEARCH_ENABLED_MODELS == {
        "anthropic/claude-opus-4.8",
        "openai/gpt-5.5",
        "google/gemini-3.7-flash",
    }


def test_ac7_stage1_web_search_enabled_models_is_a_set():
    assert isinstance(ca._STAGE1_WEB_SEARCH_ENABLED_MODELS, set)


def test_ac7_fourth_seat_is_never_in_the_web_search_enabled_set():
    assert "moonshotai/kimi-k3" not in ca._STAGE1_WEB_SEARCH_ENABLED_MODELS


# ---------------------------------------------------------------------------
# AC8 -- _stage1_query_fn passes enable_web_search=True for exactly the 3
# enabled models, False for the 4th seat and any other/unmapped model.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,expected_enable_web_search",
    [
        ("anthropic/claude-opus-4.8", True),
        ("openai/gpt-5.5", True),
        ("google/gemini-3.7-flash", True),
        ("moonshotai/kimi-k3", False),
    ],
)
def test_ac8_stage1_query_fn_passes_exact_enable_web_search_per_roster_model(
    monkeypatch, model, expected_enable_web_search
):
    captured = {}

    async def fake_query_model_with_status_and_effort(
        m, messages, timeout, reasoning_effort=None, enable_web_search=False
    ):
        captured["model"] = m
        captured["enable_web_search"] = enable_web_search
        return {"status": "ok", "content": "stub", "latency_ms": 1, "usage": {}}

    monkeypatch.setattr(
        ca, "query_model_with_status_and_effort", fake_query_model_with_status_and_effort
    )

    sentinel_messages = [{"role": "user", "content": "q"}]
    asyncio.run(ca._stage1_query_fn(model, sentinel_messages, 42.5))

    assert captured["model"] == model
    assert captured["enable_web_search"] is expected_enable_web_search


def test_ac8_unmapped_backup_model_gets_enable_web_search_false(monkeypatch):
    captured = {}

    async def fake_query_model_with_status_and_effort(
        m, messages, timeout, reasoning_effort=None, enable_web_search=False
    ):
        captured["enable_web_search"] = enable_web_search
        return {"status": "ok", "content": "stub", "latency_ms": 1, "usage": {}}

    monkeypatch.setattr(
        ca, "query_model_with_status_and_effort", fake_query_model_with_status_and_effort
    )

    asyncio.run(
        ca._stage1_query_fn("some/unmapped-backup-model", [{"role": "user", "content": "q"}], 10.0)
    )

    assert captured["enable_web_search"] is False


def test_ac8_enable_web_search_is_passed_as_a_keyword_argument_not_positional(monkeypatch):
    # AC8 explicitly requires "the exact keyword argument value per model" --
    # a positional-only pass-through would silently break if
    # query_model_with_status_and_effort's parameter order ever changes, so
    # pin that the wiring uses the keyword form.
    sig_check = {}

    async def fake_query_model_with_status_and_effort(*args, **kwargs):
        sig_check["args"] = args
        sig_check["kwargs"] = kwargs
        return {"status": "ok", "content": "stub", "latency_ms": 1, "usage": {}}

    monkeypatch.setattr(
        ca, "query_model_with_status_and_effort", fake_query_model_with_status_and_effort
    )

    asyncio.run(
        ca._stage1_query_fn("anthropic/claude-opus-4.8", [{"role": "user", "content": "q"}], 5.0)
    )

    assert "enable_web_search" in sig_check["kwargs"]
    assert sig_check["kwargs"]["enable_web_search"] is True


# ---------------------------------------------------------------------------
# AC9 -- _stage1_query_fn's call signature is unchanged: exactly
# (model, messages, timeout) -> Awaitable[dict].
# ---------------------------------------------------------------------------


def test_ac9_stage1_query_fn_signature_is_exactly_three_positional_params():
    sig = inspect.signature(ca._stage1_query_fn)
    assert list(sig.parameters.keys()) == ["model", "messages", "timeout"]
    for name in ("model", "messages", "timeout"):
        param = sig.parameters[name]
        assert param.default is inspect.Parameter.empty
        assert param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        )


def test_ac9_stage1_query_fn_is_an_async_callable():
    assert inspect.iscoroutinefunction(ca._stage1_query_fn)


def test_ac9_stage1_query_fn_callable_with_exactly_three_positional_args(monkeypatch):
    # Mirrors resilient_query.py's own documented QueryFn call convention:
    # query_fn(model, messages, timeout) -- purely positional, no kwargs.
    async def fake_query_model_with_status_and_effort(*args, **kwargs):
        return {"status": "ok", "content": "stub", "latency_ms": 1, "usage": {}}

    monkeypatch.setattr(
        ca, "query_model_with_status_and_effort", fake_query_model_with_status_and_effort
    )

    result = asyncio.run(
        ca._stage1_query_fn("z-ai/glm-5.2", [{"role": "user", "content": "q"}], 10.0)
    )

    assert result == {"status": "ok", "content": "stub", "latency_ms": 1, "usage": {}}


# ---------------------------------------------------------------------------
# AC10 -- additive only. This file's own narrow regression guard for the
# one path this contract's changes directly touch (see module docstring for
# why the AC as a whole is verified by the full suite run, not here).
# ---------------------------------------------------------------------------


def test_ac10_existing_reasoning_effort_only_call_path_unaffected_by_new_param(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(
            200,
            json_data={
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "cost": 0.01},
            },
        ),
    )

    # Exact same call shape as the pre-existing reasoning-effort contract's
    # own tests -- no enable_web_search argument at all.
    result = asyncio.run(
        la.query_model_with_status_and_effort(
            "anthropic/claude-opus-4.8",
            [{"role": "user", "content": "hi"}],
            30.0,
            reasoning_effort="high",
        )
    )

    payload = _FakeAsyncClient.captured["json"]
    assert payload["reasoning_effort"] == "high"
    assert "tools" not in payload
    assert "max_tool_calls" not in payload
    assert result["status"] == "ok"
    assert result["content"] == "hi"
    assert result["usage"]["prompt_tokens"] == 1
