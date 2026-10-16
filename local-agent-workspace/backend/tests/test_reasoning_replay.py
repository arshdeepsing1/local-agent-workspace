"""Signed reasoning in the history (reasoning_replay.py): what a reply keeps, what Settings → Reasoning → Send earlier
thinking back sends again during tool use (and to which endpoint), the HTTP 400 fallback, the conversation file and
bundle format, and whole tool loops against the offline simulator in its --replay require mode."""
import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from local_agent import reasoning_replay
from local_agent.agents import AgentManager, model_history
from local_agent.config import Settings
from local_agent.portability import export_bundle, validate_bundle
from local_agent.reasoning_replay import Collector, apply, sidecar
from local_agent.serving import ReplayClient, ServingClient, sse_chunks
from local_agent.store import Store

OPUS, OPUS_48 = "databricks-claude-opus-5-5", "databricks-claude-opus-4-8"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "simulate_databricks.py"


def reasoning_chunks(text="Look first.", signature="sig-1"):
    pieces = [text[:4], text[4:]]
    return [*({"choices": [{"delta": {"content": [{"type": "reasoning", "summary": [
        {"type": "summary_text", "text": piece, "signature": ""}]}]}, "finish_reason": None}]} for piece in pieces),
        {"choices": [{"delta": {"content": [{"type": "reasoning", "summary": [
            {"type": "summary_text", "text": "", "signature": signature}]}]}, "finish_reason": None}]}]


def signed_item(text="Look first.", signature="sig-1"):
    return {"type": "reasoning", "summary": [{"type": "summary_text", "text": text, "signature": signature}]}


# Collecting and keeping a reply's reasoning

def test_the_collector_joins_streamed_pieces_and_keeps_only_signed_items():
    collector = Collector()
    for chunk in reasoning_chunks():
        collector.add(chunk["choices"][0]["delta"])
    collector.add({"content": [{"type": "thinking", "thinking": "unsigned", "signature": ""}]})
    collector.add({"content": [{"type": "redacted_thinking", "data": "opaque"}]})
    collector.add({"reasoning_content": "plain text reasoning has no signature"})
    assert collector.result() == [signed_item(), {"type": "redacted_thinking", "data": "opaque"}]
    large = Collector()
    large.add({"content": [{"type": "thinking", "thinking": "x" * (reasoning_replay.MAX_SIDECAR_BYTES + 1), "signature": "s"}]})
    assert large.result() == [] and large.too_large


def test_a_sidecar_is_bounded_and_never_holds_a_secret():
    size = len(json.dumps([signed_item()], separators=(",", ":")).encode("utf-8"))
    assert sidecar(OPUS, [signed_item()]) == {"model": OPUS, "items": [signed_item()], "bytes": size}
    assert sidecar(OPUS, []) is None and sidecar("", [signed_item()]) is None
    assert sidecar(OPUS, [signed_item("token private-token")], lambda text: text.replace("private-token", "[R]")) is None
    assert sidecar(OPUS, [signed_item("x" * reasoning_replay.MAX_SIDECAR_BYTES)]) is None


# Sending it back: this turn, this endpoint

def tool_turn(model=OPUS, prompt="Read it."):
    call = {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"a\"}"}}
    return [{"role": "user", "content": "Earlier."},
            {"role": "assistant", "content": "Earlier reply.", "reasoning": sidecar(model, [signed_item("old")])},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "Reading.", "tool_calls": [call], "reasoning": sidecar(model, [signed_item()])},
            {"role": "tool", "tool_call_id": "call_1", "content": "contents"}]


def prepared(wire):
    history = model_history(wire)
    return [{"role": "system", "content": "System."}, *history], history


