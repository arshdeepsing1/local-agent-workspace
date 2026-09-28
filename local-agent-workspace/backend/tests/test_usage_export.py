import csv
import io
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.store import Store
from local_agent.telemetry import (
    REQUEST_TAGS_HEADER, begin_inference_call, finish_inference_call, record_response_id, request_tag_headers,
)
from local_agent.usage_export import COLUMNS, session_from_jsonl, usage_csv, usage_rows

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "usage_report.py"
MODEL = "databricks-claude-opus-4-8"


def recorded_session():
    session = {"id": "chat-1", "model": MODEL, "events": [
        {"id": "u1", "type": "user", "created": 1790238000.0, "text": "Deploy"},
        {"id": "a1", "type": "assistant", "created": 1790238001.0, "text": "=cmd|' /C calc'!A0 then check pods",
         "request_info": {"model": MODEL, "status": "completed"}},
        {"id": "t1", "type": "tool", "name": "run_command", "created": 1790238002.0, "state": "completed"},
        {"id": "t2", "type": "tool", "name": "read_file", "created": 1790238003.0, "state": "completed"},
        {"id": "a2", "type": "assistant", "created": 1790238010.0, "text": "Done.",
         "request_info": {"model": MODEL, "status": "completed"}},
    ]}
    for event_id, attempt, status, usage in (("a1", 1, 429, None), ("a1", 2, 200, {"input_tokens": 1000, "output_tokens": 50}),
                                             ("a2", 1, 200, {"input_tokens": 1200, "output_tokens": 5})):
        call = begin_inference_call(session, "agent", MODEL, event_id=event_id, max_output_tokens=8192,
                                    attempt=attempt, estimated_input_tokens=900)
        record_response_id(call, None if status == 429 else f"resp-{event_id}")
        finish_inference_call(call, {"status": "completed" if status == 200 else "error", "http_status": status,
                                     **({"usage": usage} if usage else {})})
    title = begin_inference_call(session, "title", MODEL, max_output_tokens=1024)
    finish_inference_call(title, {"status": "completed", "usage": {"input_tokens": 80, "output_tokens": 4}})
    return session


# --- Request tags and response ids ------------------------------------------------

def test_request_tags_name_the_ledger_record_conversation_and_purpose():
    session = {"id": "chat-1"}
    call = begin_inference_call(session, "compaction", MODEL)
    header = request_tag_headers(session, call)
    assert list(header) == [REQUEST_TAGS_HEADER] == ["Databricks-Ai-Gateway-Request-Tags"]
    assert json.loads(header[REQUEST_TAGS_HEADER]) == {
        "local_agent_call_id": call["id"], "local_agent_conversation": "chat-1", "local_agent_purpose": "compaction"}


def test_response_id_keeps_the_first_bounded_string():
    call = {}
    for value in (None, 7, "", "x" * 501):
        record_response_id(call, value)
    assert call == {}
    record_response_id(call, "first")
    record_response_id(call, "second")
    assert call == {"response_id": "first"}
    record_response_id(None, "ignored")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", context_window=32768)
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    yield manager, session
    store.db.close()


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(gateway)))


