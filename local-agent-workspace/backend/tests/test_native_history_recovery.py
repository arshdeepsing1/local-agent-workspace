import copy
import json

import httpx
import pytest

from local_agent.agents import AgentManager, repair_tool_history
from local_agent.config import Settings
from local_agent.portability import _validate_wire
from local_agent.store import Store


def request(call_id="reused", path="note.txt", name="read_file"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps({"path": path})}}


def assistant(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def result(call_id="reused", output="Earlier result"):
    return {"role": "tool", "tool_call_id": call_id, "content": output}


def event(call_id="reused", path="note.txt", state="completed", output="Earlier result", name="read_file"):
    return {"id": call_id + output, "type": "tool", "call_id": call_id, "name": name,
            "input": {"path": path}, "state": state, "output": output}


@pytest.mark.parametrize("state", ["completed", "error", "rejected", "pending", "running", "cancelled"])
@pytest.mark.parametrize("output", ["Latest result", ""])
def test_repeated_id_reuses_only_this_occurrences_terminal_event(state, output):
    wire = [assistant(request()), result(), assistant(request())]
    events = [event(), event(state=state, output=output)]
    original = copy.deepcopy(wire)
    repaired, context = repair_tool_history(wire, events, {})
    assert repaired[:len(wire)] == original and wire == original
    assert repaired[-1]["tool_call_id"] == "reused"
    if state in ("completed", "error", "rejected"):
        assert repaired[-1]["content"] == output
    else:
        assert "outcome is unknown" in repaired[-1]["content"]
    _validate_wire(repaired, complete=True)
    assert context == {}


def test_missing_legacy_events_cannot_lend_an_earlier_success_to_identical_request():
    wire = [assistant(request()), result(), assistant(request())]
    repaired, _ = repair_tool_history(wire, [event()], {})
    assert repaired[1] == result()
    assert "outcome is unknown" in repaired[-1]["content"]


def test_partial_event_history_can_recover_unique_name_and_input_occurrence():
    wire = [assistant(request(path="first.txt")), result(), assistant(request(path="second.txt"))]
    repaired, _ = repair_tool_history(wire, [event(path="second.txt", output="Latest saved action")], {})
    assert repaired[-1]["content"] == "Latest saved action"


def test_conflicting_recorded_sequence_stays_unknown_instead_of_reusing_later_success():
    wire = [assistant(request()), result(), assistant(request())]
    events = [event(output="Does not match the earlier result"), event(output="Cannot safely attribute")]
    repaired, _ = repair_tool_history(wire, events, {})
    assert "outcome is unknown" in repaired[-1]["content"]


def test_multiple_calls_preserve_existing_result_order_and_fill_only_missing_occurrences():
    wire = [assistant(request()), result(), assistant(request(), request("second"), request("third")),
            result("third", "Third saved"), result("second", "Second saved")]
    original = copy.deepcopy(wire)
    events = [event(), event(output="Latest first saved"), event("second", output="Second saved"),
              event("third", output="Third saved")]
    repaired, _ = repair_tool_history(wire, events, {})
    assert repaired[:len(wire)] == original
    assert repaired[-1] == result(output="Latest first saved")
    _validate_wire(repaired, complete=True)


def test_missing_past_exchange_is_not_answered_by_a_later_reused_id_and_offsets_stay_exact():
    current = {"role": "user", "content": "Current turn"}
    wire = [{"role": "user", "content": "Old turn"}, assistant(request()), current,
            assistant(request()), result(output="Later existing result"), assistant(request("last"))]
    original = copy.deepcopy(wire)
    state = {"summary": "Old turn summary", "through": 2, "compactions": 1}
    repaired, updated = repair_tool_history(wire, [event(output="Later existing result")], state)
    assert "outcome is unknown" in repaired[2]["content"]
    assert "outcome is unknown" in repaired[-1]["content"]
    assert repaired[updated["through"]] == current
    assert updated == {**state, "through": 3}
    assert state["through"] == 2 and wire == original
    assert [item for index, item in enumerate(repaired) if index not in (2, len(repaired) - 1)] == original
    assert repair_tool_history(repaired, [], updated) == (repaired, updated)
    _validate_wire(repaired, complete=True)


@pytest.mark.parametrize("through,expected", [(0, 0), (1, 2), (2, 3), (3, 5)])
def test_offset_adjustment_counts_only_insertions_at_or_before_original_boundary(through, expected):
    wire = [assistant(request()), {"role": "user", "content": "Next"}, assistant(request("missing"))]
    updated_wire, state = repair_tool_history(wire, [], {"through": through})
    assert state["through"] == expected
    assert len(updated_wire) == len(wire) + 2


@pytest.mark.parametrize("latest_event", [False, True])
async def test_native_restart_continuation_has_complete_exchanges_and_never_replays(tmp_path, monkeypatch, latest_event):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "native-recovery.sqlite3")
    session = store.create(settings.values)
    session["wire"] = [{"role": "user", "content": "Earlier"}, assistant(request()), result(),
                       {"role": "user", "content": "Latest"}, assistant(request())]
    session["events"] = [event()]
    if latest_event:
        session["events"].append(event(output="Completed just before restart"))
    original = copy.deepcopy(session["wire"])
    store.save(session)
    manager = AgentManager(store, settings)
    requests = []

    async def forbidden(*args):
        pytest.fail("Historical calls must never execute during recovery")

    async def gateway(request):
        payload = json.loads(request.content)
        requests.append(payload)
        messages = [message for message in payload["messages"] if message["role"] != "system"]
        _validate_wire(messages, complete=True)
        results = [message["content"] for message in messages if message["role"] == "tool"]
        assert results[0] == "Earlier result"
        if latest_event:
            assert results[1] == "Completed just before restart"
        else:
            assert "outcome is unknown" in results[1]
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Recovered."},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

    manager.execute_tool = forbidden
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(gateway)))
    try:
        resumed = store.get(session["id"])
        assert await manager.run_databricks(resumed, "Continue without repeating actions.") is True
        assert len(requests) == 1
        assert store.get(session["id"])["wire"][:len(original)] == original
        assert not (tmp_path / "note.txt").exists()
    finally:
        await manager.jobs.shutdown()
        store.db.close()


async def test_context_preparation_failure_persists_repaired_prefix_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "prefix-recovery.sqlite3")
    session = store.create(settings.values)
    current = {"role": "user", "content": "Current turn"}
    session["wire"] = [{"role": "user", "content": "Old turn"}, assistant(request()), current,
                       {"role": "assistant", "content": "Current answer"}]
    session["context_state"] = {"through": 2, "summary": "Old turn", "compactions": 1}
    store.save(session)
    manager = AgentManager(store, settings)

    async def fail_context(*args, **kwargs):
        raise ValueError("Synthetic context preparation failure")

    monkeypatch.setattr("local_agent.agents.prepare_context", fail_context)
    try:
        await manager.run(session, "Continue")
        saved = store.get(session["id"])
        assert saved["context_state"]["through"] == 3
        assert saved["wire"][saved["context_state"]["through"]] == current
        assert "outcome is unknown" in saved["wire"][2]["content"]
        assert any(event.get("text") == "Synthetic context preparation failure" for event in saved["events"])
    finally:
        await manager.jobs.shutdown()
        store.db.close()
