import copy
import json
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.portability import MAX_BUNDLE_BYTES, export_bundle, fresh_copies, parse_bundle


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    app = create_app(settings)
    manager = app.state.manager
    source = manager.store.create(settings.values)
    source.update(title="Completed work", terminal_reason="completed", permission_mode="bypassPermissions",
                  allowed_directories=["/old/grant"], active_skills=["old-skill"], instruction_directories=["old/path"],
                  tool_profile="read_only", subagent_tool_profile="file_editor")
    source["wire"] = [
        {"role": "user", "content": "Read the file"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-old", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"README.md"}'}}]},
        {"role": "tool", "tool_call_id": "call-old", "content": "Historical result"},
        {"role": "assistant", "content": "Read."},
        {"role": "user", "content": "Summarize"},
        {"role": "assistant", "content": "Done."},
    ]
    source["events"] = [
        {"id": "u1", "type": "user", "text": "Read the file"},
        {"id": "t1", "type": "tool", "name": "read_file", "input": {"path": "README.md"},
         "state": "completed", "output": "Historical result", "call_id": "call-old", "job_id": "old-job"},
        {"id": "a1", "type": "assistant", "text": "Done.", "request_info": {
            "model": source["model"], "status": "completed", "usage": {"output_tokens": 0, "cache_read_input_tokens": 0}}},
    ]
    source["context_state"] = {"summary": "Original decisions", "through": 4, "compactions": 1,
                               "tool_definitions": [{"function": {"name": "untrusted_old_tool"}}]}
    manager.store.save(source)
    manager.execute_tool = AsyncMock(side_effect=AssertionError("Transfer must not execute tools"))
    manager.run_databricks = AsyncMock(side_effect=AssertionError("Transfer must not call a model"))
    yield app, manager, source, destination
    manager.store.db.close()


async def client_for(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        token = (await client.get("/api/bootstrap")).json()["token"]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"X-Local-Token": token})


async def test_export_import_preserves_history_without_authority_or_execution(runtime):
    app, manager, source, destination = runtime
    before = copy.deepcopy(manager.store.get(source["id"]))
    async with await client_for(app) as client:
        response = await client.get(f"/api/sessions/{source['id']}/export")
        assert response.status_code == 200
        bundle = response.json()
        portable = bundle["sessions"][0]
        for field in ("permission_mode", "allowed_directories", "active_skills", "instruction_directories"):
            assert field not in portable
        assert "job_id" not in portable["events"][1]
        assert "tool_definitions" not in portable["context_state"]
        response = await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)
        assert response.status_code == 201, response.text
        imported = manager.store.get(response.json()["session"]["id"])
        uuid.UUID(imported["id"])
        assert imported["id"] != source["id"]
        assert imported["workspace"] == str(destination)
        assert imported["permission_mode"] == "manual"
        assert imported["allowed_directories"] == imported["active_skills"] == []
        assert imported["tool_profile"] == "read_only"
        assert imported["subagent_tool_profile"] == "file_editor"
        assert imported["context_state"] == {"summary": "Original decisions", "through": 4, "compactions": 1}
        assert imported["wire"][1]["content"] is None
        new_call = imported["wire"][1]["tool_calls"][0]["id"]
        assert new_call != "call-old"
        assert new_call == imported["wire"][2]["tool_call_id"] == imported["events"][1]["call_id"]
        assert set(e["id"] for e in imported["events"]).isdisjoint(e["id"] for e in source["events"])
        assert imported["events"][2]["request_info"]["usage"] == {"output_tokens": 0, "cache_read_input_tokens": 0}
        assert imported["provenance"]["source_workspace"] == source["workspace"]
        assert imported["provenance"]["source_created"] == source["created"]
        assert not manager.jobs.list(session_id=imported["id"])
        assert not manager.task_board.list(imported["id"])
        assert not manager.checkpoints.list(imported["id"])
        assert manager.store.get(source["id"]) == before
        # Reimport is intentionally a new copy, never an overwrite.
        again = await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)
        assert again.json()["session"]["id"] not in (source["id"], imported["id"])
        manager.execute_tool.assert_not_called()
        manager.run_databricks.assert_not_called()


