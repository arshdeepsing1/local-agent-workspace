"""Opus 5.5 as the default model, per-endpoint limit profiles, the one-time Settings migration,
budgets that follow the endpoint, and the request shapes Opus 5.5 accepts."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent import agents as agents_module
from local_agent import model_profiles
from local_agent.prompt_cache import unmarked
from local_agent.agents import AgentManager, output_limit_text
from local_agent.api import create_app
from local_agent.config import Settings, read_env
from local_agent.context import OUTPUT_LIMIT_CONTINUATION, handoff_output_tokens, prepare_context
from local_agent.model_profiles import (
    DEFAULT_MODEL, auxiliary_output_tokens, context_rate_limit, default_limits, effective_limits, limit_warnings,
    limits_note, output_concurrency_limit, profile_for,
)
from local_agent.store import Store
from local_agent.telemetry import estimated_dbu

OPUS = "databricks-claude-opus-5-5"
GPT = "databricks-gpt-oss-120b"
BACKEND = Path(__file__).resolve().parents[1]


def limits(settings):
    return settings.values["context_window"], settings.values["max_output_tokens"]


def write_settings(state, data):
    state.mkdir(parents=True, exist_ok=True)
    (state / "settings.json").write_text(json.dumps(data))


@pytest.fixture
def app_root(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    return tmp_path


# 1 Defaults


def test_a_fresh_install_without_a_model_uses_opus_5_5_at_its_recommended_limits(app_root, monkeypatch):
    monkeypatch.delenv("LOCAL_AGENT_MODEL")
    settings = Settings(app_root / "state")
    assert settings.values["model"] == OPUS == DEFAULT_MODEL
    assert limits(settings) == (200_000, 16_384)
    assert settings.public()["default_model"] == OPUS
    assert settings.public()["default_limits"] == {"context_window": 200_000, "max_output_tokens": 16_384}
    assert "model_migrated_from" not in settings.public()


@pytest.mark.parametrize("model", [GPT, "my-custom-endpoint"])
def test_an_env_model_without_opus_limits_keeps_131072_and_8192(app_root, monkeypatch, model):
    monkeypatch.setenv("LOCAL_AGENT_MODEL", model)
    assert limits(Settings(app_root / "state")) == (131_072, 8_192)


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_env_model_line_means_the_default_model(app_root, monkeypatch, value):
    monkeypatch.setenv("LOCAL_AGENT_MODEL", value)
    settings = Settings(app_root / "state")
    assert settings.values["model"] == OPUS and limits(settings) == (200_000, 16_384)


def test_an_empty_model_line_in_env_file_means_the_default_model(app_root, monkeypatch):
    monkeypatch.delenv("LOCAL_AGENT_MODEL")
    (app_root / "app").mkdir()
    (app_root / "app" / ".env").write_text("LOCAL_AGENT_MODEL=\n")
    assert Settings(app_root / "state").values["model"] == OPUS


def test_a_versioned_file_without_limits_takes_its_models_defaults(app_root, monkeypatch):
    monkeypatch.delenv("LOCAL_AGENT_MODEL")
    write_settings(app_root / "state", {"model": GPT, "settings_version": 2})
    settings = Settings(app_root / "state")
    assert settings.values["model"] == GPT and limits(settings) == (131_072, 8_192)


@pytest.mark.parametrize("model", [OPUS, GPT, "other"])
def test_a_saved_context_budget_is_kept_under_every_model(app_root, monkeypatch, model):
    monkeypatch.delenv("LOCAL_AGENT_MODEL")
    write_settings(app_root / "state", {"model": model, "context_window": 32_768, "settings_version": 2})
    settings = Settings(app_root / "state")
    assert settings.values["context_window"] == 32_768
    assert settings.values["max_output_tokens"] == default_limits(model)[1]


def test_public_settings_keep_the_budget_keys_in_a_stable_order(app_root):
    keys = list(Settings(app_root / "state").values)
    assert keys[:7] == ["workspace", "model", "env_file", "context_window", "max_output_tokens", "coding_dir",
                        "python_venv"]


# 2 Migration


@pytest.mark.parametrize("env_model", [None, GPT, OPUS, f" {OPUS} "])
def test_a_legacy_file_at_the_old_default_moves_to_opus_once(app_root, monkeypatch, env_model):
    if env_model is None:
        monkeypatch.delenv("LOCAL_AGENT_MODEL")
    else:
        monkeypatch.setenv("LOCAL_AGENT_MODEL", env_model)
    state = app_root / "state"
    write_settings(state, {"workspace": str(app_root), "model": GPT, "env_file": "", "context_window": 131_072,
                           "max_output_tokens": 8_192})
    settings = Settings(state)
    assert settings.values["model"] == OPUS and limits(settings) == (200_000, 16_384)
    assert settings.public()["model_migrated_from"] == GPT
    assert "settings_version" not in json.loads((state / "settings.json").read_text())  # Nothing written at load.
    # The user switches back to GPT OSS: the next load keeps it.
    public = settings.update({**settings.values, "model": GPT})
    assert "model_migrated_from" not in public
    saved = json.loads((state / "settings.json").read_text())
    assert saved["settings_version"] == 2
    reloaded = Settings(state)
    assert reloaded.values["model"] == GPT and limits(reloaded) == (131_072, 8_192)
    assert reloaded.migrated_from is None


def test_a_save_after_the_migration_stops_it_for_good(app_root, monkeypatch):
    monkeypatch.delenv("LOCAL_AGENT_MODEL")
    state = app_root / "state"
    write_settings(state, {"workspace": str(app_root), "model": GPT, "env_file": ""})
    settings = Settings(state)
    settings.update({"user_instructions": "Be brief."})
    assert json.loads((state / "settings.json").read_text())["settings_version"] == 2
    reloaded = Settings(state)
    assert reloaded.values["model"] == OPUS and reloaded.migrated_from is None


def test_no_migration_with_another_env_model_custom_limits_or_a_version(app_root, monkeypatch):
    state = app_root / "state"
    monkeypatch.setenv("LOCAL_AGENT_MODEL", "other")
    write_settings(state, {"model": GPT, "context_window": 131_072, "max_output_tokens": 8_192})
    assert Settings(state).values["model"] == GPT
    monkeypatch.delenv("LOCAL_AGENT_MODEL")
    write_settings(state, {"model": GPT, "context_window": 65_536})
    settings = Settings(state)
    assert settings.values["model"] == GPT and settings.migrated_from is None
    write_settings(state, {"model": GPT, "settings_version": 2})
    assert Settings(state).values["model"] == GPT


# 3 Budgets follow a model change


@pytest.fixture
def gpt_settings(app_root, monkeypatch):
    monkeypatch.setenv("LOCAL_AGENT_MODEL", GPT)
    settings = Settings(app_root / "state")
    settings.values["workspace"] = str(app_root)
    return settings


def test_budgets_at_the_old_models_defaults_follow_the_new_model(gpt_settings):
    public = gpt_settings.update({**gpt_settings.values, "model": OPUS})
    assert (public["context_window"], public["max_output_tokens"]) == (200_000, 16_384)


def test_a_custom_context_budget_stays_and_the_output_still_follows(gpt_settings):
    gpt_settings.update({**gpt_settings.values, "context_window": 65_536})
    public = gpt_settings.update({**gpt_settings.values, "model": OPUS})
    assert (public["context_window"], public["max_output_tokens"]) == (65_536, 16_384)


def test_an_output_default_that_would_not_fit_stays(gpt_settings):
    gpt_settings.update({**gpt_settings.values, "context_window": 16_384})
    public = gpt_settings.update({**gpt_settings.values, "model": OPUS})
    assert (public["context_window"], public["max_output_tokens"]) == (16_384, 8_192)


@pytest.mark.parametrize("model", [GPT, "my-endpoint"])
def test_a_context_default_that_a_custom_output_does_not_fit_is_not_followed(app_root, monkeypatch, model):
    monkeypatch.delenv("LOCAL_AGENT_MODEL")
    settings = Settings(app_root / "state")
    settings.values["workspace"] = str(app_root)
    settings.update({**settings.values, "max_output_tokens": 130_000})
    public = settings.update({**settings.values, "model": model})
    assert (public["model"], public["context_window"], public["max_output_tokens"]) == (model, 200_000, 130_000)


def test_an_output_edited_in_the_same_save_is_kept(gpt_settings):
    public = gpt_settings.update({**gpt_settings.values, "model": OPUS, "max_output_tokens": 12_000})
    assert (public["context_window"], public["max_output_tokens"]) == (200_000, 12_000)


def test_a_save_without_a_model_change_keeps_the_budgets(gpt_settings):
    public = gpt_settings.update({**gpt_settings.values, "user_instructions": "x"})
    assert (public["context_window"], public["max_output_tokens"]) == (131_072, 8_192)


# 4 Recommendation invariants


def test_the_opus_recommendation_fits_the_documented_limits():
    profile = profile_for(OPUS)
    context, output = default_limits(OPUS)
    otpm, itpm = profile["otpm"], profile["itpm"]
    assert 4 * output <= otpm - otpm // 5
    assert output <= output_concurrency_limit(profile) == 20_000
    assert 4 * context <= itpm and context_rate_limit(profile) == 250_000
    assert handoff_output_tokens(context) <= output
    assert context - output - 2048 >= 180_000
    assert limit_warnings(OPUS, context, output) == []
    assert limit_warnings(GPT, *default_limits(GPT)) == []
    assert default_limits("unknown") == (131_072, 8_192)


# 5 Warning boundaries


def codes(model, context, output):
    return [warning["code"] for warning in limit_warnings(model, context, output)]


@pytest.mark.parametrize("context, output, expected", [
    (200_000, 20_000, []),
    (200_000, 20_001, ["output_concurrency"]),
    (200_000, 100_000, ["output_concurrency"]),
    (200_000, 100_001, ["output_over_otpm"]),
    (200_000, 16_383, ["output_below_recommended"]),
    (200_000, 16_384, []),
    (250_000, 16_384, []),
    (250_001, 16_384, ["context_itpm"]),
    (1_000_001, 16_384, ["context_over_endpoint", "context_itpm"]),
    (1_048_576, 128_001, ["context_over_endpoint", "output_over_endpoint", "output_over_otpm", "context_itpm"]),
])
def test_opus_warning_boundaries(context, output, expected):
    assert codes(OPUS, context, output) == expected


def test_warning_levels_and_texts():
    warnings = {item["code"]: item for item in limit_warnings(OPUS, 300_000, 100_001)}
    assert warnings["output_over_otpm"]["level"] == "error"
    assert "Lower it to at most 20,000" in warnings["output_over_otpm"]["text"]
    assert warnings["context_itpm"]["level"] == "note"
    assert "Above 250,000 tokens" in warnings["context_itpm"]["text"]
    concurrency = limit_warnings(OPUS, 200_000, 30_000)[0]
    assert concurrency["level"] == "warning" and "Above 20,000, four requests at once" in concurrency["text"]


def test_no_warnings_without_a_profile_or_for_opus_4_8_at_20000():
    assert limit_warnings("unknown", 1_048_576, 131_072) == []
    assert limit_warnings("databricks-claude-opus-4-8", 131_072, 20_000) == []
    # The rate note uses the capped context budget: GPT OSS 120B sends at most 131,072.
    assert codes(GPT, 300_000, 8_192) == ["context_over_endpoint"]
    assert limits_note("unknown") == ("No built-in limits for this endpoint. Set the context budget at or below "
                                      "its context window.")
    assert "Claude Opus 5.5" in limits_note(OPUS) and "2026-10-13" in limits_note(OPUS)


# 6 Effective limits


@pytest.mark.parametrize("model, requested, expected", [
    (GPT, (200_000, 16_384), (131_072, 16_384, True)),
    (OPUS, (1_048_576, 131_072), (1_000_000, 128_000, True)),
    (GPT, (200_000, 131_072), (131_072, 129_023, True)),
    ("unknown", (200_000, 16_384), (200_000, 16_384, False)),
    (" Databricks-Claude-Opus-5-5 ", (200_000, 16_384), (200_000, 16_384, False)),
])
def test_effective_limits(model, requested, expected):
    result = effective_limits(model, *requested)
    assert (result["context_window"], result["max_output_tokens"], result["clamped"]) == expected


def test_auxiliary_output_tokens_add_the_thinking_reserve():
    assert auxiliary_output_tokens(OPUS, 1024) == 5_120
    assert auxiliary_output_tokens(OPUS, 200_000) == 128_000
    assert auxiliary_output_tokens("test-endpoint", 1024) == 1024
    assert profile_for(None) is None and profile_for(OPUS) is not profile_for(OPUS)


# 7, 8 Agent requests use the chat's effective limits


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Limits", title_generated=True)
    store.save(session)
    yield manager, session
    store.db.close()


def stream(text="Done.", finish="stop"):
    chunk = {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


def summary(text="Earlier work summarized."):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})


def gateway(monkeypatch, respond=None):
    requests = []
    client = httpx.AsyncClient

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if respond is not None:
            return respond(body)
        return stream() if body.get("stream") else summary()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handle)))
    return requests


def notices(session):
    return [event["text"] for event in session["events"] if event["type"] == "notice"]


async def test_a_chat_on_a_smaller_endpoint_is_capped_with_one_notice(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values.update(context_window=200_000, max_output_tokens=16_384)  # Custom on test-endpoint.
    session["model"] = GPT
    requests = gateway(monkeypatch)
    await manager.run(session, "First")
    await manager.run(session, "Second")
    assert [body["max_tokens"] for body in requests if body["stream"]] == [16_384, 16_384]
    assert session["context_info"]["context_window"] == 131_072
    clamp = [text for text in notices(session) if "131,072-token context budget" in text]
    assert clamp == [f"This chat's endpoint ({GPT}) supports less than Settings asks for, so its requests use a "
                     "131,072-token context budget (Settings: 200,000)."]
    assert not any(message.get("content") == clamp[0] for message in session["wire"])  # Never a wire message.
    manager.settings.values["context_window"] = 100_000
    await manager.run(session, "Third")
    assert len([text for text in notices(session) if "supports less" in text]) == 1
    assert "clamped_limits" not in session
    assert session["context_info"]["context_window"] == 100_000
    manager.settings.values["context_window"] = 200_000
    await manager.manual_compact(session)
    assert session["context_info"]["context_window"] == 131_072


async def test_budgets_at_the_settings_models_defaults_follow_the_chats_endpoint(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values.update(model=GPT, context_window=131_072, max_output_tokens=8_192)
    session["model"] = OPUS
    requests = gateway(monkeypatch)
    await manager.run(session, "Go")
    assert requests[-1]["max_tokens"] == 16_384
    assert session["context_info"]["context_window"] == 200_000
    assert not notices(session)
    assert manager.request_limits(session) == (200_000, 16_384)


@pytest.mark.parametrize("chat_model, expected", [("my-endpoint", (200_000, 130_000)), (GPT, (131_072, 129_023))])
def test_a_followed_context_default_that_the_custom_output_does_not_fit_keeps_settings(runtime, chat_model, expected):
    manager, session = runtime
    manager.settings.values.update(model=OPUS, context_window=200_000, max_output_tokens=130_000)
    session["model"] = chat_model
    assert manager.base_limits(session) == (200_000, 130_000)
    assert manager.request_limits(session) == expected  # GPT OSS accepts at most 131,072 tokens of context.


async def test_the_clamp_notice_names_only_what_the_endpoint_caps(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values.update(model=OPUS, context_window=300_000, max_output_tokens=16_384)
    session["model"] = GPT
    requests = gateway(monkeypatch)
    await manager.run(session, "Go")
    assert requests[-1]["max_tokens"] == 8_192  # GPT OSS's default output, not a cap.
    assert [text for text in notices(session) if "supports less" in text] == [
        f"This chat's endpoint ({GPT}) supports less than Settings asks for, so its requests use a 131,072-token "
        "context budget (Settings: 300,000)."]
    manager.settings.values.update(model="test-endpoint", context_window=1_048_576, max_output_tokens=131_072)
    session.pop("clamped_limits")
    await manager.run(session, "Again")
    assert notices(session)[-1].endswith("use a 131,072-token context budget (Settings: 1,048,576) and 129,023 max "
                                         "output tokens (Settings: 131,072).")


async def test_manual_compaction_on_a_capped_chat_gives_the_notice(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values.update(context_window=200_000, max_output_tokens=16_384)
    session["model"] = GPT
    session["wire"] = [{"role": "user", "content": "Earlier"}, {"role": "assistant", "content": "Reply"}] * 3
    gateway(monkeypatch)
    await manager.manual_compact(session)
    assert [text for text in notices(session) if "supports less" in text]


async def test_requests_size_context_with_the_chats_limits_and_its_endpoints_default_output(runtime, monkeypatch):
    manager, session = runtime
    seen = []
    prepare = agents_module.prepare_context

    async def recording(wire, state, system, definitions, context_window, *args, **options):
        seen.append((context_window, options.get("default_reply_reserve"), options.get("reply_reserve")))
        return await prepare(wire, state, system, definitions, context_window, *args, **options)
    monkeypatch.setattr(agents_module, "prepare_context", recording)
    manager.settings.values.update(model=GPT, context_window=1_048_576, max_output_tokens=8_192)
    session["model"] = OPUS
    gateway(monkeypatch)
    await manager.run(session, "Go")
    assert seen[-1] == (1_000_000, 16_384, 16_384)
    session["wire"] = [{"role": "user", "content": "Earlier"}, {"role": "assistant", "content": "Reply"}] * 3
    await manager.manual_compact(session)
    assert seen[-1] == (1_000_000, 16_384, 16_384)
    session["model"] = GPT
    manager.store.save(session)
    await manager.side_question(session["id"], "Where?")
    assert seen[-1][0] == 131_072


async def test_read_file_pages_follow_the_chats_context_budget(runtime, monkeypatch):
    manager, session = runtime
    (Path(session["workspace"]) / "a.txt").write_text("hello\n")
    seen = []
    monkeypatch.setattr(agents_module, "read_page_bytes", lambda context_window: seen.append(context_window) or 4096)
    manager.settings.values.update(context_window=1_048_576, max_output_tokens=8_192)
    session["model"] = GPT
    await manager.execute_tool(session, manager.session_tools(session), "read_file", {"path": "a.txt"}, "r")
    assert seen == [131_072]


async def test_the_clamp_notice_never_saves_the_prompt_before_it_is_marked_in_the_wire(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values.update(context_window=200_000, max_output_tokens=16_384)
    session["model"] = GPT
    gateway(monkeypatch)
    snapshots = []
    save = manager.store.save

    def recording(saved):
        wire = saved.get("wire") or []
        user = next((event for event in reversed(saved["events"]) if event.get("type") == "user"), None)
        snapshots.append((wire[-1].get("content") if wire else None, user and user.get("in_wire")))
        return save(saved)
    manager.store.save = recording
    await manager.run(session, "Clamped prompt")
    assert "supports less" in " ".join(notices(session))
    assert ("Clamped prompt", False) not in snapshots
    assert ("Clamped prompt", True) in snapshots


# 13 Budget errors and the output-limit text


async def test_budget_error_advice_uses_the_endpoints_default_output():
    wire = [{"role": "user", "content": "x" * 400_000}]
    system = {"role": "system", "content": "s"}

    async def summarize(*args):
        return "summary"
    with pytest.raises(ValueError) as error:
        await prepare_context(wire, {}, system, [], 65_536, summarize, reply_reserve=16_384,
                              default_reply_reserve=16_384)
    assert "Increase the context budget in Settings" in str(error.value)
    with pytest.raises(ValueError) as error:
        await prepare_context(wire, {}, system, [], 65_536, summarize, reply_reserve=32_768,
                              default_reply_reserve=16_384)
    assert "currently 32,768; default 16,384" in str(error.value)


def test_the_output_limit_text_names_thinking_for_models_that_always_think(runtime):
    manager, session = runtime
    assert "thinking counts" in output_limit_text(16_384, thinking=True)
    assert "thinking counts" not in output_limit_text(16_384)
    tools = manager.session_tools(session)
    session["model"] = OPUS
    assert "Your thinking counts toward this limit" in manager.context_inputs(session, tools, "manual")[0]["content"]
    session["model"] = GPT
    assert "thinking counts" not in manager.context_inputs(session, tools, "manual")[0]["content"]


# 14 Titles and /btw reserve


async def test_title_requests_add_the_thinking_reserve(runtime, monkeypatch):
    from local_agent.titles import generate_title
    manager, session = runtime
    session["events"] = [{"id": "u", "type": "user", "text": "Fix the parser"},
                         {"id": "a", "type": "assistant", "text": "Fixed."}]
    requests = gateway(monkeypatch, lambda body: summary("Parser fix"))
    for model, expected in ((OPUS, 5_120), ("test-endpoint", 1_024)):
        session["model"] = model
        assert await generate_title(manager.settings, session, save=lambda saved: None) == "Parser fix"
        assert requests[-1]["max_tokens"] == expected
        assert session["inference_calls"][-1]["max_output_tokens"] == expected


async def test_the_side_question_reserve_adds_the_thinking_reserve(runtime, monkeypatch):
    manager, session = runtime
    session["wire"] = [{"role": "user", "content": "Which parser?"}, {"role": "assistant", "content": "parse.py"}]
    requests = gateway(monkeypatch, lambda body: summary("parse.py"))
    session["model"] = OPUS
    manager.store.save(session)
    manager.settings.values.update(model=OPUS, context_window=200_000, max_output_tokens=16_384)
    await manager.side_question(session["id"], "Where is it?")
    assert requests[-1]["max_tokens"] == 8_192
    manager.settings.values.update(model="test-endpoint", context_window=131_072, max_output_tokens=8_192)
    session["model"] = "test-endpoint"
    manager.store.save(session)
    await manager.side_question(session["id"], "Where is it?")
    assert requests[-1]["max_tokens"] == 4_096


# Addendum 10: the request shape Opus 5.5 accepts


FORBIDDEN_KEYS = {"temperature", "top_p", "top_k", "tool_choice"}


def hang_stream(entered):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ("data: " + json.dumps({"choices": [{"delta": {"content": "Partial"}, "finish_reason": None}]})
                   + "\n\n").encode()
            entered.set()
            await asyncio.Event().wait()
    return httpx.Response(200, stream=Stream())


async def test_no_request_kind_sends_options_or_a_prefill_opus_5_5_rejects(runtime, monkeypatch):
    manager, session = runtime
    session.update(model=OPUS, title="New conversation", title_generated=False, permission_mode="bypassPermissions")
    manager.settings.values.update(model=OPUS, context_window=32_768, max_output_tokens=8_192)
    entered = asyncio.Event()
    agent = [lambda: stream("Part one", "length"), stream, lambda: hang_stream(entered), stream, stream, stream]
    summaries = iter([summary("y" * 20_000)])
    kinds = []

    def respond(body):
        if body.get("stream"):
            kinds.append("agent")
            return agent.pop(0)()
        system = body["messages"][0]["content"]
        if system.startswith("Create a short title"):
            kinds.append("title")
            return summary("Opus requests")
        if "tools" in body:
            kinds.append("side_question")
            return summary("An answer.")
        kinds.append("compaction")
        return next(summaries, summary("Short summary."))
    requests = gateway(monkeypatch, respond)
    await manager.run(session, "Write a long answer")  # Output-limit continuation, then a title.
    manager.start(session["id"], "Explain it")
    await asyncio.wait_for(entered.wait(), 2)
    await manager.stop(session["id"])
    session = manager.get(session["id"])
    await manager.run(session, "Continue from where you stopped.")
    await manager.side_question(session["id"], "Which file?")
    manager.settings.values["compaction_handoffs"] = False
    await manager.manual_compact(session)  # A long summary is condensed.
    manager.settings.values["compaction_handoffs"] = True
    await manager.run(session, "One more turn")
    await manager.manual_compact(session)  # A handoff.
    assert {"agent", "title", "side_question", "compaction"} <= set(kinds)
    assert kinds.count("compaction") >= 3
    assert any(unmarked(body)["messages"][-1]["content"] == OUTPUT_LIMIT_CONTINUATION for body in requests)
    for body in requests:
        assert not FORBIDDEN_KEYS & set(body), body.keys()
        last = body["messages"][-1]
        assert not (last["role"] == "assistant" and not last.get("tool_calls")), last


# 10 /api/settings/limits and GET /api/settings


def test_the_limits_route(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}

        def check(body, expected=200, auth=True):
            response = client.post("/api/settings/limits", json=body, headers=headers if auth else {})
            assert response.status_code == expected, response.text
            return response.json()
        result = check({"model": OPUS, "context_window": 200_000, "max_output_tokens": 16_384})
        assert result["profile"]["label"] == "Claude Opus 5.5"
        assert result["profile"]["output_concurrency_limit"] == 20_000
        assert result["recommended"] == {"context_window": 200_000, "max_output_tokens": 16_384}
        assert result["warnings"] == [] and result["effective"]["clamped"] is False
        assert result["defaults"] == {"context_window": 200_000, "max_output_tokens": 16_384}
        assert result["effective"]["input_budget"] == 181_568
        # The input budget requests use: near the endpoint's window it keeps 5% for estimate error, and it
        # follows the endpoint's maxima.
        result = check({"model": OPUS, "context_window": 1_000_000, "max_output_tokens": 16_384})
        assert result["effective"] == {"context_window": 1_000_000, "max_output_tokens": 16_384, "clamped": False,
                                       "input_budget": 934_826}
        result = check({"model": OPUS, "context_window": 1_048_576, "max_output_tokens": 131_072})
        assert result["effective"] == {"context_window": 1_000_000, "max_output_tokens": 128_000, "clamped": True,
                                       "input_budget": (1_000_000 - 128_000 - 2_048) * 100 // 105}
        result = check({"model": "databricks-gpt-oss-120b", "context_window": 131_072, "max_output_tokens": 8_192})
        assert result["effective"]["input_budget"] == 115_078 and result["effective"]["clamped"] is False
        result = check({"model": f" {OPUS} ", "context_window": 300_000, "max_output_tokens": 30_000})
        assert [item["code"] for item in result["warnings"]] == ["output_concurrency", "context_itpm"]
        result = check({"model": "unknown-endpoint", "context_window": 65_536, "max_output_tokens": 8_192})
        assert result["profile"] is None and result["recommended"] is None
        assert result["defaults"] == {"context_window": 131_072, "max_output_tokens": 8_192}
        assert result["effective"]["input_budget"] == 65_536 - 8_192 - 2_048
        assert result["note"].startswith("No built-in limits")
        result = check({"model": "databricks-claude-opus-4-8", "context_window": 65_536, "max_output_tokens": 8_192})
        assert result["recommended"] is None
        check({"model": OPUS, "context_window": 200_000, "max_output_tokens": 16_384}, 403, auth=False)
        for bad in ({"context_window": "200000"}, {"context_window": 1}, {"context_window": 2_000_000},
                    {"max_output_tokens": 131_073}, {"context_window": 65_536, "max_output_tokens": 63_488},
                    {"extra": 1}, {"model": ""}, {"max_output_tokens": 1}):
            check({"model": OPUS, "context_window": 200_000, "max_output_tokens": 16_384, **bad}, 422)
        check({"model": OPUS, "context_window": 65_536, "max_output_tokens": 63_487})
        response = client.get("/api/settings", headers=headers)
        assert response.status_code == 200
        assert response.json()["coding_dir_status"]["state"] == "off"
        assert response.json()["python_venv_status"]["state"] == "off"
        assert client.get("/api/settings").status_code == 403


# 11 Telemetry


def test_opus_5_5_dbu_rates():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
             "cache_creation_input_tokens": 1_000_000}
    assert estimated_dbu(OPUS, usage) == round(57.143 + 285.714 + 2.857 + 71.429, 9)
    assert estimated_dbu("Databricks-Claude-Opus-5-5", usage) == estimated_dbu(OPUS, usage)
    assert estimated_dbu("databricks-claude-opus-4-8", usage) == round(71.429 + 357.143 + 7.143 + 89.286, 9)


# 12 .env.example


def test_env_example_names_opus_5_5_and_leaves_the_optional_folders_empty():
    values = read_env(BACKEND.parent / ".env.example")
    assert values["LOCAL_AGENT_MODEL"] == DEFAULT_MODEL
    assert values["LOCAL_AGENT_CODING_DIR"] == "" and values["LOCAL_AGENT_PYTHON_VENV"] == ""


# 15 Import cycles


@pytest.mark.parametrize("module", ["model_profiles", "user_paths", "config", "jobs", "agents", "api"])
def test_each_module_imports_on_its_own(module):
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    result = subprocess.run([sys.executable, "-c", f"import local_agent.{module}"], cwd=BACKEND, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_the_profile_module_has_the_documented_constants():
    assert model_profiles.MAX_REQUEST_BYTES == 4_000_000 and model_profiles.MAX_EXECUTION_SECONDS == 597
    assert model_profiles.CONCURRENT_REQUESTS == 4
    assert model_profiles.LEGACY_DEFAULT_LIMITS == (131_072, 8_192)
