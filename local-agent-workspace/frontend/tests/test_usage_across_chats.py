"""Agent tools → Usage: a chat's turn limits (its own or the Settings defaults, saving and clearing them, a
subagent following its parent's) and the All chats view (GET /api/usage: totals, the day chart and its table, the
groups, older chats not counted yet, the list prices)."""
import json
import re

from playwright.sync_api import expect


def limits(**values):
    return {"turn_token_limit": None, "token_source": "settings", "turn_time_limit_minutes": None, "time_source": "settings",
            "follows_parent": False, **values}


def open_usage(ui, page, value, selected="session()", usage=None):
    setup = f"""window.limitsState = {json.dumps(value)}
      window.usageState = {json.dumps(usage)}
      fake.override = (path, options) => {{
        if (path === '/api/sessions/a/turn-limits') {{
          const body = JSON.parse(options.body)
          const own = key => body[key] != null
          limitsState = {{ ...limitsState,
            turn_token_limit: own('turn_token_limit') ? body.turn_token_limit || null : null,
            token_source: own('turn_token_limit') ? 'chat' : 'settings',
            turn_time_limit_minutes: own('turn_time_limit_minutes') ? body.turn_time_limit_minutes || null : null,
            time_source: own('turn_time_limit_minutes') ? 'chat' : 'settings' }}
          return fake.json(session())
        }}
        if (path.endsWith('/metrics')) return fake.json({{ ...metrics, turn_limits: limitsState }})
        if (path.startsWith('/api/usage')) return fake.json(path.endsWith('days=90') ? {{ ...usageState, days: 90, since: '2026-07-19' }} : usageState)
      }}"""
    ui.mount("components/AgentToolsDialog.js", f"{{ ...dialogProps({selected}) }}", scenario="agent_tools", setup=setup)
    page.get_by_role("button", name="Usage", exact=True).click()
    return page.get_by_role("group", name="Turn limits")


def test_turn_limits_show_the_defaults_and_save_the_chats_own(ui, page):
    group = open_usage(ui, page, limits(turn_time_limit_minutes=30))
    expect(group).to_contain_text("30 min")
    expect(group).to_contain_text("Time from Settings → Agent. At a limit the agent gets one last request")
    default = group.get_by_role("button", name="Use Settings defaults")
    expect(default).to_be_disabled()
    tokens = group.get_by_role("spinbutton", name="Tokens per turn (0 = none)")
    minutes = group.get_by_role("spinbutton", name="Minutes per turn (0 = none)")
    expect(minutes).to_have_value("30")
    tokens.fill("2.5")
    group.get_by_role("button", name="Save turn limits").click()
    expect(group.get_by_role("alert")).to_contain_text("Enter whole numbers")
    assert ui.requests("/api/sessions/a/turn-limits") == []
    tokens.fill("500000")
    minutes.fill("0")
    group.get_by_role("button", name="Save turn limits").click()
    expect(group).to_contain_text("500,000 tokens")
    expect(group).to_contain_text("Tokens from this chat.")
    assert ui.bodies("/api/sessions/a/turn-limits", "PUT") == [{"turn_token_limit": 500000, "turn_time_limit_minutes": 0}]
    expect(default).to_be_enabled()
    default.click()
    expect(group).to_contain_text("None")
    assert ui.bodies("/api/sessions/a/turn-limits", "PUT")[-1] == {"turn_token_limit": None, "turn_time_limit_minutes": None}


def test_a_subagent_follows_its_parents_turn_limits(ui, page):
    group = open_usage(ui, page, limits(turn_token_limit=100000, follows_parent=True),
                       selected="{ ...session(), is_subagent: true, parent_session_id: 'p' }")
    expect(group).to_contain_text("100,000 tokens")
    expect(group).to_contain_text("Follows its parent conversation's turn limits")
    expect(group.get_by_role("spinbutton")).to_have_count(0)


def day(date, calls=0, **values):
    row = {"date": date, "calls": calls, "input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
           "output_tokens": 0, "estimated_dbu": 0.0, "unpriced_calls": 0}
    return {**row, **values}


def usage(**values):
    totals = day("", 5, input_tokens=3000, cache_read_input_tokens=40000, cache_creation_input_tokens=1000, output_tokens=900,
                 estimated_dbu=0.42, unpriced_calls=1)
    totals.pop("date")
    base = {
        "days": 7, "since": "2026-10-10", "until": "2026-10-16", "totals": {**totals, "chats": 2}, "pending_chats": 0,
        "by_day": [day(f"2026-10-{index}") for index in range(10, 15)]
        + [day("2026-10-15", 2, input_tokens=1000, cache_read_input_tokens=30000, output_tokens=300, estimated_dbu=0.3),
           day("2026-10-16", 3, input_tokens=2000, cache_read_input_tokens=10000, cache_creation_input_tokens=1000,
               output_tokens=600, estimated_dbu=0.12, unpriced_calls=1)],
        "by_model": [{"key": "databricks-claude-opus-5-5", "priced": True, **day("", 4, input_tokens=2990, output_tokens=890,
                                                                               estimated_dbu=0.42)},
                     {"key": "team-endpoint", "priced": False, **day("", 1, input_tokens=10, output_tokens=10, unpriced_calls=1)}],
        "by_purpose": [{"key": "agent", "label": "Agent", **day("", 5, input_tokens=3000, output_tokens=900, estimated_dbu=0.42)}],
        "by_project": [{"key": "/project/a", "name": "a", **day("", 5, input_tokens=3000, output_tokens=900, estimated_dbu=0.42)}],
        "pricing": {"effective_at": "2026-10-16", "source_url": "https://example.com/proprietary",
                    "open_source_url": "https://example.com/open",
                    "models": {"databricks-claude-opus-5-5": {"input_tokens": 57.143, "output_tokens": 285.714,
                                                              "cache_read_input_tokens": 2.857, "cache_creation_input_tokens": 71.429},
                               "databricks-gpt-oss-120b": {"input_tokens": 2.143, "output_tokens": 8.571}}},
    }
    for entries in ("by_model", "by_purpose", "by_project"):
        for entry in base[entries]:
            entry.pop("date")
    return {**base, **values}


