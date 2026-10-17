"""Scheduled runs (schedules.py, schedules_api.py): cron lines and intervals with clock changes, the checks of a
schedule (caps required, never Bypass), the scheduler with a fake clock (catch-up once, ask and auto runs, a rejected
token, one run per schedule), the per-run approval limit that ends a run instead of waiting, the routes, the lock
file, the token expiry warning, and a run against the offline simulator. No network beyond 127.0.0.1 and no paid
calls (httpx MockTransport or the simulator)."""
import asyncio
import importlib.util
import json
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent import interactions, planning, schedules, spending, turns
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.schedules import Scheduler, check_schedule, next_fire, parse_spec, token_warning
from local_agent.store import Store

REAL_CLIENT = httpx.AsyncClient
APP = Path(__file__).resolve().parents[2]
SIMULATOR = APP / "scripts" / "simulate_databricks.py"
T0 = 1_800_000_000.0  # 2027-01-15 08:00 UTC


def zone(name):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        pytest.skip(f"No time zone data for {name} on this system.")


def local(zone_info, *parts):
    return datetime(*parts, tzinfo=zone_info).timestamp()


def shown(stamp, zone_info):
    return datetime.fromtimestamp(stamp, zone_info).strftime("%Y-%m-%d %H:%M %Z")


def upcoming(spec, zone_name, start, count):
    zone_info = zone(zone_name)
    parsed, after, found = parse_spec(spec), local(zone_info, *start), []
    for _ in range(count):
        after = parsed.next_after(after, after, zone_info)
        found.append(shown(after, zone_info))
    return found


# Times

@pytest.mark.parametrize("spec, zone_name, start, expected", [
    # Spring forward in New York (2026-03-08 02:00 -> 03:00): the skipped 02:30 runs an hour later that day.
    ("30 2 * * *", "America/New_York", (2026, 3, 7, 12, 0),
     ["2026-03-08 03:30 EDT", "2026-03-09 02:30 EDT"]),
    # Fall back (2026-11-01 02:00 -> 01:00): the repeated 01:30 runs once.
    ("30 1 * * *", "America/New_York", (2026, 10, 31, 12, 0),
     ["2026-11-01 01:30 EDT", "2026-11-02 01:30 EST"]),
    ("*/15 * * * *", "America/New_York", (2026, 3, 8, 1, 40),
     ["2026-03-08 01:45 EST", "2026-03-08 03:00 EDT", "2026-03-08 03:15 EDT"]),
    ("0 9 * * *", "Europe/Berlin", (2026, 10, 24, 10, 0),  # Berlin falls back on 2026-10-25.
     ["2026-10-25 09:00 CET", "2026-10-26 09:00 CET"]),
    # After the last day of a month: the next month with a 31st, and the first of the next month.
    ("0 9 31 * *", "UTC", (2026, 1, 31, 10, 0), ["2026-03-31 09:00 UTC", "2026-05-31 09:00 UTC"]),
    ("0 0 1 * *", "UTC", (2026, 1, 31, 23, 59), ["2026-02-01 00:00 UTC", "2026-03-01 00:00 UTC"]),
    ("0 0 29 2 *", "UTC", (2026, 1, 1, 0, 0), ["2028-02-29 00:00 UTC", "2032-02-29 00:00 UTC"]),
    # Weekdays, a list and a range with a step; Sunday as 7; both day fields restricted means either.
    ("0 9 * * 1-5", "UTC", (2026, 10, 16, 9, 0), ["2026-10-19 09:00 UTC", "2026-10-20 09:00 UTC"]),
    ("5,35 8-12/2 * * *", "UTC", (2026, 1, 1, 9, 0), ["2026-01-01 10:05 UTC", "2026-01-01 10:35 UTC", "2026-01-01 12:05 UTC"]),
    ("0 12 * * 7", "UTC", (2026, 10, 12, 0, 0), ["2026-10-18 12:00 UTC", "2026-10-25 12:00 UTC"]),
    ("0 6 13 * 5", "UTC", (2026, 11, 10, 0, 0), ["2026-11-13 06:00 UTC", "2026-11-20 06:00 UTC", "2026-11-27 06:00 UTC"]),
])
def test_cron_lines_find_their_next_times_across_clock_changes(spec, zone_name, start, expected):
    assert upcoming(spec, zone_name, start, len(expected)) == expected


def test_intervals_count_from_when_the_schedule_was_saved():
    every = parse_spec("every 30 minutes")
    assert every.text == "every 30 minutes" and parse_spec("Every  2 hours").text == "every 2 hours"
    assert parse_spec("every day").text == "every day"
    assert every.next_after(T0, T0) == T0 + 1800 and every.next_after(T0 + 1800, T0) == T0 + 3600
    assert every.next_after(T0 + 1801, T0) == T0 + 3600
    assert every.due_times(T0 + 1800, T0 + 1800 * 4 + 5, T0) == (4, T0 + 1800 * 4)


@pytest.mark.parametrize("spec, message", [
    ("", "Enter when it runs"), ("* * * * *", "at most every 5 minutes"), ("0,3 * * * *", "at most every 5 minutes"),
    ("every 2 minutes", "at most every 5 minutes"), ("every 40 days", "at least every 31 days"),
    ("60 * * * *", "within 0-59"), ("0 24 * * *", "within 0-23"), ("0 9 0 * *", "within 1-31"),
    ("0 9 * 13 *", "within 1-12"), ("0 9 * * 8", "within 0-7"), ("0 9 * *", "five fields"),
    ("0 9 * * mon", "not a number"), ("0 9-7 * * *", "within 0-23"), ("tomorrow at 9", "five fields"),
])
def test_bad_schedule_texts_say_what_is_wrong(spec, message):
    with pytest.raises(ValueError, match=message):
        parse_spec(spec)


