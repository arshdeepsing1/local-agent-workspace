"""One unreadable conversation file must not stop the app or hide the others."""
import json

import pytest
from fastapi.testclient import TestClient

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.store import Store, UnreadableConversation

GARBAGE_ID = "11111111-1111-4111-8111-111111111111"
EMPTY_ID = "22222222-2222-4222-8222-222222222222"
MISMATCH_ID = "33333333-3333-4333-8333-333333333333"
BODY_ID = "44444444-4444-4444-8444-444444444444"
EVENTS_ID = "55555555-5555-4555-8555-555555555555"


def header(session_id, **extra):
    data = {"id": session_id, "title": "Broken", "created": 1.0, "updated": 2.0, "workspace": "/srv/project",
            "model": "test-model", **extra}
    return json.dumps({"record": "session", "version": 1, "data": data}) + "\n"


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A state folder with one good conversation and several unreadable files."""
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(workspace), env_file="")
    store = Store(settings.state_dir / "conversations.sqlite3")
    good = store.create(settings.values)
    good["events"].append({"id": "e1", "type": "tool", "state": "running", "created": 1.0})
    store.save(good)
    store.db.close()
    folder = settings.state_dir / "conversations"
    files = {
        GARBAGE_ID: b"\x00\x01 SECRET-CONTENT not json at all\n",
        EMPTY_ID: b"",
        MISMATCH_ID: header("another-conversation").encode(),
        BODY_ID: (header(BODY_ID) + '{"record": "event", "data": {"text": "SECRET-CONTENT"\n').encode(),
        EVENTS_ID: (header(EVENTS_ID) + json.dumps({"record": "event", "data": "SECRET-CONTENT"}) + "\n").encode(),
    }
    for session_id, raw in files.items():
        (folder / f"{session_id}.jsonl").write_bytes(raw)
    yield settings, good, folder, files


def test_app_starts_lists_and_opens_the_readable_conversations(state):
    settings, good, folder, files = state
    with TestClient(create_app(settings)) as client:
        bootstrap = client.get("/api/bootstrap").json()
        client.headers["X-Local-Token"] = bootstrap["token"]

        assert [item["id"] for item in client.get("/api/sessions").json()] == [good["id"]]
        opened = client.get(f"/api/sessions/{good['id']}").json()
        assert opened["events"][0]["state"] == "cancelled"  # startup recovery still ran for it

        problems = {item["file"]: item["reason"] for item in bootstrap["storage_problems"]}
        assert problems == {
            f"{GARBAGE_ID}.jsonl": "line 1 is not valid JSON",
            f"{EMPTY_ID}.jsonl": "the file is empty",
            f"{MISMATCH_ID}.jsonl": "its conversation ID does not match the file name",
            f"{BODY_ID}.jsonl": "line 2 is not valid JSON",
            f"{EVENTS_ID}.jsonl": "its records could not be loaded",
        }
        assert "SECRET-CONTENT" not in json.dumps(bootstrap)

        # Opening one directly is a clear client error, never a server error.
        for session_id in (GARBAGE_ID, EMPTY_ID, MISMATCH_ID, BODY_ID):
            response = client.get(f"/api/sessions/{session_id}")
            assert response.status_code == 400
            assert response.json()["detail"].startswith(f"Conversation file {session_id}.jsonl could not be read: ")
            assert "SECRET-CONTENT" not in response.text

    # The files are left exactly as they were.
    for session_id, raw in files.items():
        assert (folder / f"{session_id}.jsonl").read_bytes() == raw


def test_a_repaired_file_is_listed_again(state):
    settings, good, folder, _ = state
    with TestClient(create_app(settings)) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        (folder / f"{BODY_ID}.jsonl").write_text(header(BODY_ID, updated=3.0))
        assert [item["id"] for item in client.get("/api/sessions").json()] == [good["id"], BODY_ID]
        assert client.get(f"/api/sessions/{BODY_ID}").json()["title"] == "Broken"
        files = [item["file"] for item in client.get("/api/bootstrap").json()["storage_problems"]]
        assert f"{BODY_ID}.jsonl" not in files and len(files) == 4


def test_store_list_skips_and_reports_without_reading_contents(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    session = store.create({"workspace": str(tmp_path), "model": "m"})
    (store.conversations / f"{GARBAGE_ID}.jsonl").write_text("not json\n")
    (store.conversations / f"{EMPTY_ID}.jsonl").write_text(header(EMPTY_ID).replace('"updated": 2.0', '"updated": "soon"'))
    assert [item["id"] for item in store.list()] == [session["id"]]
    assert store.problems() == [
        {"file": f"{GARBAGE_ID}.jsonl", "reason": "line 1 is not valid JSON"},
        {"file": f"{EMPTY_ID}.jsonl", "reason": "its header lacks a valid title, workspace or update time"},
    ]
    with pytest.raises(UnreadableConversation, match="not valid JSON"):
        store.get(GARBAGE_ID)
    store.db.close()
