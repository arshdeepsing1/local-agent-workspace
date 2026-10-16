"""The in-memory result store (result_store.py) and get_tool_output."""
import json
import sys

import httpx
import pytest

from local_agent import result_store
from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.permissions import evaluate, tool_kind
from local_agent.result_store import ResultStore
from local_agent.store import Store
from local_agent.tool_profiles import tool_allowed


def test_pages_follow_next_offset_and_stay_within_8_kb():
    store = ResultStore()
    text = "".join(f"line {n:05d} 🌍\n" for n in range(3000))
    result_id = store.add("chat", text)
    assert result_id.startswith("out-") and len(result_id) == 16
    pages, offset = [], 0
    while offset is not None:
        page = store.page("chat", result_id, offset, 8000)
        assert len(json.dumps(page, indent=2).encode()) <= 8000 and page["total_chars"] == len(text)
        pages.append(page["output"])
        offset = page["next_offset"]
    assert "".join(pages) == text
    with pytest.raises(ValueError, match="max_chars"):
        store.page("chat", result_id, 0, 8001)
    with pytest.raises(ValueError, match="beyond the result"):
        store.page("chat", result_id, len(text) + 1)


def test_unknown_ids_other_chats_and_images_are_refused():
    store = ResultStore()
    result_id = store.add("chat", "text")
    image_id = store.add_image("chat", "image/png", "aGVsbG8=")
    with pytest.raises(ValueError, match="no longer available.*Call the tool again"):
        store.page("other", result_id)
    with pytest.raises(ValueError, match="no longer available"):
        store.page("chat", "out-000000000000")
    with pytest.raises(ValueError, match="is an image"):
        store.page("chat", image_id)
    assert store.image("chat", image_id) == {"mime_type": "image/png", "data": "aGVsbG8="}
    assert store.image("other", image_id) is None and store.image("chat", result_id) is None


def test_lru_limits_per_chat_and_in_total(monkeypatch):
    store = ResultStore()
    ids = [store.add("chat", f"result {n}") for n in range(25)]
    assert [key[1] for key in store.entries if key[0] == "chat"] == ids[5:]
    store.page("chat", ids[5])  # Read: now the most recently used.
    store.add("chat", "one more")
    assert (("chat", ids[5])) in store.entries and ("chat", ids[6]) not in store.entries
    monkeypatch.setattr(result_store, "MAX_TOTAL", 100)
    big = store.add("other", "x" * 90)
    assert store.total <= 100 and ("other", big) in store.entries
    assert store.add("other", "y" * 200 * 1)  # Larger than the total alone: only it stays.
    assert len(store.entries) == 1


def test_text_is_capped_and_delete_session_empties_a_chat():
    store = ResultStore()
    result_id = store.add("chat", "x" * (result_store.MAX_TEXT + 10))
    assert store.page("chat", result_id)["total_chars"] == result_store.MAX_TEXT
    store.add("other", "kept")
    store.delete_session("chat")
    assert not store.has_text("chat") and store.has_text("other")
    assert store.total == len("kept")


def test_get_tool_output_is_a_read_allowed_in_plan_and_every_profile():
    assert tool_kind("get_tool_output") == "read"
    assert evaluate("plan", "get_tool_output", {"result_id": "out-x"}).decision == "allow"
    for profile in ("read_only", "file_editor", "inherit"):
        assert tool_allowed({"tool_profile": profile}, "get_tool_output")
    assert not tool_allowed({"disabled_tools": ["get_tool_output"]}, "get_tool_output")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-token")
    store = Store(settings.state_dir / "results.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    yield manager, session, project
    store.db.close()


def stream(text=None, call=None):
    delta = {"content": text} if call is None else {"tool_calls": [{"index": 0, "id": call["id"], "type": "function",
                                                                    "function": {"name": call["name"], "arguments": call["arguments"]}}]}
    finish = "tool_calls" if call else "stop"
    return httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]})
                          + "\n\ndata: [DONE]\n\n")


async def test_a_long_mcp_result_is_kept_redacted_and_read_with_get_tool_output(runtime, monkeypatch):
    """A real stdio server returns 30 KB with the token in it: the first page reaches the model,
    get_tool_output is offered from the next request on and reads the rest, redacted."""
    manager, session, project = runtime
    server = project.parent / "long_server.py"
    server.write_text('''from mcp.server import MCPServer
server = MCPServer("Long", log_level="ERROR")
@server.tool()
def long() -> str:
    return "".join(f"row {n:05d} synthetic-token\\n" for n in range(1500))
server.run()
''')
    manager.extensions.update_config({"servers": [{"id": "big", "name": "Big", "transport": "stdio",
                                                   "command": sys.executable, "args": [str(server)], "enabled": True}]})
    session["permission_mode"] = "bypassPermissions"
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = [tool["function"]["name"] for tool in payload["tools"]]
        if len(requests) == 1:
            assert "get_tool_output" not in names and "mcp__big__long" in names
            return stream(call={"id": "first", "name": "mcp__big__long", "arguments": "{}"})
        assert "get_tool_output" in names
        if len(requests) == 2:
            first = json.loads(payload["messages"][-1]["content"])
            assert first["truncated"] is True and first["result_id"].startswith("out-")
            assert first["next_offset"] == len(first["output"]) and first["total_chars"] > 30_000
            return stream(call={"id": "second", "name": "get_tool_output", "arguments": json.dumps(
                {"result_id": first["result_id"], "offset": first["total_chars"] - 100, "max_chars": 100})})
        page = json.loads(payload["messages"][-1]["content"])
        assert page["next_offset"] is None and page["output"].endswith("row 01499 [REDACTED]\n")
        return stream("Done.")
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))
    await manager.run_databricks(session, "Read the long result")
    assert len(requests) == 3 and session["tool_output_offered"] is True
    assert "synthetic-token" not in json.dumps([entry for entry in manager.results.entries.values()])
    # Offered in every later request too (a stable tool list), even once the store is empty.
    manager.results.delete_session(session["id"])
    assert "get_tool_output" in [tool["function"]["name"] for tool in manager.step_definitions(session, [])]
