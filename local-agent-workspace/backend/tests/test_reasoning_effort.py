"""Reasoning effort: the fields agent requests carry per endpoint, format and level; Settings → Reasoning and its
preview; a chat's own effort (saved, changed mid-turn, kept by forks and imports); the fallback when Databricks
rejects a reasoning field; and the reasoning text and thinking time a streamed response records."""
import asyncio
import copy
import json

import httpx
import pytest

from local_agent import reasoning
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.reasoning import EFFORT_LEVELS, effort_request, reasoning_delta, reasoning_summary
from local_agent.store import Store
from local_agent.telemetry import session_metrics
from local_agent.usage_export import usage_rows

OPUS = "databricks-claude-opus-5-5"
OPUS_48 = "databricks-claude-opus-4-8"
GPT = "databricks-gpt-oss-120b"
GPT_20B = "databricks-gpt-oss-20b"
ADAPTIVE = {"type": "adaptive", "display": "summarized"}


# The request fields, per endpoint × format × effort (reasoning.effort_request is what the turn loop sends)

@pytest.mark.parametrize("effort", EFFORT_LEVELS[1:])
@pytest.mark.parametrize("model", [OPUS, OPUS_48])
def test_claude_opus_gets_adaptive_thinking_and_output_config_effort(model, effort):
    request = effort_request(model, effort)
    assert request["fields"] == {"thinking": ADAPTIVE, "output_config": {"effort": effort}}
    assert (request["format"], request["sent_effort"], request["effort_field"]) == ("anthropic", effort, "output_config.effort")
    plain = effort_request(model, effort, summaries=False)["fields"]
    assert plain == {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}


def test_default_sends_nothing_except_the_display_choice_on_an_endpoint_that_always_thinks():
    for model in (OPUS, OPUS_48, GPT, GPT_20B, "test-endpoint", "databricks-claude-sonnet-4-5"):
        assert effort_request(model, "default", summaries=False)["fields"] == {}, model
    # Opus 5.5 thinks on every request; display only makes its thinking text visible (not depth or cost).
    assert effort_request(OPUS, "default")["fields"] == {"thinking": ADAPTIVE}
    assert effort_request(OPUS, "default")["effort_field"] is None
    # Opus 4.8 without thinking does not think: Default must not turn it on.
    assert effort_request(OPUS_48, "default")["fields"] == {}
    assert effort_request(GPT, "default")["fields"] == {}