async def test_included_children_remap_parent_event_call_and_origin_but_single_export_drops_links(runtime):
    app, manager, source, destination = runtime
    child = manager.store.create(manager.settings.values)
    child.update(is_subagent=True, parent_session_id=source["id"], parent_event_id="t1", parent_call_id="call-old",
                 tool_profile="read_only", terminal_reason="stopped", delegation={"status": "running", "completed_tools": 0},
                 events=[{"id": "child-event", "type": "user", "text": "Inspect", "origin": {
                     "kind": "delegated", "parent_session_id": source["id"], "parent_event_id": "t1", "parent_call_id": "call-old"}}])
    source["events"][1].update(child_session_id=child["id"], delegation={"status": "running", "completed_tools": 0})
    manager.store.save(child)
    manager.store.save(source)
    async with await client_for(app) as client:
        bundle = (await client.get(f"/api/sessions/{source['id']}/export?include_children=true")).json()
        result = (await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)).json()
        assert result["imported_count"] == 2
        parent = manager.store.get(result["session"]["id"])
        copied = manager.store.get(parent["events"][1]["child_session_id"])
        assert copied["parent_session_id"] == parent["id"]
        assert copied["parent_event_id"] == parent["events"][1]["id"]
        assert copied["parent_call_id"] == parent["events"][1]["call_id"]
        assert copied["events"][0]["origin"]["parent_session_id"] == parent["id"]
        assert copied["tool_profile"] == "read_only"
        assert copied["delegation"]["terminal_reason"] == "interrupted"
        single = export_bundle([source], source["id"])
        copies, _ = fresh_copies(single, str(destination))
        assert "child_session_id" not in copies[0]["events"][1]
        child_only = export_bundle([child], child["id"])
        copies, _ = fresh_copies(child_only, str(destination))
        assert not any(key in copies[0] for key in ("parent_session_id", "parent_event_id", "parent_call_id"))
        assert "origin" not in copies[0]["events"][0]
        assert copies[0]["is_subagent"] is True


async def test_import_marks_pending_approval_and_partial_response_historical(runtime):
    app, manager, source, destination = runtime
    bundle = export_bundle([source], source["id"])
    record = bundle["sessions"][0]
    record["events"][1]["state"] = "pending"
    record["events"][2]["request_info"]["status"] = "running"
    async with await client_for(app) as client:
        response = await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)
        imported = response.json()["session"]
        assert imported["events"][1]["state"] == "cancelled"
        assert imported["events"][2]["request_info"]["status"] == "interrupted"
        assert imported["terminal_reason"] == "interrupted"
        assert not manager.pending and not manager.tasks
        response = await client.post(f"/api/sessions/{imported['id']}/approvals/{imported['events'][1]['id']}", json={"allowed": True})
        assert response.status_code in (400, 404)
        manager.execute_tool.assert_not_called()


@pytest.mark.parametrize("mutation", [
    lambda b: b.update(version=2),
    lambda b: b.update(version=True),
    lambda b: b.update(credentials={"token": "not-authority"}),
    lambda b: b["sessions"][0].update(permission_mode="bypassPermissions"),
    lambda b: b["sessions"][0].update(allowed_directories=["/"]),
    lambda b: b["sessions"][0].update(tool_profile="arbitrary"),
    lambda b: b["sessions"][0].update(tool_profile=None),
    lambda b: b["sessions"][0].update(subagent_tool_profile=None),
    lambda b: b["sessions"][0]["events"][0].update(job_id="existing-job"),
    lambda b: b["sessions"][0]["wire"][0].update(role="system"),
    lambda b: b["sessions"][0]["wire"][2].update(tool_call_id="unknown"),
    lambda b: b["sessions"][0]["context_state"].update(through=999),
    lambda b: b["sessions"][0]["context_state"].update(through=2),
    lambda b: b["sessions"][0]["context_state"].update(summary=""),
    lambda b: b["sessions"][0]["events"][1].update(id="u1"),
    lambda b: b["sessions"].append(copy.deepcopy(b["sessions"][0])),
    lambda b: b["sessions"][0]["events"][2]["request_info"]["usage"].update(output_tokens=-1),
    lambda b: b["sessions"][0]["events"][1].update(input=[]),
])
async def test_invalid_import_is_atomic_and_never_reflects_values(runtime, mutation):
    app, manager, source, destination = runtime
    bundle = export_bundle([source], source["id"])
    mutation(bundle)
    before = manager.store.list()
    async with await client_for(app) as client:
        response = await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)
        assert response.status_code == 400, response.text
        assert "not-authority" not in response.text
    assert manager.store.list() == before


