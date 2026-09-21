"""The OpenRouter client's request shaping and response handling. No network.

``_post`` is replaced throughout, so nothing here opens a socket. What is being
tested is the part that can silently go wrong: the request shape OpenRouter
expects, and every way a response can be unusable.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from mentor.planner import openrouter
from mentor.planner.client import PlannerError
from mentor.planner.prompts import STEP_JSON_SCHEMA

pytestmark = pytest.mark.offline

ANSWER = {
    "action": "open_menu",
    "target_id": "el_00",
    "instruction": "Open the File menu",
    "expect": "a dropdown appears",
    "confidence": 0.9,
}


def body(content: str, **extra) -> dict:
    payload = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 40, "cost": 0.0},
        "model": "test/model",
    }
    payload.update(extra)
    return payload


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    return openrouter.OpenRouterClient(model="test/model")


def capture(monkeypatch, client, response: dict) -> list[dict]:
    """Replace the POST and record what would have been sent."""
    sent: list[dict] = []

    def fake_post(data: bytes) -> dict:
        sent.append(json.loads(data.decode("utf-8")))
        return response

    monkeypatch.setattr(client, "_post", fake_post)
    return sent


# --- the request -------------------------------------------------------------


def test_the_request_uses_the_openai_chat_shape(monkeypatch, client):
    sent = capture(monkeypatch, client, body(json.dumps(ANSWER)))
    client.complete_json("SYSTEM", "USER", STEP_JSON_SCHEMA)
    request = sent[0]
    assert request["model"] == "test/model"
    assert request["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "USER"},
    ]


def test_the_schema_is_sent_as_a_strict_json_schema(monkeypatch, client):
    """Without strict, a model is free to return whatever shape it likes."""
    sent = capture(monkeypatch, client, body(json.dumps(ANSWER)))
    client.complete_json("s", "u", STEP_JSON_SCHEMA)
    fmt = sent[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == STEP_JSON_SCHEMA


# --- the response ------------------------------------------------------------


def test_a_clean_json_answer_is_parsed(monkeypatch, client):
    capture(monkeypatch, client, body(json.dumps(ANSWER)))
    assert client.complete_json("s", "u", STEP_JSON_SCHEMA) == ANSWER


def test_a_markdown_fenced_answer_is_still_parsed(monkeypatch, client):
    """Some models fence their JSON even when asked for structured output."""
    fenced = "```json\n" + json.dumps(ANSWER) + "\n```"
    capture(monkeypatch, client, body(fenced))
    assert client.complete_json("s", "u", STEP_JSON_SCHEMA) == ANSWER


def test_a_bare_fence_without_a_language_is_handled(monkeypatch, client):
    capture(monkeypatch, client, body("```\n" + json.dumps(ANSWER) + "\n```"))
    assert client.complete_json("s", "u", STEP_JSON_SCHEMA) == ANSWER


def test_prose_instead_of_json_fails_with_a_usable_message(monkeypatch, client):
    """The likely outcome on a model that ignores response_format."""
    capture(monkeypatch, client, body("Sure! You should click the File menu."))
    with pytest.raises(PlannerError, match="structured outputs"):
        client.complete_json("s", "u", STEP_JSON_SCHEMA)


def test_an_empty_answer_is_rejected(monkeypatch, client):
    capture(monkeypatch, client, body("   "))
    with pytest.raises(PlannerError, match="empty answer"):
        client.complete_json("s", "u", STEP_JSON_SCHEMA)


def test_a_response_with_no_choices_is_rejected(monkeypatch, client):
    capture(monkeypatch, client, {"choices": []})
    with pytest.raises(PlannerError, match="no message"):
        client.complete_json("s", "u", STEP_JSON_SCHEMA)


def test_json_that_is_not_an_object_is_rejected(monkeypatch, client):
    capture(monkeypatch, client, body('["el_00"]'))
    with pytest.raises(PlannerError, match="not an object"):
        client.complete_json("s", "u", STEP_JSON_SCHEMA)


# --- keys and errors ---------------------------------------------------------


def test_a_missing_key_fails_before_any_request(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(openrouter.config, "openrouter_api_key", lambda: None)
    with pytest.raises(PlannerError, match="No OpenRouter API key"):
        openrouter.OpenRouterClient()


def _http_error(code: int) -> urllib.error.HTTPError:
    import io

    return urllib.error.HTTPError(
        openrouter.ENDPOINT, code, "err", {}, io.BytesIO(b'{"error":"nope"}')
    )


def test_a_rejected_key_is_reported_plainly(monkeypatch, client):
    monkeypatch.setattr(
        openrouter.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(_http_error(401)),
    )
    with pytest.raises(PlannerError, match="key was rejected"):
        client.complete_json("s", "u", STEP_JSON_SCHEMA)


def test_needing_credit_points_at_the_free_models(monkeypatch, client):
    monkeypatch.setattr(
        openrouter.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(_http_error(402)),
    )
    with pytest.raises(PlannerError, match=":free"):
        client.complete_json("s", "u", STEP_JSON_SCHEMA)


def test_a_client_error_is_not_retried(monkeypatch, client):
    """Retrying a 400 only spends the user's time."""
    calls = []

    def boom(*_a, **_k):
        calls.append(1)
        raise _http_error(400)

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", boom)
    with pytest.raises(PlannerError, match="OpenRouter error 400"):
        client.complete_json("s", "u", STEP_JSON_SCHEMA)
    assert len(calls) == 1


def test_a_rate_limit_is_retried_then_gives_up(monkeypatch, client):
    calls = []

    def boom(*_a, **_k):
        calls.append(1)
        raise _http_error(429)

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", boom)
    monkeypatch.setattr(openrouter.time, "sleep", lambda _s: None)
    client.max_retries = 2
    with pytest.raises(PlannerError, match="Could not reach OpenRouter"):
        client.complete_json("s", "u", STEP_JSON_SCHEMA)
    assert len(calls) == 3


# --- the fence stripper ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a":1}', '{"a":1}'),
        ('```json\n{"a":1}\n```', '{"a":1}'),
        ('```\n{"a":1}\n```', '{"a":1}'),
        ('  {"a":1}  ', '{"a":1}'),
    ],
)
def test_fence_stripping(raw, expected):
    assert openrouter._strip_code_fence(raw) == expected


def test_the_free_model_list_is_all_free_suffixed_or_the_router_alias():
    for model in openrouter.FREE_MODELS_WITH_STRUCTURED_OUTPUT:
        assert model.endswith(":free") or model == "openrouter/free", model