@pytest.mark.parametrize("model", [GPT, GPT_20B])
@pytest.mark.parametrize("effort,sent", [("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "high"),
                                         ("max", "high")])
def test_gpt_oss_gets_reasoning_effort_with_extra_high_and_max_sent_as_high(model, effort, sent):
    request = effort_request(model, effort)
    assert request["fields"] == {"reasoning_effort": sent}
    assert (request["format"], request["sent_effort"], request["effort_field"]) == ("openai", sent, "reasoning_effort")


@pytest.mark.parametrize("model,fmt", [("test-endpoint", "none"), ("databricks-claude-sonnet-4-5", "anthropic"),
                                       ("databricks-gpt-5", "openai"), ("my-gpt-oss-finetune", "openai"),
                                       ("databricks-meta-llama-3-3-70b-instruct", "none")])
def test_an_endpoint_without_a_profile_guesses_its_format_and_offers_every_level(model, fmt):
    assert reasoning.request_format(model, "auto") == fmt
    endpoint = reasoning.endpoint_reasoning(model)
    assert endpoint["verified"] is False and endpoint["effort_levels"] == list(EFFORT_LEVELS[1:])
    assert effort_request(model, "max")["sent_effort"] == ("max" if fmt != "none" else None)


@pytest.mark.parametrize("setting,fields", [
    ("anthropic", {"thinking": ADAPTIVE, "output_config": {"effort": "xhigh"}}),
    ("openai", {"reasoning_effort": "xhigh"}),
    ("none", {}),
])
def test_the_request_format_setting_wins_over_the_endpoint(setting, fields):
    assert effort_request("test-endpoint", "xhigh", setting)["fields"] == fields
    assert effort_request(OPUS, "high", "none")["fields"] == {}
    assert effort_request(OPUS, "high", "openai")["fields"] == {"reasoning_effort": "high"}
    assert effort_request(GPT, "max", "anthropic")["fields"] == {"thinking": {"type": "adaptive", "display": "summarized"},
                                                                  "output_config": {"effort": "high"}}


def test_an_unknown_effort_is_default_and_a_rejected_format_sends_nothing():
    assert effort_request(OPUS, "turbo", summaries=False)["fields"] == {}
    request = effort_request(OPUS, "high", rejected="output_config.effort")
    assert request["fields"] == {} and request["skipped"] == "rejected_earlier"
    assert request["rejected_field"] == "output_config.effort" and request["effort"] == "high"


@pytest.mark.parametrize("body,field", [
    ('{"error_code": "BAD_REQUEST", "message": "output_config: Extra inputs are not permitted"}', "output_config.effort"),
    ('{"message": "thinking.type: Input should be \'enabled\' or \'disabled\'"}', "thinking"),
    ('{"message": "thinking.display is not supported"}', "thinking.display"),
    ('{"message": "Unknown parameter: effort"}', "output_config.effort"),
    ('{"message": "budget_tokens: Field required"}', "thinking"),
    ('{"message": "The prompt is too long"}', None),
    ('{"message": "reasoning_effort is not supported"}', None),  # Not a field this request added.
])
def test_a_rejection_names_a_field_the_request_added(body, field):
    assert reasoning.rejected_field(body, effort_request(OPUS, "high")) == field
    assert reasoning.rejected_field(body, effort_request(OPUS, "default", summaries=False)) is None


def test_a_rejection_of_reasoning_effort():
    request = effort_request(GPT, "high")
    assert reasoning.rejected_field('{"message": "reasoning_effort: unexpected keyword"}', request) == "reasoning_effort"
    assert reasoning.rejected_field('{"message": "thinking is not allowed"}', request) is None


# Settings and the preview route

@pytest.fixture
async def api(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.env = {}
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            yield client, app.state.manager, settings, project


def settings_body(settings, **values):
    return {key: settings.values[key] for key in ("workspace", "model", "env_file")} | values


async def test_settings_default_and_validate_the_reasoning_choices(api):
    client, manager, settings, _ = api
    public = (await client.get("/api/settings")).json()
    assert (public["reasoning_effort"], public["reasoning_format"], public["thinking_summaries"]) == ("default", "auto", True)
    # The endpoint chooser lists the endpoints the app has facts about.
    assert public["endpoint_profiles"] == [{"model": OPUS, "label": "Claude Opus 5.5"}, {"model": OPUS_48, "label": "Claude Opus 4.8"},
                                           {"model": GPT, "label": "GPT OSS 120B"}, {"model": GPT_20B, "label": "GPT OSS 20B"}]
    saved = await client.put("/api/settings", json=settings_body(settings, reasoning_effort="xhigh",
                                                                 reasoning_format="openai", thinking_summaries=False))
    assert saved.status_code == 200, saved.text
    assert (saved.json()["reasoning_effort"], saved.json()["reasoning_format"], saved.json()["thinking_summaries"]) == (
        "xhigh", "openai", False)
    stored = json.loads(settings.path.read_text())
    assert (stored["reasoning_effort"], stored["reasoning_format"], stored["thinking_summaries"]) == ("xhigh", "openai", False)
    assert Settings(settings.state_dir).values["reasoning_effort"] == "xhigh"  # Read back on the next start.
    for bad in ({"reasoning_effort": "turbo"}, {"reasoning_format": "both"}, {"thinking_summaries": "yes"},
                {"reasoning_effort": None, "reasoning_format": 1}):
        response = await client.put("/api/settings", json=settings_body(settings, **bad))
        assert response.status_code == 422, bad
    with pytest.raises(ValueError, match="Default reasoning effort"):
        settings.update({"reasoning_effort": "extreme"})
    with pytest.raises(ValueError, match="request format"):
        settings.update({"reasoning_format": "budget"})
    with pytest.raises(ValueError, match="thinking summaries"):
        settings.update({"thinking_summaries": 1})
    assert settings.values["reasoning_effort"] == "xhigh"


async def test_the_preview_is_what_the_request_builder_sends(api):
    client, manager, _, _ = api
    for model in (OPUS, OPUS_48, GPT, "test-endpoint"):
        for effort in EFFORT_LEVELS:
            for fmt in reasoning.REQUEST_FORMATS:
                for summaries in (True, False):
                    response = await client.post("/api/settings/reasoning", json={
                        "model": model, "reasoning_effort": effort, "reasoning_format": fmt,
                        "thinking_summaries": summaries})
                    assert response.status_code == 200
                    assert response.json()["fields"] == effort_request(model, effort, fmt, summaries)["fields"]
    gpt = (await client.post("/api/settings/reasoning", json={"model": GPT, "reasoning_effort": "max"})).json()
    assert gpt["levels"][0] == {"value": "default", "label": "Default (Medium)", "sent": None}
    assert gpt["levels"][4] == {"value": "xhigh", "label": "Extra high", "sent": "high", "note": "sent as High"}
    assert gpt["fields"] == {"reasoning_effort": "high"} and gpt["verified"] is True
    assert any("Max is sent as High" in note for note in gpt["notes"])
    opus = (await client.post("/api/settings/reasoning", json={"model": OPUS})).json()
    assert opus["levels"][0]["label"] == "Default (Medium)" and opus["label"] == "Claude Opus 5.5"
    assert opus["fields"] == {"thinking": ADAPTIVE}
    older = (await client.post("/api/settings/reasoning", json={"model": OPUS_48})).json()
    assert older["levels"][0]["label"] == "Default (no thinking)" and older["fields"] == {}
    unknown = (await client.post("/api/settings/reasoning", json={"model": "test-endpoint", "reasoning_effort": "high"})).json()
    assert unknown["verified"] is False and unknown["format"] == "none" and unknown["levels"][0]["label"] == "Default"
    assert "Choose a Request format" in " ".join(unknown["notes"])
    for bad in ({"model": ""}, {"model": OPUS, "reasoning_effort": "turbo"}, {"model": OPUS, "extra": 1},
                {"model": OPUS, "thinking_summaries": "no"}):
        assert (await client.post("/api/settings/reasoning", json=bad)).status_code == 422, bad
    # A rejection is shown until a Settings save forgets it.
    manager.reasoning_rejections[(OPUS, "anthropic")] = "output_config.effort"
    rejected = (await client.post("/api/settings/reasoning", json={"model": OPUS, "reasoning_effort": "high"})).json()
    assert rejected["fields"] == {} and rejected["skipped"] == "rejected_earlier"
    assert any("rejected output_config.effort" in note for note in rejected["notes"])


# A chat's own effort

async def test_a_new_chat_takes_the_settings_effort_or_its_own_and_saves_changes(api):
    client, manager, settings, _ = api
    settings.values["reasoning_effort"] = "high"
    first = (await client.post("/api/sessions")).json()
    assert first["reasoning_effort"] == "high"
    second = (await client.post("/api/sessions", json={"permission_mode": "manual", "reasoning_effort": "low"})).json()
    assert second["reasoning_effort"] == "low"
    assert (await client.post("/api/sessions", json={"reasoning_effort": "turbo"})).status_code == 422
    changed = await client.put(f"/api/sessions/{first['id']}/reasoning-effort", json={"reasoning_effort": "max"})
    assert changed.status_code == 200 and changed.json()["reasoning_effort"] == "max"
    for bad in ({"reasoning_effort": "turbo"}, {}, {"reasoning_effort": "max", "model": OPUS}):
        assert (await client.put(f"/api/sessions/{first['id']}/reasoning-effort", json=bad)).status_code == 422
    # Saved in the conversation's JSONL header, like its model.
    header = json.loads((manager.store.conversations / f"{first['id']}.jsonl").read_text().splitlines()[0])
    assert header["data"]["reasoning_effort"] == "max"
    assert manager.store.header(first["id"])["reasoning_effort"] == "max"
    assert {item["id"]: item.get("reasoning_effort") for item in (await client.get("/api/sessions")).json()}[first["id"]] == "max"
    # A chat saved before efforts existed uses the Settings default.
    old = manager.store.create(settings.values)
    del old["reasoning_effort"]
    manager.store.save(old)
    settings.values.update(model=OPUS, reasoning_effort="xhigh")
    old["model"] = OPUS
    assert manager.reasoning_request(old)["fields"]["output_config"] == {"effort": "xhigh"}
    # A subagent chat with its own effort uses it (the parent's effort, once delegation copies it).
    child = {"id": "child", "model": OPUS, "is_subagent": True, "reasoning_effort": "low"}
    assert manager.reasoning_request(child)["fields"]["output_config"] == {"effort": "low"}


async def test_forks_exports_and_imports_keep_the_effort(api):
    client, manager, settings, project = api
    source = manager.store.create(settings.values)
    source.update(title="Effort", title_generated=True, terminal_reason="completed", reasoning_effort="xhigh",
                  events=[{"id": "u", "type": "user", "text": "Hi", "in_wire": True},
                          {"id": "a", "type": "assistant", "text": "Hello", "reasoning_summary": "Greeting.",
                           "reasoning_started": 1_700_000_000.5, "reasoning_seconds": 3.2,
                           "request_info": {"model": OPUS, "status": "completed", "finish_reason": "stop",
                                            "reasoning": {"effort": "xhigh", "format": "anthropic",
                                                          "fields": {"output_config": {"effort": "xhigh"}},
                                                          "sent_effort": "xhigh", "effort_field": "output_config.effort"}}}],
                  wire=[{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}])
    manager.store.save(source)
    fork = await client.post(f"/api/sessions/{source['id']}/fork")
    assert fork.status_code == 201, fork.text
    copied = manager.store.get(fork.json()["id"])
    assert copied["reasoning_effort"] == "xhigh"
    assert copied["events"][1]["reasoning_seconds"] == 3.2
    assert copied["events"][1]["request_info"]["reasoning"]["effort_field"] == "output_config.effort"
    bundle = (await client.get(f"/api/sessions/{source['id']}/export")).json()
    assert bundle["sessions"][0]["reasoning_effort"] == "xhigh"
    imported = await client.post("/api/sessions/import", params={"workspace": str(project)}, json=bundle)
    assert imported.status_code == 201, imported.text
    assert manager.store.get(imported.json()["session"]["id"])["reasoning_effort"] == "xhigh"
    broken = copy.deepcopy(bundle)
    broken["sessions"][0]["reasoning_effort"] = "turbo"
    assert (await client.post("/api/sessions/import", params={"workspace": str(project)}, json=broken)).status_code == 400


# Agent requests (fake Databricks gateway)

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(tmp_path / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Effort", title_generated=True, model=OPUS, permission_mode="bypassPermissions")
    yield AgentManager(store, settings), session
    store.db.close()


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(gateway)))


def sse(*deltas, finish="stop", usage=None):
    chunks = [{"choices": [{"delta": delta, "finish_reason": None}]} for delta in deltas]
    chunks[-1]["choices"][0]["finish_reason"] = finish
    if usage:
        chunks.append({"choices": [], "usage": usage})
    return "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def tool_call():
    return {"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                            "function": {"name": "list_tasks", "arguments": "{}"}}]}


def sent_fields(request):
    return {key: request[key] for key in reasoning.REQUEST_FIELDS if key in request}


def notices(session):
    return [event["text"] for event in session["events"] if event["type"] == "notice"]


def agent_calls(session):
    return [call for call in session["inference_calls"] if call["purpose"] == "agent"]


@pytest.mark.parametrize("model,effort,fmt,expected", [
    (OPUS, "high", "auto", {"thinking": ADAPTIVE, "output_config": {"effort": "high"}}),
    (OPUS, "default", "auto", {"thinking": ADAPTIVE}),
    (OPUS_48, "default", "auto", {}),
    (OPUS_48, "low", "auto", {"thinking": ADAPTIVE, "output_config": {"effort": "low"}}),
    (GPT, "max", "auto", {"reasoning_effort": "high"}),
    ("test-endpoint", "high", "auto", {}),
    ("test-endpoint", "high", "openai", {"reasoning_effort": "high"}),
    (OPUS, "max", "none", {}),
])
async def test_agent_requests_carry_the_fields_and_record_them(runtime, monkeypatch, model, effort, fmt, expected):
    manager, session = runtime
    session.update(model=model, reasoning_effort=effort)
    manager.settings.values["reasoning_format"] = fmt
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text=sse({"content": "Done."}))
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 1 and sent_fields(requests[0]) == expected
    assert requests[0]["max_tokens"] and requests[0]["stream"] is True
    reply = next(event for event in session["events"] if event["type"] == "assistant")
    recorded = reply["request_info"].get("reasoning")
    call = agent_calls(session)[0]
    if effort == "default" and not expected:
        assert recorded is None and "effort" not in call  # Nothing chosen and nothing sent: as before.
    else:
        assert recorded["effort"] == effort and recorded["fields"] == expected
        assert call["effort"] == effort
        assert session_metrics(session)["calls"][0]["effort"] == effort
        assert usage_rows(session)[0]["effort"] == effort
    if expected.get("output_config"):
        assert call["effort_field"] == recorded["effort_field"] == "output_config.effort"


async def test_display_follows_the_show_thinking_summaries_setting(runtime, monkeypatch):
    manager, session = runtime
    session["reasoning_effort"] = "medium"
    manager.settings.values["thinking_summaries"] = False
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text=sse({"content": "Done."}))
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    manager.settings.values["thinking_summaries"] = True
    await manager.run(session, "Again")
    assert [request["thinking"] for request in requests] == [{"type": "adaptive"}, ADAPTIVE]


