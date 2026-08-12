"""Tests for the deterministic (non-network) parts of live_adapters.py.

The actual HTTP calls (_post_chat_completion, real_query_model,
real_fetch_evidence) are exercised by a real, cheap smoke test outside the
mutation-tested suite - see docs/pipeline-architecture-spec.md's dry-run log.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from scripts.grounding_pass import Claim
from scripts.live_adapters import (
    _get_openrouter_key,
    _is_retryable_error,
    _post_chat_completion,
    build_evidence_prompt,
    parse_evidence_response,
)


# --- _get_openrouter_key ---


def test_get_openrouter_key_reads_correct_keyring_args(monkeypatch):
    captured = {}

    def fake_get_password(service, username):
        captured["service"] = service
        captured["username"] = username
        return "sk-real-key"

    monkeypatch.setattr("scripts.live_adapters.keyring.get_password", fake_get_password)

    key = _get_openrouter_key()

    assert key == "sk-real-key"
    assert captured == {"service": "llm-council", "username": "openrouter_api_key"}


def test_get_openrouter_key_raises_exact_message_when_missing(monkeypatch):
    monkeypatch.setattr(
        "scripts.live_adapters.keyring.get_password", lambda service, username: None
    )

    with pytest.raises(RuntimeError) as exc_info:
        _get_openrouter_key()

    # Exact equality, not pytest.raises(match=...) - match() does a substring
    # re.search, so a mutant that wraps the message in extra characters
    # (e.g. "XX...XX") would still match a substring pattern and survive.
    assert (
        str(exc_info.value)
        == "No OpenRouter key in keychain - run `llm-council setup-key --stdin` first."
    )


def test_build_evidence_prompt_exact_content():
    claim = Claim(id="3", text="The sky is blue.")
    prompt = build_evidence_prompt(claim)
    assert prompt == (
        "Research this claim using web search and respond with ONLY a JSON "
        "object (no markdown fences, no other text), in exactly this shape:\n"
        '{"verdict": "supports"|"contradicts"|"unverifiable", '
        '"source": "<url of your best source, or empty string if unverifiable>", '
        '"date": "<retrieval date YYYY-MM-DD, or empty string if unverifiable>"}\n\n'
        "Claim: The sky is blue."
    )


def test_parse_evidence_response_supports():
    raw = '{"verdict": "supports", "source": "http://example.com", "date": "2026-08-09"}'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert len(result) == 1
    assert result[0].supports is True
    assert result[0].source == "http://example.com"
    assert result[0].date == "2026-08-09"


def test_parse_evidence_response_contradicts():
    raw = '{"verdict": "contradicts", "source": "http://example.com", "date": "2026-08-09"}'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert result[0].supports is False


def test_parse_evidence_response_unverifiable_yields_empty_list():
    raw = '{"verdict": "unverifiable", "source": "", "date": ""}'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert result == []


def test_parse_evidence_response_missing_source_yields_empty_list():
    raw = '{"verdict": "supports", "source": "", "date": "2026-08-09"}'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert result == []


def test_parse_evidence_response_malformed_json_yields_empty_list_not_crash():
    result = parse_evidence_response("not json at all", retrieval_date="2026-08-01")
    assert result == []


def test_parse_evidence_response_strips_markdown_fences():
    raw = '```json\n{"verdict": "supports", "source": "http://x.com", "date": "2026-08-09"}\n```'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert len(result) == 1
    assert result[0].source == "http://x.com"


def test_parse_evidence_response_strips_json_tag_with_no_newline_after_it():
    # No newline between "json" and the opening brace - proves the slice
    # strips exactly the 4-char "json" tag, not 5 (which would eat the "{")
    raw = '```json{"verdict": "supports", "source": "http://x.com", "date": "2026-08-09"}```'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert len(result) == 1
    assert result[0].source == "http://x.com"


def test_parse_evidence_response_strips_only_backticks_not_other_chars():
    # strip("`") must remove only backtick characters from the ends, not a
    # broader charset - "XX" here would survive a strip("`") pass (only the
    # leading/trailing backticks come off) but wrongly vanish under a mutant
    # that strips any of {X, `}, which would then produce valid JSON where
    # the real code produces malformed JSON (-> empty list).
    raw = '```XX{"verdict": "supports", "source": "http://x.com", "date": "2026-08-09"}'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert result == []


def test_parse_evidence_response_missing_date_falls_back_to_retrieval_date():
    raw = '{"verdict": "supports", "source": "http://x.com"}'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert result[0].date == "2026-08-01"


def test_parse_evidence_response_unknown_verdict_yields_empty_list():
    raw = '{"verdict": "maybe", "source": "http://x.com", "date": "2026-08-09"}'
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert result == []


@pytest.mark.parametrize("raw", ["0", "[]", "null", "true", '"just a string"'])
def test_parse_evidence_response_valid_json_non_dict_yields_empty_list_not_crash(raw):
    # Regression: json.loads succeeds on any valid JSON scalar/array, not
    # just objects - data.get("verdict") crashed with AttributeError when
    # data was an int/list/None/bool/str instead of a dict. Found by a
    # hypothesis stress-fuzz test feeding arbitrary text through this
    # function (tests/test_stress_adversarial.py).
    result = parse_evidence_response(raw, retrieval_date="2026-08-01")
    assert result == []


# --- _is_retryable_error: pure classifier, retryable vs not ---


def test_is_retryable_error_5xx_http_error_is_retryable():
    exc = urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, None)
    assert _is_retryable_error(exc) is True


def test_is_retryable_error_4xx_http_error_is_not_retryable():
    exc = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)
    assert _is_retryable_error(exc) is False


def test_is_retryable_error_boundary_499_not_retryable_500_retryable():
    assert _is_retryable_error(urllib.error.HTTPError("u", 499, "x", {}, None)) is False
    assert _is_retryable_error(urllib.error.HTTPError("u", 500, "x", {}, None)) is True


def test_is_retryable_error_url_error_is_retryable():
    assert _is_retryable_error(urllib.error.URLError("connection refused")) is True


def test_is_retryable_error_timeout_and_connection_error_are_retryable():
    assert _is_retryable_error(TimeoutError()) is True
    assert _is_retryable_error(ConnectionError()) is True


def test_is_retryable_error_unrelated_exception_is_not_retryable():
    assert _is_retryable_error(ValueError("bad json")) is False


# --- _post_chat_completion: retry loop, with urlopen mocked ---


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_post_chat_completion_builds_the_exact_request(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = req.headers
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "scripts.live_adapters._get_openrouter_key", lambda: "sk-test-key-123"
    )

    _post_chat_completion("my/model", "my prompt", max_tokens=777, sleep_fn=lambda s: None)

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer sk-test-key-123"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["body"] == {
        "model": "my/model",
        "messages": [{"role": "user", "content": "my prompt"}],
        "max_tokens": 777,
    }
    assert captured["timeout"] == 90


def test_post_chat_completion_default_max_tokens_is_2000(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data)
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("scripts.live_adapters._get_openrouter_key", lambda: "k")

    _post_chat_completion("m", "p", sleep_fn=lambda s: None)

    assert captured["body"]["max_tokens"] == 2000


def test_post_chat_completion_succeeds_first_try_no_retry_no_sleep(monkeypatch):
    calls = []
    sleeps = []

    def fake_urlopen(req, timeout):
        calls.append(1)
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = _post_chat_completion(
        "m", "p", sleep_fn=lambda s: sleeps.append(s)
    )

    assert len(calls) == 1
    assert sleeps == []
    assert result["choices"][0]["message"]["content"] == "ok"


def test_post_chat_completion_retries_on_retryable_error_then_succeeds(monkeypatch):
    attempts = {"n": 0}
    sleeps = []

    def fake_urlopen(req, timeout):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise urllib.error.HTTPError("u", 503, "unavailable", {}, None)
        return _FakeResponse({"choices": [{"message": {"content": "recovered"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = _post_chat_completion("m", "p", sleep_fn=lambda s: sleeps.append(s))

    assert attempts["n"] == 3
    assert len(sleeps) == 2  # slept before attempt 2 and attempt 3
    assert result["choices"][0]["message"]["content"] == "recovered"


def test_post_chat_completion_backoff_is_exponential(monkeypatch):
    sleeps = []

    def fake_urlopen(req, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(urllib.error.URLError):
        _post_chat_completion("m", "p", max_retries=3, sleep_fn=lambda s: sleeps.append(s))

    assert sleeps == [1.0, 2.0, 4.0]


def test_post_chat_completion_gives_up_after_max_retries(monkeypatch):
    attempts = {"n": 0}

    def fake_urlopen(req, timeout):
        attempts["n"] += 1
        raise urllib.error.HTTPError("u", 503, "unavailable", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        _post_chat_completion("m", "p", max_retries=2, sleep_fn=lambda s: None)

    assert attempts["n"] == 3  # initial attempt + 2 retries


def test_post_chat_completion_non_retryable_error_raises_immediately_no_sleep(monkeypatch):
    attempts = {"n": 0}
    sleeps = []

    def fake_urlopen(req, timeout):
        attempts["n"] += 1
        raise urllib.error.HTTPError("u", 401, "unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        _post_chat_completion("m", "p", sleep_fn=lambda s: sleeps.append(s))

    assert attempts["n"] == 1
    assert sleeps == []
