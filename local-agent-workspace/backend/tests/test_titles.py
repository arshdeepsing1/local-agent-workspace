import asyncio
import json

import httpx
import pytest

from local_agent.config import Settings
from local_agent.titles import clean_title, fallback_title, generate_title, needs_title


@pytest.mark.parametrize("text, expected", [
    ('## Title: "Fix login redirect"', "Fix login redirect"),
    ("  सुधारें लॉगिन की समस्या।  ", "सुधारें लॉगिन की समस्या।"),
    ("Fix login\nHere is the explanation", None),
    ("", None),
    ("word " * 15, None),
])
def test_title_output_is_a_short_single_heading(text, expected):
    assert clean_title(text) == expected
    assert len(clean_title("longword " * 10)) <= 60


def test_legacy_title_eligibility_preserves_explicit_names():
    prompt = "  Please\n\n simplify  this conversation heading for the history sidebar."
    session = {"events": [{"type": "user", "text": prompt}], "title": "New conversation"}
    for title in ("New conversation", fallback_title(prompt), prompt.replace("\n", " ")[:64]):
        session["title"] = title
        assert needs_title(session)
    for title in ("My project notes", "Worktree: codex/fix"):
        session["title"] = title
        assert not needs_title(session)
    session.update(title=fallback_title(prompt), title_generated=True)
    assert not needs_title(session)
    assert not needs_title({"title": "New conversation", "events": []})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.credentials = lambda: ("https://gateway.example", "private-test-token")
    session = {"model": "selected-model", "events": [
        {"type": "user", "text": "Fix the login redirect"},
        {"type": "assistant", "text": "The redirect now preserves the requested page."},
    ]}
    return settings, session


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(gateway)))


@pytest.mark.parametrize("content", ["Fix login redirect", [{"type": "text", "text": "Fix login redirect"}]])
async def test_request_uses_bounded_redacted_excerpts_and_selected_endpoint(setup, monkeypatch, content):
    settings, session = setup
    session["events"][0]["text"] = "private-test-token " + "अ" * 2000
    session["events"].insert(1, {"type": "tool", "output": "Tool contents must not enter title request"})
    session["events"][-1]["text"] = "अ" * 2000
    before = json.dumps(session)

    async def gateway(request):
        assert request.url == "https://gateway.example/serving-endpoints/selected-model/invocations"
        body = json.loads(request.content)
        assert body["stream"] is False and "tools" not in body
        assert len(body["messages"]) == 2
        excerpt = json.loads(body["messages"][1]["content"])
        assert len(excerpt["request"].encode()) <= 2000
        assert len(excerpt["response"].encode()) <= 1000
        assert "[REDACTED]" in excerpt["request"]
        assert "private-test-token" not in str(body)
        assert "Tool contents" not in str(body)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

    mock_gateway(monkeypatch, gateway)
    assert await generate_title(settings, session) == "Fix login redirect"
    assert json.dumps(session) == before


async def test_recorded_title_call_preserves_provider_usage(setup, monkeypatch):
    settings, session = setup
    saves = []

    async def gateway(request):
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": "Fix login redirect"}}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 5},
        })

    mock_gateway(monkeypatch, gateway)
    assert await generate_title(settings, session, lambda value: saves.append(json.loads(json.dumps(value)))) == "Fix login redirect"
    assert saves
    call = session["inference_calls"][0]
    assert call["purpose"] == "title" and call["status"] == "completed" and call["http_status"] == 200
    assert call["usage"] == {"input_tokens": 42, "output_tokens": 5}


@pytest.mark.parametrize("payload", [
    [], {}, {"choices": []}, {"choices": "invalid"}, {"choices": [None]},
    {"choices": [{"finish_reason": "stop", "message": None}]},
    {"choices": [{"finish_reason": "length", "message": {"content": "Incomplete heading"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"reasoning_content": "Not a title"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": [{"type": "text", "text": None}]}}]},
])
async def test_unusable_response_is_best_effort(setup, monkeypatch, payload):
    mock_gateway(monkeypatch, lambda request: httpx.Response(200, json=payload))
    assert await generate_title(*setup) is None


@pytest.mark.parametrize("status, text", [(503, "Unavailable"), (200, "not-json"), (302, "redirect")])
async def test_http_failure_and_invalid_json_preserve_fallback(setup, monkeypatch, status, text):
    mock_gateway(monkeypatch, lambda request: httpx.Response(status, text=text))
    assert await generate_title(*setup) is None


async def test_title_deadline_cancels_a_stalled_request(setup, monkeypatch):
    cancelled = asyncio.Event()

    async def gateway(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("local_agent.titles.TITLE_TIMEOUT", .01)
    mock_gateway(monkeypatch, gateway)
    assert await asyncio.wait_for(generate_title(*setup), 1) is None
    assert cancelled.is_set()