async def test_an_effort_chosen_while_a_response_runs_applies_from_the_next_request(runtime, monkeypatch):
    manager, session = runtime
    session["reasoning_effort"] = "low"
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            await manager.change_effort(session, "max")  # The user picks Max while the first response streams.
            return httpx.Response(200, text=sse(tool_call(), finish="tool_calls"))
        return httpx.Response(200, text=sse({"content": "Done."}))
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Plan it")
    assert [request["output_config"] for request in requests] == [{"effort": "low"}, {"effort": "max"}]
    assert manager.store.get(session["id"])["reasoning_effort"] == "max"
    assert [call["effort"] for call in agent_calls(session)] == ["low", "max"]
    assert any(text.startswith("Reasoning effort changed to Max. It applies from the agent's next model request.")
               for text in notices(session))


async def test_a_rejected_reasoning_field_is_resent_once_without_it_and_remembered(runtime, monkeypatch):
    manager, session = runtime
    session["reasoning_effort"] = "high"
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        if "output_config" in requests[-1]:
            return httpx.Response(400, json={"error_code": "BAD_REQUEST",
                                             "message": "output_config: Extra inputs are not permitted"})
        return httpx.Response(200, text=sse({"content": "Done."}))
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 2
    assert sent_fields(requests[0]) == {"thinking": ADAPTIVE, "output_config": {"effort": "high"}}
    assert sent_fields(requests[1]) == {}
    assert {key: value for key, value in requests[0].items() if key not in reasoning.REQUEST_FIELDS} == requests[1]
    replies = [event for event in session["events"] if event["type"] == "assistant"]
    assert len(replies) == 1 and replies[0]["text"] == "Done." and replies[0]["request_info"]["status"] == "completed"
    assert replies[0]["request_info"]["reasoning"] == {"effort": "high", "format": "anthropic", "fields": {},
                                                       "skipped": "rejected", "rejected_field": "output_config.effort"}
    assert notices(session) == [
        f"Databricks rejected the reasoning field output_config.effort for {OPUS} (HTTP 400), so the app sent the request "
        "again without reasoning fields. Requests to this endpoint skip them until the app restarts or Settings are "
        "saved. To send them another way, change Settings → Reasoning → Request format."]
    calls = agent_calls(session)
    assert [(call["attempt"], call["status"], call.get("http_status"), call.get("retry_reason"), call.get("effort"),
             call.get("reasoning_skipped")) for call in calls] == [
        (1, "error", 400, None, "high", None), (2, "completed", 200, "reasoning_rejected", "high", "rejected")]
    assert [call.get("reasoning_fields") for call in calls] == [["output_config", "thinking"], None]
    assert session_metrics(session)["calls"][0]["reasoning_fields"] == ["output_config", "thinking"]
    assert manager.reasoning_rejections == {(OPUS, "anthropic"): "output_config.effort"}
    assert session["terminal_reason"] == "completed"
    # Later requests to the endpoint skip the fields without another rejection or notice.
    await manager.run(session, "Again")
    assert len(requests) == 3 and sent_fields(requests[2]) == {}
    assert agent_calls(session)[-1]["reasoning_skipped"] == "rejected_earlier"
    assert len(notices(session)) == 1