def test_a_cron_line_no_date_matches_has_no_next_time():
    assert next_fire({"spec": "0 9 30 2 *", "timezone": "UTC"}, T0) is None
    with pytest.raises(ValueError, match="No date matches"):
        schedules.preview("0 9 30 2 *", "UTC", now=T0)
    with pytest.raises(ValueError, match="Unknown time zone"):
        schedules.preview("0 9 * * *", "Mars/Olympus", now=T0)
    assert schedules.preview("0 9 * * *", "UTC", now=T0)["next_runs"] == [T0 + 3600, T0 + 3600 + 86400, T0 + 3600 + 2 * 86400]


# The checks of a schedule

def good(**changes):
    values = {"name": "Nightly check", "prompt": "Run the tests and report failures.", "spec": "0 2 * * *",
              "permission_mode": "dontAsk", "approval_timeout_minutes": 10, "turn_token_limit": 200_000,
              "turn_time_limit_minutes": 30, "spending_limit_dbu": 2.5}
    return {**values, **changes}


def test_a_schedule_needs_its_caps_and_never_bypasses_permissions():
    checked = check_schedule(good(name="  Nightly   check "))
    assert checked["name"] == "Nightly check" and checked["run_policy"] == "ask" and checked["catch_up"] == "once"
    assert checked["permission_mode"] == "dontAsk" and checked["max_agent_steps"] is None
    for key, message in (("approval_timeout_minutes", "an approval limit"), ("turn_token_limit", "a token limit per run"),
                         ("turn_time_limit_minutes", "a time limit per run")):
        for bad in (None, 0, -1, 2.5, True):
            with pytest.raises(ValueError, match=message):
                check_schedule(good(**{key: bad}))
    with pytest.raises(ValueError, match="1 to 60"):
        check_schedule(good(approval_timeout_minutes=61))
    with pytest.raises(ValueError, match="never use Bypass permissions"):
        check_schedule(good(permission_mode="bypassPermissions"))
    with pytest.raises(ValueError, match="Permission mode"):
        check_schedule(good(permission_mode="root"))
    for key, bad, message in (("name", "", "Name the schedule"), ("prompt", "  ", "Enter the prompt"),
                              ("model", "bad name!", "endpoint name"), ("reasoning_effort", "huge", "Reasoning effort"),
                              ("run_policy", "always", "Run policy"), ("catch_up", "all", "Missed runs"),
                              ("spending_limit_dbu", -1, "Spending limit per run"), ("max_agent_steps", 65, "Agent steps"),
                              ("timezone", "../etc", "Time zone")):
        with pytest.raises(ValueError, match=message):
            check_schedule(good(**{key: bad}))


def test_the_token_warning_starts_seven_days_before_expiry():
    today = date(2026, 10, 17)
    assert token_warning("", today) is None and token_warning("2026-10-25", today) is None
    assert "expires in 7 days (2026-10-24)" in token_warning("2026-10-24", today)
    assert "expires tomorrow" in token_warning("2026-10-18", today)
    assert "expires today" in token_warning("2026-10-17", today)
    assert "expired on 2026-10-16" in token_warning("2026-10-16", today)


def test_token_expires_on_is_a_checked_setting(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    assert settings.values["token_expires_on"] == "" and settings.values["schedules_enabled"] is False
    for bad in ("2026-13-01", "17.10.2026", "2026-1-5", 20261017):
        with pytest.raises(ValueError, match="Token expires on"):
            settings.update({"token_expires_on": bad})
    with pytest.raises(ValueError, match="Scheduled runs"):
        settings.update({"schedules_enabled": "yes"})
    soon = date.fromordinal(date.today().toordinal() + 3).isoformat()
    public = settings.update({"token_expires_on": f" {soon} ", "schedules_enabled": True})
    assert public["token_expires_on"] == soon and "in 3 days" in public["token_warning"]
    saved = json.loads((tmp_path / "state" / "settings.json").read_text())
    assert saved["token_expires_on"] == soon and saved["schedules_enabled"] is True
    saved.update(token_expires_on="soon", schedules_enabled="on")  # Hand-edited values mean none and off.
    (tmp_path / "state" / "settings.json").write_text(json.dumps(saved))
    reloaded = Settings(tmp_path / "state")
    assert reloaded.values["token_expires_on"] == "" and reloaded.values["schedules_enabled"] is False


# The scheduler with a fake clock

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(tmp_path / "tests.sqlite3")
    manager = AgentManager(store, settings)
    scheduler = manager.scheduler = Scheduler(manager, settings)
    now = [T0]
    monkeypatch.setattr(schedules, "clock", lambda: now[0])
    yield manager, scheduler, project, now
    store.db.close()


def sse(delta, finish):
    chunks = [{"choices": [{"delta": delta, "finish_reason": finish}]},
              {"choices": [], "usage": {"prompt_tokens": 1_000, "completion_tokens": 100}}]
    return "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def text_reply(text):
    return sse({"content": text}, "stop")


def call_reply(name, arguments, call_id="c1"):
    return sse({"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)}}]}, "tool_calls")


def gateway(monkeypatch, replies=lambda requests: text_reply("Done."), check=200):
    """The fake Databricks: the connection check answers `check`; agent requests get replies(requests)."""
    seen = {"checks": 0, "requests": []}

    async def answer(request):
        if request.method == "GET":
            seen["checks"] += 1
            assert request.url.path == "/api/2.0/serving-endpoints"
            return httpx.Response(check, json={"endpoints": [{"name": "test-endpoint"}]} if check == 200 else {})
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}]})
        seen["requests"].append(body)
        return httpx.Response(200, text=replies(seen["requests"]))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(answer)))
    return seen


