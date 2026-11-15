"""A chat saved before the upgrade opens on the page as it did: the parent of two subagents from the pre-upgrade
fixtures (backend/tests/fixtures/pre_upgrade) shows its run, its two subagent rows and no batch approval, and no
label or error that only newer chats can have."""
import json
from pathlib import Path

from playwright.sync_api import expect

FIXTURES = Path(__file__).resolve().parents[2] / "backend" / "tests" / "fixtures" / "pre_upgrade"


def load(name):
    """The session a saved file holds, as the page receives it: the header, its fields and its events in order
    (Store reads the same records; the wire and the inference calls are not part of what the page shows)."""
    session, events = {}, []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["record"] == "session":
            session.update(record["data"])
        elif record["record"] == "field":
            session[record["name"]] = record["data"]
        elif record["record"] == "event":
            events.append(record["data"])
    return {**session, "events": events, "status": "idle"}


def test_a_chat_from_before_the_upgrade_shows_its_run_and_two_subagent_rows_without_batch_features(ui, page):
    parent = load("parent.jsonl")
    children = {child["id"]: child for child in (load("child_a.jsonl"), load("child_b.jsonl"))}
    assert [event.get("name") for event in parent["events"] if event["type"] == "tool"] == ["delegate_task"] * 2
    assert not any("origin" in (event.get("delegation_batch") or {}) or "batch_approval" in event for event in parent["events"])
    problems = []
    page.on("console", lambda message: problems.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: problems.append(str(error)))
    ui.mount("components/Conversation.js",
             "{ session: window.fixtureSessions.parent, onError: fake.spy('onError'), onSelectSession: fake.spy('select') }",
             setup=f"""window.fixtureSessions = {{ parent: {json.dumps(parent)} }}
               window.fixtureChildren = {json.dumps(children)}
               fake.handle = path => {{ const id = decodeURIComponent(path.split('/')[3] || '')
                 return /^\\/api\\/sessions\\/[^/]+$/.test(path) && fixtureChildren[id] ? fake.json(fixtureChildren[id]) : fake.json({{ ok: true }}) }}""")
    expect(page.locator(".run-count")).to_have_text("2 subagents")
    expect(page.locator(".run-aggregate")).to_have_text("2 completed")
    page.get_by_role("button", name="2 subagents").click()  # A finished run is folded until you open it.
    expect(page.get_by_role("button", name="Open subagent")).to_have_count(2)
    # Each row names its agent type (the old cards have only the fields they had then).
    tools = page.locator(".tool-card[data-event-id]")
    expect(tools).to_have_count(2)
    for row, task in zip((tools.nth(0), tools.nth(1)), ("Survey the docs folder", "Survey the tests folder")):
        expect(row).to_be_visible()
        expect(row).to_contain_text(task)
        expect(row).to_contain_text("explore", ignore_case=True)
    # Nothing newer chats have: no batch approval block, no waiting label, no "Started by" label.
    expect(page.locator(".run-batch-approval")).to_have_count(0)
    expect(page.locator(".batch-waiting")).to_have_count(0)
    expect(page.get_by_text("Started by", exact=False)).to_have_count(0)
    expect(page.get_by_role("group", name="Approval for 2 subagents")).to_have_count(0)
    page.get_by_role("button", name="Open subagent").first.click()
    assert ui.spy("select") == [["pre-upgrade-child-a"]]
    assert ui.spy("onError") == []
    assert problems == []