async def test_a_400_that_names_no_reasoning_field_is_not_resent(runtime, monkeypatch):
    manager, session = runtime
    session["reasoning_effort"] = "high"
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"message": "The prompt is too long for this model"})
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 1 and manager.reasoning_rejections == {}
    assert session["events"][-1]["type"] == "error" and "HTTP 400" in session["events"][-1]["text"]
    assert [call.get("retry_reason") for call in agent_calls(session)] == [None]


async def test_a_resend_that_is_rejected_again_is_not_repeated(runtime, monkeypatch):
    manager, session = runtime
    session["reasoning_effort"] = "high"
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"message": "thinking: not supported on this route"})
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 2 and sent_fields(requests[1]) == {}
    assert session["events"][-1]["type"] == "error"


async def test_a_settings_save_forgets_rejections(api):
    client, manager, settings, _ = api
    manager.reasoning_rejections[(OPUS, "anthropic")] = "thinking"
    assert (await client.put("/api/settings", json=settings_body(settings))).status_code == 200
    assert manager.reasoning_rejections == {}


# Reasoning text in the stream

@pytest.mark.parametrize("delta,text", [
    ({"content": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "Plan.", "signature": "s"}]}]}, "Plan."),
    ({"content": [{"type": "thinking", "thinking": "Plan.", "signature": "s"}]}, "Plan."),
    ({"reasoning_content": "Plan."}, "Plan."),
    ({"reasoning": "Plan."}, "Plan."),
    ({"reasoning": {"text": "Plan."}}, "Plan."),
    ({"content": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": ""}]}]}, ""),
    ({"content": [{"type": "thinking", "thinking": ""}]}, ""),
    ({"content": [{"type": "redacted_thinking", "data": "opaque"}]}, ""),
])
def test_each_reasoning_delta_shape(delta, text):
    assert reasoning_delta(delta) == (text, True)


