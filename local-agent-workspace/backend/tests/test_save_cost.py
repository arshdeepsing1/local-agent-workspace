"""Saving a long conversation costs less (integration review B, finding 14): the JSON of long
strings is reused between saves, a tool result its finished card already saved is not saved
again, and stream checkpoints are spaced by how long a save takes."""
import copy
import json

import httpx
import pytest

from local_agent import drafts, store as store_module
from local_agent.agents import AgentManager, repair_tool_history
from local_agent.config import Settings
from local_agent.drafts import DRAFT_BYTES, DRAFT_INTERVAL_SECONDS, StreamDraft
from local_agent.store import CACHED_STRING_CHARS, Store


def session_with(events, wire=()):
    return {"id": "long", "title": "Long", "created": 10, "updated": 20, "workspace": "/project", "model": "model",
            "events": events, "wire": list(wire)}


def lines(store, session_id="long"):
    return (store.conversations / f"{session_id}.jsonl").read_text(encoding="utf-8").split("\n")[:-1]


AWKWARD = ["plain", "non-ASCII é ☃ 漢字", "emoji 😀", "line separator ", "control \x00\x01\x1f\x7f",
           "quotes \" and \\ backslashes / and \t\r\n", "x" * CACHED_STRING_CHARS + "é\"\\ "]


def test_saved_lines_are_byte_identical_to_json_dumps(tmp_path):
    store = Store(tmp_path / "sessions.db")
    event = {"id": "e", "type": "tool", "texts": AWKWARD, "numbers": [0, -1, 1.5, 1e300, float("nan"), float("inf"),
             10 ** 30, True, None], "empty": {}, "nested": {"a": [{}, [], ""]}, 3: "int key", "tuple": (1, "two")}
    session = session_with([event], [{"role": "tool", "content": AWKWARD[-1]}])
    for _ in range(2):  # The second save reuses the cached encodings.
        store.save(session, touch=False)
        assert lines(store)[1:] == [json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                                    for record in list(Store._records(session))[1:]]
    store.db.close()


def test_a_changed_value_next_to_a_cached_string_is_saved(tmp_path):
    store = Store(tmp_path / "sessions.db")
    text = "o" * 5000
    record = {"id": "e", "type": "tool", "output": text}
    session = session_with([record])
    store.save(session)
    record["output"] = text + "y"  # Changed in place, as many places change events.
    store.save(session)
    assert store.get("long")["events"][0]["output"] == text + "y"
    store.delete("long")
    assert "long" not in store._encoded
    store.db.close()


def test_a_second_save_encodes_only_new_long_strings(tmp_path, monkeypatch):
    store = Store(tmp_path / "sessions.db")
    session = session_with([{"id": f"e{n}", "type": "tool", "output": f"{n} " + "r" * 50_000} for n in range(40)])
    store.save(session)
    encoded = []
    encode = store_module._encode_json
    monkeypatch.setattr(store_module, "_encode_json", lambda value: encoded.append(value) or encode(value))
    session["events"].append({"id": "new", "type": "tool", "output": "new " + "n" * 50_000})
    store.save(session)
    assert [value for value in encoded if isinstance(value, str) and len(value) >= CACHED_STRING_CHARS] == [
        session["events"][-1]["output"]]
    assert store.get("long")["events"] == session["events"]
    store.db.close()


# --- The tool loop -----------------------------------------------------------------------------

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Saves", title_generated=True, permission_mode="bypassPermissions")
    yield manager, session, project
    store.db.close()


def stream(delta, finish):
    chunk = {"choices": [{"delta": delta, "finish_reason": finish}]}
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


async def test_a_tool_result_its_card_saved_is_not_saved_again_and_every_save_repairs_whole(runtime, monkeypatch):
    manager, session, project = runtime
    for name in ("a.txt", "b.txt", "c.txt"):
        (project / name).write_text(f"contents of {name}\n")
    calls = [{"index": n, "id": f"c{n}", "function": {"name": "read_file", "arguments": json.dumps({"path": name})}}
             for n, name in enumerate(("a.txt", "b.txt", "missing.txt"))]
    responses = iter([stream({"tool_calls": calls}, "tool_calls"), stream({"content": "Done."}, "stop")])
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(
        lambda request: next(responses))))
    snapshots = []
    save = manager.store.save

    def recording(saved, **options):
        snapshots.append(copy.deepcopy({key: value for key, value in saved.items() if key != "updated"}))
        return save(saved, **options)
    monkeypatch.setattr(manager.store, "save", recording)
    await manager.run(session, "Read them")
    assert session["terminal_reason"] in ("completed", "tool_error")
    assert [event["state"] for event in session["events"] if event["type"] == "tool"] == ["completed", "completed", "error"]
    # No save only appends a tool message to what the previous save held.
    for saved, following in zip(snapshots, snapshots[1:]):
        wire, next_wire = saved.pop("wire"), following.pop("wire")
        assert not (following == saved and next_wire[:-1] == wire and next_wire[-1]["role"] == "tool")
        saved["wire"], following["wire"] = wire, next_wire
    # Whatever save the app stops after, repairing the saved history gives what the model was sent for
    # every call whose card had finished (a running one is reported as interrupted).
    final = session["wire"]
    for saved in snapshots:
        finished = {event["call_id"] for event in saved["events"] if event["type"] == "tool"
                    and event.get("state") in ("completed", "error", "rejected")}
        repaired, _ = repair_tool_history(saved["wire"], saved["events"], {})
        assert len(repaired) <= len(final)
        for message, sent in zip(repaired, final):
            if message["role"] != "tool" or message["tool_call_id"] in finished:
                assert message == sent


# --- Stream checkpoints ----------------------------------------------------------------------------

class SlowStore:
    def __init__(self, clock, cost):
        self.clock, self.cost, self.saves = clock, cost, 0

    def save(self, session):
        self.saves += 1
        self.clock[0] += self.cost


async def test_checkpoints_are_spaced_by_the_cost_of_a_save(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(drafts, "_now", lambda: clock[0])
    slow = SlowStore(clock, 0.2)
    draft = StreamDraft(slow, {})
    try:
        assert draft.next_wait() == DRAFT_INTERVAL_SECONDS
        draft.changed(DRAFT_BYTES)  # The first checkpoint is not held back.
        assert slow.saves == 1
        assert draft.next_wait() == pytest.approx(4.0)  # 20 times the 0.2-second save.
        clock[0] += 1.0
        draft.changed(DRAFT_BYTES)
        assert slow.saves == 1  # DRAFT_BYTES does not checkpoint before the spacing has passed.
        assert draft.next_wait() == pytest.approx(3.0)
        clock[0] += 3.01
        draft.changed(1)
        assert slow.saves == 2
        # A cheap save keeps the one-second interval and the immediate DRAFT_BYTES checkpoint.
        slow.cost = 0.001
        draft.changed(DRAFT_BYTES)
        clock[0] += 4.01
        draft.changed(DRAFT_BYTES)
        assert slow.saves == 3 and draft.next_wait() == DRAFT_INTERVAL_SECONDS
        clock[0] += 0.05
        draft.changed(DRAFT_BYTES)
        assert slow.saves == 4
    finally:
        await draft.close()