async def test_size_caps_apply_before_reading_entire_body_and_without_content_length(runtime):
    app, manager, source, destination = runtime
    async with await client_for(app) as client:
        async def chunks():
            yield b"x" * (MAX_BUNDLE_BYTES + 1)
            raise AssertionError("Oversized stream should not be read further")
        response = await client.post("/api/sessions/import", params={"workspace": str(destination)}, content=chunks())
        assert response.status_code == 413
        response = await client.post("/api/sessions/import", params={"workspace": str(destination)}, content=b"{}",
                                     headers={"Content-Length": str(MAX_BUNDLE_BYTES + 1)})
        assert response.status_code == 413
        assert len(manager.store.list()) == 1


@pytest.mark.parametrize("raw", [b'{"format":"one","format":"two"}', b'NaN', b'{}', b'not json', b'[' * 1100])
def test_parser_rejects_invalid_complex_or_duplicate_json(raw):
    with pytest.raises(ValueError):
        parse_bundle(raw)


async def test_destination_is_explicit_existing_and_never_exported_path(runtime):
    app, manager, source, destination = runtime
    bundle = export_bundle([source], source["id"])
    async with await client_for(app) as client:
        assert (await client.post("/api/sessions/import", json=bundle)).status_code == 422
        for path in ("", "relative", str(destination / "missing"), str(manager.settings.state_dir)):
            assert (await client.post("/api/sessions/import", params={"workspace": path}, json=bundle)).status_code == 400
        assert len(manager.store.list()) == 1


async def test_fork_preserves_full_history_compaction_and_source_without_children(runtime):
    app, manager, source, _ = runtime
    before = manager.store.get(source["id"])
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{source['id']}/fork")
        assert response.status_code == 201, response.text
        fork = manager.store.get(response.json()["id"])
        assert fork["title"] == "Completed work (fork)"
        assert len(fork["wire"]) == len(source["wire"])
        assert len(fork["events"]) == len(source["events"])
        assert fork["context_state"]["through"] == 4
        assert fork["permission_mode"] == "manual" and fork["allowed_directories"] == []
        assert fork["provenance"]["kind"] == "fork"
        assert fork["tool_profile"] == "read_only"
        assert fork["workspace"] == source["workspace"]
        assert manager.store.get(source["id"]) == before
        manager.execute_tool.assert_not_called()
        manager.run_databricks.assert_not_called()


async def test_fork_rejects_active_or_not_completed_conversations_and_export_active_children(runtime):
    app, manager, source, _ = runtime
    async with await client_for(app) as client:
        manager.statuses[source["id"]] = "compacting"
        assert (await client.post(f"/api/sessions/{source['id']}/fork")).status_code == 409
        assert (await client.get(f"/api/sessions/{source['id']}/export")).status_code == 409
        manager.statuses[source["id"]] = "idle"
        for reason in ("stopped", "step_limit", "inference_error"):
            source["terminal_reason"] = reason
            manager.store.save(source)
            assert (await client.post(f"/api/sessions/{source['id']}/fork")).status_code == 400
        assert len(manager.store.list()) == 1


async def test_transfer_routes_require_local_token(runtime):
    app, _, source, destination = runtime
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        for method, path in (("GET", f"/api/sessions/{source['id']}/export"), ("POST", f"/api/sessions/{source['id']}/fork"),
                             ("POST", f"/api/sessions/import?workspace={destination}")):
            assert (await client.request(method, path)).status_code == 403


