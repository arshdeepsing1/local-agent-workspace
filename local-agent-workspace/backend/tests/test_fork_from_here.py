"""Fork from here: a new chat from any user message (or the whole chat, also while it runs), with an
optional title and an optional new Git worktree."""
import copy
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from local_agent import history
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.context import context_messages
from local_agent.portability import export_bundle, validate_bundle

from test_history_cut import CASES


def git(path, *arguments):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(path),
                           *arguments], check=True, text=True, capture_output=True).stdout.strip()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    app = create_app(settings)
    manager = app.state.manager
    yield app, manager, project
    manager.store.db.close()


async def client_for(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        token = (await client.get("/api/bootstrap")).json()["token"]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                             headers={"X-Local-Token": token})


def stored(manager, project, events, wire, state=None, **fields):
    session = manager.store.create(manager.settings.values)
    session.update(title="Source", title_generated=True, events=copy.deepcopy(events), wire=copy.deepcopy(wire),
                   workspace=str(project), terminal_reason="completed", **fields)
    if state is not None:
        session["context_state"] = copy.deepcopy(state)
    manager.store.save(session)
    return session


def shape(wire):
    """The wire with tool call IDs numbered in order (forks give every exchange fresh IDs)."""
    names, result = {}, []
    for message in wire:
        message = copy.deepcopy(message)
        for call in message.get("tool_calls") or []:
            call["id"] = names.setdefault(call["id"], f"call-{len(names)}")
        if message.get("tool_call_id"):
            message["tool_call_id"] = names[message["tool_call_id"]]
        result.append(message)
    return result


def reply_gateway(monkeypatch):
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Forked answer."},"finish_reason":"stop"}]}'
                                        "\n\ndata: [DONE]\n\n")
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(gateway)))
    return requests


@pytest.mark.parametrize("name", [name for name, (_, expected) in CASES.items()
                                  if any(not isinstance(value, str) for value in expected.values())])
async def test_forks_at_every_boundary_type_are_valid_and_send_exactly_the_cut_history(runtime, monkeypatch, name):
    app, manager, project = runtime
    template, expectations = CASES[name]
    source = stored(manager, project, template["events"], template["wire"], template.get("context_state"))
    path = manager.store._path(source["id"])
    original = path.read_bytes()
    async with await client_for(app) as client:
        requests = reply_gateway(monkeypatch)  # After the API client exists: it patches httpx.AsyncClient.
        for event_id, expected in expectations.items():
            if isinstance(expected, str):
                response = await client.post(f"/api/sessions/{source['id']}/fork", json={"before_event_id": event_id})
                assert (response.status_code, response.json()["detail"]) == (400, expected)
                continue
            response = await client.post(f"/api/sessions/{source['id']}/fork", json={"before_event_id": event_id})
            assert response.status_code == 201, response.text
            fork = manager.store.get(response.json()["id"])
            event_cut, wire_cut, dropped = expected
            assert len(fork["events"]) == event_cut and len(fork["wire"]) == wire_cut
            assert shape(fork["wire"]) == shape(source["wire"][:wire_cut])
            assert fork["title"] == "Source (fork)" and fork["permission_mode"] == "manual"
            assert fork["provenance"]["kind"] == "fork" and fork["provenance"]["source_session_id"] == source["id"]
            assert bool((fork.get("context_state") or {}).get("summary")) == (
                bool((source.get("context_state") or {}).get("summary")) and not dropped)
            validate_bundle(export_bundle([fork], fork["id"]))
            await manager.run(fork, "Fork prompt")
            sent = requests[-1]["messages"][1:]
            expected_wire = fork["wire"][:wire_cut] + [{"role": "user", "content": "Fork prompt"}]
            assert sent == context_messages(expected_wire, fork.get("context_state"))
    assert path.read_bytes() == original


async def test_event_contract_fields_survive_a_fork(runtime):
    app, manager, project = runtime
    events = [{"id": "u1", "type": "user", "text": "Look at @a.txt", "in_wire": True,
               "attachments": [{"kind": "file", "path": "a.txt", "start_line": 1, "end_line": 20, "truncated": True},
                               {"kind": "shell", "command": "ls -la", "exit_code": 0}]},
              {"id": "a1", "type": "assistant", "text": "Partial",
               "request_info": {"model": "test-model", "status": "cancelled"}},
              {"id": "n1", "type": "notice", "text": "Stopped. You can continue this conversation.",
               "stop_phase": "streaming"},
              {"id": "u2", "type": "user", "text": "Also this", "in_wire": True, "steered": True},
              {"id": "u3", "type": "user", "text": "Failed early", "in_wire": False}]
    wire = [{"role": "user", "content": "Look at @a.txt\n\n[snapshot]"},
            {"role": "assistant", "content": history.stopped_reply("Partial", history.STOP_MARKER)},
            {"role": "user", "content": "Also this"}]
    source = stored(manager, project, events, wire)
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{source['id']}/fork", json={})
        assert response.status_code == 201, response.text
    fork = manager.store.get(response.json()["id"])
    first, _, notice, steered, failed = fork["events"]
    assert first["in_wire"] is True and first["attachments"] == events[0]["attachments"]
    assert notice["stop_phase"] == "streaming"
    assert steered["steered"] is True and steered["in_wire"] is True
    assert failed["in_wire"] is False
    assert fork["wire"] == wire
    # The cut mapping still works in the copy.
    assert history.cut_points(fork, steered["id"]) == (3, 2)


