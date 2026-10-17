"""A chat's usage with its subagents: each subagent's token totals in its parent's index (also once it was
deleted), the metrics route's subagents section and the usage CSV with the subagents' rows."""
import asyncio
import csv
import io
import json

import httpx
import pytest

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.telemetry import sum_usage, usage_totals

from test_spending_paths import PER_CALL, REAL_CLIENT, delegate_reply, is_child, sse

OPUS = "databricks-claude-opus-5-5"
CHILD_USAGE = {"prompt_tokens": 4_000, "completion_tokens": 500, "prompt_tokens_details": {"cached_tokens": 1_000}}


def gateway(monkeypatch, child_steps=1):
    """A parent that delegates once, then finishes; a child that answers after child_steps - 1 list_tasks calls."""
    counts = {"child": 0}

    async def handler(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            # A title (the child's): its usage counts too.
            return httpx.Response(200, json={"choices": [{"message": {"content": "T"}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 100, "completion_tokens": 10}})
        if is_child(body):
            counts["child"] += 1
            if counts["child"] < child_steps:
                return httpx.Response(200, text=sse({"tool_calls": [{"index": 0, "id": f"t{counts['child']}", "type": "function",
                                                                     "function": {"name": "list_tasks", "arguments": "{}"}}]},
                                                    "tool_calls", CHILD_USAGE))
            return httpx.Response(200, text=sse({"content": "Mapped."}, "stop", CHILD_USAGE))
        if any(message["role"] == "tool" for message in body["messages"]):
            return httpx.Response(200, text=sse({"content": "Done."}, "stop"))
        return httpx.Response(200, text=delegate_reply())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(handler)))


@pytest.fixture
async def api(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="", model=OPUS)
    settings.env = {}
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            manager = app.state.manager
            parent = manager.store.create(settings.values)
            parent.update(title="Spend", title_generated=True, permission_mode="bypassPermissions")
            manager.store.save(parent)
            yield client, manager, parent


async def run(manager, parent):
    manager.start(parent["id"], "Delegate")
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    return manager.store.get(parent["id"])


def test_usage_totals_and_their_sums_keep_unknown_fields_unknown():
    calls = [{"id": "a", "purpose": "agent", "model": OPUS, "status": "completed",
              "usage": {"input_tokens": 10, "output_tokens": 2}}]
    totals = usage_totals(calls)
    assert totals == {"calls": 1, "input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": None,
                      "cache_creation_input_tokens": None}
    both = sum_usage([totals, {**totals, "cache_read_input_tokens": 5}])
    assert (both["calls"], both["input_tokens"], both["cache_read_input_tokens"]) == (2, 20, None)
    assert sum_usage([]) == {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0}


async def test_the_metrics_route_rolls_up_the_subagents_usage(api, monkeypatch):
    client, manager, parent = api
    gateway(monkeypatch, child_steps=2)
    saved = await run(manager, parent)
    entry, = saved["subagents"]
    # Two agent requests and its title.
    assert entry["usage"] == {"calls": 3, "input_tokens": 2 * 3_000 + 100, "output_tokens": 2 * 500 + 10,
                              "cache_read_input_tokens": 2 * 1_000, "cache_creation_input_tokens": None}
    metrics = (await client.get(f"/api/sessions/{parent['id']}/metrics")).json()
    rollup = metrics["subagents"]
    child, = rollup["children"]
    assert {key: child[key] for key in ("id", "agent_type", "task", "status")} == {
        "id": entry["id"], "agent_type": "explore", "task": "Map it", "status": "completed"}
    assert child["usage"] == entry["usage"] and rollup["totals"] == entry["usage"] and rollup["without_usage"] == 0
    assert child["estimated_dbu"] == pytest.approx(entry["estimated_dbu"])
    # The chat's own totals stay its own; the spend counts both.
    assert metrics["totals"]["calls"] == 2 and metrics["spending"]["subagents_dbu"] == pytest.approx(entry["estimated_dbu"])
    # A subagent chat and a chat without subagents have no subagents section.
    assert "subagents" not in (await client.get(f"/api/sessions/{entry['id']}/metrics")).json()
    other = manager.store.create(manager.settings.values)
    manager.store.save(other)
    assert "subagents" not in (await client.get(f"/api/sessions/{other['id']}/metrics")).json()


async def test_a_deleted_subagent_keeps_its_usage_and_older_entries_are_counted_apart(api, monkeypatch):
    client, manager, parent = api
    gateway(monkeypatch)
    saved = await run(manager, parent)
    entry, = saved["subagents"]
    response = await client.delete(f"/api/sessions/{entry['id']}")
    assert response.status_code == 200
    saved = manager.store.get(parent["id"])
    kept, = saved["subagents"]
    assert kept["deleted"] is True and kept["usage"] == entry["usage"]
    saved["subagents"].append({"id": "older", "task": "Before usage", "agent_type": "general", "status": "completed"})
    manager.store.save(saved)
    rollup = (await client.get(f"/api/sessions/{parent['id']}/metrics")).json()["subagents"]
    assert [child.get("deleted", False) for child in rollup["children"]] == [True, False]
    assert rollup["without_usage"] == 1 and rollup["totals"] == entry["usage"] and rollup["children"][1]["usage"] is None


async def test_the_usage_csv_can_add_the_subagents_rows(api, monkeypatch):
    client, manager, parent = api
    gateway(monkeypatch)
    saved = await run(manager, parent)
    child_id = saved["subagents"][0]["id"]
    alone = list(csv.DictReader(io.StringIO((await client.get(f"/api/sessions/{parent['id']}/usage.csv")).text)))
    assert {row["conversation_id"] for row in alone} == {parent["id"]} and len(alone) == 2
    text = (await client.get(f"/api/sessions/{parent['id']}/usage.csv?include_subagents=true")).text
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [row["conversation_id"] for row in rows] == [parent["id"], parent["id"], child_id, child_id]
    assert [row["n"] for row in rows] == ["1", "2", "3", "4"]
    assert [(row["purpose"], row["input_tokens"]) for row in rows[2:]] == [("agent", "3000"), ("title", "100")]
    assert float(rows[0]["estimated_dbu"]) == pytest.approx(PER_CALL)
