"""The first stream snapshot sends the latest events; /events pages earlier ones; /info reads only the header."""
import pytest
from fastapi.testclient import TestClient

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.store import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(workspace), env_file="")
    settings.env = {}
    app = create_app(settings)
    with TestClient(app) as client:
        token = client.get("/api/bootstrap").json()["token"]
        client.headers["X-Local-Token"] = token
        client.token = token
        yield client, app.state.manager


def chat(manager, events):
    session = manager.store.create(manager.settings.values)
    session["events"] = events
    manager.store.save(session)
    return session["id"]


def turns(*sizes):
    """Turns of one user event followed by size - 1 assistant events."""
    events = []
    for number, size in enumerate(sizes):
        events.append({"id": f"u{number}", "type": "user", "text": f"Question {number}", "created": float(len(events))})
        events += [{"id": f"a{number}-{index}", "type": "assistant", "text": "Answer", "created": float(len(events) + index)}
                   for index in range(size - 1)]
    return events


def snapshot(client, session_id):
    with client.websocket_connect(f"/api/sessions/{session_id}/stream", subprotocols=["local-workspace", client.token]) as socket:
        message = socket.receive_json()
    assert message["type"] == "snapshot"
    return message["session"]


@pytest.mark.parametrize("sizes,start", [
    ((5, 5), 0),                  # Short: everything.
    ((150, 100), 50),             # The latest 200 already hold the whole latest turn.
    ((10, 300), 10),              # A 300-event latest turn is sent whole.
    ((100, 1200), 300),           # Capped at 1,000 events.
    ((250,), 0),                  # A single turn longer than 200 events starts at its user event.
])
def test_the_first_snapshot_holds_the_latest_events_and_the_whole_latest_turn(client, sizes, start):
    client, manager = client
    events = turns(*sizes)
    session_id = chat(manager, events)
    sent = snapshot(client, session_id)
    assert sent["events_start"] == start and sent["events_total"] == len(events)
    assert [event["id"] for event in sent["events"]] == [event["id"] for event in events[start:]]


def test_other_snapshots_stay_complete(client):
    client, manager = client
    session_id = chat(manager, turns(150, 150))
    with client.websocket_connect(f"/api/sessions/{session_id}/stream", subprotocols=["local-workspace", client.token]) as socket:
        assert socket.receive_json()["session"]["events_start"] == 100
        assert socket.receive_json()["type"] == "changes"
        response = client.post(f"/api/sessions/{session_id}/folders", json={"path": str(manager.settings.values["workspace"])})
        assert response.status_code == 200
        message = socket.receive_json()
    assert message["type"] == "snapshot" and "events_start" not in message["session"]
    assert len(message["session"]["events"]) == 300


def test_earlier_events_come_in_pages(client):
    client, manager = client
    events = turns(10, 300)
    session_id = chat(manager, events)
    page = client.get(f"/api/sessions/{session_id}/events", params={"before": "u1", "limit": 4}).json()
    assert page == {"events": events[6:10], "start": 6, "total": 310}
    page = client.get(f"/api/sessions/{session_id}/events", params={"before": "a0-2"}).json()
    assert page == {"events": events[:3], "start": 0, "total": 310}
    assert client.get(f"/api/sessions/{session_id}/events", params={"before": "u0"}).json()["events"] == []
    page = client.get(f"/api/sessions/{session_id}/events", params={"before": events[-1]["id"], "limit": 500}).json()
    assert page["start"] == 0 and page["events"] == events[:309]


def test_unknown_or_invalid_page_requests_are_refused(client):
    client, manager = client
    session_id = chat(manager, turns(3))
    response = client.get(f"/api/sessions/{session_id}/events", params={"before": "gone"})
    assert response.status_code == 400
    assert response.json()["detail"] == "That message is no longer in this conversation."
    for limit in (0, 501):
        assert client.get(f"/api/sessions/{session_id}/events", params={"before": "u0", "limit": limit}).status_code == 422
    assert client.get("/api/sessions/missing/events", params={"before": "u0"}).status_code == 404


def test_info_reads_only_the_header(client, monkeypatch):
    client, manager = client
    session_id = chat(manager, turns(3))
    full = []
    original = Store._read_lines

    def counting(path, *, metadata_only=False):
        if not metadata_only:
            full.append(path.name)
        return original(path, metadata_only=metadata_only)
    monkeypatch.setattr(Store, "_read_lines", staticmethod(counting))
    info = client.get(f"/api/sessions/{session_id}/info")
    assert info.status_code == 200
    body = info.json()
    assert body["id"] == session_id and body["status"] == "idle" and "events" not in body and "wire" not in body
    assert full == []
    manager.statuses[session_id] = "running"
    assert client.get(f"/api/sessions/{session_id}/info").json()["status"] == "running"


def test_info_reports_missing_and_unreadable_chats(client):
    client, manager = client
    assert client.get("/api/sessions/no-such-chat/info").status_code == 404
    assert client.get("/api/sessions/no-such-chat/info").json()["detail"] == "Conversation not found."
    path = manager.store.conversations / "broken-chat.jsonl"
    path.write_text("not json\n")
    response = client.get("/api/sessions/broken-chat/info")
    assert response.status_code == 400
    assert "broken-chat.jsonl could not be read" in response.json()["detail"]


def test_a_damaged_body_found_on_opening_is_hidden_and_info_then_reports_it(client):
    client, manager = client
    path = manager.store.conversations / "damaged-body.jsonl"
    path.write_text('{"record":"session","version":1,"data":{"id":"damaged-body","title":"T","updated":2.0,'
                    '"workspace":"/srv/x","model":"m"}}\n{"record":"event","data":\n')
    assert [item["id"] for item in client.get("/api/sessions").json()] == ["damaged-body"]
    assert client.get("/api/sessions/damaged-body/info").status_code == 200  # Only the header is read.
    response = client.get("/api/sessions/damaged-body")
    assert response.status_code == 400
    assert client.get("/api/sessions").json() == []
    assert client.get("/api/sessions/damaged-body/info").status_code == 400
    assert path.read_text().endswith('"data":\n')  # Never rewritten.