async def test_title_override_and_validation(runtime):
    app, manager, project = runtime
    template, _ = CASES["a_plain"]
    source = stored(manager, project, template["events"], template["wire"])
    async with await client_for(app) as client:
        url = f"/api/sessions/{source['id']}/fork"
        response = await client.post(url, json={"before_event_id": "u3", "title": "  Second   try\t"})
        assert response.status_code == 201
        fork = manager.store.get(response.json()["id"])
        assert fork["title"] == "Second try" and fork["title_generated"] is True and len(fork["events"]) == 4
        count = len(manager.store.list())
        response = await client.post(url, json={"title": "Bad\x07title"})
        assert (response.status_code, response.json()["detail"]) == (
            400, "Enter a conversation title without control characters.")
        assert (await client.post(url, json={"title": ""})).status_code == 422
        assert (await client.post(url, json={"title": "x" * 161})).status_code == 422
        assert (await client.post(url, json={"before_event_id": "a1"})).status_code == 400
        assert (await client.post(url, json={"unknown": True})).status_code == 422
        assert len(manager.store.list()) == count


async def test_a_busy_chat_forks_only_before_its_running_turn(runtime):
    app, manager, project = runtime
    template, _ = CASES["f_steered"]
    events = copy.deepcopy(template["events"]) + [
        {"id": "u3", "type": "user", "text": "Running", "in_wire": True},
        {"id": "u3s", "type": "user", "text": "Steer", "in_wire": True, "steered": True}]
    wire = copy.deepcopy(template["wire"]) + [{"role": "user", "content": "Running"},
                                              {"role": "assistant", "content": None, "tool_calls": [
                                                  {"id": "live", "type": "function",
                                                   "function": {"name": "list_files", "arguments": "{}"}}]},
                                              {"role": "tool", "tool_call_id": "live", "content": "[]"},
                                              {"role": "user", "content": "Steer"}]
    source = stored(manager, project, events, wire)
    manager.statuses[source["id"]] = "running"
    async with await client_for(app) as client:
        url = f"/api/sessions/{source['id']}/fork"
        for event_id in ("u1", "s1", "u2", "u3"):
            response = await client.post(url, json={"before_event_id": event_id})
            assert response.status_code == 201, (event_id, response.text)
        fork = manager.store.get(response.json()["id"])
        assert len(fork["wire"]) == len(template["wire"])
        for body in ({"before_event_id": "u3s"}, {}):
            response = await client.post(url, json=body)
            assert response.status_code == 409
            assert "forking from this point" in response.json()["detail"]["message"]
        assert (await client.post(url)).status_code == 409  # The whole-chat fork still needs an idle chat.


def repository(path):
    path.mkdir(exist_ok=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Isolated Test")
    git(path, "config", "user.email", "isolated@example.invalid")
    (path / "tracked.txt").write_text("committed")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    (path / "tracked.txt").write_text("uncommitted")  # Not copied into the worktree.


async def test_worktree_forks_and_forks_of_worktree_chats(runtime):
    app, manager, project = runtime
    repository(project)
    root = str(project.resolve())
    template, _ = CASES["a_plain"]
    source = stored(manager, project, template["events"], template["wire"])
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{source['id']}/fork",
                                     json={"before_event_id": "u3", "worktree_branch": "fork/first"})
        assert response.status_code == 201, response.text
        first = manager.store.get(response.json()["id"])
        record = manager.worktrees.list()[0]
        assert first["workspace"] == record["path"] != root
        assert first["worktree"] == {"id": record["id"], "branch": "fork/first", "source_workspace": root}
        assert (Path(record["path"]) / "tracked.txt").read_text() == "committed"
        assert len(first["events"]) == 4

        response = await client.post(f"/api/sessions/{first['id']}/fork", json={"worktree_branch": "fork/second"})
        assert response.status_code == 201, response.text
        second = manager.store.get(response.json()["id"])
        assert second["workspace"] not in (root, first["workspace"])
        assert second["worktree"]["branch"] == "fork/second" and second["worktree"]["source_workspace"] == root

        response = await client.post(f"/api/sessions/{first['id']}/fork", json={"before_event_id": first["events"][2]["id"]})
        assert response.status_code == 201, response.text
        same = manager.store.get(response.json()["id"])
        assert same["workspace"] == first["workspace"]
        assert same["worktree"] == first["worktree"]

        # A new worktree chat from a worktree chat also records the root project.
        record = next(item for item in manager.worktrees.list() if item["branch"] == "fork/second")
        response = await client.post(f"/api/worktrees/{record['id']}/session", params={"session_id": second["id"]})
        assert response.status_code == 200, response.text
        assert response.json()["worktree"] == {"id": record["id"], "branch": "fork/second", "source_workspace": root}


async def test_a_worktree_fork_of_a_non_git_folder_creates_nothing(runtime):
    app, manager, project = runtime
    template, _ = CASES["a_plain"]
    source = stored(manager, project, template["events"], template["wire"])
    count = len(manager.store.list())
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{source['id']}/fork",
                                     json={"before_event_id": "u2", "worktree_branch": "fork/none"})
    assert response.status_code == 400
    assert len(manager.store.list()) == count and manager.worktrees.list() == []


async def test_new_worktree_chat_records_its_project(runtime):
    app, manager, project = runtime
    repository(project)
    async with await client_for(app) as client:
        record = (await client.post("/api/worktrees", json={"branch": "chat/new"})).json()
        response = await client.post(f"/api/worktrees/{record['id']}/session")
        assert response.status_code == 200, response.text
        assert response.json()["worktree"] == {"id": record["id"], "branch": "chat/new",
                                               "source_workspace": str(project.resolve())}
