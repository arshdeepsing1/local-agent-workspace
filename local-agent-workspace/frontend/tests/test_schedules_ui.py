"""Agent tools → Schedules (SchedulesPanel.js): the project's schedules, the form and its checks (the caps are required,
no Bypass permissions), a run waiting for Run, the history with its chats; the banners of ready runs, of a rejected
token and of the token's expiry (App.js); and the Settings fields (Scheduled runs, Token expires on)."""
import json
import re

from playwright.sync_api import expect

from test_agent_tools import open_dialog
from test_workspace import start_app

NOW = 1_800_000_000  # Epoch seconds.
SCHEDULE = {
    "id": "s1", "name": "Nightly tests", "prompt": "Run the tests.", "spec": "0 2 * * *", "timezone": "", "zone": "UTC",
    "model": "", "reasoning_effort": "", "permission_mode": "dontAsk", "mode_label": "Don't ask",
    "approval_timeout_minutes": 10, "turn_token_limit": 500000, "turn_time_limit_minutes": 30, "spending_limit_dbu": 2,
    "max_agent_steps": None, "run_policy": "ask", "catch_up": "once", "workspace": "/project", "enabled": True,
    "created": NOW - 86400, "anchor": NOW - 86400, "next_run": NOW + 3600, "running": False,
    "pending": {"due": NOW - 60, "created": NOW - 60, "trigger": "scheduled"},
    "history": [
        {"id": "h1", "at": NOW - 86400, "due": NOW - 86400, "trigger": "scheduled", "outcome": "started", "session_id": "run-1",
         "started": NOW - 86400, "ended": NOW - 86000, "terminal_reason": "completed", "steps": 4, "tokens": 182000,
         "duration_s": 400, "estimated_dbu": 1.25},
        {"id": "h2", "at": NOW - 7200, "due": NOW - 7200, "trigger": "catch_up", "outcome": "started", "session_id": "run-2",
         "started": NOW - 7200, "ended": NOW - 7000, "terminal_reason": "approval_expired",
         "note": "Caught up once: 3 runs missed while the app was not running."},
        {"id": "h3", "at": NOW - 3600, "trigger": "scheduled", "outcome": "not_run",
         "note": "Not run: Databricks rejected the token (HTTP 401). Renew it in your credential file, then resume the schedule."},
    ],
}
LIMITS = {"max_approval_minutes": 60, "max_turn_tokens": 100000000, "max_turn_minutes": 1440, "min_interval_minutes": 5,
          "max_name": 80, "max_prompt": 20000, "max_schedules": 50, "max_running": 2,
          "modes": ["manual", "auto", "acceptEdits", "plan", "dontAsk"]}
STATE = """window.scheduleState = { enabled: ENABLED, running: ENABLED, problem: null, project: '/project', other_projects: 0,
    token_warning: WARNING, limits: LIMITS, schedules: SCHEDULES }
  fake.override = (path, options) => {
    if (path === '/api/schedules/preview') {
      const body = JSON.parse(options.body)
      return body.spec.startsWith('*') ? fake.json({ detail: 'A schedule runs at most every 5 minutes.' }, 400)
        : fake.json({ text: body.spec, zone: 'UTC', next_runs: [NOW + 3600, NOW + 90000, NOW + 176400] })
    }
    if (!path.startsWith('/api/schedules')) return undefined
    const method = options.method || 'GET'
    if (method === 'POST' && path.startsWith('/api/schedules?')) {
      scheduleState.schedules = [...scheduleState.schedules, { ...SCHEDULE, ...JSON.parse(options.body), id: 's2', pending: null, history: [] }]
    }
    if (path.includes('/skip')) scheduleState.schedules = scheduleState.schedules.map(item => ({ ...item, pending: null }))
    if (path.includes('/pause')) scheduleState.schedules = scheduleState.schedules.map(item => ({ ...item, enabled: false, paused_reason: 'paused' }))
    if (path.includes('/run')) return fake.json({ ...scheduleState, session_id: 'run-3' })
    return fake.json(scheduleState)
  }"""