def test_all_chats_shows_totals_days_groups_and_prices(ui, page):
    open_usage(ui, page, limits(), usage=usage())
    page.get_by_role("button", name="All chats").click()
    section = page.get_by_role("region", name="Usage")
    expect(page.get_by_role("heading", name="Usage across chats")).to_be_visible()
    cards = section.locator(".usage-cards")
    expect(cards).to_contain_text("Chats with calls2")
    expect(cards).to_contain_text("Cache read40,000")
    expect(cards).to_contain_text("Estimated cost0.42 DBU")
    expect(cards).to_contain_text("Calls without price data1")
    assert ui.requests("/api/usage?days=30") != []
    chart = section.get_by_role("img", name="Tokens by day, all chats")
    expect(chart).to_be_visible()
    # Each day is a hit target with its values; hovering one shows them, the table lists every day with calls.
    bar = section.get_by_label("Oct 15: 31,300 tokens, 0.3 DBU")
    bar.hover()
    tooltip = section.locator(".usage-day-tooltip")
    expect(tooltip).to_contain_text("30,000 cache read")
    expect(tooltip).to_contain_text("1,000 uncached + cache write")
    section.get_by_text("Show days as a table").click()
    rows = section.locator(".usage-day-table tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("2026-10-16")
    expect(rows.first).to_contain_text("3,000")  # Uncached input and cache write.
    endpoints = section.locator(".usage-breakdown").filter(has_text="By endpoint")
    expect(endpoints.get_by_role("row", name="team-endpoint")).to_contain_text("No price data")
    expect(section.locator(".usage-breakdown").filter(has_text="By project")).to_contain_text("a")
    section.get_by_text("List prices (DBU per million tokens, 2026-10-16)").click()
    prices = section.locator(".usage-prices tbody tr")
    expect(prices.filter(has_text="databricks-gpt-oss-120b")).to_contain_text("—")
    # Another period asks again; This chat goes back to the chat's own usage.
    expect(section.locator(".usage-range")).to_have_text("Oct 10 – Oct 16")
    section.get_by_label("Period").select_option("90")
    expect(section.locator(".usage-range")).to_have_text("Jul 19 – Oct 16")
    assert len(ui.requests("/api/usage?days=90")) == 1
    page.get_by_role("button", name="This chat").click()
    expect(page.get_by_role("group", name="Turn limits")).to_be_visible()


def test_all_chats_says_when_older_chats_are_not_counted_and_when_there_are_no_calls(ui, page):
    empty = usage(pending_chats=3, totals={**usage()["totals"], "calls": 0, "chats": 0})
    open_usage(ui, page, limits(), selected="null", usage=empty)
    expect(page.get_by_text("Start a conversation to track its model usage.")).to_be_visible()
    page.get_by_role("button", name="All chats").click()
    expect(page.get_by_text("3 older chats are not counted yet: Refresh usage counts the next ones.")).to_be_visible()
    expect(page.get_by_text("No model calls in the last 7 days.")).to_be_visible()
    expect(page.get_by_role("img", name="Tokens by day, all chats")).to_have_count(0)
    assert ui.requests("/api/sessions/null/metrics") == []


def test_a_turn_limit_changed_elsewhere_refreshes_the_open_panel(ui, page):
    group = open_usage(ui, page, limits())
    expect(group).to_contain_text("None")
    ui.run("limitsState = { ...limitsState, turn_token_limit: 75000, token_source: 'chat' }")
    ui.rerender("{ ...dialogProps({ ...session(), turn_token_limit: 75000 }) }")  # A pushed turn_limits message.
    expect(group).to_contain_text("75,000 tokens")
    expect(group).to_contain_text("Tokens from this chat.")


def test_settings_save_default_turn_limits(ui, page):
    ui.app(scenario="workspace")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.get_by_role("dialog")
    tokens = dialog.get_by_role("spinbutton", name="Token limit per turn")
    minutes = dialog.get_by_role("spinbutton", name="Time limit per turn (minutes)")
    expect(tokens).to_have_value("0")
    expect(minutes).to_have_value("0")
    expect(dialog.get_by_text(re.compile("one last request to summarize what is done and what remains"))).to_be_visible()
    tokens.fill("2000000")
    minutes.fill("45")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    body = ui.bodies("/api/settings", "PUT")[0]
    assert (body["turn_token_limit"], body["turn_time_limit_minutes"]) == (2000000, 45)
