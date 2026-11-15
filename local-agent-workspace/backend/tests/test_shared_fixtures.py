"""Chats saved by the code before the upgrade (commit e59f328, see fixtures/pre_upgrade/README.md) still open
with the current Store, and saving one again loses nothing it had."""
import json

import pytest

from conftest import PRE_UPGRADE_FILES


def key_paths(value, prefix=""):
    """Every dict key path inside value (list items share one path), for comparing what a chat holds."""
    paths = set()
    if isinstance(value, dict):
        for key, item in value.items():
            paths.add(f"{prefix}/{key}")
            paths |= key_paths(item, f"{prefix}/{key}")
    elif isinstance(value, list):
        for item in value:
            paths |= key_paths(item, f"{prefix}[]")
    return paths


def delegation_cards(session):
    return [event for event in session["events"] if event.get("type") == "tool" and event.get("name") == "delegate_task"]


def test_the_current_store_lists_and_opens_all_four_chats(pre_upgrade_store):
    fixture = pre_upgrade_store
    store = fixture.store
    listed = {item["id"] for item in store.list()}
    assert {fixture.parent, fixture.compacted} <= listed
    assert listed <= set(fixture.ids)
    assert store.problems() == []
    for session_id in fixture.ids:
        session = store.get(session_id)
        assert session["id"] == session_id and session["workspace"] == "/project"
        assert session["events"] and session["wire"]
        assert store.header(session_id)["id"] == session_id
        assert store.get(session_id) == session  # Reading twice gives the same chat.
    assert len(set(fixture.ids)) == 4 and set(PRE_UPGRADE_FILES) == {"parent", "child_a", "child_b", "compacted"}


def test_the_parents_cards_still_name_the_two_children(pre_upgrade_store):
    fixture = pre_upgrade_store
    parent = fixture.store.get(fixture.parent)
    cards = delegation_cards(parent)
    assert [card["child_session_id"] for card in cards] == [fixture.child_a, fixture.child_b]
    assert [card["call_id"] for card in cards] == ["call-a", "call-b"]
    # One parallel batch of two, each card finished with its delegation record.
    batches = [card["delegation_batch"] for card in cards]
    assert [(batch["index"], batch["size"]) for batch in batches] == [(0, 2), (1, 2)]
    assert batches[0]["id"] == batches[1]["id"]
    assert all(card["state"] == "completed" and card["delegation"]["status"] == "completed" for card in cards)
    # The index in the header lists both, as a header-only read (the sidebar) shows it.
    for index in (parent["subagents"], fixture.store.header(fixture.parent)["subagents"]):
        assert [entry["id"] for entry in index] == [fixture.child_a, fixture.child_b]
        assert all(entry["status"] == "completed" and entry["created"] < entry["ended"] for entry in index)
        assert all(entry["transcript"] == f"conversations/{entry['id']}.jsonl" for entry in index)
        assert all(entry["usage"]["calls"] == 4 and entry["turn_spend"]["tokens"] for entry in index)
    assert [event["type"] for event in parent["events"]][:1] == ["user"]
    assert parent["events"][-1]["type"] == "assistant" and parent["events"][-1]["text"]
    for card in cards:  # Each child's transcript file exists under the name the card gives.
        assert fixture.file(card["child_session_id"]).is_file()


def test_the_childrens_headers_name_the_parent(pre_upgrade_store):
    fixture = pre_upgrade_store
    parent = fixture.store.get(fixture.parent)
    cards = {card["child_session_id"]: card for card in delegation_cards(parent)}
    for child_id in (fixture.child_a, fixture.child_b):
        header = fixture.store.header(child_id)
        assert header["is_subagent"] is True and header["parent_session_id"] == fixture.parent
        assert header["parent_event_id"] == cards[child_id]["id"]
        assert header["parent_call_id"] == cards[child_id]["call_id"]
        assert header["agent_type"] == "explore" and header["delegation"]["status"] == "completed"
        child = fixture.store.get(child_id)
        tools = [event["name"] for event in child["events"] if event["type"] == "tool"]
        assert tools == ["list_files", "read_file", "submit_report"]
    assert fixture.store.header(fixture.compacted).get("parent_session_id") is None


def test_the_compacted_chats_summary_and_count_are_intact(pre_upgrade_store):
    fixture = pre_upgrade_store
    session = fixture.store.get(fixture.compacted)
    state = session["context_state"]
    assert state["compactions"] == 1
    assert state["summary"].startswith("## Goal and current status\nRead notes.txt in /project")
    assert "- Write the parser for notes.txt." in state["summary"]
    assert len(state["handoff_files"]) == 1 and state["handoff_files"][0]["compaction"] == 1
    assert 0 < state["through"] < len(session["wire"])  # The wire after the cut is still there.
    assert session["wire"][state["through"]]["role"] == "user"
    assert any(event["type"] == "notice" and "Context compacted (compaction 1)" in event["text"]
               for event in session["events"])
    assert [event["type"] for event in session["events"]][0] == "user"
    assert session["events"][-1]["type"] == "assistant"


@pytest.mark.parametrize("name", list(PRE_UPGRADE_FILES))
def test_saving_a_chat_again_loses_no_event_and_no_field(pre_upgrade_store, name):
    fixture = pre_upgrade_store
    session_id = getattr(fixture, name)
    before = json.loads(json.dumps(fixture.store.get(session_id)))
    stored_before = [json.loads(line) for line in fixture.file(session_id).read_text(encoding="utf-8").splitlines()]
    fixture.store.save(fixture.store.get(session_id))
    after = fixture.store.get(session_id)
    assert len(after["events"]) == len(before["events"]) and len(after["wire"]) == len(before["wire"])
    assert [event["id"] for event in after["events"]] == [event["id"] for event in before["events"]]
    assert after["wire"] == before["wire"]
    # No field is lost anywhere (the current code may add some): top level, events, wire, state and ledger.
    assert key_paths(before) <= key_paths(after)
    for old, new in zip(before["events"], after["events"]):
        assert key_paths(old) <= key_paths(new) and {k: new[k] for k in old} == old
    assert set(before) <= set(after)
    for key in set(before) - {"updated", "context_info"}:
        assert after[key] == before[key], key
    # The file holds the same kinds of records as before, with at least as many of each.
    stored_after = [json.loads(line) for line in fixture.file(session_id).read_text(encoding="utf-8").splitlines()]
    for record in {item["record"] for item in stored_before}:
        assert (sum(item["record"] == record for item in stored_after)
                >= sum(item["record"] == record for item in stored_before))
    # And the chat still opens as a header-only read, and the relations between the chats hold.
    assert fixture.store.header(session_id)["id"] == session_id
    if name == "parent":
        assert [card["child_session_id"] for card in delegation_cards(after)] == [fixture.child_a, fixture.child_b]
        assert [entry["id"] for entry in after["subagents"]] == [fixture.child_a, fixture.child_b]
