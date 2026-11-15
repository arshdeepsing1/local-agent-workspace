"""Workflow limits as settings: how many subagents run at once (subagents_running_max, which also sets each
request's share of the endpoint's rate limit) and how many one chat may start in one turn (subagents_per_turn_max)."""
import asyncio
import json

import pytest
from pydantic import ValidationError

from local_agent import model_profiles
from local_agent.api import SettingsEdit
from local_agent.config import Settings
from local_agent.model_profiles import context_rate_limit, output_concurrency_limit, public_profile

from test_parallel_subagents import (Gateway, calls_reply, card, delegate, eventually, make_runtime, reply, run_turn,
                                     tool_messages)

OPUS = "databricks-claude-opus-5-5"
KEYS = {"subagents_running_max": (3, 1, 8), "subagents_per_turn_max": (6, 1, 24)}


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    async for value in make_runtime(tmp_path, monkeypatch):
        yield value
    model_profiles.set_subagents_running_max(3)  # The rate limit share is process-wide.


def held(gate, text):
    async def handler(count, body):
        await gate.wait()
        return reply(text)
    return handler


def done(text):
    async def handler(count, body):
        return reply(text)
    return handler


def change(manager, **values):
    manager.settings.update(values)


async def drain():
    """Let every task that is ready run, so a start that would happen has happened (no timing involved)."""
    for _ in range(30):
        await asyncio.sleep(0)


def is_queued(session, call_id):
    return (card(session, call_id).get("delegation") or {}).get("status") == "queued"


# The two settings: defaults, range, saving.

def test_the_defaults_are_three_running_and_six_per_turn(tmp_path):
    settings = Settings(tmp_path / "state")
    assert settings.values["subagents_running_max"] == 3 and settings.values["subagents_per_turn_max"] == 6
    assert settings.public()["subagents_running_max"] == 3 and settings.public()["subagents_per_turn_max"] == 6


@pytest.mark.parametrize("key, bad", [
    ("subagents_running_max", 0), ("subagents_running_max", 9), ("subagents_running_max", "3"),
    ("subagents_running_max", True), ("subagents_running_max", 2.5),
    ("subagents_per_turn_max", 0), ("subagents_per_turn_max", 25), ("subagents_per_turn_max", "6"),
    ("subagents_per_turn_max", True), ("subagents_per_turn_max", 2.5),
])
def test_a_value_out_of_range_or_of_the_wrong_type_is_refused(tmp_path, key, bad):
    settings = Settings(tmp_path / "state")
    with pytest.raises(ValueError, match="from 1 to"):
        settings.update({key: bad})
    assert settings.values[key] == KEYS[key][0]


@pytest.mark.parametrize("key", list(KEYS))
def test_the_edges_are_saved_and_read_back(tmp_path, key):
    default, low, high = KEYS[key]
    for value in (low, high):
        settings = Settings(tmp_path / "state")
        assert settings.update({key: value})[key] == value
        assert Settings(tmp_path / "state").values[key] == value


@pytest.mark.parametrize("key, bad", [("subagents_running_max", 0), ("subagents_running_max", 9),
                                      ("subagents_running_max", "3"), ("subagents_per_turn_max", 0),
                                      ("subagents_per_turn_max", 25), ("subagents_per_turn_max", "6")])
def test_the_settings_route_model_rejects_them_too(key, bad):
    base = {"workspace": "/tmp", "model": "m", "env_file": ""}
    with pytest.raises(ValidationError):
        SettingsEdit(**base, **{key: bad})
    assert SettingsEdit(**base, **{key: KEYS[key][1]}).model_dump(exclude_none=True)[key] == KEYS[key][1]


@pytest.mark.parametrize("key", list(KEYS))
def test_a_hand_edited_file_value_that_is_not_valid_falls_back_to_the_default(tmp_path, key):
    state = tmp_path / "state"
    state.mkdir()
    for bad in (0, 99, "x", None):
        (state / "settings.json").write_text(json.dumps({key: bad}))
        assert Settings(state).values[key] == KEYS[key][0]


# The share of the endpoint's rate limit follows the running limit: one chat plus that many subagents.

