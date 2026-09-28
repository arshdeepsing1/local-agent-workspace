import asyncio
import copy
import json

import httpx
import pytest

from local_agent.api import create_app
from local_agent.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="", model="default-endpoint")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-model-token")
    return settings


@pytest.fixture
async def app_client(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            yield app, client


async def create_session(client):
    response = await client.post("/api/sessions")
    assert response.status_code == 200
    return response.json()["id"]


async def test_model_switch_changes_only_selected_session_and_survives_reopen(settings, monkeypatch):
    original_defaults = copy.deepcopy(settings.values)
    app = create_app(settings)
    manager = app.state.manager
    broadcasts = []
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            selected, other = await create_session(client), await create_session(client)
            session = manager.store.get(selected)
            session.update(
                title="A custom title", title_generated=True, permission_mode="acceptEdits",
                allowed_directories=[str(settings.state_dir.parent)],
                events=[{"id": "old-user", "type": "user", "text": "Remember the marker."},
                        {"id": "old-reply", "type": "assistant", "text": "Remembered."}],
                wire=[{"role": "user", "content": "Remember the marker."},
                      {"role": "assistant", "content": "Remembered."}],
                context_state={"summary": "Earlier work", "through": 0, "compactions": 1},
                context_info={"estimated_tokens": 1234}, active_skills=["review"],
            )
            manager.store.save(session)
            before = copy.deepcopy(session)
            other_before = manager.store.get(other)

            async def broadcast(session_id, message):
                assert manager.store.get(session_id)["model"] == "new-endpoint"
                broadcasts.append((session_id, message))

            def no_external_client(**kwargs):
                raise AssertionError("Changing the model must not contact its endpoint.")

            with monkeypatch.context() as patch:
                patch.setattr(manager, "broadcast", broadcast)
                patch.setattr(httpx, "AsyncClient", no_external_client)
                response = await client.put(f"/api/sessions/{selected}/model", json={"model": "  new-endpoint  "})
            assert response.status_code == 200
            assert response.json()["model"] == "new-endpoint"
            assert response.json()["status"] == "idle"
            assert "wire" not in response.json() and "context_state" not in response.json()
            saved = manager.store.get(selected)
            assert {key: value for key, value in saved.items() if key not in ("model", "updated")} == {
                key: value for key, value in before.items() if key not in ("model", "updated")}
            assert manager.store.get(other) == other_before
            assert settings.values == original_defaults
            assert broadcasts == [(selected, {"type": "model", "model": "new-endpoint"})]

    reopened = create_app(settings)
    async with reopened.router.lifespan_context(reopened):
        restored = reopened.state.manager.store.get(selected)
        assert restored == saved
        assert reopened.state.manager.store.get(other) == other_before
        assert settings.values == original_defaults


@pytest.mark.parametrize("phase", ["running", "awaiting_approval", "compacting", "naming"])
async def test_busy_conversation_rejects_model_changes(app_client, phase):
    app, client = app_client
    session_id = await create_session(client)
    manager = app.state.manager
    before = manager.store.get(session_id)
    manager.statuses[session_id] = phase
    response = await client.put(f"/api/sessions/{session_id}/model", json={"model": "new-endpoint"})
    assert response.status_code == 409
    assert "Stop" in response.json()["detail"]
    assert manager.store.get(session_id) == before


@pytest.mark.parametrize("body, status", [
    ({}, 422), ({"model": ""}, 422), ({"model": " \n\t "}, 400),
    ({"model": None}, 422), ({"model": 123}, 422), ({"model": True}, 422),
    ({"model": []}, 422), ({"model": "x" * 257}, 422),
])
async def test_model_validation_rejects_invalid_input_without_mutation(app_client, body, status):
    app, client = app_client
    session_id = await create_session(client)
    before = app.state.manager.store.get(session_id)
    response = await client.put(f"/api/sessions/{session_id}/model", json=body)
    assert response.status_code == status
    assert app.state.manager.store.get(session_id) == before


async def test_model_route_requires_local_auth_and_existing_session(app_client):
    app, client = app_client
    session_id = await create_session(client)
    before = app.state.manager.store.get(session_id)
    route = f"/api/sessions/{session_id}/model"
    authenticated_token = client.headers.pop("X-Local-Token")
    assert (await client.put(route, json={"model": "new-endpoint"})).status_code == 403
    client.headers["X-Local-Token"] = "wrong-token"
    assert (await client.put(route, json={"model": "new-endpoint"})).status_code == 403
    client.headers["X-Local-Token"] = authenticated_token
    assert (await client.put("/api/sessions/missing/model", json={"model": "new-endpoint"})).status_code == 404
    assert app.state.manager.store.get(session_id) == before
    assert (await client.put(route, json={"model": "x" * 256})).status_code == 200


async def test_model_change_is_rejected_during_session_deletion(app_client, monkeypatch):
    app, client = app_client
    session_id = await create_session(client)
    manager = app.state.manager
    entered, release = asyncio.Event(), asyncio.Event()
    remove_jobs = manager.jobs.remove_session

    async def gated_cleanup(deleting_id):
        entered.set()
        await release.wait()
        await remove_jobs(deleting_id)

    monkeypatch.setattr(manager.jobs, "remove_session", gated_cleanup)
    deletion = asyncio.create_task(client.delete(f"/api/sessions/{session_id}"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        response = await client.put(f"/api/sessions/{session_id}/model", json={"model": "new-endpoint"})
        assert response.status_code == 404
        assert manager.store.get(session_id)["model"] == "default-endpoint"
    finally:
        release.set()
        assert (await asyncio.wait_for(deletion, 2)).status_code == 200
    assert manager.store.get(session_id) is None


async def test_next_turn_uses_new_endpoint_with_existing_history(app_client, monkeypatch):
    app, client = app_client
    session_id = await create_session(client)
    manager = app.state.manager
    session = manager.store.get(session_id)
    session.update(title="Keep this title", title_generated=True,
                   events=[{"type": "user", "text": "Remember alpha-marker."},
                           {"type": "assistant", "text": "Remembered."}],
                   wire=[{"role": "user", "content": "Remember alpha-marker."},
                         {"role": "assistant", "content": "Remembered."}])
    manager.store.save(session)
    requests = []

    async def gateway(request):
        requests.append((request.url.raw_path.decode(), json.loads(request.content)))
        chunk = {"choices": [{"delta": {"content": "Your marker is alpha-marker."}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(gateway)))
    changed = await client.put(f"/api/sessions/{session_id}/model", json={"model": "other/model"})
    assert changed.status_code == 200
    response = await client.post(f"/api/sessions/{session_id}/messages", json={"text": "What is my marker?"})
    assert response.status_code == 200
    await asyncio.wait_for(manager.tasks[session_id], 3)
    assert len(requests) == 1
    path, payload = requests[0]
    assert path == "/serving-endpoints/other%2Fmodel/invocations"
    assert payload["stream"] is True
    assert payload["messages"][1:] == [
        {"role": "user", "content": "Remember alpha-marker."},
        {"role": "assistant", "content": "Remembered."},
        {"role": "user", "content": "What is my marker?"},
    ]
    saved = manager.store.get(session_id)
    assert saved["model"] == "other/model"
    assert saved["wire"][-1] == {"role": "assistant", "content": "Your marker is alpha-marker."}
    assert manager.settings.values["model"] == "default-endpoint"
