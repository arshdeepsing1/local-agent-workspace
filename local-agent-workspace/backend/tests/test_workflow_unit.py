"""Parent and children as one unit: fork, export, delete and transcript of a chat with its subagent chats.

The parent below is built the way the app saved one before the workflow upgrade: two ended subagent chats,
linked both ways (child headers name the parent, the parent's cards name the children), and a subagents index.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.planning import index_entry
from local_agent.portability import MAX_SESSIONS, export_bundle, parse_bundle
from local_agent.transcript import transcript
from local_agent.usage_export import session_from_jsonl

FIXTURES = Path(__file__).parent / "fixtures" / "workflow"
PARENT_ID = "11111111-1111-4111-8111-111111111111"
CHILD_IDS = ("22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333")
BASE = 1_790_000_000.0
TASKS = ("Survey the tables\nlist each one", "Check the indexes")


def reply(text, **extra):
    return {"type": "assistant", "text": text, "request_info": {"model": "test-model", "status": "completed",
                                                               "finish_reason": "stop"}, **extra}


def child_dict(number, workspace, task, parent_id=PARENT_ID, child_id=None, started=None, secret=""):
    """An ended subagent chat of the parent's delegate_task card number (1, 2, ...)."""
    started = BASE + 10 * number if started is None else started
    prompt = "Delegated task:\n" + task
    return {
        "id": child_id or CHILD_IDS[number - 1], "title": "Subagent: " + task.splitlines()[0], "created": started,
        "updated": started + 5, "workspace": workspace, "model": "test-model", "is_subagent": True,
        "parent_session_id": parent_id, "parent_event_id": f"card{number}", "parent_call_id": f"call-{number}",
        "agent_type": "explore", "tool_profile": "read_only", "terminal_reason": "completed",
        "delegation": {"status": "completed", "terminal_reason": "completed", "started": started, "ended": started + 5,
                       "completed_tools": 0},
        "permission_mode": "bypassPermissions", "allowed_directories": ["/old/grant"],
        "allow_rules": [{"tool": "run_command", "command": "ls"}], "active_skills": [],
        "events": [{"id": f"c{number}-u", "type": "user", "text": prompt, "created": started,
                    "origin": {"kind": "delegated", "parent_session_id": parent_id, "parent_event_id": f"card{number}",
                               "parent_call_id": f"call-{number}"}},
                   {"id": f"c{number}-a", **reply(f"# Findings\nFound {number} things. {secret}".rstrip())}],
        "wire": [{"role": "user", "content": prompt},
                 {"role": "assistant", "content": f"# Findings\nFound {number} things. {secret}".rstrip()}],
        "inference_calls": [], "inference_ledger_version": 1,
    }


def card(number, child_id, task):
    return {"id": f"card{number}", "type": "tool", "name": "delegate_task", "call_id": f"call-{number}", "state": "completed",
            "input": {"task": task, "agent_type": "explore"}, "output": json.dumps({"status": "completed"}),
            "child_session_id": child_id,
            "delegation": {"status": "completed", "terminal_reason": "completed", "started": BASE + 10 * number,
                           "ended": BASE + 10 * number + 5, "completed_tools": 0}}


