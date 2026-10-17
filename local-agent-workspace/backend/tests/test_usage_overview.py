"""Usage across chats (M8d3): the usage summary each conversation keeps in its header, the overview by day,
endpoint, purpose and project (usage_overview.py), GET /api/usage, scripts/usage_report.py --all, and the list
prices of more pay-per-token endpoints."""
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from local_agent import spending, telemetry
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.store import Store
from local_agent.telemetry import (
    MODEL_DBU_PER_MILLION, OPEN_PRICING_SOURCE, PRICING_SOURCE, SUMMARY_DAYS, SUMMARY_ROWS, estimated_dbu,
    pricing_source, summary_rows, usage_summary,
)
from local_agent.usage_overview import MAX_PROJECTS, overview

REPORT = Path(__file__).resolve().parents[2] / "scripts" / "usage_report.py"
OPUS, GPT_OSS = "databricks-claude-opus-5-5", "databricks-gpt-oss-120b"
REAL_CLIENT = httpx.AsyncClient


def at(day, hour=12):
    """A ledger time (ISO, UTC) at local noon of a local date."""
    return datetime.combine(day, datetime.min.time()).replace(hour=hour).astimezone().isoformat()


def call(model=OPUS, purpose="agent", created=None, **usage):
    record = {"id": f"c-{model}-{purpose}-{created}-{sorted(usage.items())}", "purpose": purpose, "model": model,
              "created": created or at(date.today()), "status": "completed"}
    if usage:
        record["usage"] = usage
    return record


# The summary in the header

