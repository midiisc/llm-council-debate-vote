"""Tests for docs/specs/reasoning-effort-wiring-contract.md's Contract 4
(Stage 1 reasoning-effort wiring, added 2026-08-14), covering the two
pieces of new/changed surface not exercised by the existing wiring tests
in tests/test_council_adapter.py and
tests/test_council_adapter_resilient_stage1.py (both of which monkeypatch
`query_model_with_status_and_effort` out entirely with a fake that ignores
`reasoning_effort` -- correct for THEIR plumbing-focused ACs, but leaving
this function's own body and the per-model effort map unexercised):

1. `scripts/live_adapters.py::query_model_with_status_and_effort` --
   ACs 11-18: reasoning_effort injection, status-dict shape, and the
   STATUS_* taxonomy (ok/rate_limited/auth_error/timeout/error).
2. `scripts/council_adapter.py::_STAGE1_REASONING_EFFORT` /
   `_stage1_query_fn` -- the hardcoded per-model effort map and the
   closure that looks a model up in it (falling back to `None` for any
   model not in the map, e.g. a backup substitute) before calling
   `query_model_with_status_and_effort`.

No real network/creds: `httpx.AsyncClient`, `resolve_endpoint`,
`resolve_model_name`, and `build_openrouter_payload` are all monkeypatched
at their defining module (these are function-local imports inside
`query_model_with_status_and_effort`, so patching the *importing* module's
namespace would not take effect -- patch the source modules instead).
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

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
# Shared HTTP-mocking harness for query_model_with_status_and_effort
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
    """Records the exact kwargs passed to .post() -- and to the
    constructor itself -- on the class so the test can inspect them after
    the coroutine under test returns."""

    captured: Dict[str, Any] = {}
    constructor_kwargs: Dict[str, Any] = {}
    to_return: Any = None  # _FakeResponse instance OR an Exception to raise

    def __init__(self, *a, **kw):
        self._timeout = kw.get("timeout")
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
    base_payload_extra=None,
    time_sequence=None,
):
    """Wires _FakeAsyncClient plus fakes for resolve_endpoint/
    resolve_model_name/build_openrouter_payload, all patched at their
    DEFINING module since query_model_with_status_and_effort imports them
    function-locally at call time.

    Returns a dict with the calls each fake captured:
    {"build_payload": {"model", "messages"}, "resolve_model_name": [(model, route), ...]}.
    """
    _FakeAsyncClient.captured = {}
    _FakeAsyncClient.constructor_kwargs = {}
    _FakeAsyncClient.to_return = response_or_exc
    # query_model_with_status_and_effort does `import httpx` function-locally,
    # which just rebinds the name to whatever is in sys.modules["httpx"] --
    # patching the real httpx module's own AsyncClient attribute (not some
    # attribute on `la`) is what actually intercepts the call.
    monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        _gw_resolver_module, "resolve_endpoint", lambda: (api_url, api_key, route)
    )

    resolve_model_name_calls = []

    def _fake_resolve_model_name(model, route):
        resolve_model_name_calls.append((model, route))
        return resolved_model

    monkeypatch.setattr(_gw_resolver_module, "resolve_model_name", _fake_resolve_model_name)

    captured_payload_call = {}

    def _fake_build_payload(*, model, messages):
        captured_payload_call["model"] = model
        captured_payload_call["messages"] = messages
        payload = {"model": model, "messages": messages, "_base_marker": True}
        if base_payload_extra:
            payload.update(base_payload_extra)
        return payload

    monkeypatch.setattr(_gw_openrouter_module, "build_openrouter_payload", _fake_build_payload)

    if time_sequence is not None:
        remaining = list(time_sequence)

        def _fake_time():
            if remaining:
                return remaining.pop(0)
            return remaining[-1] if time_sequence else 0.0

        monkeypatch.setattr("time.time", _fake_time)

    original_wait_for = asyncio.wait_for
    wait_for_calls = []

    async def _spying_wait_for(coro, timeout=None):
        wait_for_calls.append(timeout)
        return await original_wait_for(coro, timeout=timeout)

    monkeypatch.setattr("asyncio.wait_for", _spying_wait_for)

    return {
        "build_payload": captured_payload_call,
        "resolve_model_name_calls": resolve_model_name_calls,
        "wait_for_timeouts": wait_for_calls,
    }


def _run(model="anthropic/claude-opus-4.8", messages=None, timeout=30.0, reasoning_effort=None):
    messages = messages if messages is not None else [{"role": "user", "content": "hi"}]
    return asyncio.run(
        la.query_model_with_status_and_effort(
            model, messages, timeout, reasoning_effort=reasoning_effort
        )
    )


# ---------------------------------------------------------------------------
# AC11 / AC12 -- reasoning_effort request-body injection
# ---------------------------------------------------------------------------


def test_ac11_default_reasoning_effort_none_omits_the_key_from_payload(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}}),
    )

    _run(reasoning_effort=None)

    sent_payload = _FakeAsyncClient.captured["json"]
    assert "reasoning_effort" not in sent_payload


def test_ac12_non_none_reasoning_effort_adds_exact_top_level_field(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}}),
    )

    _run(reasoning_effort="high")

    sent_payload = _FakeAsyncClient.captured["json"]
    assert sent_payload["reasoning_effort"] == "high"
    # Never the nested `reasoning` object (Contract 4's whole rationale).
    assert "reasoning" not in sent_payload


def test_ac12_reasoning_effort_is_additive_to_the_base_payload_not_a_replacement(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}}),
        base_payload_extra={"temperature": 0.7},
    )

    _run(reasoning_effort="medium")

    sent_payload = _FakeAsyncClient.captured["json"]
    assert sent_payload["_base_marker"] is True
    assert sent_payload["temperature"] == 0.7
    assert sent_payload["reasoning_effort"] == "medium"


def test_resolved_model_name_is_what_gets_sent_to_build_openrouter_payload(monkeypatch):
    mocks = _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}}),
        resolved_model="openrouter/resolved-slug",
    )

    _run(model="raw/unresolved-slug")

    assert mocks["build_payload"]["model"] == "openrouter/resolved-slug"


def test_resolve_model_name_is_called_with_the_raw_model_and_the_resolved_route(monkeypatch):
    mocks = _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}}),
        route="distinctive-route-marker",
    )

    _run(model="raw/unresolved-slug")

    assert mocks["resolve_model_name_calls"] == [("raw/unresolved-slug", "distinctive-route-marker")]


def test_auth_header_uses_resolved_api_key_as_bearer_token(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}}),
        api_key="sk-distinctive-marker-key",
    )

    _run()

    headers = _FakeAsyncClient.captured["headers"]
    assert headers["Authorization"] == "Bearer sk-distinctive-marker-key"
    assert headers["Content-Type"] == "application/json"


def test_post_is_sent_to_the_resolved_api_url(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}}),
        api_url="https://distinctive.example/v1/chat/completions",
    )

    _run()

    assert _FakeAsyncClient.captured["url"] == "https://distinctive.example/v1/chat/completions"


def test_messages_argument_is_passed_through_unchanged(monkeypatch):
    mocks = _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}}),
    )
    sentinel_messages = [{"role": "user", "content": "distinctive-marker-text"}]

    _run(messages=sentinel_messages)

    assert mocks["build_payload"]["messages"] == sentinel_messages


def test_default_timeout_parameter_is_120_seconds_when_not_specified(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}}),
    )

    asyncio.run(
        la.query_model_with_status_and_effort(
            "anthropic/claude-opus-4.8", [{"role": "user", "content": "hi"}]
        )
    )

    assert _FakeAsyncClient.constructor_kwargs["timeout"] == 120.0


def test_httpx_asyncclient_is_constructed_with_the_given_timeout(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}}),
    )

    _run(timeout=77.0)

    assert _FakeAsyncClient.constructor_kwargs["timeout"] == 77.0


def test_asyncio_wait_for_is_given_the_same_timeout_as_the_httpx_client(monkeypatch):
    mocks = _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "hi"}}], "usage": {}}),
    )

    _run(timeout=63.5)

    assert mocks["wait_for_timeouts"] == [63.5]


# ---------------------------------------------------------------------------
# AC13 -- success (2xx) status-dict shape
# ---------------------------------------------------------------------------


def test_ac13_latency_ms_is_elapsed_seconds_times_1000_not_divided_or_off_by_one(monkeypatch):
    """Pins latency_ms's exact formula: int((end - start) * 1000). A fixed,
    monkeypatched time.time() sequence makes /1000, `+` instead of `-`, and
    *1001 all produce a different, wrong number from a real 2.5s elapsed
    (2500) -- a fast-completing fake response alone can't distinguish these
    since a ~0s elapsed collapses every one of those variants to 0."""
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "x"}}], "usage": {}}),
        time_sequence=[1_700_000_000.0, 1_700_000_002.5],
    )

    result = _run()

    assert result["latency_ms"] == 2500


def test_ac13_success_returns_status_ok_with_content_and_latency(monkeypatch):
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

    result = _run()

    assert result["status"] == "ok"
    assert result["content"] == "the answer"
    assert isinstance(result["latency_ms"], int)
    assert result["latency_ms"] >= 0


def test_ac13_success_usage_dict_shape_matches_gateway_adapter_fields_exactly(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(
            200,
            json_data={
                "choices": [{"message": {"content": "x"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "cost": 0.01,
                    "cached_tokens": 7,
                },
            },
        ),
    )

    result = _run()

    assert result["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cost": 0.01,
        "cached_tokens": 7,
        "cache_write_tokens": 0,
    }


def test_ac13_success_usage_fields_default_to_zero_when_usage_object_missing_fields(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "x"}}], "usage": {}}),
    )

    result = _run()

    assert result["usage"]["prompt_tokens"] == 0
    assert result["usage"]["completion_tokens"] == 0
    assert result["usage"]["total_tokens"] == 0
    assert result["usage"]["cost"] is None
    assert result["usage"]["cached_tokens"] == 0
    assert result["usage"]["cache_write_tokens"] == 0


def test_ac13_success_extracts_cache_write_tokens_via_anthropic_top_level_field(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(
            200,
            json_data={
                "choices": [{"message": {"content": "x"}}],
                "usage": {"cache_creation_input_tokens": 42},
            },
        ),
    )

    result = _run()

    assert result["usage"]["cache_write_tokens"] == 42


def test_ac13_success_usage_key_entirely_absent_from_body_still_defaults_cleanly(monkeypatch):
    """No "usage" key at all (not even an empty {}) -- the missing-key
    default must be a dict, not None, or every usage.get(...) call below
    it raises AttributeError."""
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data={"choices": [{"message": {"content": "no usage field"}}]}),
    )

    result = _run()

    assert result["status"] == "ok"
    assert result["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost": None,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
    }


# ---------------------------------------------------------------------------
# AC14 -- 429 rate limited
# ---------------------------------------------------------------------------


def test_ac14_429_maps_to_rate_limited_status(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(429, headers={"Retry-After": "17"}),
        resolved_model="the-limited-model",
    )

    result = _run(model="the-limited-model")

    assert result["status"] == "rate_limited"
    assert result["retry_after"] == 17
    assert "the-limited-model" in result["error"]
    assert isinstance(result["latency_ms"], int)


def test_ac14_429_missing_retry_after_header_defaults_to_60(monkeypatch):
    _install_http_mocks(monkeypatch, _FakeResponse(429, headers={}))

    result = _run()

    assert result["retry_after"] == 60


def test_ac14_429_non_digit_retry_after_header_defaults_to_60(monkeypatch):
    _install_http_mocks(monkeypatch, _FakeResponse(429, headers={"Retry-After": "not-a-number"}))

    result = _run()

    assert result["retry_after"] == 60


# ---------------------------------------------------------------------------
# AC15 -- 401 / 403 auth error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [401, 403])
def test_ac15_401_and_403_map_to_auth_error_status(monkeypatch, status_code):
    _install_http_mocks(monkeypatch, _FakeResponse(status_code), resolved_model="gated-model")

    result = _run(model="gated-model")

    assert result["status"] == "auth_error"
    assert "gated-model" in result["error"]
    assert str(status_code) in result["error"]
    assert isinstance(result["latency_ms"], int)


# ---------------------------------------------------------------------------
# AC16 -- timeout
# ---------------------------------------------------------------------------


def test_ac16_httpx_timeout_exception_maps_to_timeout_status(monkeypatch):
    import httpx as _real_httpx

    _install_http_mocks(monkeypatch, _real_httpx.TimeoutException("timed out"))

    result = _run(timeout=5.0)

    assert result["status"] == "timeout"
    assert "5.0" in result["error"]
    assert isinstance(result["latency_ms"], int)


def test_ac16_asyncio_timeout_error_maps_to_timeout_status(monkeypatch):
    _install_http_mocks(monkeypatch, asyncio.TimeoutError())

    result = _run(timeout=9.0)

    assert result["status"] == "timeout"
    assert "9.0" in result["error"]


def test_ac16_timeout_latency_ms_is_elapsed_seconds_times_1000(monkeypatch):
    import httpx as _real_httpx

    _install_http_mocks(
        monkeypatch,
        _real_httpx.TimeoutException("timed out"),
        time_sequence=[1_700_000_000.0, 1_700_000_003.25],
    )

    result = _run()

    assert result["latency_ms"] == 3250


# ---------------------------------------------------------------------------
# AC17 / AC18 -- HTTP 400 and any other exception -> error, never a crash
# ---------------------------------------------------------------------------


def test_ac17_400_maps_to_error_status_with_truncated_body_text(monkeypatch):
    long_body = "x" * 500
    _install_http_mocks(monkeypatch, _FakeResponse(400, text=long_body), resolved_model="bad-request-model")

    result = _run(model="bad-request-model")

    assert result["status"] == "error"
    assert "bad-request-model" in result["error"]
    assert len(result["error"]) < 500
    assert isinstance(result["latency_ms"], int)


def test_ac17_400_error_message_is_truncated_to_first_200_chars_of_body(monkeypatch):
    _install_http_mocks(monkeypatch, _FakeResponse(400, text="A" * 50 + "B" * 500))

    result = _run()

    # Everything after the first 200 chars of response.text must be absent.
    assert "B" * 500 not in result["error"]
    assert "A" * 50 in result["error"]


def test_ac17_400_error_message_truncates_at_exactly_the_200th_character(monkeypatch):
    # A body of exactly 201 unique characters -- [:200] and [:201] differ
    # by exactly the trailing "Z", the only way to pin the exact boundary
    # rather than just "some truncation happens".
    body = "".join(chr(ord("a") + (i % 26)) for i in range(200)) + "Z"
    assert len(body) == 201
    _install_http_mocks(monkeypatch, _FakeResponse(400, text=body))

    result = _run()

    assert body[:200] in result["error"]
    assert body not in result["error"]
    assert not result["error"].endswith("Z")


def test_ac17_non_2xx_non_special_status_via_raise_for_status_maps_to_error(monkeypatch):
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(500, raise_exc=RuntimeError("upstream 500")),
    )

    result = _run()

    assert result["status"] == "error"
    assert "upstream 500" in result["error"]


def test_ac18_malformed_200_body_is_caught_and_mapped_to_error_not_raised(monkeypatch):
    # Missing "choices" entirely -> KeyError during response parsing.
    # AC18 requires this mirror the package's own catch-all handling
    # (mapped to status="error"), not propagate as an uncaught exception.
    _install_http_mocks(monkeypatch, _FakeResponse(200, json_data={"usage": {}}))

    result = _run()

    assert result["status"] == "error"
    assert isinstance(result["latency_ms"], int)


def test_ac18_response_json_parse_failure_is_caught_and_mapped_to_error(monkeypatch):
    _install_http_mocks(
        monkeypatch, _FakeResponse(200, json_data=ValueError("not valid json"))
    )

    result = _run()

    assert result["status"] == "error"
    assert "not valid json" in result["error"]


def test_ac18_generic_error_latency_ms_is_elapsed_seconds_times_1000(monkeypatch):
    # 3 time.time() calls before the except-block's own latency_ms for this
    # path: start_time, the unconditional post-response latency_ms
    # computed before any status-code branch, then the except block's own
    # (separately computed, overwriting) latency_ms once response.json()
    # raises. Only the first and third values determine the returned
    # latency_ms; the middle one just needs to be consumed.
    _install_http_mocks(
        monkeypatch,
        _FakeResponse(200, json_data=ValueError("boom")),
        time_sequence=[1_700_000_000.0, 1_700_000_000.1, 1_700_000_001.75],
    )

    result = _run()

    assert result["status"] == "error"
    assert result["latency_ms"] == 1750


# ---------------------------------------------------------------------------
# council_adapter.py -- _STAGE1_REASONING_EFFORT map + _stage1_query_fn
# ---------------------------------------------------------------------------


def test_stage1_reasoning_effort_map_exact_contents():
    # 2026-08-14: briefly reverted opus-4.8/gpt-5.5 to "medium" after a
    # dry-run showed "high" dropping Stage 2 CSS 0.721->0.572, then RESTORED
    # to "high" same day once CSS was correctly understood as a ranking-
    # agreement metric, not a correctness/quality proxy - both dry-run
    # values fell in the pipeline's normal "handled" range (moderate/weak
    # consensus), never "significant disagreement". See
    # docs/pipeline-architecture-spec.md's CSS interpretation reference and
    # docs/upstream-deltas.md's 2026-08-14 "CSS correction" entry.
    assert ca._STAGE1_REASONING_EFFORT == {
        "anthropic/claude-opus-4.8": "high",
        "openai/gpt-5.5": "high",
        "google/gemini-3.6-flash": "medium",
        "z-ai/glm-5.2": "medium",
    }


@pytest.mark.parametrize(
    "model,expected_effort",
    [
        ("anthropic/claude-opus-4.8", "high"),
        ("openai/gpt-5.5", "high"),
        ("google/gemini-3.6-flash", "medium"),
        ("z-ai/glm-5.2", "medium"),
    ],
)
def test_stage1_query_fn_looks_up_exact_effort_per_mapped_model(monkeypatch, model, expected_effort):
    captured = {}

    async def fake_query_model_with_status_and_effort(m, messages, timeout, reasoning_effort=None):
        captured["model"] = m
        captured["messages"] = messages
        captured["timeout"] = timeout
        captured["reasoning_effort"] = reasoning_effort
        return {"status": "ok", "content": "stub", "latency_ms": 1, "usage": {}}

    monkeypatch.setattr(ca, "query_model_with_status_and_effort", fake_query_model_with_status_and_effort)

    sentinel_messages = [{"role": "user", "content": "q"}]
    result = asyncio.run(ca._stage1_query_fn(model, sentinel_messages, 42.5))

    assert captured["reasoning_effort"] == expected_effort
    assert captured["model"] == model
    assert captured["messages"] == sentinel_messages
    assert captured["timeout"] == 42.5
    assert result == {"status": "ok", "content": "stub", "latency_ms": 1, "usage": {}}


def test_stage1_query_fn_unmapped_model_gets_reasoning_effort_none(monkeypatch):
    """A backup substitute outside the primary 4-seat roster is not in
    _STAGE1_REASONING_EFFORT -- must get reasoning_effort=None (unchanged
    behavior), never a KeyError."""
    captured = {}

    async def fake_query_model_with_status_and_effort(m, messages, timeout, reasoning_effort=None):
        captured["reasoning_effort"] = reasoning_effort
        return {"status": "ok", "content": "stub", "latency_ms": 1, "usage": {}}

    monkeypatch.setattr(ca, "query_model_with_status_and_effort", fake_query_model_with_status_and_effort)

    asyncio.run(ca._stage1_query_fn("some/unmapped-backup-model", [{"role": "user", "content": "q"}], 10.0))

    assert captured["reasoning_effort"] is None


def test_stage1_query_fn_returns_the_inner_call_result_unmodified(monkeypatch):
    sentinel_result = {"status": "rate_limited", "latency_ms": 5, "error": "distinctive-marker", "retry_after": 3}

    async def fake_query_model_with_status_and_effort(m, messages, timeout, reasoning_effort=None):
        return sentinel_result

    monkeypatch.setattr(ca, "query_model_with_status_and_effort", fake_query_model_with_status_and_effort)

    result = asyncio.run(ca._stage1_query_fn("z-ai/glm-5.2", [{"role": "user", "content": "q"}], 10.0))

    assert result is sentinel_result