async def settled(manager):
    """Wait for the runs the scheduler started."""
    for _ in range(200):
        tasks = [task for task in manager.tasks.values() if not task.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
    raise AssertionError("A run did not end.")


def make(scheduler, project, **changes):
    return scheduler.create(good(**changes), str(project))


def history(scheduler, item):
    return scheduler.store.get(item["id"])["history"]


async def test_an_auto_run_starts_a_chat_with_the_schedules_mode_limits_and_tag(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    seen = gateway(monkeypatch)
    item = make(scheduler, project, spec="every 30 minutes", run_policy="auto", permission_mode="acceptEdits",
                reasoning_effort="high", max_agent_steps=12, model="test-endpoint")
    assert item["next_run"] == T0 + 1800
    now[0] = T0 + 1800 + 10
    await scheduler.tick()
    await settled(manager)
    entry = history(scheduler, item)[-1]
    assert entry["outcome"] == "started" and entry["trigger"] == "scheduled" and "note" not in entry
    assert entry["terminal_reason"] == "completed" and entry["steps"] == 1 and entry["tokens"] == 1_100
    chat = manager.store.get(entry["session_id"])
    assert chat["permission_mode"] == "acceptEdits" and chat["reasoning_effort"] == "high" and chat["max_agent_steps"] == 12
    assert (chat["turn_token_limit"], chat["turn_time_limit_minutes"], chat["spending_limit_dbu"]) == (200_000, 30, 2.5)
    assert chat["tags"] == ["scheduled"] and chat["title"].startswith("Nightly check · ") and chat["title_generated"]
    assert chat["schedule"] == {"id": item["id"], "name": "Nightly check", "trigger": "scheduled", "due": T0 + 1800}
    assert chat["workspace"] == str(project) and chat["events"][0]["text"] == "Run the tests and report failures."
    assert seen["checks"] == 1 and len(seen["requests"]) == 1  # No title request: the chat is named after the schedule.
    assert scheduler.store.get(item["id"])["next_run"] == T0 + 3600
    assert manager.scheduled_turns == {} and scheduler.runs == {}


async def test_a_schedule_without_a_spending_limit_keeps_the_settings_default(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    gateway(monkeypatch)
    manager.settings.values["spending_limit_dbu"] = 4.0
    item = make(scheduler, project, spending_limit_dbu=0)
    session_id = await scheduler.launch(item["id"], "run_now", user=True)
    await settled(manager)
    chat = manager.store.get(session_id)
    assert "spending_limit_dbu" not in chat and spending.limit_for(manager, chat) == (4.0, "settings")


async def test_missed_runs_are_caught_up_once_or_skipped_when_too_old(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    seen = gateway(monkeypatch)
    hourly = make(scheduler, project, name="Hourly", spec="every hour", run_policy="auto")
    skipping = make(scheduler, project, name="Skipping", spec="every hour", run_policy="auto", catch_up="skip")
    now[0] = T0 + 4 * 3600 + 600  # The app was closed for four runs.
    await scheduler.tick()
    await settled(manager)
    caught = history(scheduler, hourly)
    assert len(caught) == 1 and caught[0]["outcome"] == "started" and caught[0]["trigger"] == "catch_up"
    assert caught[0]["note"] == "Caught up once: 4 runs missed while the app was not running."
    assert caught[0]["due"] == T0 + 4 * 3600 and len(seen["requests"]) == 1
    assert history(scheduler, skipping)[0]["outcome"] == "skipped"
    assert "missed runs are skipped" in history(scheduler, skipping)[0]["note"]
    assert scheduler.store.get(hourly["id"])["next_run"] == T0 + 5 * 3600
    # Closed for two days: the latest missed run is due within 24 hours, so it still runs once.
    now[0] = T0 + 53 * 3600 + 120
    await scheduler.tick()
    await settled(manager)
    assert history(scheduler, hourly)[-1]["note"].startswith("Caught up once: 49 runs missed")
    # A daily run whose latest missed time is more than 24 hours old is skipped with a note.
    daily = make(scheduler, project, name="Daily", spec="every day", run_policy="auto")
    now[0] += 3 * 86400 + 4 * 3600
    await scheduler.tick()
    await settled(manager)
    entry = history(scheduler, daily)[-1]
    assert entry["outcome"] == "started" and entry["note"].startswith("Caught up once: 3 runs")
    calls = len(seen["requests"])
    stale = make(scheduler, project, name="Stale", spec="every 5 days", run_policy="auto")
    now[0] += 5 * 86400 + 25 * 3600
    await scheduler.tick()
    entry = history(scheduler, stale)[-1]
    assert entry["outcome"] == "skipped" and "more than 24 hours ago" in entry["note"]
    await settled(manager)
    assert all(history(scheduler, item)[-1]["outcome"] == "started" for item in (hourly, daily))
    assert len(seen["requests"]) == calls + 2  # Hourly and daily again; nothing for the stale one.


async def test_an_ask_run_waits_for_run_or_skip_and_a_newer_time_replaces_it(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    seen = gateway(monkeypatch)
    sent = []
    manager.notifier.send = sent.append
    manager.settings.values["system_notifications"] = True
    item = make(scheduler, project, spec="every hour")
    now[0] = T0 + 3600
    await scheduler.tick()
    assert scheduler.store.get(item["id"])["pending"] == {"due": T0 + 3600, "created": T0 + 3600, "trigger": "scheduled"}
    assert sent == ["Scheduled run ready: Nightly check"] and seen["checks"] == 0 and not manager.tasks
    assert scheduler.attention()["ready"] == [{"id": item["id"], "name": "Nightly check", "workspace": str(project),
                                               "due": T0 + 3600, "note": None}]
    now[0] = T0 + 7200
    await scheduler.tick()
    assert history(scheduler, item)[-1]["note"] == "Not run: nobody chose Run before the next scheduled time."
    assert scheduler.store.get(item["id"])["pending"]["due"] == T0 + 7200
    scheduler.skip(item["id"])
    assert history(scheduler, item)[-1]["outcome"] == "skipped" and "pending" not in scheduler.store.get(item["id"])
    with pytest.raises(ValueError, match="No run of this schedule is waiting"):
        scheduler.skip(item["id"])
    now[0] = T0 + 3 * 3600
    await scheduler.tick()
    started = await scheduler.launch(item["id"], "ready", T0 + 3 * 3600, user=True)
    await settled(manager)
    assert history(scheduler, item)[-1]["session_id"] == started and "pending" not in scheduler.store.get(item["id"])
    assert len(seen["requests"]) == 1
    # A waiting run expires after 24 hours (the ticks of that day found nothing new due: the schedule was paused).
    scheduler.pause(item["id"])
    scheduler.resume(item["id"])
    now[0] = scheduler.store.get(item["id"])["next_run"]
    await scheduler.tick()
    scheduler.store.update(item["id"], lambda saved: saved.update(next_run=now[0] + 2 * 86400))
    now[0] += 86400 + 60
    await scheduler.tick()
    assert history(scheduler, item)[-1]["note"] == "Not run: nobody chose Run within 24 hours."


async def test_a_rejected_token_pauses_the_schedule_without_a_model_call(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    seen = gateway(monkeypatch, check=401)
    sent = []
    manager.notifier.send = sent.append
    manager.settings.values["system_notifications"] = True
    item = make(scheduler, project, spec="every hour", run_policy="auto")
    now[0] = T0 + 3600
    await scheduler.tick()
    saved = scheduler.store.get(item["id"])
    assert saved["enabled"] is False and saved["paused_reason"] == "token_rejected"
    assert saved["history"][-1]["note"].startswith("Not run: Databricks rejected the token (HTTP 401)")
    assert sent == ["Scheduled runs paused: Databricks rejected the token (Nightly check)"]
    assert seen["checks"] == 1 and not seen["requests"] and manager.store.list() == []
    assert scheduler.attention()["paused"] == [{"id": item["id"], "name": "Nightly check", "workspace": str(project)}]
    now[0] = T0 + 7200
    await scheduler.tick()
    assert seen["checks"] == 1  # Paused: nothing runs until you resume it.
    resumed = scheduler.resume(item["id"])
    assert resumed["enabled"] and "paused_reason" not in resumed and resumed["next_run"] == T0 + 3 * 3600
    # Run now with a rejected token says so and pauses nothing; an unreachable Databricks is noted.
    with pytest.raises(ValueError, match="rejected the token"):
        await scheduler.launch(item["id"], "run_now", user=True)
    assert scheduler.store.get(item["id"])["enabled"] is True
    gateway(monkeypatch, check=503)
    now[0] = T0 + 3 * 3600
    await scheduler.tick()
    saved = scheduler.store.get(item["id"])
    assert saved["enabled"] and saved["history"][-1]["note"] == "Not run: Databricks answered the connection check with HTTP 503."


async def test_one_run_per_schedule_and_at_most_two_at_a_time(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    release = asyncio.Event()

    async def slow(request):
        if request.method == "GET":
            return httpx.Response(200, json={"endpoints": []})
        await release.wait()
        return httpx.Response(200, text=text_reply("Done."))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(slow)))
    items = [make(scheduler, project, name=f"Run {number}", spec="every 30 minutes", run_policy="auto") for number in range(3)]
    now[0] = T0 + 1800
    await scheduler.tick()
    outcomes = [history(scheduler, item)[-1] for item in items]
    assert [entry["outcome"] for entry in outcomes] == ["started", "started", "not_run"]
    assert outcomes[2]["note"] == "Not run: 2 scheduled runs were already running."
    now[0] = T0 + 3600
    await scheduler.tick()
    assert history(scheduler, items[0])[-1]["note"] == "Not run: the previous run of this schedule is still running."
    with pytest.raises(ValueError, match="still running"):
        await scheduler.launch(items[0]["id"], "run_now", user=True)
    assert scheduler.public(scheduler.store.get(items[0]["id"]))["running"] is True
    release.set()
    await settled(manager)
    assert history(scheduler, items[0])[-2]["terminal_reason"] == "completed"
    assert scheduler.public(scheduler.store.get(items[0]["id"]))["running"] is False


async def test_a_long_run_keeps_its_history_entry_and_its_schedule_never_runs_twice(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    release = asyncio.Event()

    async def slow(request):
        if request.method == "GET":
            return httpx.Response(200, json={"endpoints": []})
        await release.wait()
        return httpx.Response(200, text=text_reply("Done."))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(slow)))
    item = make(scheduler, project, spec="every 5 minutes", run_policy="auto")
    now[0] = T0 + 300
    await scheduler.tick()
    first = history(scheduler, item)[-1]["session_id"]
    for step in range(2, 30):  # More "still running" notes than the history keeps.
        now[0] = T0 + 300 * step
        await scheduler.tick()
    saved = history(scheduler, item)
    assert len(saved) == schedules.HISTORY and saved[0]["session_id"] == first and "ended" not in saved[0]
    assert all(entry["note"] == "Not run: the previous run of this schedule is still running." for entry in saved[1:])
    assert [sid for sid in manager.tasks if manager.busy_task(sid)] == [first]
    release.set()
    await settled(manager)
    assert history(scheduler, item)[0]["terminal_reason"] == "completed"


def test_the_history_drops_ended_entries_before_a_running_one():
    item = {"history": [{"id": "open", "outcome": "started", "session_id": "a"}]
            + [{"id": f"n{index}", "outcome": "not_run"} for index in range(schedules.HISTORY - 1)]}
    for index in range(5):
        Scheduler.add_history(item, {"id": f"new{index}", "outcome": "not_run", "note": None})
    ids = [entry["id"] for entry in item["history"]]
    assert len(ids) == schedules.HISTORY and ids[0] == "open" and ids[-1] == "new4" and "n4" not in ids
    assert "note" not in item["history"][-1]
    # Entries that never ended (runs of a crashed app) cannot fill it beyond its size either.
    item = {"history": [{"id": f"o{index}", "outcome": "started"} for index in range(schedules.HISTORY)]}
    Scheduler.add_history(item, {"id": "last", "outcome": "not_run"})
    assert len(item["history"]) == schedules.HISTORY and item["history"][0]["id"] == "o1"


async def test_your_own_turn_in_a_runs_chat_does_not_block_the_next_run(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    release = asyncio.Event()
    calls = []

    async def answer(request):
        if request.method == "GET":
            return httpx.Response(200, json={"endpoints": []})
        calls.append(1)
        if len(calls) > 1:
            await release.wait()
        return httpx.Response(200, text=text_reply("Done."))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(answer)))
    item = make(scheduler, project, spec="every hour", run_policy="auto")
    now[0] = T0 + 3600
    await scheduler.tick()
    await settled(manager)
    session_id = history(scheduler, item)[-1]["session_id"]
    manager.start(session_id, "A follow-up question of my own")  # You continue the run's chat.
    await asyncio.sleep(0.05)
    assert manager.busy_task(session_id) and scheduler.public(scheduler.store.get(item["id"]))["running"] is False
    now[0] = T0 + 7200
    await scheduler.tick()
    entry = history(scheduler, item)[-1]
    assert entry["outcome"] == "started" and entry["session_id"] != session_id
    release.set()
    await settled(manager)
    assert history(scheduler, item)[-1]["terminal_reason"] == "completed"


async def test_a_schedule_changed_outside_the_app_is_paused_instead_of_run(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    seen = gateway(monkeypatch)
    path = manager.settings.state_dir / "schedules.json"

    def tamper(index, **values):
        data = json.loads(path.read_text())
        data["schedules"][index].update(values)
        path.write_text(json.dumps(data))
    bypass = make(scheduler, project, name="Bypass", spec="every hour", run_policy="auto")
    uncapped = make(scheduler, project, name="Uncapped", spec="every hour")
    tamper(0, permission_mode="bypassPermissions")
    tamper(1, approval_timeout_minutes=0, turn_token_limit=0)
    with pytest.raises(ValueError, match="never use Bypass permissions"):
        await scheduler.launch(bypass["id"], "run_now", user=True)
    now[0] = T0 + 3600
    await scheduler.tick()
    for item, message in ((bypass, "never use Bypass permissions"), (uncapped, "need an approval limit")):
        saved = scheduler.store.get(item["id"])
        assert saved["enabled"] is False and saved["paused_reason"] == "invalid" and "pending" not in saved
        assert saved["paused_note"].startswith("The saved schedule is not valid: ") and message in saved["paused_note"]
        assert saved["history"][-1]["outcome"] == "not_run" and message in saved["history"][-1]["note"]
    assert seen["checks"] == 0 and manager.store.list() == [] and scheduler.attention()["ready"] == []
    # Changed while its connection check ran: the run checks the schedule again before its chat is made.
    later = make(scheduler, project, name="Later", spec="every hour", run_policy="auto")

    async def check_then_tamper(request):
        tamper(2, turn_time_limit_minutes=0)
        return httpx.Response(200, json={"endpoints": []})
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(check_then_tamper)))
    now[0] = T0 + 2 * 3600
    await scheduler.tick()
    saved = scheduler.store.get(later["id"])
    assert saved["paused_reason"] == "invalid" and "a time limit per run" in saved["paused_note"]
    assert manager.store.list() == [] and manager.scheduled_turns == {}
    # A scheduled turn's approval limit is never "no limit", even when its value is damaged.
    session = manager.store.create({**manager.settings.values, "workspace": str(project)})
    manager.turns[session["id"]] = {"started": 0, "first_event": 0, "first_call": 0,
                                    "scheduled": {"schedule_id": "x", "approval_timeout_minutes": 0}}
    assert manager.approval_seconds(session) == interactions.MINUTE
    manager.turns.pop(session["id"])


async def test_a_card_nobody_answers_ends_the_run_at_its_approval_limit(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    monkeypatch.setattr(interactions, "MINUTE", 0.05)  # Each minute of the approval limit lasts 50 ms.
    seen = gateway(monkeypatch, lambda requests: call_reply("run_command", {"command": "touch ran.txt"}, f"c{len(requests)}"))
    item = make(scheduler, project, permission_mode="manual", approval_timeout_minutes=2)
    started = time.monotonic()
    session_id = await scheduler.launch(item["id"], "run_now", user=True)
    await settled(manager)
    assert time.monotonic() - started < 5
    chat = manager.store.get(session_id)
    card = next(event for event in chat["events"] if event.get("name") == "run_command")
    assert (card["state"], card["approval"]) == ("rejected", "expired") and not (project / "ran.txt").exists()
    notice = next(event for event in reversed(chat["events"]) if event.get("terminal_reason"))
    assert notice["type"] == "notice" and notice["terminal_reason"] == "approval_expired"
    assert notice["text"].startswith("A card waited 2 minutes, this scheduled run's approval limit")
    assert chat["terminal_reason"] == "approval_expired" and len(seen["requests"]) == 1
    assert history(scheduler, item)[-1]["terminal_reason"] == "approval_expired"
    # Only the scheduled turn has the limit: your next message in the chat waits as Settings say.
    manager.turns[session_id] = {"started": 0, "first_event": 0, "first_call": 0}
    assert manager.approval_seconds(chat) == 60 * 0.05  # Settings → Approval wait: 60 minutes.
    manager.turns.pop(session_id)


async def test_a_card_of_the_last_step_ends_the_run_at_the_approval_limit_too(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    monkeypatch.setattr(interactions, "MINUTE", 0.05)
    gateway(monkeypatch, lambda requests: call_reply("run_command", {"command": "touch ran.txt"}, f"c{len(requests)}"))
    item = make(scheduler, project, permission_mode="manual", approval_timeout_minutes=1, max_agent_steps=1)
    session_id = await scheduler.launch(item["id"], "run_now", user=True)
    await settled(manager)
    chat = manager.store.get(session_id)
    card = next(event for event in chat["events"] if event.get("name") == "run_command")
    assert card["approval"] == "expired" and chat["terminal_reason"] == "approval_expired"
    assert [event["terminal_reason"] for event in chat["events"] if event.get("terminal_reason")] == ["approval_expired"]
    assert history(scheduler, item)[-1]["terminal_reason"] == "approval_expired"


async def test_a_question_nobody_answers_ends_the_run_too(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    monkeypatch.setattr(interactions, "MINUTE", 0.05)
    question = {"questions": [{"question": "Which branch?", "options": [{"label": "main"}, {"label": "dev"}]}]}
    seen = gateway(monkeypatch, lambda requests: call_reply("ask_user", question, f"q{len(requests)}"))
    item = make(scheduler, project, permission_mode="manual", approval_timeout_minutes=1)
    session_id = await scheduler.launch(item["id"], "run_now", user=True)
    await settled(manager)
    chat = manager.store.get(session_id)
    asked = next(event for event in chat["events"] if event.get("name") == "ask_user")
    assert asked["question_outcome"] == "expired" and chat["terminal_reason"] == "approval_expired"
    assert len(seen["requests"]) == 1


async def test_a_subagents_card_ends_its_parents_run_too(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    monkeypatch.setattr(interactions, "MINUTE", 0.05)
    parent, child = [], []

    async def answer(request):
        if request.method == "GET":
            return httpx.Response(200, json={"endpoints": []})
        body = json.loads(request.content)
        first = next((m["content"] for m in body["messages"] if m["role"] == "user"), "")
        if isinstance(first, str) and first.startswith("Delegated task:\n"):
            child.append(body)
            return httpx.Response(200, text=call_reply("run_command", {"command": "touch ran.txt"}, f"x{len(child)}"))
        parent.append(body)
        return httpx.Response(200, text=call_reply("delegate_task", {"task": "Check it", "agent_type": "general"}, "d1")
                              if len(parent) == 1 else text_reply("Done."))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(answer)))
    item = make(scheduler, project, permission_mode="acceptEdits", approval_timeout_minutes=1)
    session_id = await scheduler.launch(item["id"], "run_now", user=True)
    await settled(manager)
    chat = manager.store.get(session_id)
    assert chat["terminal_reason"] == "approval_expired" and len(parent) == 1 and len(child) == 1
    child_chat = manager.store.get(chat["subagents"][0]["id"])
    error = next(event for event in reversed(child_chat["events"]) if event.get("terminal_reason"))
    assert error["type"] == "error" and error["terminal_reason"] == "approval_expired"
    assert not (project / "ran.txt").exists()


async def test_a_scheduled_runs_subagents_run_in_the_foreground_and_its_chat_is_never_woken(runtime, monkeypatch):
    """background: true is ignored in a scheduled run: the run waits for its subagent, so the subagent's cards have
    the run's approval limit, the next due run is skipped and the history counts the subagent's tokens."""
    manager, scheduler, project, now = runtime
    manager.settings.values["background_subagent_wake"] = True
    gates, parent, child = [asyncio.Event(), asyncio.Event()], [], []

    async def answer(request):
        if request.method == "GET":
            return httpx.Response(200, json={"endpoints": []})
        body = json.loads(request.content)
        first = next((m["content"] for m in body["messages"] if m["role"] == "user"), "")
        if isinstance(first, str) and first.startswith("Delegated task:\n"):
            child.append(body)
            await gates[len(child) - 1].wait()
            return httpx.Response(200, text=text_reply("## Report\nChecked."))
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}]})
        parent.append(body)
        return httpx.Response(200, text=call_reply("delegate_task", {"task": f"Check {len(parent)}", "background": True},
                                                   f"d{len(parent)}") if len(parent) in (1, 3) else text_reply("Done."))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(answer)))
    item = make(scheduler, project, spec="every 30 minutes", run_policy="auto", permission_mode="acceptEdits",
                approval_timeout_minutes=1)
    now[0] = T0 + 1800
    await scheduler.tick()
    session_id = history(scheduler, item)[-1]["session_id"]
    for _ in range(500):
        if child:
            break
        await asyncio.sleep(0.01)
    chat = manager.get(session_id)
    assert manager.busy_task(session_id) and not manager.delegates.background.get(session_id)
    assert manager.approval_seconds(manager.get(chat["subagents"][0]["id"])) == 60  # The run's 1 minute.
    preview = manager.delegates.card_preview(chat, {"task": "Check it", "background": True})
    assert preview.startswith("Start a subagent (") and f"Runs in the foreground: {planning.SCHEDULED_FOREGROUND}" in preview
    now[0] = T0 + 3600
    await scheduler.tick()
    assert history(scheduler, item)[-1]["note"] == "Not run: the previous run of this schedule is still running."
    gates[0].set()
    await settled(manager)
    chat = manager.store.get(session_id)
    entry = next(entry for entry in history(scheduler, item) if entry.get("session_id") == session_id)
    assert entry["terminal_reason"] == "completed" and entry["tokens"] == 3 * 1_100  # Its two requests and the subagent's.
    result = json.loads(next(event for event in chat["events"] if event.get("name") == "delegate_task")["output"])
    assert result["status"] == "completed" and result["note"] == f"Ran in the foreground: {planning.SCHEDULED_FOREGROUND}"
    # A message of yours in the run's chat may start a background subagent; when it finishes, the wake option does
    # not continue the run's chat (nobody may be watching it): its notice waits for your next message.
    manager.start(session_id, "Check it again in the background")
    await asyncio.wait_for(manager.tasks[session_id], 10)
    assert manager.delegates.background_running(session_id) == 1
    gates[1].set()
    for _ in range(500):
        if not manager.delegates.background_running(session_id):
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)
    chat = manager.get(session_id)
    assert len(parent) == 4 and not manager.busy_task(session_id) and manager.delegates.wake_due(chat)
    assert not any(event.get("background_wake") for event in chat["events"])