@pytest.mark.parametrize("call_id", ["interrupted", "call-old"])
@pytest.mark.parametrize("arguments", ['{"path":"file.txt"}', '{"path":'])
async def test_imported_interrupted_tool_exchange_can_continue_without_replay(runtime, monkeypatch, arguments, call_id):
    app, manager, source, destination = runtime
    source["wire"].append({"role": "user", "content": "One more request"})
    source["wire"].append({"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": "write_file", "arguments": arguments}}]})
    source["events"].append({"id": "interrupted-event", "type": "tool", "name": "write_file", "state": "pending",
                              "input": {"path": "file.txt"}, "call_id": call_id, "output": ""})
    bundle = export_bundle([source], source["id"])
    async with await client_for(app) as client:
        result = await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)
        assert result.status_code == 201
    imported = manager.store.get(result.json()["session"]["id"])
    original = copy.deepcopy(imported["wire"])
    assert original[1]["tool_calls"][0]["id"] != original[-1]["tool_calls"][0]["id"]
    requests = []
    def gateway(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Ready to continue."},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(gateway)))
    manager.settings.credentials = lambda: ("https://example.invalid", "test-token")
    # Exercise the actual normal continuation path, with network mocked and every
    # historical tool forbidden from executing.
    del manager.run_databricks
    assert await manager.run_databricks(imported, "Check current state first.") is True
    assert len(requests) == 1
    assert imported["wire"][:len(original)] == original
    assert imported["context_state"]["through"] == 4
    assert any("outcome is unknown" in str(message.get("content")) for message in requests[0]["messages"])
    manager.execute_tool.assert_not_called()
    assert not (destination / "file.txt").exists()


async def test_import_id_collision_never_overwrites_source_or_leaves_partial_children(runtime, monkeypatch):
    app, manager, source, destination = runtime
    child = manager.store.create(manager.settings.values)
    child.update(parent_session_id=source["id"], is_subagent=True, tool_profile="file_editor")
    manager.store.save(child)
    bundle = export_bundle([source, child], source["id"])
    before = manager.store.list()
    generated = iter([uuid.uuid4(), uuid.UUID(source["id"]), *[uuid.uuid4() for _ in range(5)]])
    monkeypatch.setattr("local_agent.portability.uuid.uuid4", lambda: next(generated))
    async with await client_for(app) as client:
        response = await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)
        assert response.status_code == 409
    assert manager.store.list() == before
    assert manager.store.get(source["id"])["permission_mode"] == "bypassPermissions"


def test_reused_call_ids_in_separate_completed_exchanges_get_distinct_copy_ids(runtime):
    _, _, source, destination = runtime
    source["wire"].extend(copy.deepcopy(source["wire"][:4]))
    bundle = export_bundle([source], source["id"])
    copies, _ = fresh_copies(bundle, str(destination))
    first = copies[0]["wire"][1]["tool_calls"][0]["id"]
    second = copies[0]["wire"][7]["tool_calls"][0]["id"]
    assert first != second
    assert first == copies[0]["wire"][2]["tool_call_id"]
    assert second == copies[0]["wire"][8]["tool_call_id"]


async def test_child_limit_and_disconnected_bundles_are_rejected_atomically(runtime):
    app, manager, source, destination = runtime
    children = []
    for _ in range(32):
        child = manager.store.create(manager.settings.values)
        child.update(parent_session_id=source["id"], is_subagent=True)
        manager.store.save(child)
        children.append(child)
    async with await client_for(app) as client:
        response = await client.get(f"/api/sessions/{source['id']}/export?include_children=true")
        assert response.status_code == 400
        bundle = export_bundle([source, *children[:31]], source["id"])
        bundle["sessions"].append(copy.deepcopy(children[-1]))
        assert (await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)).status_code == 400
        bundle = export_bundle([source, children[0]], source["id"])
        bundle["sessions"][1]["parent_session_id"] = "unrelated-old-conversation"
        assert (await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)).status_code == 400
    assert len(manager.store.list()) == 33


async def test_legacy_fork_accepts_finished_reply_but_not_error_or_interrupted_draft(runtime):
    app, manager, source, _ = runtime
    source.pop("terminal_reason")
    manager.store.save(source)
    async with await client_for(app) as client:
        assert (await client.post(f"/api/sessions/{source['id']}/fork")).status_code == 201
        source["events"][-1]["request_info"]["status"] = "interrupted"
        manager.store.save(source)
        assert (await client.post(f"/api/sessions/{source['id']}/fork")).status_code == 400
        source["events"][-1]["request_info"]["status"] = "completed"
        source["events"].append({"id": "error", "type": "error", "text": "Response failed."})
        manager.store.save(source)
        assert (await client.post(f"/api/sessions/{source['id']}/fork")).status_code == 400