def family_dicts(workspace="/project", extra=0, secret=""):
    """(parent, [children]) as plain dicts with fixed IDs. extra adds more ended children of cards 3, 4, ..."""
    parent = {
        "id": PARENT_ID, "title": "Plan the migration", "created": BASE, "updated": BASE + 200, "workspace": workspace,
        "model": "test-model", "terminal_reason": "completed", "permission_mode": "bypassPermissions",
        "allowed_directories": ["/old/grant"], "allow_rules": [{"tool": "run_command", "command": "ls"}],
        "active_skills": [], "inference_calls": [], "inference_ledger_version": 1,
        "events": [
            {"id": "u1", "type": "user", "text": "Plan the migration", "created": BASE},
            {"id": "a1", **reply("I will delegate two tasks.")},
            card(1, CHILD_IDS[0], TASKS[0]),
            {"id": "n1", "type": "notice", "text": "Background subagent explore finished: Survey the tables",
             "subagent_id": CHILD_IDS[0], "subagent_status": "completed"},
            {"id": "a2", **reply("The first part is done.")},
            {"id": "u2", "type": "user", "text": "Now the second part", "created": BASE + 100},
            card(2, CHILD_IDS[1], TASKS[1]),
            {"id": "a3", **reply("All done.")},
        ],
        "wire": [
            {"role": "user", "content": "Plan the migration"},
            {"role": "assistant", "content": "I will delegate two tasks.", "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "delegate_task", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "completed"},
            {"role": "assistant", "content": "The first part is done."},
            {"role": "user", "content": "Now the second part"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-2", "type": "function", "function": {"name": "delegate_task", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-2", "content": "completed"},
            {"role": "assistant", "content": "All done."},
        ],
    }
    children = [child_dict(1, workspace, TASKS[0]), child_dict(2, workspace, TASKS[1], secret=secret)]
    for number in range(3, 3 + extra):
        child_id = f"44444444-4444-4444-8444-{number:012d}"
        task = f"Extra task {number}"
        parent["events"].insert(-1, card(number, child_id, task))
        children.append(child_dict(number, workspace, task, child_id=child_id, started=BASE + 1000 + number))
    parent["subagents"] = [index_entry(child, next(event["input"]["task"] for event in parent["events"]
                                                   if event.get("child_session_id") == child["id"])) for child in children]
    return parent, children


def golden(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


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
    yield app, manager, project, destination
    manager.store.db.close()


def save_family(manager, project, **options):
    parent, children = family_dicts(str(project), **options)
    manager.store.insert_sessions([parent, *children])
    return parent, children


async def client_for(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        token = (await client.get("/api/bootstrap")).json()["token"]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                             headers={"X-Local-Token": token})


def source_bytes(manager, ids):
    return {session_id: (manager.store.conversations / f"{session_id}.jsonl").read_bytes() for session_id in ids}


def cards(session):
    return [event for event in session["events"] if event.get("name") == "delegate_task"]


def children_of(manager, session_id):
    return sorted(manager.delegates.children(session_id), key=lambda child: child["created"])


class Pending:
    """A background worker that has not finished."""
    def done(self):
        return False


def check_links(manager, fork):
    """The fork's cards, notices and index name its own copies; each copy names the fork and its card."""
    copies = {child["id"]: manager.store.get(child["id"]) for child in children_of(manager, fork["id"])}
    named = [event["child_session_id"] for event in cards(fork) if "child_session_id" in event]
    assert sorted(named) == sorted(copies)
    assert not set(named) & {*CHILD_IDS, PARENT_ID}
    for event in cards(fork):
        if "child_session_id" in event:
            child = copies[event["child_session_id"]]
            assert child["parent_session_id"] == fork["id"] and child["is_subagent"] is True
            assert child["parent_event_id"] == event["id"]
            assert child["parent_call_id"] == event["call_id"]
            assert child["events"][0]["origin"]["parent_session_id"] == fork["id"]
            assert child["events"][0]["origin"]["parent_event_id"] == event["id"]
    assert sorted(item["id"] for item in fork["subagents"]) == sorted(copies)
    for event in fork["events"]:
        if event.get("subagent_id"):
            assert event["subagent_id"] in copies
    return copies


def headings(text):
    """(level, text) of each Markdown heading outside fenced code."""
    result, fenced = [], False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,6} ", line):
            result.append((len(line) - len(line.lstrip("#")), line))
    return result


def assert_valid_outline(text):
    levels = [level for level, _ in headings(text)]
    assert levels[0] == 1
    assert all(after <= before + 1 for before, after in zip(levels, levels[1:])), levels


# An old chat still works (SC-021) ----------------------------------------------------------------

async def test_a_chat_saved_before_the_upgrade_opens_lists_its_children_and_exports_with_both_by_default(runtime):
    app, manager, project, destination = runtime
    save_family(manager, project)
    async with await client_for(app) as client:
        assert (await client.get(f"/api/sessions/{PARENT_ID}")).status_code == 200
        listed = (await client.get(f"/api/sessions/{PARENT_ID}/children")).json()
        assert sorted(child["id"] for child in listed) == sorted(CHILD_IDS)
        response = await client.get(f"/api/sessions/{PARENT_ID}/export")
        assert response.status_code == 200
        bundle = response.json()
        assert sorted(item["id"] for item in bundle["sessions"]) == sorted([PARENT_ID, *CHILD_IDS])
        assert set(bundle) == {"format", "version", "root_session_id", "exported_at", "sessions"}
        alone = (await client.get(f"/api/sessions/{PARENT_ID}/export", params={"include_children": "false"})).json()
        assert [item["id"] for item in alone["sessions"]] == [PARENT_ID]
        imported = (await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)).json()
        assert imported["imported_count"] == 3
        check_links(manager, manager.store.get(imported["session"]["id"]))