def test_only_this_turns_replies_from_the_same_endpoint_get_their_reasoning_back():
    wire = tool_turn()
    messages, history = prepared(wire)
    assert all("reasoning" not in message for message in history)  # The history never carries the sidecar.
    sent, count = apply(messages, history, wire, OPUS)
    assert count == 1 and sent[4]["content"] == [signed_item(), {"type": "text", "text": "Reading."}]
    assert sent[2]["content"] == "Earlier reply."  # An earlier turn is never sent back.
    assert sent[4]["tool_calls"] == wire[3]["tool_calls"] and messages[4]["content"] == "Reading."
    # Another endpoint (a model change or a fallback) never receives them; matching ignores case.
    assert apply(messages, history, wire, OPUS_48) == (messages, 0)
    assert apply(messages, history, wire, OPUS.upper())[1] == 1


def test_a_compacted_or_rewritten_reply_is_not_sent_back():
    wire = tool_turn()
    messages, history = prepared(wire)
    summary = [messages[0], {"role": "user", "content": "Summary."}, messages[-1]]  # The reply was summarized away.
    assert apply(summary, history, wire, OPUS) == (summary, 0)
    broken = tool_turn()
    broken[3]["tool_calls"][0]["function"]["arguments"] = "{not json"
    messages, history = prepared(broken)
    assert apply(messages, history, broken, OPUS) == (messages, 0)  # model_history rewrote it without its calls.


def test_newer_replies_go_first_within_the_byte_limit():
    wire = tool_turn()
    second = {"id": "call_2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    wire += [{"role": "assistant", "content": None, "tool_calls": [second], "reasoning": sidecar(OPUS, [signed_item("new")])},
             {"role": "tool", "tool_call_id": "call_2", "content": "more"}]
    messages, history = prepared(wire)
    sent, count = apply(messages, history, wire, OPUS, limit=wire[5]["reasoning"]["bytes"])
    assert count == 1 and sent[6]["content"] == [signed_item("new")] and sent[4]["content"] == "Reading."
    assert reasoning_replay.pending_bytes(wire, OPUS) == wire[3]["reasoning"]["bytes"] + wire[5]["reasoning"]["bytes"]
    assert reasoning_replay.prune(wire) == 3 and reasoning_replay.pending_bytes(wire, OPUS) == 0


# The conversation file and bundles