def test_reused_call_correlation_tracks_parent_events_and_hooks_per_occurrence(runtime):
    _, manager, source, destination = runtime
    repeated = copy.deepcopy(source["wire"][:4])
    repeated[2]["content"] = "Second result"
    source["wire"].extend(repeated)
    first = source["events"][1]
    second = {**copy.deepcopy(first), "id": "t2", "output": "Second result"}
    first["input"]["call_id"] = "ordinary tool argument; do not rewrite"
    hooks = [{"id": f"hook-{index}", "type": "tool", "name": "hook_after_tool", "state": "completed",
              "input": {"hook_id": "check", "tool": "read_file", "call_id": "call-old"}, "output": "checked"}
             for index in (1, 2)]
    source["events"] = [source["events"][0], first, hooks[0], source["events"][2], second, hooks[1]]
    child = manager.store.create(manager.settings.values)
    child.update(parent_session_id=source["id"], parent_event_id="t2", parent_call_id="call-old", is_subagent=True,
                 events=[{"id": "child-user", "type": "user", "text": "Delegated", "origin": {
                     "kind": "delegated", "parent_session_id": source["id"], "parent_event_id": "t2", "parent_call_id": "call-old"}}])
    second["child_session_id"] = child["id"]
    bundle = export_bundle([source, child], source["id"])
    copies, _ = fresh_copies(bundle, str(destination))
    parent, copied_child = copies
    call_one = parent["wire"][1]["tool_calls"][0]["id"]
    call_two = parent["wire"][7]["tool_calls"][0]["id"]
    assert call_one != call_two
    assert parent["events"][1]["call_id"] == parent["events"][2]["input"]["call_id"] == call_one
    assert parent["events"][4]["call_id"] == parent["events"][5]["input"]["call_id"] == call_two
    assert parent["events"][1]["input"]["call_id"] == "ordinary tool argument; do not rewrite"
    assert copied_child["parent_call_id"] == copied_child["events"][0]["origin"]["parent_call_id"] == call_two
    assert copied_child["parent_event_id"] == parent["events"][4]["id"]


def test_ambiguous_legacy_tool_events_do_not_supply_results_for_a_different_request(runtime):
    _, _, source, destination = runtime
    source["wire"].extend(copy.deepcopy(source["wire"][:4]))
    copies, _ = fresh_copies(export_bundle([source], source["id"]), str(destination))
    # Two identical exchanges but only one event: don't guess which exchange it
    # belongs to. Both wire results remain complete and the event remains visible.
    call_ids = {call["id"] for message in copies[0]["wire"] for call in message.get("tool_calls", [])}
    assert copies[0]["events"][1]["call_id"] not in call_ids
    assert copies[0]["events"][1]["output"] == "Historical result"


@pytest.mark.parametrize("in_key", [False, True])
async def test_invalid_utf8_in_arbitrary_history_is_rejected_before_insert(runtime, in_key):
    app, manager, source, destination = runtime
    bundle = export_bundle([source], source["id"])
    if in_key:
        bundle["sessions"][0]["events"][1]["input"]["\ud800"] = "invalid key"
    else:
        bundle["sessions"][0]["events"][1]["output"] = "\ud800"
    before = manager.store.list()
    async with await client_for(app) as client:
        response = await client.post("/api/sessions/import", params={"workspace": str(destination)},
                                     content=json.dumps(bundle).encode("utf-8"))
        assert response.status_code == 400
        assert "UTF-8" in response.json()["detail"]
    assert manager.store.list() == before


def test_wide_json_is_rejected_before_allocating_a_second_wide_traversal():
    with pytest.raises(ValueError, match="too complex"):
        parse_bundle(b"[" + b"0," * 200000 + b"0]")


def test_oversized_export_is_rejected_before_validation_and_full_copy(runtime, monkeypatch):
    _, _, source, _ = runtime
    source["events"][0]["text"] = "x" * (MAX_BUNDLE_BYTES + 1)
    monkeypatch.setattr("local_agent.portability.validate_bundle", lambda value: pytest.fail("Oversized data reached validation/copy"))
    with pytest.raises(ValueError, match="16 MiB"):
        export_bundle([source], source["id"])