async def test_a_bundle_exported_before_the_workflow_change_still_imports(runtime):
    app, manager, project, destination = runtime
    raw = (FIXTURES / "export_before_workflow.json").read_bytes()
    assert len(parse_bundle(raw)["sessions"]) == 3
    async with await client_for(app) as client:
        result = await client.post("/api/sessions/import", params={"workspace": str(destination)}, content=raw)
        assert result.status_code == 201, result.text
        assert result.json()["imported_count"] == 3
        check_links(manager, manager.store.get(result.json()["session"]["id"]))


async def test_a_bundle_is_not_given_new_fields_so_older_versions_can_still_read_it(runtime):
    app, manager, project, destination = runtime
    save_family(manager, project, extra=38)
    async with await client_for(app) as client:
        bundle = (await client.get(f"/api/sessions/{PARENT_ID}/export")).json()
    old = json.loads((FIXTURES / "export_before_workflow.json").read_text(encoding="utf-8"))
    assert set(bundle) == set(old)
    assert set(bundle["sessions"][0]) <= set(old["sessions"][0]) | {"tags"}
    assert not any("child_omitted" in event for item in bundle["sessions"] for event in item["events"])


# Fork copies the ended children --------------------------------------------------------------------

async def test_fork_copies_both_children_with_new_links_a_rebuilt_index_and_reset_permissions(runtime):
    app, manager, project, destination = runtime
    save_family(manager, project)
    before = source_bytes(manager, [PARENT_ID, *CHILD_IDS])
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{PARENT_ID}/fork")
    assert response.status_code == 201, response.text
    assert response.json()["children_copied"] == 2 and response.json()["children_omitted"] == []
    fork = manager.store.get(response.json()["id"])
    copies = check_links(manager, fork)
    assert len(copies) == 2 and len(manager.store.list()) == 6
    for session in (fork, *copies.values()):
        assert session["permission_mode"] == "manual"
        assert session["allowed_directories"] == [] and session["allow_rules"] == []
        assert session["workspace"] == str(project)
        assert session["provenance"]["kind"] == "fork"
    assert {copy["provenance"]["source_session_id"] for copy in copies.values()} == set(CHILD_IDS)
    assert all(copy["delegation"]["terminal_reason"] == "completed" for copy in copies.values())
    assert not any("child_omitted" in event for event in fork["events"])
    assert source_bytes(manager, [PARENT_ID, *CHILD_IDS]) == before


async def test_fork_from_here_copies_only_the_children_whose_cards_it_keeps(runtime):
    app, manager, project, destination = runtime
    save_family(manager, project)
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{PARENT_ID}/fork", json={"before_event_id": "u2"})
    assert response.status_code == 201, response.text
    fork = manager.store.get(response.json()["id"])
    copies = check_links(manager, fork)
    assert len(cards(fork)) == 1 and len(copies) == 1
    assert next(iter(copies.values()))["provenance"]["source_session_id"] == CHILD_IDS[0]
    assert response.json()["children_copied"] == 1 and response.json()["children_omitted"] == []
    assert len(manager.store.list()) == 5


async def test_fork_is_still_refused_while_a_background_subagent_runs(runtime):
    app, manager, project, destination = runtime
    save_family(manager, project)
    manager.delegates.background[PARENT_ID] = [{"worker": Pending()}]
    async with await client_for(app) as client:
        for body in (None, {"before_event_id": "u2"}):
            response = await client.post(f"/api/sessions/{PARENT_ID}/fork", **({"json": body} if body else {}))
            assert response.status_code == 409 and "background subagent" in response.text
    assert len(manager.store.list()) == 3