def state(schedules=(SCHEDULE,), enabled=True, warning=None):
    return (STATE.replace("ENABLED", json.dumps(enabled)).replace("WARNING", json.dumps(warning))
            .replace("LIMITS", json.dumps(LIMITS)).replace("SCHEDULES", json.dumps(list(schedules)))
            .replace("SCHEDULE", json.dumps(SCHEDULE)).replace("NOW", str(NOW)))


def open_schedules(ui, page, setup=None):
    open_dialog(ui, setup or state())
    page.get_by_role("button", name="Schedules", exact=True).click()
    region = page.get_by_role("region", name="Schedules")
    expect(region).to_be_visible()
    return region


def test_the_projects_schedules_with_a_waiting_run_and_the_history(ui, page):
    region = open_schedules(ui, page)
    item = region.get_by_role("list", name="Schedules").get_by_role("listitem").first
    expect(item.get_by_text("Nightly tests")).to_be_visible()
    expect(item.get_by_text("Waiting for Run")).to_be_visible()
    expect(item.get_by_text(re.compile(r"^0 2 \* \* \* · UTC · next "))).to_be_visible()
    expect(item.get_by_text(re.compile("Don't ask · Asks before each run · cards wait 10 minutes · 500k tokens and 30 minutes per run · 2 DBU"))).to_be_visible()
    expect(region.get_by_text("Project: /project")).to_be_visible()
    assert ui.js("fake.calls.some(call => call.path === '/api/schedules?session_id=a')")
    # The history, newest first: a run that did not start says why; ended runs say how and open their chat.
    item.get_by_text("History (3)").click()
    runs = item.get_by_role("list", name="Runs of Nightly tests").get_by_role("listitem")
    expect(runs).to_have_count(3)
    expect(runs.nth(0)).to_contain_text("Scheduled · Not run: Databricks rejected the token (HTTP 401).")
    expect(runs.nth(1)).to_contain_text("Caught up · Stopped at the approval limit")
    expect(runs.nth(1)).to_contain_text("Caught up once: 3 runs missed while the app was not running.")
    expect(runs.nth(2)).to_contain_text("Scheduled · Completed · 4 steps · 6 min 40 s · 182k tokens · ~1.3 DBU")
    expect(runs.nth(0).get_by_role("button", name=re.compile("^Open the chat"))).to_have_count(0)
    runs.nth(2).get_by_role("button", name=re.compile("^Open the chat of the run at ")).click()
    assert ui.spy("select") == [["run-1"]] and ui.spy("close") == [[]]


def test_run_and_skip_a_waiting_run_pause_and_run_now(ui, page):
    region = open_schedules(ui, page)
    waiting = region.get_by_role("group", name="Run of Nightly tests waiting")
    waiting.get_by_role("button", name="Skip").click()
    expect(region.get_by_role("status").last).to_have_text("Run skipped.")
    expect(waiting).to_have_count(0)
    assert ui.js("fake.requests('/api/schedules/s1/skip?session_id=a', 'POST').length") == 1
    region.get_by_role("button", name="Run Nightly tests now").click()
    expect(region.get_by_role("status").last).to_have_text("Run started. Its chat is in the history below.")
    assert ui.js("fake.requests('/api/schedules/s1/run?session_id=a', 'POST').length") == 1
    region.get_by_role("button", name="Pause Nightly tests").click()
    expect(region.get_by_role("button", name="Resume Nightly tests")).to_be_visible()
    expect(region.get_by_text("Paused", exact=True)).to_be_visible()