def test_old_files_load_and_bundles_keep_reasoning_only_when_asked(tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    session = store.create({"workspace": str(tmp_path), "model": OPUS})
    session["wire"] = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello."}]
    store.save(session)
    assert store.get(session["id"])["wire"] == session["wire"]  # A history saved before the sidecar loads as it was.
    session["wire"] = tool_turn()[2:]
    store.save(session)
    assert store.get(session["id"])["wire"][1]["reasoning"]["items"] == [signed_item()]
    plain = export_bundle([session], session["id"])
    assert all("reasoning" not in message for message in plain["sessions"][0]["wire"])
    kept = export_bundle([session], session["id"], with_reasoning=True)
    assert kept["sessions"][0]["wire"][1]["reasoning"] == session["wire"][1]["reasoning"]
    assert validate_bundle(json.loads(json.dumps(kept)))["sessions"][0]["wire"][1]["reasoning"]["model"] == OPUS
    for bad in ({"role": "user", "content": "x", "reasoning": session["wire"][1]["reasoning"]},
                {"role": "assistant", "content": "x", "reasoning": {"model": OPUS, "items": [{"type": "text"}], "bytes": 1}},
                {"role": "assistant", "content": "x", "reasoning": {"model": OPUS, "items": [signed_item("x" * 70_000)],
                                                                   "bytes": 1}}):
        bundle = json.loads(json.dumps(plain))
        bundle["sessions"][0]["wire"] = [bad] if bad["role"] == "user" else [{"role": "user", "content": "q"}, bad]
        with pytest.raises(ValueError):
            validate_bundle(bundle)
    store.db.close()


# Whole turns

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_MODEL", OPUS)
    (tmp_path / "README.md").write_text("# Demo\n\nA tiny project.\n")
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="", thinking_replay=True)
    store = Store(tmp_path / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Replay", title_generated=True)
    yield manager, session
    store.db.close()


def tool_step(call_id="call_1", signature="sig-1"):
    return [*reasoning_chunks(signature=signature), *sse_chunks("Reading.", "tool_calls", tool_calls=[
        {"id": call_id, "name": "read_file", "arguments": {"path": "README.md"}}])]


def agent(chunks=None, **fixture):
    return {"model": fixture.pop("model", OPUS), "match": lambda body: body.get("stream") is True,
            **({"chunks": chunks} if chunks is not None else {}), **fixture}


def items_in(body):
    return [part for message in body["messages"] if message.get("role") == "assistant"
            and isinstance(message.get("content"), list) for part in message["content"] if part.get("type") != "text"]


async def test_a_tool_loop_sends_its_signed_reasoning_back_and_the_next_turn_drops_it(runtime):
    manager, session = runtime
    manager.serving = ReplayClient(manager.settings, [agent(tool_step()), agent(sse_chunks("It is a demo."))])
    await manager.run(session, "What is this project?")
    bodies = manager.serving.transport.bodies(OPUS)
    assert items_in(bodies[0]) == [] and items_in(bodies[1]) == [signed_item()]
    reply = [event for event in session["events"] if event["type"] == "assistant"][-1]
    assert reply["request_info"]["reasoning_replayed"] == 1
    assert session["inference_calls"][1]["reasoning_replayed"] == 1
    assert session["context_info"]["replayed_reasoning_tokens"] > 0
    assert session["wire"][1]["reasoning"]["model"] == OPUS  # Kept with the reply until the next turn.
    manager.serving.transport.add(agent(sse_chunks("Sure.")))
    await manager.run(session, "Thanks.")
    assert all("reasoning" not in message for message in session["wire"])
    assert items_in(manager.serving.transport.bodies(OPUS)[2]) == []


async def test_a_step_note_does_not_stop_the_reasoning_going_back(runtime, monkeypatch):
    # The changed-files or jobs note joins the last message after the reasoning is matched to the history.
    manager, session = runtime
    notes = iter(["", "Background jobs finished since your last step: 1234abcd `make` completed (exit 0)."])
    monkeypatch.setattr(manager, "step_notices", lambda session, tools: next(notes, ""))
    manager.serving = ReplayClient(manager.settings, [agent(tool_step()), agent(sse_chunks("It is a demo."))])
    await manager.run(session, "What is this project?")
    second = manager.serving.transport.bodies(OPUS)[1]
    assert items_in(second) == [signed_item()]
    assert "Background jobs finished since your last step" in json.dumps(second["messages"][-1])
    assert "Background jobs finished" not in json.dumps(second["messages"][0])


async def test_with_the_setting_off_nothing_is_kept_or_sent(runtime):
    manager, session = runtime
    manager.settings.values["thinking_replay"] = False
    manager.serving = ReplayClient(manager.settings, [agent(tool_step()), agent(sse_chunks("Done."))])
    await manager.run(session, "What is this project?")
    assert items_in(manager.serving.transport.bodies(OPUS)[1]) == []
    assert "sig-1" not in json.dumps(manager.store.get(session["id"]))


async def test_a_400_naming_the_thinking_resends_without_it_and_remembers_the_endpoint(runtime):
    manager, session = runtime
    manager.serving = ReplayClient(manager.settings, [
        agent(tool_step()),
        agent(status=400, json={"error_code": "BAD_REQUEST", "message": "messages[2]: invalid signature in thinking block"}),
        agent(sse_chunks("Done."))])
    await manager.run(session, "What is this project?")
    bodies = manager.serving.transport.bodies(OPUS)
    assert items_in(bodies[1]) == [signed_item()] and items_in(bodies[2]) == []
    assert OPUS in manager.replay_rejections and manager.replay_model(session) is None
    assert any("earlier thinking sent back" in event.get("text", "") for event in session["events"])
    assert [call.get("retry_reason") for call in session["inference_calls"][:3]] == [None, None, "replay_rejected"]
    # Saved Settings forget the rejection, like the other request fallbacks.
    manager.replay_rejections.clear()
    assert manager.replay_model(session) == OPUS


async def test_an_unexplained_400_counts_against_the_thinking_only_once_the_resend_is_accepted(runtime):
    manager, session = runtime
    manager.serving = ReplayClient(manager.settings, [
        agent(tool_step()), agent(status=400, text="Bad request", times=None)])
    await manager.run(session, "What is this project?")
    assert OPUS not in manager.replay_rejections  # The request failed without it too: not about the thinking.
    assert any(event["type"] == "error" and "HTTP 400" in event["text"] for event in session["events"])


async def test_a_fallback_endpoint_never_receives_another_endpoints_reasoning(runtime, monkeypatch):
    manager, session = runtime

    async def sleep(delay):
        pass
    monkeypatch.setattr("local_agent.agents.asyncio.sleep", sleep)
    manager.settings.values["fallback_model"] = OPUS_48
    manager.serving = ReplayClient(manager.settings, [
        agent(tool_step()), agent(status=503, text="Unavailable", times=4),
        agent(sse_chunks("Done by the fallback."), model=OPUS_48)])
    await manager.run(session, "What is this project?")
    (fallback,) = manager.serving.transport.bodies(OPUS_48)
    assert items_in(fallback) == [] and "sig-1" not in json.dumps(fallback)
    assert all(items_in(body) == [signed_item()] for body in manager.serving.transport.bodies(OPUS)[1:])


@pytest.fixture(scope="module")
def simulator():
    spec = importlib.util.spec_from_file_location("simulate_databricks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gateway(simulator):
    gateway = simulator.Gateway
    gateway.speed, gateway.claude_fields, gateway.cache_mode = 0.0, "both", "accept"
    gateway.replay_mode, gateway.vision_mode, gateway.response_format_mode = "require", "accept", "accept"
    gateway.prompt_cache = simulator.PromptCache()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    gateway.replay_mode = "accept"
    server.shutdown()
    server.server_close()


class Recording(httpx.AsyncBaseTransport):
    """Forwards to the simulator and keeps each JSON body sent."""

    def __init__(self):
        self.inner, self.bodies = httpx.AsyncHTTPTransport(), []

    async def handle_async_request(self, request):
        content = await request.aread()
        if content:
            self.bodies.append(json.loads(content))
        return await self.inner.handle_async_request(request)


class GatewayClient(ServingClient):
    def __init__(self, settings, host):
        super().__init__(settings, Recording())
        self.host = host

    def credentials(self):
        return self.host, "simulated-token"


async def test_a_tool_loop_round_trips_its_signed_reasoning_through_the_simulator(runtime, gateway):
    manager, session = runtime
    manager.serving = GatewayClient(manager.settings, gateway)
    await manager.run(session, "read the project")  # project_overview, read_file, then the answer.
    reply = [event for event in session["events"] if event["type"] == "assistant"][-1]
    assert reply["text"].startswith("I looked at the project overview"), session["events"][-1]
    agent_bodies = [body for body in manager.serving.transport.bodies if body.get("stream")]
    assert len(agent_bodies) == 3 and [len(items_in(body)) for body in agent_bodies] == [0, 1, 2]
    signature = items_in(agent_bodies[2])[1]["summary"][0]["signature"]
    assert signature.startswith("sim-") and "sim-" not in json.dumps(model_history(session["wire"]))


async def test_without_the_setting_a_route_that_requires_signed_thinking_rejects_the_tool_loop(runtime, gateway):
    manager, session = runtime
    manager.settings.values["thinking_replay"] = False
    manager.serving = GatewayClient(manager.settings, gateway)
    await manager.run(session, "read the project")
    assert any(event["type"] == "error" and "signed thinking block" in event["text"] for event in session["events"])


async def test_a_reply_that_failed_keeps_no_reasoning(runtime):
    manager, session = runtime
    cut = [*reasoning_chunks(), *sse_chunks("Partial", finish="length")]
    manager.serving = ReplayClient(manager.settings, [agent(cut), agent(sse_chunks("Shorter."))])
    await manager.run(session, "Write it all.")
    assert all("reasoning" not in message for message in session["wire"])  # Output limit, then a plain reply.
    assert items_in(manager.serving.transport.bodies(OPUS)[1]) == []