async def test_the_time_limit_stops_a_scheduled_run(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    clock = [1_000_000.0]
    monkeypatch.setattr(turns, "clock", lambda: clock[0])

    def replies(requests):
        clock[0] += 400  # Each step takes 400 seconds.
        return call_reply("list_tasks", {}, f"c{len(requests)}")
    seen = gateway(monkeypatch, replies)
    item = make(scheduler, project, turn_time_limit_minutes=10)
    session_id = await scheduler.launch(item["id"], "run_now", user=True)
    await settled(manager)
    chat = manager.store.get(session_id)
    assert chat["terminal_reason"] == "budget_limit" and len(seen["requests"]) == 3  # 0, 400 s, then the wrap-up at 800 s.
    assert history(scheduler, item)[-1]["terminal_reason"] == "budget_limit"


async def test_a_run_that_cannot_start_is_noted_and_leaves_no_chat(runtime, monkeypatch, tmp_path):
    manager, scheduler, project, now = runtime
    gateway(monkeypatch)
    (project / ".env").write_text("TOKEN=abc\n")
    item = make(scheduler, project, spec="every hour", run_policy="auto", prompt="Check @.env please")
    now[0] = T0 + 3600
    await scheduler.tick()
    entry = history(scheduler, item)[-1]
    assert entry["outcome"] == "not_run" and manager.store.list() == []
    assert entry["note"] == "Not run: @.env: Credential files are excluded from file tools."
    gone = make(scheduler, project, name="Gone", spec="every hour", run_policy="auto")
    scheduler.store.update(gone["id"], lambda saved: saved.update(workspace=str(tmp_path / "deleted")))
    now[0] = T0 + 7200
    await scheduler.tick()
    assert history(scheduler, gone)[-1]["note"] == "Not run: Choose an existing project directory."


async def test_due_runs_a_stopped_or_failing_tick_does_not_start_get_a_note(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    hang = asyncio.Event()

    async def answer(request):
        if request.method == "GET":
            await hang.wait()
        return httpx.Response(200, json={"endpoints": []})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(answer)))
    first = make(scheduler, project, name="First", spec="every hour", run_policy="auto")
    second = make(scheduler, project, name="Second", spec="every hour", run_policy="auto")
    now[0] = T0 + 3600 + 5
    task = asyncio.create_task(scheduler.tick())
    for _ in range(100):  # Until the first run's connection check waits.
        await asyncio.sleep(0.01)
        if scheduler.launching:
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for item in (first, second):
        entry = history(scheduler, item)[-1]
        assert entry["outcome"] == "not_run" and entry["due"] == T0 + 3600
        assert entry["note"].startswith("Not run: the scheduler stopped before the run started")
    assert manager.store.list() == [] and not scheduler.launching
    # One run that fails unexpectedly does not keep the next one from starting.
    gateway(monkeypatch)
    plain = scheduler.create_chat

    def broken(item, *args):
        if item["id"] == first["id"]:
            raise RuntimeError("unexpected")
        return plain(item, *args)
    monkeypatch.setattr(scheduler, "create_chat", broken)
    now[0] = T0 + 2 * 3600
    await scheduler.tick()
    await settled(manager)
    assert history(scheduler, first)[-1]["note"].startswith("Not run: it could not be started because of an unexpected")
    assert history(scheduler, second)[-1]["terminal_reason"] == "completed"