def test_each_requests_share_follows_the_running_limit(tmp_path):
    settings = Settings(tmp_path / "state")
    profile = model_profiles.profile_for(OPUS)
    try:
        assert model_profiles.concurrent_requests() == 4 == model_profiles.CONCURRENT_REQUESTS
        base = (output_concurrency_limit(profile), context_rate_limit(profile))
        assert base == (profile["otpm"] * 4 // 5 // 4, profile["itpm"] // 4)
        settings.update({"subagents_running_max": 7})
        assert model_profiles.concurrent_requests() == 8
        assert output_concurrency_limit(profile) == profile["otpm"] * 4 // 5 // 8 < base[0]
        assert context_rate_limit(profile) == profile["itpm"] // 8 < base[1]
        shown = public_profile(OPUS)
        assert shown["output_concurrency_limit"] == output_concurrency_limit(profile)
        assert shown["context_rate_limit"] == context_rate_limit(profile)
        settings.update({"subagents_running_max": 1})
        assert model_profiles.concurrent_requests() == 2
        assert output_concurrency_limit(profile) > base[0] and context_rate_limit(profile) > base[1]
        assert Settings(tmp_path / "state").values["subagents_running_max"] == 1
        assert model_profiles.concurrent_requests() == 2  # Loading the saved file sets it, too.
    finally:
        model_profiles.set_subagents_running_max(3)


def test_the_limit_warnings_use_the_same_share(tmp_path):
    settings = Settings(tmp_path / "state")
    profile = model_profiles.profile_for(OPUS)
    try:
        settings.update({"subagents_running_max": 8})
        output = output_concurrency_limit(profile) + 1  # Over the share of nine requests at once.
        warned = [item for item in model_profiles.limit_warnings(OPUS, 200_000, output) if item["code"] == "output_concurrency"]
        assert len(warned) == 1 and "nine requests at once" in warned[0]["text"] and "up to eight subagents" in warned[0]["text"]
        settings.update({"subagents_running_max": 1})
        assert "output_concurrency" not in {item["code"] for item in model_profiles.limit_warnings(OPUS, 200_000, output)}
    finally:
        model_profiles.set_subagents_running_max(3)


async def test_the_rate_limit_too_low_text_names_the_requests_at_once_from_the_running_limit(runtime):
    from local_agent import agents
    from local_agent.rate_limits import OTPM, RateLimitInfo
    manager, parent, _ = runtime
    info = RateLimitInfo(limit_type=OTPM, limit=40_000)

    def refused_text():
        manager.rate_gate.note(parent["model"], "other", 0, info)
        preflight = manager.otpm_preflight(parent, 50_000)
        error, _ = manager.rate_limit_error(parent, agents.InferenceError("429", "rate_limit", 429), {}, {}, 50_000, "")
        return preflight, error
    manager.settings.update({"subagents_running_max": 7})
    agents_parse = agents.parse_rate_limit
    agents.parse_rate_limit = lambda payload, diagnostic, redact: info
    try:
        preflight, error = refused_text()
        for text in (str(preflight), str(error)):
            assert "With 8 requests at once" in text and "at most 5,000 each" in text
        manager.settings.update({"subagents_running_max": 3})
        preflight, error = refused_text()
        for text in (str(preflight), str(error)):
            assert "With 4 requests at once" in text and "at most 10,000 each" in text
    finally:
        agents.parse_rate_limit = agents_parse


# How many run together.

async def test_with_a_limit_of_one_three_delegations_run_one_after_another(runtime, monkeypatch):
    manager, parent, _ = runtime
    change(manager, subagents_running_max=1)
    gates = {task: asyncio.Event() for task in ("A", "B", "C")}
    gateway = Gateway(monkeypatch, [lambda: calls_reply(*(delegate(task, f"call-{task}") for task in gates)),
                                    lambda: reply("All done.")],
                      {task: held(gates[task], f"{task} done.") for task in gates})
    manager.start(parent["id"], "Delegate three")
    await asyncio.wait_for(gateway.child_started("A").wait(), 5)
    live = manager.live[parent["id"]]
    await eventually(lambda: is_queued(live, "call-B") and is_queued(live, "call-C"))
    assert manager.delegates.running() == 1 and not gateway.child_started("B").is_set()
    gates["A"].set()
    await asyncio.wait_for(gateway.child_started("B").wait(), 5)
    await eventually(lambda: manager.delegates.running() == 1)
    assert not gateway.child_started("C").is_set()
    gates["B"].set()
    await asyncio.wait_for(gateway.child_started("C").wait(), 5)
    gates["C"].set()
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    messages = tool_messages(manager.store.get(parent["id"])["wire"])
    assert [json.loads(output)["output"] for _, output in messages] == ["A done.", "B done.", "C done."]


async def test_with_a_limit_of_five_five_delegations_run_together(runtime, monkeypatch):
    manager, parent, _ = runtime
    change(manager, subagents_running_max=5)
    names = ["A", "B", "C", "D", "E"]
    gate = asyncio.Event()
    gateway = Gateway(monkeypatch, [lambda: calls_reply(*(delegate(task, f"call-{task}") for task in names)),
                                    lambda: reply("All done.")],
                      {task: held(gate, f"{task} done.") for task in names})
    manager.start(parent["id"], "Delegate five")
    for task in names:
        await asyncio.wait_for(gateway.child_started(task).wait(), 5)
    assert manager.delegates.running() == 5
    gate.set()
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    assert len(tool_messages(manager.store.get(parent["id"])["wire"])) == 5


async def test_raising_the_limit_starts_the_queued_delegations(runtime, monkeypatch):
    manager, parent, _ = runtime
    change(manager, subagents_running_max=1)
    gate = asyncio.Event()
    gateway = Gateway(monkeypatch, [lambda: calls_reply(*(delegate(task, f"call-{task}") for task in "ABC")),
                                    lambda: reply("All done.")],
                      {task: held(gate, f"{task} done.") for task in "ABC"})
    manager.start(parent["id"], "Delegate three")
    await asyncio.wait_for(gateway.child_started("A").wait(), 5)
    live = manager.live[parent["id"]]
    await eventually(lambda: is_queued(live, "call-B") and is_queued(live, "call-C"))
    change(manager, subagents_running_max=3)
    await asyncio.wait_for(gateway.child_started("B").wait(), 5)
    await asyncio.wait_for(gateway.child_started("C").wait(), 5)
    assert manager.delegates.running() == 3
    gate.set()
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    assert manager.delegates.waiters == []


async def test_lowering_the_limit_stops_nothing_and_holds_the_next_until_enough_have_ended(runtime, monkeypatch):
    manager, parent, _ = runtime
    gates = {task: asyncio.Event() for task in "ABCD"}
    gateway = Gateway(monkeypatch, [lambda: calls_reply(*(delegate(task, f"call-{task}") for task in gates)),
                                    lambda: reply("All done.")],
                      {task: held(gates[task], f"{task} done.") for task in gates})
    manager.start(parent["id"], "Delegate four")
    for task in "ABC":
        await asyncio.wait_for(gateway.child_started(task).wait(), 5)
    live = manager.live[parent["id"]]
    await eventually(lambda: is_queued(live, "call-D"))
    change(manager, subagents_running_max=2)
    await drain()
    assert manager.delegates.running() == 3 and not gateway.child_started("D").is_set()  # Nothing was stopped.
    gates["A"].set()
    await eventually(lambda: manager.delegates.running() == 2)
    await drain()
    assert not gateway.child_started("D").is_set()  # Two still run, which is the limit.
    gates["B"].set()
    await asyncio.wait_for(gateway.child_started("D").wait(), 5)
    for gate in gates.values():
        gate.set()
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    messages = tool_messages(manager.store.get(parent["id"])["wire"])
    assert [json.loads(output)["status"] for _, output in messages] == ["completed"] * 4


# How many one chat starts in one turn.

REFUSED = "This turn already started 2 subagents, the limit in Settings"


async def test_a_batch_over_the_turn_limit_starts_the_first_and_refuses_the_rest(runtime, monkeypatch):
    manager, parent, _ = runtime
    change(manager, subagents_per_turn_max=2)
    names = ["A", "B", "C", "D"]
    gateway = Gateway(monkeypatch, [lambda: calls_reply(*(delegate(task, f"call-{task}") for task in names)),
                                    lambda: reply("Used the two reports.")],
                      {task: done(f"{task} done.") for task in names})
    saved = await run_turn(manager, parent)
    assert set(gateway.requests) - {"parent"} == {"A", "B"}  # The refused two never reached the model.
    second = gateway.requests["parent"][1]  # The turn went on: the next step has all four results.
    results = dict(tool_messages(second["messages"]))
    assert list(results) == ["call-A", "call-B", "call-C", "call-D"]
    assert json.loads(results["call-A"])["output"] == "A done." and json.loads(results["call-B"])["output"] == "B done."
    for call_id in ("call-C", "call-D"):
        assert REFUSED in results[call_id] and "Continue with their results" in results[call_id]
        assert "child_session_id" not in card(saved, call_id) and card(saved, call_id).get("delegation") is None
    assert len(saved["subagents"]) == 2 and manager.delegates.running() == 0 and manager.delegates.waiters == []
    assert saved["events"][-1]["text"] == "Used the two reports."


async def test_a_new_turn_may_start_as_many_again(runtime, monkeypatch):
    manager, parent, _ = runtime
    change(manager, subagents_per_turn_max=2)
    names = ["A", "B", "C", "D", "E", "F", "G", "H"]
    gateway = Gateway(monkeypatch, [lambda: calls_reply(*(delegate(task, f"call-{task}") for task in names[:4])),
                                    lambda: reply("First turn done."),
                                    lambda: calls_reply(*(delegate(task, f"call-{task}") for task in names[4:])),
                                    lambda: reply("Second turn done.")],
                      {task: done(f"{task} done.") for task in names})
    await run_turn(manager, parent, "First")
    saved = await run_turn(manager, parent, "Second")
    assert set(gateway.requests) - {"parent"} == {"A", "B", "E", "F"}
    results = dict(tool_messages(gateway.requests["parent"][3]["messages"]))
    assert json.loads(results["call-E"])["output"] == "E done." and json.loads(results["call-F"])["output"] == "F done."
    assert REFUSED in results["call-G"] and REFUSED in results["call-H"]
    assert len(saved["subagents"]) == 4


async def test_calls_in_separate_responses_of_one_turn_count_together(runtime, monkeypatch):
    manager, parent, _ = runtime
    change(manager, subagents_per_turn_max=2)
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("A", "call-A"), delegate("B", "call-B")),
                                    lambda: calls_reply(delegate("C", "call-C")),
                                    lambda: reply("Done.")],
                      {task: done(f"{task} done.") for task in "ABC"})
    saved = await run_turn(manager, parent)
    assert set(gateway.requests) - {"parent"} == {"A", "B"}
    assert REFUSED in card(saved, "call-C")["output"] and len(saved["subagents"]) == 2