async def test_a_child_that_is_still_busy_is_never_copied(runtime):
    app, manager, project, destination = runtime
    save_family(manager, project)
    for status in ("running", "queued", "waiting"):
        manager.statuses[CHILD_IDS[1]] = status
        async with await client_for(app) as client:
            response = await client.post(f"/api/sessions/{PARENT_ID}/fork")
        assert response.status_code == 201, response.text
        fork = manager.store.get(response.json()["id"])
        copies = check_links(manager, fork)
        assert {copy["provenance"]["source_session_id"] for copy in copies.values()} == {CHILD_IDS[0]}
        assert response.json()["children_omitted"] == [{"id": CHILD_IDS[1], "title": "Subagent: Check the indexes",
                                                        "reason": "busy"}]
        second = cards(fork)[1]
        assert "child_session_id" not in second and second["child_omitted"] == "busy"


async def test_fork_with_forty_children_copies_the_newest_that_fit_and_marks_the_rest(runtime):
    app, manager, project, destination = runtime
    save_family(manager, project, extra=38)
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{PARENT_ID}/fork")
    assert response.status_code == 201, response.text
    result = response.json()
    fork = manager.store.get(result["id"])
    copies = check_links(manager, fork)
    assert len(copies) == result["children_copied"] == MAX_SESSIONS - 1
    assert len(result["children_omitted"]) == 9 and {item["reason"] for item in result["children_omitted"]} == {"limit"}
    # Newest first by start time: the two first children and the seven oldest extras are the ones left out.
    kept = {copy["provenance"]["source_session_id"] for copy in copies.values()}
    assert not kept & {*CHILD_IDS, *(f"44444444-4444-4444-8444-{number:012d}" for number in range(3, 10))}
    omitted = [event for event in cards(fork) if "child_session_id" not in event]
    assert len(omitted) == 9 and {event["child_omitted"] for event in omitted} == {"limit"}
    assert sorted(item["id"] for item in result["children_omitted"]) == sorted(
        {*CHILD_IDS, *(f"44444444-4444-4444-8444-{number:012d}" for number in range(3, 10))})
    assert len(fork["subagents"]) == MAX_SESSIONS - 1 and len(manager.store.list()) == 41 + 32


# Export includes the children by default -----------------------------------------------------------------

async def test_export_with_forty_children_keeps_the_newest_that_fit_and_reports_the_rest(runtime):
    app, manager, project, destination = runtime
    save_family(manager, project, extra=38)
    async with await client_for(app) as client:
        plain = await client.get(f"/api/sessions/{PARENT_ID}/export")
        assert plain.status_code == 200, plain.text
        assert len(plain.json()["sessions"]) == MAX_SESSIONS and plain.headers["X-Children-Omitted"] == "9"
        report = (await client.get(f"/api/sessions/{PARENT_ID}/export", params={"report": "true"})).json()
        assert report["bundle"] == plain.json() | {"exported_at": report["bundle"]["exported_at"]}
        assert report["children_omitted"]["count"] == 9
        assert {item["reason"] for item in report["children_omitted"]["children"]} == {"limit"}
        imported = await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=report["bundle"])
        assert imported.status_code == 201 and imported.json()["imported_count"] == MAX_SESSIONS
        none = await client.get(f"/api/sessions/{PARENT_ID}/export", params={"include_children": "false"})
        assert "X-Children-Omitted" not in none.headers and len(none.json()["sessions"]) == 1