async def test_a_deleted_or_restarted_run_gets_its_end_in_the_history(runtime, monkeypatch):
    manager, scheduler, project, now = runtime
    gateway(monkeypatch)
    item = make(scheduler, project)
    session_id = await scheduler.launch(item["id"], "run_now", user=True)
    await settled(manager)
    # As after a restart: the entry has no end yet, and its chat is gone.
    scheduler.store.update(item["id"], lambda saved: saved["history"][-1].pop("ended"))
    manager.store.delete(session_id)
    await scheduler.tick()
    assert history(scheduler, item)[-1]["terminal_reason"] == "deleted"


def test_an_unreadable_schedules_file_is_reported_and_never_overwritten(runtime):
    manager, scheduler, project, now = runtime
    path = manager.settings.state_dir / "schedules.json"
    path.write_text("{not json")
    with pytest.raises(ValueError, match="could not be read"):
        make(scheduler, project)
    assert path.read_text() == "{not json"
    asyncio.run(scheduler.tick())
    assert path.read_text() == "{not json" and scheduler.attention() == {"ready": [], "paused": []}


# The app: routes, the loop and its lock

def client_for(tmp_path, monkeypatch, **values):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", **values)
    return settings, project


def test_the_scheduler_starts_only_when_scheduled_runs_are_on(tmp_path, monkeypatch):
    settings, project = client_for(tmp_path, monkeypatch)
    app = create_app(settings)
    with TestClient(app) as client:
        scheduler = app.state.manager.scheduler
        assert not scheduler.running and scheduler.lock is None
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        listing = client.get("/api/schedules", headers=headers).json()
        assert (listing["enabled"], listing["running"], listing["problem"]) == (False, False, None)
        body = {"workspace": str(project), "model": "test-endpoint", "env_file": ""}
        assert client.put("/api/settings", headers=headers, json={**body, "schedules_enabled": True}).json()["schedules_enabled"]
        assert scheduler.running and scheduler.lock is not None
        # A second app on the same state folder does not run them while this one does.
        other = Scheduler(app.state.manager, settings)
        assert other.acquire() is False and "Another app instance" in other.lock_problem
        client.put("/api/settings", headers=headers, json={**body, "schedules_enabled": False})
        assert not scheduler.running and scheduler.lock is None
        assert other.acquire() is True
        other.release()
    settings.values["schedules_enabled"] = True
    app = create_app(settings)
    with TestClient(app):
        assert app.state.manager.scheduler.running
    assert not app.state.manager.scheduler.running and app.state.manager.scheduler.lock is None  # Stopped at shutdown.