def test_the_summary_groups_calls_by_local_date_model_and_purpose():
    today, yesterday = date.today(), date.today() - timedelta(days=1)
    session = {"inference_calls": [
        call(created=at(today), input_tokens=1000, output_tokens=100, cache_read_input_tokens=5000),
        call(created=at(today), input_tokens=2000, output_tokens=200),
        call(purpose="title", created=at(today), input_tokens=50, output_tokens=5),
        call(model="team-endpoint", created=at(yesterday), input_tokens=10, output_tokens=1),
        call(created=at(yesterday)),  # No usage reported: counted, no tokens, not "unpriced".
        "not a record",
    ]}
    summary = usage_summary(session)
    assert summary["version"] == 1 and summary["calls"] == 6
    rows = {(row["date"], row["model"], row["purpose"]): row for row in summary_rows(summary)}
    agent = rows[(today.isoformat(), OPUS, "agent")]
    assert (agent["calls"], agent["input_tokens"], agent["cache_read_input_tokens"], agent["output_tokens"]) == (2, 3000, 5000, 300)
    assert agent["estimated_dbu"] == pytest.approx(estimated_dbu(OPUS, {"input_tokens": 3000, "output_tokens": 300,
                                                                       "cache_read_input_tokens": 5000}))
    assert rows[(today.isoformat(), OPUS, "title")]["calls"] == 1
    unpriced = rows[(yesterday.isoformat(), "team-endpoint", "agent")]
    assert unpriced["unpriced_calls"] == 1 and unpriced["estimated_dbu"] == 0
    assert rows[(yesterday.isoformat(), OPUS, "agent")] == {
        "date": yesterday.isoformat(), "model": OPUS, "purpose": "agent", "calls": 1, "input_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0, "estimated_dbu": 0, "unpriced_calls": 0}
    assert json.loads(json.dumps(summary)) == summary  # It lives in the header line.


def test_the_summary_is_bounded_and_damaged_rows_are_skipped():
    start = date.today() - timedelta(days=SUMMARY_DAYS + 30)
    calls = [call(model=f"endpoint-{index % 3}", created=at(start + timedelta(days=index)), input_tokens=1, output_tokens=1)
             for index in range(SUMMARY_DAYS + 31)]
    summary = usage_summary({"inference_calls": calls})
    rows = summary_rows(summary)
    assert len(rows) <= SUMMARY_ROWS and len(summary["rows"]) <= SUMMARY_ROWS
    assert rows[-1]["date"] == date.today().isoformat()  # The newest are kept.
    assert len({row["date"] for row in rows}) <= SUMMARY_DAYS
    damaged = {**summary, "rows": [["2026-01-01", "m", "agent", -1, 0, 0, 0, 0, 0.0, 0], ["x"], "row",
                                   ["2026-01-01", "m", "agent", 1, 0, 0, 0, 0, float("nan"), 0],
                                   ["2026-01-01", "m", "agent", 1, 0, 0, 0, 0, True, 0], rows[0] and list(rows[0].values())]}
    assert [row["date"] for row in summary_rows(damaged)] == [rows[0]["date"]]
    assert summary_rows({**summary, "version": 2}) == [] and summary_rows(None) == [] and summary_rows({"rows": "x"}) == []


# The overview

def chat(workspace, *calls, worktree=None):
    header = {"id": workspace, "workspace": workspace, **({"worktree": worktree} if worktree else {})}
    return header, usage_summary({"inference_calls": list(calls)})


def test_the_overview_sums_by_day_model_purpose_and_project():
    today = date(2026, 10, 16)
    chats = [
        chat("/project/a", call(created=at(today), input_tokens=1000, output_tokens=100),
             call(purpose="compaction", created=at(today - timedelta(days=1)), input_tokens=4000, output_tokens=400)),
        chat("/project/b", call(model=GPT_OSS, created=at(today), input_tokens=500, output_tokens=50),
             call(model="team-endpoint", created=at(today), input_tokens=10, output_tokens=1)),
        # A worktree chat counts for its source project.
        chat("/srv/other/worktrees/x", call(created=at(today), input_tokens=1, output_tokens=1),
             worktree={"source_workspace": "/project/a"}),
        chat("/project/old", call(created=at(today - timedelta(days=40)), input_tokens=9, output_tokens=9)),
    ]
    value = overview(chats, 7, today=today)
    assert (value["days"], value["since"], value["until"]) == (7, "2026-10-10", "2026-10-16")
    assert [row["date"] for row in value["by_day"]] == [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    assert value["by_day"][-1]["calls"] == 4 and value["by_day"][-2]["input_tokens"] == 4000
    totals = value["totals"]
    assert totals["chats"] == 3 and totals["calls"] == 5 and totals["unpriced_calls"] == 1  # The 40-day-old chat is outside.
    assert totals["input_tokens"] == 5511 and totals["output_tokens"] == 552
    models = {row["key"]: row for row in value["by_model"]}
    assert models[OPUS]["calls"] == 3 and models[GPT_OSS]["priced"] is True and models["team-endpoint"]["priced"] is False
    assert models[GPT_OSS]["estimated_dbu"] == pytest.approx(estimated_dbu(GPT_OSS, {"input_tokens": 500, "output_tokens": 50}), abs=1e-6)
    assert value["by_model"][0]["key"] == OPUS  # Most DBU first.
    purposes = {row["key"]: row for row in value["by_purpose"]}
    assert purposes["compaction"]["label"] == "Compaction" and purposes["agent"]["calls"] == 4
    projects = {row["key"]: row for row in value["by_project"]}
    assert set(projects) == {"/project/a", "/project/b"} and projects["/project/a"]["calls"] == 3
    assert projects["/project/a"]["name"] == "a"
    assert value["pricing"]["models"] is MODEL_DBU_PER_MILLION
    assert overview(chats, 90, today=today)["totals"]["chats"] == 4
    assert overview(chats, 0, today=today)["days"] == 1 and overview(chats, 1000, today=today)["days"] == 90


def test_projects_past_the_cap_are_summed_into_one_row():
    today = date(2026, 10, 16)
    chats = [chat(f"/project/p{index:02d}", call(created=at(today), input_tokens=index + 1, output_tokens=1))
             for index in range(MAX_PROJECTS + 3)]
    rows = overview(chats, 1, today=today)["by_project"]
    assert len(rows) == MAX_PROJECTS + 1 and rows[-1]["key"] == "" and rows[-1]["name"] == "3 other projects"
    assert rows[-1]["calls"] == 3 and rows[0]["key"] == f"/project/p{MAX_PROJECTS + 2:02d}"


# The ledger keeps it current, and GET /api/usage

def sse(text, usage):
    chunks = [{"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}, {"choices": [], "usage": usage}]
    return "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 40, "completion_tokens": 4}})
        return httpx.Response(200, text=sse("Hi.", {"prompt_tokens": 10_000, "completion_tokens": 1_000}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    return settings, tmp_path


async def test_each_turn_saves_the_summary_in_the_header_and_a_restart_reads_it(runtime):
    settings, tmp_path = runtime
    store = Store(tmp_path / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(model=OPUS)
    store.save(session)
    manager = AgentManager(store, settings)
    await manager.run(session, "Hello")
    rows = summary_rows(store.header(session["id"])["usage_summary"])
    assert [(row["purpose"], row["calls"], row["input_tokens"]) for row in rows] == [("agent", 1, 10_000), ("title", 1, 40)]
    await manager.run(session, "Again")
    store.db.close()
    restarted = Store(tmp_path / "tests.sqlite3")  # Headers only: the ledger is not loaded for the summary.
    header = next(item for item in restarted.list() if item["id"] == session["id"])
    assert summary_rows(header["usage_summary"])[0]["calls"] == 2 and header["usage_summary"]["calls"] == 3
    restarted.db.close()


@pytest.fixture
async def api(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.env = {}
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            yield client, app.state.manager


def saved_chat(manager, workspace, calls, summary=True):
    session = manager.store.create({**manager.settings.values, "workspace": workspace})
    session["inference_calls"] = calls
    if summary:
        session["usage_summary"] = usage_summary(session)
    manager.store.save(session)
    return session


async def test_get_usage_reads_headers_and_summarizes_older_chats_once(api, monkeypatch):
    client, manager = api
    today = date.today()
    saved_chat(manager, "/project/a", [call(created=at(today), input_tokens=1000, output_tokens=100)])
    older = saved_chat(manager, "/project/b", [call(model=GPT_OSS, created=at(today), input_tokens=500, output_tokens=50)],
                       summary=False)
    saved_chat(manager, "/project/c", [call(created=at(today), input_tokens=7, output_tokens=7)], summary=False)
    monkeypatch.setattr("local_agent.api.USAGE_SCAN", 1)
    first = (await client.get("/api/usage", params={"days": 7})).json()
    assert first["days"] == 7 and first["pending_chats"] == 1 and first["totals"]["chats"] == 2
    assert {row["key"] for row in first["by_project"]} <= {"/project/a", "/project/b", "/project/c"}
    second = (await client.get("/api/usage")).json()
    assert second["pending_chats"] == 0 and second["totals"]["chats"] == 3 and second["days"] == 30
    assert manager.store.header(older["id"])["usage_summary"]["version"] == 1  # Saved: not loaded again.
    by_model = {row["key"]: row for row in second["by_model"]}
    assert by_model[GPT_OSS]["estimated_dbu"] > 0 and second["totals"]["unpriced_calls"] == 0
    # The sessions list leaves summaries and result records out.
    assert all("usage_summary" not in item for item in (await client.get("/api/sessions")).json())
    for days in (0, 91, "x"):
        assert (await client.get("/api/usage", params={"days": days})).status_code == 422


async def test_a_running_chat_counts_its_live_ledger(api):
    client, manager = api
    session = saved_chat(manager, "/project/a", [])
    live = manager.store.get(session["id"])
    live["inference_calls"] = [call(created=at(date.today()), input_tokens=3, output_tokens=3)]
    manager.live[session["id"]] = live  # A turn in progress: its header is older than its ledger.
    try:
        assert (await client.get("/api/usage")).json()["totals"]["calls"] == 1
    finally:
        manager.live.pop(session["id"])


# List prices

def test_more_pay_per_token_endpoints_have_cited_list_prices():
    for model in ("databricks-claude-sonnet-5", "databricks-claude-haiku-4-5", "databricks-gpt-5", GPT_OSS,
                  "databricks-meta-llama-3-3-70b-instruct"):
        assert model in MODEL_DBU_PER_MILLION
    assert pricing_source(OPUS) == PRICING_SOURCE and pricing_source(GPT_OSS) == OPEN_PRICING_SOURCE
    assert pricing_source("team-endpoint") is None and pricing_source(None) is None
    # GPT OSS lists input and output only: a reported cache read is priced at its input rate.
    rates = MODEL_DBU_PER_MILLION[GPT_OSS]
    assert estimated_dbu(GPT_OSS, {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}) \
        == pytest.approx(2 * rates["input_tokens"] + rates["output_tokens"])
    for rates in MODEL_DBU_PER_MILLION.values():
        assert set(rates) <= set(telemetry.PRICED_FIELDS) and {"input_tokens", "output_tokens"} <= set(rates)
        assert all(isinstance(value, float) and value > 0 for value in rates.values())


def test_the_spending_limit_now_counts_gpt_oss_calls(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    store = Store(tmp_path / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(model=GPT_OSS, spending_limit_dbu=0.01,
                   inference_calls=[call(model=GPT_OSS, input_tokens=4_000_000, output_tokens=200_000)])
    verdict, text = spending.check(manager, session)
    assert verdict == "stop" and "spending limit" in text
    store.db.close()


# scripts/usage_report.py --all

def test_usage_report_all_sums_a_state_folder(tmp_path):
    state = tmp_path / "state"
    store = Store(state / "conversations.sqlite3")
    for workspace, calls in (("/project/a", [call(input_tokens=1000, output_tokens=100)]),
                             ("/project/b", [call(model="team-endpoint", input_tokens=5, output_tokens=5)])):
        session = store.create({"workspace": workspace, "model": OPUS, "permission_mode": "manual"})
        session["inference_calls"] = calls
        store.save(session)
    store.db.close()
    (state / "conversations" / "broken.jsonl").write_text("{not json\n")
    done = subprocess.run([sys.executable, str(REPORT), "--all", str(state), "--days", "7"], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    assert "2 chats, 2 calls" in done.stdout and "1 unreadable files skipped" in done.stdout
    assert "By project:" in done.stdout and "(+1 unpriced calls)" in done.stdout
    as_json = json.loads(subprocess.run([sys.executable, str(REPORT), "--all", str(state), "--json"], capture_output=True,
                                        text=True, timeout=60).stdout)
    assert as_json["totals"]["calls"] == 2 and as_json["days"] == 30
    refused = subprocess.run([sys.executable, str(REPORT), "--all", str(state), "--days", "91"], capture_output=True, text=True,
                             timeout=60)
    assert refused.returncode == 2 and "--days" in refused.stderr
    missing = subprocess.run([sys.executable, str(REPORT), "--all", str(tmp_path / "none")], capture_output=True, text=True,
                             timeout=60)
    assert missing.returncode == 2
