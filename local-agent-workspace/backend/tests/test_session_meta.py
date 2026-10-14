"""Pin, archive and tags: header fields saved without changing the sort order or the unread state."""
import httpx
import pytest

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.portability import validate_bundle


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), model="test-model", env_file="")
    settings.env = {}
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            session = (await client.post("/api/sessions")).json()
            yield client, app.state.manager, session


def completed(manager, session_id):
    session = manager.get(session_id)
    session.update(terminal_reason="completed",
                   events=[{"id": "u", "type": "user", "text": "Hi"}, {"id": "a", "type": "assistant", "text": "Hello"}],
                   wire=[{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}])
    manager.store.save(session)
    return session


@pytest.mark.parametrize("tags,expected", [
    (["  Release \n notes  "], ["Release notes"]),
    (["bug", "Bug", "BUG", "ui"], ["bug", "ui"]),
    (["Cafe\u0301"], ["Caf\u00e9"]),  # NFC
    (["x" * 32], ["x" * 32]),
    (["a\tb"], ["a b"]),
    ([], []),
    ([""], 400), (["   "], 400), (["x" * 33], 400), (["a\x00b"], 400), (["a\x7f"], 400), (["a\x9fb"], 400),
    (["t"] * 11, 422), ([1], 422), ("bug", 422), (None, 400),
])
async def test_tags_are_normalized_or_rejected(runtime, tags, expected):
    client, manager, session = runtime
    response = await client.put(f"/api/sessions/{session['id']}/meta", json={"tags": tags})
    if isinstance(expected, int):
        assert response.status_code == expected
        if expected == 400:
            assert response.json()["detail"] in ("Tags have 1 to 32 characters and no control characters.",
                                                 "Change pin, archive or tags.")
        assert "tags" not in manager.store.get(session["id"])
    else:
        assert response.status_code == 200
        assert response.json()["tags"] == expected
        assert manager.store.get(session["id"])["tags"] == expected


@pytest.mark.parametrize("body,status", [({}, 400), ({"pinned": "yes"}, 422), ({"pinned": 1}, 422),
                                         ({"title": "x"}, 422), ({"pinned": True, "other": 1}, 422)])
async def test_meta_needs_a_known_field_of_the_right_type(runtime, body, status):
    client, manager, session = runtime
    response = await client.put(f"/api/sessions/{session['id']}/meta", json=body)
    assert response.status_code == status
    if status == 400:
        assert response.json()["detail"] == "Change pin, archive or tags."


async def test_pin_and_archive_keep_the_update_time_and_the_list_order(runtime):
    client, manager, older = runtime
    newer = (await client.post("/api/sessions")).json()
    before = {item["id"]: item["updated"] for item in (await client.get("/api/sessions")).json()}
    broadcasts = []

    async def broadcast(session_id, message):
        broadcasts.append((session_id, message))
    manager.broadcast = broadcast
    response = await client.put(f"/api/sessions/{older['id']}/meta", json={"pinned": True, "archived": True, "tags": ["x"]})
    assert response.status_code == 200
    assert response.json()["pinned"] is True and response.json()["archived"] is True
    assert response.json()["updated"] == before[older["id"]]
    listed = (await client.get("/api/sessions")).json()
    assert [item["id"] for item in listed] == [newer["id"], older["id"]]
    assert {item["id"]: item["updated"] for item in listed} == before
    assert listed[1]["pinned"] is True and listed[1]["tags"] == ["x"]
    assert broadcasts == [(older["id"], {"type": "meta", "pinned": True, "archived": True, "tags": ["x"]})]
    await client.put(f"/api/sessions/{older['id']}/meta", json={"pinned": False})
    assert broadcasts[-1][1] == {"type": "meta", "pinned": False, "archived": True, "tags": ["x"]}


async def test_meta_changes_are_allowed_while_a_response_runs_and_survive_its_next_save(runtime):
    client, manager, session = runtime
    live = manager.get(session["id"])
    manager.live[session["id"]] = live
    manager.statuses[session["id"]] = "running"
    response = await client.put(f"/api/sessions/{session['id']}/meta", json={"pinned": True, "tags": ["wip"]})
    assert response.status_code == 200 and response.json()["status"] == "running"
    assert live["pinned"] is True and live["tags"] == ["wip"]
    live["events"].append({"id": "later", "type": "assistant", "text": "Next step"})
    manager.store.save(live)  # The running turn saves its own dict next.
    saved = manager.store.get(session["id"])
    assert saved["pinned"] is True and saved["tags"] == ["wip"] and saved["events"][-1]["id"] == "later"
    manager.live.pop(session["id"])
    manager.statuses.pop(session["id"])


async def test_export_keeps_tags_but_not_pin_or_archive_and_copies_keep_tags(runtime, tmp_path):
    client, manager, session = runtime
    completed(manager, session["id"])
    await client.put(f"/api/sessions/{session['id']}/meta", json={"pinned": True, "archived": True, "tags": ["release"]})
    bundle = (await client.get(f"/api/sessions/{session['id']}/export")).json()
    exported = bundle["sessions"][0]
    assert exported["tags"] == ["release"]
    assert "pinned" not in exported and "archived" not in exported
    validate_bundle(bundle)

    fork = await client.post(f"/api/sessions/{session['id']}/fork")
    assert fork.status_code == 201
    forked = manager.store.get(fork.json()["id"])
    assert forked["tags"] == ["release"] and "pinned" not in forked and "archived" not in forked

    imported = await client.post("/api/sessions/import", params={"workspace": str(tmp_path / "project")}, json=bundle)
    assert imported.status_code == 201
    copy = manager.store.get(imported.json()["session"]["id"])
    assert copy["tags"] == ["release"] and "pinned" not in copy


def test_bundles_reject_bad_tags():
    from local_agent.portability import Conversation
    base = {"id": "a", "title": "t", "workspace": "/srv/x", "model": "m", "created": 1.0, "updated": 1.0,
            "events": [], "wire": []}
    Conversation.model_validate({**base, "tags": ["ok"]})
    for tags in (["x" * 33], [""], ["t"] * 11, [1]):
        with pytest.raises(ValueError):
            Conversation.model_validate({**base, "tags": tags})