async def test_a_stop_and_a_start_that_overlap_keep_the_lock(runtime):
    manager, scheduler, project, now = runtime
    manager.settings.values["schedules_enabled"] = True
    assert scheduler.start()
    stopping = asyncio.create_task(scheduler.stop())
    await asyncio.sleep(0)
    assert scheduler.start()  # A second Settings save turns it on again while the first one's stop waits.
    await stopping
    other = Scheduler(manager, manager.settings)
    assert scheduler.running and scheduler.lock is not None and other.acquire() is False
    await scheduler.stop()
    assert scheduler.lock is None and other.acquire() is True
    other.release()


def test_the_preview_of_an_edit_keeps_the_saved_interval_times(tmp_path, monkeypatch):
    settings, project = client_for(tmp_path, monkeypatch)
    now = [T0]
    monkeypatch.setattr(schedules, "clock", lambda: now[0])
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        item = client.post("/api/schedules", headers=headers, json=good(spec="every 2 hours")).json()["schedules"][0]
        now[0] = T0 + 5400  # 90 minutes later, Edit changes only the prompt.

        def first_time(**body):
            return client.post("/api/schedules/preview", headers=headers, json=body).json()["next_runs"][0]
        assert first_time(spec="every 2 hours", schedule_id=item["id"]) == item["next_run"] == T0 + 7200
        assert first_time(spec="Every  2 hours", timezone=" ", schedule_id=item["id"]) == T0 + 7200
        assert first_time(spec="every 2 hours") == T0 + 5400 + 7200  # A new schedule counts from now.
        assert first_time(spec="every 3 hours", schedule_id=item["id"]) == T0 + 5400 + 3 * 3600  # New timing: from now.
        assert first_time(spec="every 2 hours", schedule_id="missing") == T0 + 5400 + 7200
        saved = client.put(f"/api/schedules/{item['id']}", headers=headers,
                           json=good(spec="every 2 hours", prompt="Another prompt")).json()["schedules"][0]
        assert saved["next_run"] == T0 + 7200


