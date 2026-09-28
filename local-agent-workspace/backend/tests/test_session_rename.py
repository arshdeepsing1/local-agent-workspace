import asyncio
import copy

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), model="test-model", env_file="")
    settings.env = {}
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            session = (await client.post("/api/sessions")).json()
            yield client, app.state.manager, session


async def test_rename_persists_metadata_without_changing_history(runtime):
    client, manager, session = runtime
    session = manager.get(session["id"])
    session.update(events=[{"type": "user", "text": "Keep this transcript."}],
                   wire=[{"role": "user", "content": "Keep this transcript."}],
                   context_state={"summary": "Existing summary", "through": 0, "compactions": 1})
    manager.store.save(session)
    before = copy.deepcopy(session)
    broadcasts = []

    async def broadcast(sid, message):
        broadcasts.append((sid, message))
        assert manager.store.get(sid)["title"] == message["title"]

    manager.broadcast = broadcast
    response = await client.put(f"/api/sessions/{session['id']}/title", json={"title": "  Release\n notes — नमस्ते  "})
    assert response.status_code == 200
    assert response.json()["title"] == "Release notes — नमस्ते"
    assert response.json()["status"] == "idle"
    assert "wire" not in response.json()
    saved = manager.store.get(session["id"])
    assert saved["title_generated"] is True
    for key in ("events", "wire", "context_state", "workspace", "model", "permission_mode", "allowed_directories"):
        assert saved[key] == before[key]
    assert broadcasts == [(session["id"], {"type": "title", "title": saved["title"]})]
    assert (await client.get("/api/sessions")).json()[0]["title"] == saved["title"]
    restarted = AgentManager(manager.store, manager.settings)
    assert restarted.get(session["id"])["title"] == saved["title"]


@pytest.mark.parametrize("title,status", [
    ("", 422), (" \n\t", 400), ("x" * 161, 422), (None, 422), (42, 422),
    (True, 422), ("A\x00B", 400), ("A\x7fB", 400),
])
async def test_rename_rejects_invalid_titles_without_mutation(runtime, title, status):
    client, manager, session = runtime
    before = manager.store.get(session["id"])
    response = await client.put(f"/api/sessions/{session['id']}/title", json={"title": title})
    assert response.status_code == status
    assert manager.store.get(session["id"]) == before


async def test_rename_accepts_maximum_length_and_requires_existing_authorized_session(runtime):
    client, manager, session = runtime
    path = f"/api/sessions/{session['id']}/title"
    response = await client.put(path, json={"title": "x" * 160})
    assert response.status_code == 200
    assert response.json()["title"] == "x" * 160
    assert (await client.put("/api/sessions/missing/title", json={"title": "Title"})).status_code == 404
    assert (await client.put(path, headers={"X-Local-Token": "wrong"}, json={"title": "Title"})).status_code == 403
    assert manager.store.get(session["id"])["title"] == "x" * 160


@pytest.mark.parametrize("status", ["running", "awaiting_approval", "compacting"])
async def test_rename_updates_live_session_without_changing_status(runtime, status):
    client, manager, session = runtime
    live = manager.get(session["id"])
    manager.live[session["id"]] = live
    manager.statuses[session["id"]] = status
    response = await client.put(f"/api/sessions/{session['id']}/title", json={"title": "My active task"})
    assert response.status_code == 200
    assert response.json()["status"] == status
    assert live["title"] == "My active task"
    assert live["title_generated"] is True
    manager.store.save(live)
    assert manager.store.get(session["id"])["title"] == "My active task"


async def test_failed_rename_does_not_mutate_live_or_saved_title(runtime, monkeypatch):
    client, manager, session = runtime
    live = manager.get(session["id"])
    manager.live[session["id"]] = live
    before = copy.deepcopy(live)

    def fail_save(session):
        raise OSError("Simulated save failure")

    monkeypatch.setattr(manager.store, "save", fail_save)
    response = await client.put(f"/api/sessions/{session['id']}/title", json={"title": "Not saved"})
    assert response.status_code == 400
    assert live == before
    assert manager.store.get(session["id"]) == before


@pytest.mark.parametrize("title", ["My chosen title", "New conversation", "First request"])
async def test_manual_title_is_not_replaced_on_first_or_later_turn(runtime, monkeypatch, title):
    client, manager, session = runtime

    async def complete(*args):
        return True

    async def forbidden_title(*args):
        pytest.fail("Manual titles must not trigger model title generation")

    monkeypatch.setattr(manager, "run_databricks", complete)
    monkeypatch.setattr("local_agent.agents.generate_title", forbidden_title)
    assert (await client.put(f"/api/sessions/{session['id']}/title", json={"title": title})).status_code == 200
    for prompt in ("First request", "Follow up"):
        manager.start(session["id"], prompt)
        await asyncio.wait_for(manager.tasks[session["id"]], 2)
        saved = manager.store.get(session["id"])
        assert saved["title"] == title
        assert saved["terminal_reason"] == "completed"
        assert not any(event["type"] == "error" for event in saved["events"])


@pytest.mark.parametrize("chosen_title", ["My renamed conversation", "First request"])
async def test_manual_rename_wins_over_in_flight_automatic_title(runtime, monkeypatch, chosen_title):
    client, manager, session = runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def complete(*args):
        return True

    async def delayed_title(*args):
        entered.set()
        await release.wait()
        return "Automatically generated title"

    monkeypatch.setattr(manager, "run_databricks", complete)
    monkeypatch.setattr("local_agent.agents.generate_title", delayed_title)
    manager.start(session["id"], "First request")
    await asyncio.wait_for(entered.wait(), 2)
    try:
        response = await client.put(f"/api/sessions/{session['id']}/title", json={"title": chosen_title})
        assert response.status_code == 200
        assert response.json()["status"] == "naming"
    finally:
        release.set()
    await asyncio.wait_for(manager.tasks[session["id"]], 2)
    saved = manager.store.get(session["id"])
    assert saved["title"] == chosen_title
    assert saved["title_generated"] is True