def test_a_new_schedule_needs_its_caps_and_never_offers_bypass(ui, page):
    region = open_schedules(ui, page, state(schedules=[]))
    expect(region.get_by_text("No schedules in this project yet.")).to_be_visible()
    region.get_by_role("button", name="New schedule").click()
    form = region.get_by_role("form", name="New schedule")
    expect(form.get_by_role("textbox", name="Name")).to_be_focused()
    modes = form.get_by_role("combobox", name="Permission mode").locator("option")
    expect(modes).to_have_text(["Don't ask (recommended)", "Plan", "Manual", "Accept edits", "Auto"])
    expect(form.get_by_role("combobox", name="Run policy")).to_have_value("ask")
    form.get_by_role("textbox", name="Name").fill("Morning summary")
    form.get_by_role("textbox", name="Prompt").fill("Summarize yesterday's commits.")
    form.get_by_role("textbox", name="When it runs").fill("* * * * *")
    expect(form.get_by_text("A schedule runs at most every 5 minutes.")).to_be_visible()
    form.get_by_role("textbox", name="When it runs").fill("0 9 * * 1-5")
    expect(form.get_by_role("status")).to_contain_text("Next runs (UTC): ")
    assert ui.bodies("/api/schedules/preview", "POST")[-1] == {"spec": "0 9 * * 1-5", "timezone": ""}
    form.get_by_role("spinbutton", name="Token limit per run").fill("")
    form.get_by_role("button", name="Add schedule").click()
    expect(form.get_by_role("alert")).to_have_text("Set a token limit per run: scheduled runs need one.")
    form.get_by_role("spinbutton", name="Token limit per run").fill("200000")
    form.get_by_role("spinbutton", name="Approval limit (minutes)").fill("61")
    form.get_by_role("button", name="Add schedule").click()
    expect(form.get_by_role("alert")).to_have_text("Set an approval limit of 1 to 60 minutes.")
    assert ui.js("fake.requests('/api/schedules?session_id=a', 'POST').length") == 0
    form.get_by_role("spinbutton", name="Approval limit (minutes)").fill("15")
    form.get_by_role("combobox", name="Run policy").select_option("auto")
    expect(form.get_by_text(re.compile("Runs start at their times without a click"))).to_be_visible()
    form.get_by_role("button", name="Add schedule").click()
    expect(region.get_by_role("status").last).to_have_text("Schedule added.")
    assert ui.bodies("/api/schedules?session_id=a", "POST") == [{
        "name": "Morning summary", "prompt": "Summarize yesterday's commits.", "spec": "0 9 * * 1-5", "timezone": "", "model": "",
        "reasoning_effort": "", "permission_mode": "dontAsk", "approval_timeout_minutes": 15, "turn_token_limit": 200000,
        "turn_time_limit_minutes": 30, "spending_limit_dbu": 0, "max_agent_steps": None, "run_policy": "auto", "catch_up": "once"}]


def test_edit_keeps_the_fields_and_a_server_refusal_shows(ui, page):
    setup = state() + """
      const plain = fake.override
      fake.override = (path, options) => path === '/api/schedules/s1?session_id=a' && options.method === 'PUT'
        ? fake.json({ detail: 'Scheduled runs need a time limit per run: a whole number of minutes from 1 to 1,440.' }, 400)
        : plain(path, options)"""
    region = open_schedules(ui, page, setup)
    region.get_by_role("button", name="Edit Nightly tests").click()
    form = region.get_by_role("form", name="Edit schedule")
    expect(form.get_by_role("textbox", name="Name")).to_have_value("Nightly tests")
    expect(form.get_by_role("spinbutton", name="Spending limit per run (DBU)")).to_have_value("2")
    # The preview of an edit names the schedule, so an interval that keeps its timing shows its saved times.
    expect(form.get_by_role("status")).to_contain_text("Next runs (UTC): ")
    assert ui.bodies("/api/schedules/preview", "POST")[-1] == {"spec": "0 2 * * *", "timezone": "", "schedule_id": "s1"}
    form.get_by_role("button", name="Save schedule").click()
    expect(region.get_by_role("alert")).to_have_text("Scheduled runs need a time limit per run: a whole number of minutes from 1 to 1,440.")
    assert ui.bodies("/api/schedules/s1?session_id=a", "PUT")[0]["approval_timeout_minutes"] == 10


def test_off_in_settings_and_the_token_warning_are_said(ui, page):
    region = open_schedules(ui, page, state(enabled=False, warning="Your Databricks token expires in 3 days (2027-01-18)."))
    expect(region.get_by_text("Scheduled runs are off in Settings → Agent: schedules do not run at their times. Run now still works.")).to_be_visible()
    expect(region.get_by_text("Your Databricks token expires in 3 days (2027-01-18).")).to_be_visible()


# The app's banners