def test_the_schedule_routes(tmp_path, monkeypatch):
    settings, project = client_for(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        assert client.post("/api/schedules", json=good()).status_code == 403  # The local token is required.
        refused = client.post("/api/schedules", headers=headers, json=good(turn_token_limit=None))
        assert refused.status_code == 400 and "a token limit per run" in refused.json()["detail"]
        refused = client.post("/api/schedules", headers=headers, json=good(permission_mode="bypassPermissions"))
        assert refused.status_code == 400 and "never use Bypass" in refused.json()["detail"]
        assert client.post("/api/schedules", headers=headers, json={**good(), "extra": 1}).status_code == 422
        listing = client.post("/api/schedules", headers=headers, json=good()).json()
        item = listing["schedules"][0]
        assert listing["project"] == str(project) and listing["limits"]["max_approval_minutes"] == 60
        assert item["workspace"] == str(project) and item["mode_label"] == "Don't ask" and item["running"] is False
        assert item["enabled"] and item["next_run"] > time.time() and item["history"] == []
        preview = client.post("/api/schedules/preview", headers=headers, json={"spec": "0 9 * * 1-5", "timezone": "UTC"}).json()
        assert preview["zone"] == "UTC" and len(preview["next_runs"]) == 3
        assert client.post("/api/schedules/preview", headers=headers, json={"spec": "* * * * *"}).status_code == 400
        edited = client.put(f"/api/schedules/{item['id']}", headers=headers, json=good(name="Renamed", spec="every 2 hours")).json()
        assert edited["schedules"][0]["name"] == "Renamed" and edited["schedules"][0]["spec"] == "every 2 hours"
        paused = client.post(f"/api/schedules/{item['id']}/pause", headers=headers).json()["schedules"][0]
        assert paused["enabled"] is False and paused["paused_reason"] == "paused"
        resumed = client.post(f"/api/schedules/{item['id']}/resume", headers=headers).json()["schedules"][0]
        assert resumed["enabled"] is True and "paused_reason" not in resumed
        assert client.post(f"/api/schedules/{item['id']}/skip", headers=headers).status_code == 400
        assert client.get("/api/schedules/ready", headers=headers).json() == {"ready": [], "paused": []}
        # Another project's chat lists only its own project's schedules.
        chat = client.post("/api/sessions", headers=headers, json={"workspace": str(other)}).json()
        theirs = client.get(f"/api/schedules?session_id={chat['id']}", headers=headers).json()
        assert theirs["project"] == str(other) and theirs["schedules"] == [] and theirs["other_projects"] == 1
        for method, path in (("put", "/api/schedules/missing"), ("delete", "/api/schedules/missing"),
                             ("post", "/api/schedules/missing/pause"), ("post", "/api/schedules/missing/run")):
            response = getattr(client, method)(path, headers=headers, **({"json": good()} if method == "put" else {}))
            assert response.status_code == 404
        assert client.delete(f"/api/schedules/{item['id']}", headers=headers).json()["schedules"] == []
    saved = json.loads((tmp_path / "state" / "schedules.json").read_text())
    assert saved == {"version": 1, "schedules": []}
    assert oct((tmp_path / "state" / "schedules.json").stat().st_mode & 0o777) == "0o600"


def test_a_schedule_is_never_made_for_the_state_folder(tmp_path, monkeypatch):
    settings, project = client_for(tmp_path, monkeypatch)
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        settings.values["workspace"] = str(settings.state_dir)
        refused = client.post("/api/schedules", headers=headers, json=good())
        assert refused.status_code == 400 and "state folder" in refused.json()["detail"]


# Against the offline simulator

@pytest.fixture
def simulated(tmp_path):
    spec = importlib.util.spec_from_file_location("simulate_databricks", SIMULATOR)
    simulator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(simulator)
    gateway_server, gateway_url = simulator.start_gateway(speed=0.0)
    state, workspace = tmp_path / "state", tmp_path / "project"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Demo\n")
    with simulator.patched_settings(gateway_url, state, workspace):
        server, thread, url = simulator.start_app()
        try:
            yield url, server.config.app.state.manager, workspace
        finally:
            server.should_exit = True
            thread.join(15)
            gateway_server.shutdown()
            gateway_server.server_close()


def test_run_now_against_the_simulator(simulated):
    url, manager, workspace = simulated
    token = httpx.get(f"{url}/api/bootstrap").json()["token"]
    headers = {"X-Local-Token": token}
    created = httpx.post(f"{url}/api/schedules", headers=headers,
                         json=good(model="databricks-claude-opus-5-5", prompt="Hello from a schedule")).json()
    item = created["schedules"][0]
    started = httpx.post(f"{url}/api/schedules/{item['id']}/run", headers=headers, timeout=30).json()
    session_id = started["session_id"]
    deadline = time.time() + 30
    entry = {}
    while time.time() < deadline:
        listing = httpx.get(f"{url}/api/schedules", headers=headers).json()
        entry = listing["schedules"][0]["history"][-1]
        if "ended" in entry:
            break
        time.sleep(0.05)
    assert entry["session_id"] == session_id and entry["trigger"] == "run_now" and entry["terminal_reason"] == "completed"
    assert entry["estimated_dbu"] > 0 and entry["tokens"] > 0
    chat = httpx.get(f"{url}/api/sessions/{session_id}", headers=headers).json()
    assert chat["tags"] == ["scheduled"] and chat["permission_mode"] == "dontAsk" and chat["workspace"] == str(workspace)