async def test_every_request_is_tagged_and_response_ids_are_recorded(runtime, monkeypatch):
    manager, session = runtime
    session["wire"] = [message for i in range(4) for message in (
        {"role": "user", "content": f"Earlier request {i}: " + "x" * 21000},
        {"role": "assistant", "content": f"Completed step {i}."})]
    tagged = []

    async def gateway(request):
        payload = json.loads(request.content)
        tagged.append(json.loads(request.headers[REQUEST_TAGS_HEADER]))
        if payload["stream"]:
            chunk = {"id": "chatcmpl-stream", "choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 9000, "completion_tokens": 3}}
            return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        name = "Short title" if "short title" in payload["messages"][0]["content"] else "Earlier steps done."
        return httpx.Response(200, json={"id": f"chatcmpl-{len(tagged)}", "choices": [
            {"message": {"content": name}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 50, "completion_tokens": 5}})

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Continue")
    calls = session["inference_calls"]
    assert {call["purpose"] for call in calls} == {"compaction", "agent", "title"}
    assert [tags["local_agent_call_id"] for tags in tagged] == [call["id"] for call in calls]
    assert all(tags["local_agent_conversation"] == session["id"] for tags in tagged)
    assert [tags["local_agent_purpose"] for tags in tagged] == [call["purpose"] for call in calls]
    assert all(call.get("response_id", "").startswith("chatcmpl-") for call in calls)
    agent = next(call for call in calls if call["purpose"] == "agent")
    assert agent["response_id"] == "chatcmpl-stream"
    # Response ids stay in the ledger; displayed request details are unchanged.
    assert all("response_id" not in (event.get("request_info") or {}) for event in session["events"])


# --- Rows and CSV --------------------------------------------------------------------

def test_rows_link_calls_to_replies_tools_and_retries():
    rows = usage_rows(recorded_session())
    assert [(row["purpose"], row["attempt"], row["http_status"]) for row in rows] == [
        ("agent", 1, 429), ("agent", 2, 200), ("agent", 1, 200), ("title", "", "")]
    first, retry, second, title = rows
    assert first["event_id"] == retry["event_id"] == "a1" and first["estimated_dbu"] == 0.0
    assert retry["tools_called"] == "run_command, read_file" and retry["response_id"] == "resp-a1"
    assert retry["input_tokens"] == 1000 and retry["estimated_input_tokens"] == 900
    assert second["reply_excerpt"] == "Done." and second["tools_called"] == ""
    assert title["event_id"] == "" and title["reply_excerpt"] == ""
    assert all(row["conversation_id"] == "chat-1" and row["call_id"] for row in rows)
    assert rows[0]["started_utc"].endswith("Z") and rows[0]["seconds"] != ""


def test_legacy_requests_without_a_ledger_still_get_rows():
    session = recorded_session()
    session["inference_calls"] = []
    rows = usage_rows(session)
    assert [row["event_id"] for row in rows] == ["a1", "a2"]
    assert all(row["call_id"] == "" for row in rows)


def test_csv_has_all_columns_and_neutralizes_spreadsheet_formulas():
    text = usage_csv(usage_rows(recorded_session()))
    rows = list(csv.DictReader(io.StringIO(text)))
    assert tuple(rows[0]) == COLUMNS
    assert rows[1]["reply_excerpt"].startswith("'=cmd")
    assert rows[1]["tools_called"] == "run_command, read_file"


def test_jsonl_written_by_the_store_round_trips(tmp_path):
    store = Store(tmp_path / "state" / "tests.sqlite3")
    try:
        session = store.create({"workspace": str(tmp_path), "model": MODEL})
        session.update(recorded_session(), id=session["id"])
        store.save(session)
        loaded = session_from_jsonl(tmp_path / "state" / "conversations" / f"{session['id']}.jsonl")
        assert usage_rows(loaded) == usage_rows(store.get(session["id"]))
    finally:
        store.db.close()


# --- Endpoint and script -----------------------------------------------------------------

def test_csv_endpoint_requires_the_token_and_downloads_usage(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/bootstrap").json()["token"]
        manager = client.app.state.manager
        session = manager.store.create(settings.values)
        session.update(recorded_session(), id=session["id"])
        manager.store.save(session)
        path = f"/api/sessions/{session['id']}/usage.csv"
        assert client.get(path).status_code == 403
        response = client.get(path, headers={"X-Local-Token": token})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert response.headers["content-disposition"] == f'attachment; filename="usage-{session["id"][:8]}.csv"'
        assert len(list(csv.DictReader(io.StringIO(response.text)))) == 4
        assert client.get("/api/sessions/missing/usage.csv", headers={"X-Local-Token": token}).status_code == 404


def test_script_prints_rows_and_writes_the_same_csv(tmp_path):
    store = Store(tmp_path / "state" / "tests.sqlite3")
    try:
        session = store.create({"workspace": str(tmp_path), "model": MODEL})
        session.update(recorded_session(), id=session["id"])
        store.save(session)
    finally:
        store.db.close()
    source = tmp_path / "state" / "conversations" / f"{session['id']}.jsonl"
    output = tmp_path / "usage.csv"
    result = subprocess.run([sys.executable, str(SCRIPT), str(source), "--csv", str(output)],
                            capture_output=True, text=True, check=True)
    assert "Wrote 4 rows" in result.stdout
    assert "-> tools: run_command, read_file" in result.stdout
    assert "Total: 4 calls, 2,280 input, 59 output tokens" in result.stdout
    assert output.read_text(encoding="utf-8") == usage_csv(usage_rows(session_from_jsonl(source)))
    usage = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert usage.returncode == 2 and "Usage" in usage.stdout