def test_answers_and_unknown_shapes_carry_no_reasoning():
    for delta in ({"content": "Answer"}, {"content": [{"type": "text", "text": "Answer"}]}, {}, {"reasoning": 3},
                  {"content": [{"type": "thinking", "text": "internal"}]} | {"x": 1}):
        text, _ = reasoning_delta(delta)
        assert text == ""
    assert reasoning_delta({"content": "Answer"}) == ("", False)
    assert reasoning_summary([{"type": "thinking", "text": "internal"}]) == ""


class TimedStream(httpx.AsyncByteStream):
    """SSE chunks sent one by one, moving the request clock forward before each."""
    def __init__(self, clock, chunks):
        self.clock, self.chunks = clock, chunks

    async def __aiter__(self):
        for seconds, chunk in self.chunks:
            self.clock["now"] += seconds
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
        yield b"data: [DONE]\n\n"


def chunk(delta, finish=None, usage=None):
    value = {"choices": [{"delta": delta, "finish_reason": finish}]}
    if usage:
        value["usage"] = usage
    return value


@pytest.fixture
def clock(monkeypatch):
    value = {"now": 1000.0}
    monkeypatch.setattr("local_agent.agents.thinking_clock", lambda: value["now"])
    return value


@pytest.mark.parametrize("shape", ["summary", "thinking", "reasoning_content", "reasoning"])
async def test_reasoning_text_is_kept_redacted_and_timed_until_the_answer_starts(runtime, monkeypatch, clock, shape):
    manager, session = runtime
    manager.settings.redact = lambda text: text.replace("private-token", "[REDACTED]")
    parts = {"summary": lambda text: {"content": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": text}]}]},
             "thinking": lambda text: {"content": [{"type": "thinking", "thinking": text}]},
             "reasoning_content": lambda text: {"reasoning_content": text},
             "reasoning": lambda text: {"reasoning": {"text": text}}}[shape]
    broadcasts = []
    original = manager.broadcast

    async def broadcast(session_id, data):
        broadcasts.append(copy.deepcopy(data))
        await original(session_id, data)
    manager.broadcast = broadcast

    async def gateway(request):
        return httpx.Response(200, stream=TimedStream(clock, [
            (2.0, chunk(parts("Reading the **parser** with private-token. "))), (5.0, chunk(parts("Then the tests."))),
            (4.5, chunk({"content": "The answer."})), (3.0, chunk({"content": " More."}, "stop"))]))
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Explain")
    saved = manager.store.get(session["id"])
    reply = next(event for event in saved["events"] if event["type"] == "assistant")
    assert reply["reasoning_summary"] == "Reading the **parser** with [REDACTED]. Then the tests."
    assert reply["text"] == "The answer. More."
    assert reply["reasoning_seconds"] == 9.5  # From the first reasoning chunk to the first answer text.
    assert isinstance(reply["reasoning_started"], float)
    assert saved["wire"][-1] == {"role": "assistant", "content": "The answer. More."}  # Never replayed.
    # The UI gets the duration before the answer's first text.
    timed = next(index for index, data in enumerate(broadcasts) if data.get("type") == "event"
                 and data["event"].get("reasoning_seconds") is not None)
    first_text = next(index for index, data in enumerate(broadcasts) if data.get("type") == "delta")
    assert timed < first_text


async def test_thinking_without_text_and_reported_reasoning_tokens_record_a_duration(runtime, monkeypatch, clock):
    manager, session = runtime
    responses = [
        # Thinking items with no text (display omitted), then a tool call ends the thinking.
        [(1.0, chunk({"content": [{"type": "thinking", "thinking": ""}]})), (6.0, chunk(tool_call(), "tool_calls"))],
        # No reasoning items, but the endpoint counted reasoning tokens: about the time before the answer.
        [(4.0, chunk({"content": "Done."})), (1.0, chunk({}, "stop", usage={
            "prompt_tokens": 10, "completion_tokens": 30, "completion_tokens_details": {"reasoning_tokens": 20}}))],
    ]

    async def gateway(request):
        return httpx.Response(200, stream=TimedStream(clock, responses.pop(0)))
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Go")
    first, second = [event for event in manager.store.get(session["id"])["events"] if event["type"] == "assistant"]
    assert "reasoning_summary" not in first and first["reasoning_seconds"] == 6.0 and "reasoning_started" in first
    assert "reasoning_summary" not in second and "reasoning_started" not in second
    assert second["reasoning_seconds"] == 4.0
    assert second["request_info"]["usage"]["reasoning_tokens"] == 20


async def test_no_reasoning_records_no_thinking_time(runtime, monkeypatch, clock):
    manager, session = runtime

    async def gateway(request):
        return httpx.Response(200, stream=TimedStream(clock, [(3.0, chunk({"content": "Done."}, "stop"))]))
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Go")
    reply = next(event for event in session["events"] if event["type"] == "assistant")
    assert "reasoning_seconds" not in reply and "reasoning_started" not in reply


async def test_reasoning_text_is_capped(runtime, monkeypatch):
    manager, session = runtime

    async def gateway(request):
        return httpx.Response(200, text=sse({"reasoning_content": "x" * 9000}, {"reasoning_content": "y" * 9000},
                                            {"content": "Done."}))
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Go")
    reply = next(event for event in session["events"] if event["type"] == "assistant")
    assert len(reply["reasoning_summary"]) == reasoning.SUMMARY_LIMIT and reply["reasoning_truncated"] is True


async def test_a_stopped_response_keeps_its_thinking_time(runtime, monkeypatch, clock):
    manager, session = runtime
    started = asyncio.Event()

    class Hanging(httpx.AsyncByteStream):
        async def __aiter__(self):
            clock["now"] += 1
            yield ("data: " + json.dumps(chunk({"reasoning_content": "Thinking hard."})) + "\n\n").encode()
            clock["now"] += 2
            started.set()
            await asyncio.sleep(30)
            yield b"data: [DONE]\n\n"

    async def gateway(request):
        return httpx.Response(200, stream=Hanging())
    mock_gateway(monkeypatch, gateway)
    task = asyncio.create_task(manager.run(session, "Go"))
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(0.05)
    task.cancel()  # Stop.
    try:
        await task
    except asyncio.CancelledError:
        pass
    reply = next(event for event in session["events"] if event["type"] == "assistant")
    assert reply["reasoning_summary"] == "Thinking hard." and reply["reasoning_seconds"] == 2.0