async def test_export_skips_a_busy_child_and_says_so_instead_of_failing(runtime):
    app, manager, project, destination = runtime
    save_family(manager, project)
    manager.statuses[CHILD_IDS[0]] = "running"
    async with await client_for(app) as client:
        response = await client.get(f"/api/sessions/{PARENT_ID}/export", params={"report": "true"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["id"] for item in body["bundle"]["sessions"]] == [PARENT_ID, CHILD_IDS[1]]
        assert body["children_omitted"] == {"count": 1, "children": [{"id": CHILD_IDS[0], "title": "Subagent: Survey the tables",
                                                                      "reason": "busy"}]}
        # The chat itself being busy still refuses the export.
        manager.statuses[PARENT_ID] = "running"
        assert (await client.get(f"/api/sessions/{PARENT_ID}/export")).status_code == 409


async def test_export_over_the_size_limit_drops_the_oldest_children_that_do_not_fit(runtime, monkeypatch):
    app, manager, project, destination = runtime
    parent, children = save_family(manager, project)
    fits = export_bundle([parent, children[1]], PARENT_ID)
    monkeypatch.setattr("local_agent.portability.MAX_BUNDLE_BYTES", len(json.dumps(fits, ensure_ascii=False)) + 60)
    async with await client_for(app) as client:
        response = await client.get(f"/api/sessions/{PARENT_ID}/export", params={"report": "true"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["bundle"]["sessions"]] == [PARENT_ID, CHILD_IDS[1]]
    assert body["children_omitted"]["children"] == [{"id": CHILD_IDS[0], "title": "Subagent: Survey the tables",
                                                     "reason": "size"}]


# Delete --------------------------------------------------------------------------------------------------

def files_of(manager, session_id):
    return [path for path in manager.store.conversations.glob(f"{session_id}.*")]


async def test_delete_with_children_removes_the_parent_and_its_ended_children(runtime):
    app, manager, project, destination = runtime
    save_family(manager, project)
    async with await client_for(app) as client:
        response = await client.delete(f"/api/sessions/{PARENT_ID}", params={"children": "delete"})
    assert response.status_code == 200 and sorted(response.json()["deleted"]) == sorted([PARENT_ID, *CHILD_IDS])
    assert not any(files_of(manager, session_id) for session_id in (PARENT_ID, *CHILD_IDS))


async def test_delete_keeps_the_children_by_default_and_detaches_them(runtime):
    app, manager, project, destination = runtime
    save_family(manager, project)
    async with await client_for(app) as client:
        for request in ({}, {"params": {"children": "keep"}}):
            response = await client.delete(f"/api/sessions/{PARENT_ID}", **request)
            assert response.status_code == 200 and response.json()["deleted"] == [PARENT_ID]
            for child_id in CHILD_IDS:
                child = manager.store.get(child_id)
                assert child["parent_deleted"] is True
                assert not any(key in child for key in ("parent_session_id", "parent_event_id", "parent_call_id"))
            # Put the parent back for the second request.
            if not request:
                parent, _ = family_dicts(str(project))
                manager.store.insert_sessions([parent])
                for child_id in CHILD_IDS:
                    child = manager.store.get(child_id)
                    child.pop("parent_deleted")
                    child.update(parent_session_id=PARENT_ID, parent_event_id="card1", parent_call_id="call-1")
                    manager.store.save(child)
    assert not files_of(manager, PARENT_ID) and all(files_of(manager, child_id) for child_id in CHILD_IDS)


# One transcript for a parent with its children -------------------------------------------------------------

def test_the_transcript_without_the_option_is_byte_identical_to_the_one_before_the_change():
    parent, children = family_dicts()
    assert transcript(parent) == golden("transcript_parent.md")
    assert transcript(children[0]) == golden("transcript_child_1.md")
    assert transcript(parent, subagents=None) == golden("transcript_parent.md")


def test_the_transcript_with_subagents_nests_each_child_under_its_card_in_order():
    parent, children = family_dicts()
    text = transcript(parent, subagents={child["id"]: child for child in children})
    order = ["- Delegated: Survey the tables", "### Subagent: explore, Survey the tables", f"`{CHILD_IDS[0]}`",
             "Found 1 things.", "_End of subagent conversation " + CHILD_IDS[0][:8],
             "## Turn 2 ·", "- Delegated: Check the indexes", "### Subagent: explore, Check the indexes",
             "Found 2 things.", "_End of subagent conversation " + CHILD_IDS[1][:8], "**Assistant:**\n\nAll done."]
    positions = [text.index(item) for item in order]
    assert positions == sorted(positions) and len(set(positions)) == len(positions)
    assert_valid_outline(text)
    # The child's own headings sit below its section heading; the parent's text is untouched.
    assert "#### Turn 1" in text and "##### Findings" in text
    assert text.startswith("# Transcript: Plan the migration\n")
    assert text.count("Subagent conversation not found") == 0
    # What was before the option is still the parent's own part, line for line.
    assert golden("transcript_parent.md").splitlines()[0:5] == text.splitlines()[0:5]


def test_a_child_that_is_gone_is_noted_and_the_others_still_appear():
    parent, children = family_dicts()
    text = transcript(parent, subagents={children[0]["id"]: children[0]})
    assert f"Subagent conversation not found: `{CHILD_IDS[1]}`" in text
    assert text.count("### Subagent: ") == 1 and "Found 1 things." in text
    assert_valid_outline(text)
    assert "Subagent conversation not found" in transcript(parent, subagents={})


def test_secrets_in_a_child_are_redacted_like_the_parents():
    token = "ghp_" + "A" * 36
    parent, children = family_dicts(secret=f"token {token} and fake-passphrase-123")
    text = transcript(parent, subagents={child["id"]: child for child in children},
                      redact=lambda value: value.replace("fake-passphrase-123", "[REDACTED]"))
    assert "Found 2 things." in text and token not in text and "fake-passphrase-123" not in text
    assert text.count("[REDACTED]") >= 2


def test_a_document_over_the_size_limit_is_cut_with_a_note():
    parent, children = family_dicts()
    mapping = {child["id"]: child for child in children}
    full = transcript(parent, subagents=mapping)
    # Room for the text up to the second child's findings, but not for the rest and the note.
    limit = len(full[:full.index("Found 2 things.")].encode("utf-8")) + 20
    text = transcript(parent, subagents=mapping, max_bytes=limit)
    assert text.endswith("\n") and "_Transcript cut:" in text and "Found 2 things." not in text
    assert "Found 1 things." in text and len(text.encode("utf-8")) <= limit
    assert full.startswith(text.split("\n\n_Transcript cut:")[0])
    assert transcript(parent, subagents=mapping, max_bytes=10**9) == full
    # Without the option nothing is cut, however small the limit.
    assert transcript(parent, max_bytes=10) == golden("transcript_parent.md")


async def test_the_transcript_route_adds_the_children_only_when_asked(runtime):
    app, manager, project, destination = runtime
    token = "ghp_" + "B" * 36
    save_family(manager, project, secret=f"token {token}")
    async with await client_for(app) as client:
        plain = await client.get(f"/api/sessions/{PARENT_ID}/transcript.md")
        assert plain.status_code == 200
        assert plain.text == (await client.get(f"/api/sessions/{PARENT_ID}/transcript.md", params={"subagents": "0"})).text
        assert plain.text == transcript(manager.store.get(PARENT_ID), redact=app.state.manager.settings.redact)
        assert "### Subagent:" not in plain.text
        joined = await client.get(f"/api/sessions/{PARENT_ID}/transcript.md", params={"subagents": "1"})
    assert joined.status_code == 200 and joined.text.count("### Subagent: explore") == 2
    assert token not in joined.text and "Found 2 things." in joined.text
    assert_valid_outline(joined.text)
    assert joined.headers["content-disposition"] == plain.headers["content-disposition"]


async def test_the_transcript_route_notes_a_child_that_was_deleted(runtime):
    app, manager, project, destination = runtime
    save_family(manager, project)
    manager.store.delete(CHILD_IDS[1])
    async with await client_for(app) as client:
        joined = await client.get(f"/api/sessions/{PARENT_ID}/transcript.md", params={"subagents": "1"})
    assert f"Subagent conversation not found: `{CHILD_IDS[1]}`" in joined.text and "Found 1 things." in joined.text


async def test_the_script_writes_one_file_with_the_option_and_the_old_output_without_it(runtime, tmp_path):
    app, manager, project, destination = runtime
    save_family(manager, project)
    script = Path(__file__).resolve().parents[2] / "scripts" / "conversation_transcript.py"
    folder = manager.store.conversations
    parent_file = str(folder / f"{PARENT_ID}.jsonl")

    def run(*arguments):
        return subprocess.run([sys.executable, str(script), parent_file, *arguments], check=True, text=True,
                              capture_output=True, env={**os.environ, "TZ": "UTC"}).stdout

    default = run()
    assert default == transcript(session_from_jsonl(parent_file))
    assert "### Subagent:" not in default
    joined = run("--with-subagents")
    assert joined.count("### Subagent: explore") == 2 and joined.index("Survey the tables\n") < joined.index("Found 1 things.")
    assert joined.index("Found 1 things.") < joined.index("Found 2 things.")
    assert_valid_outline(joined)
    out = tmp_path / "all.md"
    run("--with-subagents", "--out", str(out))
    assert out.read_text(encoding="utf-8") == joined


# The chats the code before the upgrade saved (conftest pre_upgrade_store) -------------------------------------------

@pytest.fixture
def old_app(pre_upgrade_store, tmp_path, monkeypatch):
    """The app over the pre-upgrade state folder, and an empty folder to import into."""
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    settings = Settings(pre_upgrade_store.state)
    settings.values.update(workspace="/project", env_file="")
    app = create_app(settings)
    yield app, app.state.manager, pre_upgrade_store, destination
    app.state.manager.store.db.close()



async def test_the_pre_upgrade_parent_exports_with_both_children_and_imports_with_links_intact(old_app):
    app, manager, old, destination = old_app
    async with await client_for(app) as client:
        response = await client.get(f"/api/sessions/{old.parent}/export")
        assert response.status_code == 200, response.text
        bundle = response.json()
        assert sorted(item["id"] for item in bundle["sessions"]) == sorted([old.parent, old.child_a, old.child_b])
        alone = (await client.get(f"/api/sessions/{old.parent}/export", params={"include_children": "false"})).json()
        assert [item["id"] for item in alone["sessions"]] == [old.parent]
        imported = await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)
        assert imported.status_code == 201, imported.text
        assert imported.json()["imported_count"] == 3
    copy = manager.store.get(imported.json()["session"]["id"])
    named = [event["child_session_id"] for event in cards(copy) if "child_session_id" in event]
    assert len(named) == 2 and not set(named) & {old.parent, old.child_a, old.child_b}
    for child_id in named:
        child = manager.store.get(child_id)
        assert child["parent_session_id"] == copy["id"] and child["is_subagent"] is True
        assert child["parent_call_id"] in {event["call_id"] for event in cards(copy)}
    assert sorted(item["id"] for item in copy["subagents"]) == sorted(named)


async def test_a_fork_of_the_pre_upgrade_parent_makes_three_files_with_links_both_ways(old_app):
    app, manager, old, destination = old_app
    for session_id in (old.parent, old.child_a, old.child_b):  # The fixture's /project does not exist here.
        session = manager.store.get(session_id)
        session["workspace"] = str(destination)
        manager.store.save(session)
    before = {path.name for path in manager.store.conversations.glob("*.jsonl")}
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{old.parent}/fork")
    assert response.status_code == 201, response.text
    assert response.json()["children_copied"] == 2 and response.json()["children_omitted"] == []
    new = {path.name for path in manager.store.conversations.glob("*.jsonl")} - before
    assert len(new) == 3
    fork = manager.store.get(response.json()["id"])
    assert f"{fork['id']}.jsonl" in new
    named = [event["child_session_id"] for event in cards(fork) if "child_session_id" in event]
    assert len(named) == 2 and {f"{child_id}.jsonl" for child_id in named} | {f"{fork['id']}.jsonl"} == new
    for event in cards(fork):
        child = manager.store.get(event["child_session_id"])
        assert child["parent_session_id"] == fork["id"] and child["parent_event_id"] == event["id"]
        assert child["parent_call_id"] == event["call_id"]
        assert child["provenance"]["source_session_id"] in {old.child_a, old.child_b}
    assert sorted(item["id"] for item in fork["subagents"]) == sorted(named)
    # The originals are untouched.
    assert [event["child_session_id"] for event in cards(manager.store.get(old.parent))] == [old.child_a, old.child_b]


def test_the_pre_upgrade_parents_transcript_with_subagents_holds_both_childrens_sections(pre_upgrade_store):
    old = pre_upgrade_store
    parent = old.store.get(old.parent)
    children = {child_id: old.store.get(child_id) for child_id in (old.child_a, old.child_b)}
    plain = transcript(parent)
    joined = transcript(parent, subagents=children)
    assert "### Subagent:" not in plain and joined.count("### Subagent:") == 2
    for child_id in children:
        assert f"`{child_id}`" in joined and f"_End of subagent conversation {child_id[:8]}" in joined
    assert "Subagent conversation not found" not in joined
    assert_valid_outline(joined)