READY = """fake.settings = { ...fake.settings, schedules_enabled: true }
  const plain = fake.override
  fake.override = (path, options) => {
    if (path === '/api/schedules/ready') return fake.json(window.readyState)
    if (path === '/api/schedules/s1/run') { window.readyState = { ready: [], paused: [] }; return fake.json({ session_id: 'a', schedules: [] }) }
    if (path === '/api/schedules/s1/skip') { window.readyState = { ready: [], paused: [] }; return fake.json({ schedules: [] }) }
    return plain(path, options)
  }
  window.readyState = { ready: [{ id: 's1', name: 'Nightly tests', workspace: '/project', due: 1, note: null }],
    paused: [{ id: 's2', name: 'Weekly report', workspace: '/project' }] }"""


def test_a_ready_run_banner_runs_it_and_opens_its_chat(ui, page):
    start_app(ui, page, setup=READY)
    banner = page.locator(".schedule-alert").filter(has_text="Scheduled run ready")
    expect(banner).to_have_text(re.compile("Scheduled run ready: Nightly tests"))
    expect(page.get_by_role("alert").filter(has_text="Scheduled runs paused: Databricks rejected the token (Weekly report)")).to_be_visible()
    banner.get_by_role("button", name="Run Nightly tests").click()
    expect(banner).to_have_count(0)
    expect(page).to_have_url(re.compile(r"[?&]session=a\b"))
    assert ui.js("fake.requests('/api/schedules/s1/run', 'POST').length") == 1


def test_a_ready_run_can_be_skipped_and_nothing_is_asked_with_scheduled_runs_off(ui, page):
    start_app(ui, page, setup=READY)
    page.get_by_role("button", name="Skip Nightly tests").click()
    expect(page.locator(".schedule-alert").filter(has_text="Scheduled run ready")).to_have_count(0)
    assert ui.js("fake.requests('/api/schedules/s1/skip', 'POST').length") == 1


def test_no_schedule_requests_while_scheduled_runs_are_off(ui, page):
    start_app(ui, page)
    expect(page.locator(".schedule-alert")).to_have_count(0)
    assert not ui.js("fake.calls.some(call => call.path.startsWith('/api/schedules'))")


def test_the_token_expiry_banner_opens_settings_and_can_be_dismissed(ui, page):
    warning = "Your Databricks token expires tomorrow (2027-01-16). Scheduled runs stop working when it expires."
    start_app(ui, page, setup=f"fake.settings = {{ ...fake.settings, token_warning: {json.dumps(warning)} }}")
    banner = page.locator(".token-banner")
    expect(banner).to_contain_text(warning)
    banner.get_by_role("button", name="Open settings").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    page.get_by_role("button", name="Cancel").click()
    banner.get_by_role("button", name="Dismiss").click()
    expect(banner).to_have_count(0)


def test_settings_save_scheduled_runs_and_the_token_expiry(ui, page):
    start_app(ui, page, setup="""fake.override = (path, options) => path === '/api/settings' && options.method === 'PUT'
      ? fake.json({ ...fake.settings, ...JSON.parse(options.body) }) : undefined""")
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.get_by_role("dialog")
    toggle = dialog.get_by_role("checkbox", name="Scheduled runs at their times")
    expect(toggle).not_to_be_checked()
    toggle.check()
    dialog.get_by_label("Token expires on").fill("2027-01-31")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    body = ui.bodies("/api/settings", "PUT")[0]
    assert body["schedules_enabled"] is True and body["token_expires_on"] == "2027-01-31"


def test_the_panel_and_its_form_fit_a_phone(ui, page):
    page.set_viewport_size({"width": 390, "height": 844})
    region = open_schedules(ui, page)
    region.get_by_role("button", name="Edit Nightly tests").click()
    expect(region.get_by_role("form", name="Edit schedule")).to_be_visible()
    region.get_by_text("History (3)").click()
    assert ui.js("(() => { const body = document.querySelector('.agent-tools-body'); return body.scrollWidth <= body.clientWidth + 1 })()")
    box = region.get_by_role("spinbutton", name="Token limit per run").bounding_box()
    assert box["width"] > 200  # One field per row on a phone.