async def test_background_delegations_count_toward_the_turn_limit(runtime, monkeypatch):
    manager, parent, _ = runtime
    change(manager, subagents_per_turn_max=2)
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("A", "call-A"),
                                                        delegate("B", "call-B", background=True),
                                                        delegate("C", "call-C", background=True)),
                                    lambda: reply("Done.")],
                      {task: done(f"{task} done.") for task in "ABC"})
    saved = await run_turn(manager, parent)
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    assert set(gateway.requests) - {"parent"} == {"A", "B"}
    assert REFUSED in card(saved, "call-C")["output"]
    assert "background" not in json.loads(tool_messages(gateway.requests["parent"][1]["messages"])[0][1])
    assert len(manager.store.get(parent["id"])["subagents"]) == 2


async def test_the_default_allows_six_in_a_turn(runtime, monkeypatch):
    manager, parent, _ = runtime
    names = list("ABCDEFG")
    gateway = Gateway(monkeypatch, [lambda: calls_reply(*(delegate(task, f"call-{task}") for task in names)),
                                    lambda: reply("Done.")],
                      {task: done(f"{task} done.") for task in names})
    saved = await run_turn(manager, parent)
    assert set(gateway.requests) - {"parent"} == set("ABCDEF")
    assert "This turn already started 6 subagents, the limit in Settings" in card(saved, "call-G")["output"]


async def test_a_chain_of_wake_turns_shares_the_allowance_of_the_turn_that_started_the_subagents(runtime, monkeypatch):
    """With Continue the chat when a background subagent finishes on, the app's own message starts a turn nobody
    watches. It does not get a fresh allowance: only your next message does."""
    manager, parent, _ = runtime
    change(manager, subagents_per_turn_max=2, background_subagent_wake=True)
    gate = asyncio.Event()

    def parent_reply():
        last = gateway.requests["parent"][-1]["messages"][-1]
        if last["role"] == "tool":
            return reply("Done.")
        if "Start two" in last["content"]:
            return calls_reply(delegate("A", "call-A", background=True), delegate("B", "call-B", background=True))
        if "Go on" in last["content"]:
            return calls_reply(delegate("D", "call-D", background=True), delegate("E", "call-E", background=True))
        return calls_reply(delegate("C", "call-C", background=True))  # What a wake turn tries.
    gateway = Gateway(monkeypatch, [parent_reply], {"A": held(gate, "A done."), "B": held(gate, "B done."),
                                                    "C": held(asyncio.Event(), "C done."), "D": held(asyncio.Event(), "D done."),
                                                    "E": held(asyncio.Event(), "E done.")})
    await run_turn(manager, parent, "Start two")
    gate.set()
    await eventually(lambda: any(item.get("background_wake") for item in manager.get(parent["id"])["events"])
                     and any(item.get("call_id") == "call-C" and item.get("state") in ("rejected", "error", "completed")
                             for item in manager.get(parent["id"])["events"]) and not manager.busy_task(parent["id"]))
    await drain()
    saved = manager.store.get(parent["id"])
    tried = [item for item in saved["events"] if item.get("call_id") == "call-C"]
    assert tried and all(REFUSED in item["output"] for item in tried)
    assert set(gateway.requests) - {"parent"} == {"A", "B"}
    # A message of yours is a new turn: two more may start.
    await run_turn(manager, parent, "Go on")
    assert {"D", "E"} <= set(gateway.requests) and "C" not in gateway.requests
