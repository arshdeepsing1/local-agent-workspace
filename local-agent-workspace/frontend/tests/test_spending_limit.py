"""A chat's spending limit in Agent tools → Usage (its own value or the Settings default, the share
used, editing it), a subagent's view of its parent's limit, and the Settings default field."""
import re

from playwright.sync_api import expect


def spending(**values):
    base = {"limit_dbu": 2, "source": "settings", "spent_dbu": 1.7, "own_dbu": 1.2, "subagents_dbu": 0.5,
            "unpriced_calls": 0, "percent": 85}
    return {**base, **values}


def open_usage(ui, page, value, selected="session()"):
    import json
    setup = f"""window.spendingState = {json.dumps(value)}
      fake.override = (path, options) => {{
        if (path === '/api/sessions/a/spending-limit') {{
          const limit = JSON.parse(options.body).spending_limit_dbu
          spendingState = limit == null ? {{ ...spendingState, source: 'settings', limit_dbu: 2 }}
            : {{ ...spendingState, source: 'chat', limit_dbu: limit || null, percent: limit ? Math.round(spendingState.spent_dbu * 1000 / limit) / 10 : null }}
          return fake.json({{ ...session(), spending_limit_dbu: limit ?? undefined }})
        }}
        if (path.endsWith('/metrics')) return fake.json({{ ...metrics, spending: spendingState }})
      }}"""
    ui.mount("components/AgentToolsDialog.js", f"{{ ...dialogProps({selected}) }}", scenario="agent_tools", setup=setup)
    page.get_by_role("button", name="Usage", exact=True).click()
    return page.get_by_role("group", name="Spending limit")


def test_usage_shows_the_settings_limit_and_saves_a_chat_limit(ui, page):
    group = open_usage(ui, page, spending())
    expect(group).to_contain_text("1.7 of 2 DBU")
    expect(group).to_contain_text("85%")
    expect(group).to_contain_text("The default from Settings → Agent.")
    expect(group).to_contain_text("Includes 0.5 DBU from subagents.")
    expect(group.get_by_role("meter")).to_have_attribute("aria-label", "Estimated spend: 85% of the limit")
    default = group.get_by_role("button", name="Use Settings default")
    expect(default).to_be_disabled()
    field = group.get_by_role("spinbutton", name="Limit for this chat (DBU, 0 = none)")
    expect(field).to_have_value("2")
    field.fill("-1")
    group.get_by_role("button", name="Save limit").click()
    expect(group.get_by_role("alert")).to_have_text("Enter a number of DBU from 0 (no limit) to 100,000.")
    assert ui.requests("/api/sessions/a/spending-limit") == []
    field.fill("4")
    group.get_by_role("button", name="Save limit").click()
    expect(group).to_contain_text("1.7 of 4 DBU")
    expect(group).to_contain_text("This chat's own limit.")
    assert ui.bodies("/api/sessions/a/spending-limit", "PUT") == [{"spending_limit_dbu": 4}]
    expect(default).to_be_enabled()
    default.click()
    expect(group).to_contain_text("The default from Settings → Agent.")
    assert ui.bodies("/api/sessions/a/spending-limit", "PUT")[-1] == {"spending_limit_dbu": None}
    field.fill("0")
    group.get_by_role("button", name="Save limit").click()
    expect(group).to_contain_text("1.7 DBU · no limit")
    expect(group).to_contain_text("No limit for this chat.")
    expect(group.get_by_role("meter")).to_have_count(0)


def test_a_reached_limit_and_unpriced_calls_are_explained(ui, page):
    group = open_usage(ui, page, spending(source="chat", spent_dbu=2.4, percent=120, unpriced_calls=2, subagents_dbu=0))
    expect(group).to_contain_text("2.4 of 2 DBU")
    expect(group).to_contain_text("2 calls to endpoints without price data are not counted.")
    expect(group.get_by_role("status")).to_have_text(
        "The agent stops before its next model request until you raise the limit.")
    expect(group).not_to_contain_text("from subagents")


def test_a_subagent_shows_its_parent_limit_without_an_editor(ui, page):
    group = open_usage(ui, page, spending(), selected="{ ...session(), is_subagent: true, parent_session_id: 'p' }")
    expect(group).to_contain_text("Follows its parent conversation's limit")
    expect(group.get_by_role("spinbutton")).to_have_count(0)
    expect(group.get_by_role("button")).to_have_count(0)


def test_no_limit_by_default(ui, page):
    group = open_usage(ui, page, spending(limit_dbu=None, percent=None, spent_dbu=0, subagents_dbu=0))
    expect(group).to_contain_text("0 DBU · no limit")
    expect(group).to_contain_text("No limit (Settings → Agent sets a default for every chat).")
    expect(group.get_by_role("spinbutton")).to_have_value("0")


def test_the_overview_names_a_chat_limit(ui, page):
    ui.mount("components/AgentToolsDialog.js", "{ ...dialogProps({ ...session(), spending_limit_dbu: 3.5 }) }",
             scenario="agent_tools")
    expect(page.get_by_role("region", name="Overview")).to_contain_text("Spending limit: 3.5 DBU (this chat)")


def test_settings_save_a_default_limit(ui, page):
    ui.app(scenario="workspace")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.get_by_role("dialog")
    field = dialog.get_by_role("spinbutton", name="Spending limit per conversation (DBU)")
    expect(field).to_have_value("0")
    expect(dialog.get_by_text(re.compile("warns at 80%. 0 means no limit"))).to_be_visible()
    field.fill("12.5")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["spending_limit_dbu"] == 12.5


def test_settings_take_a_typed_decimal_limit(ui, page):
    ui.app(scenario="workspace")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.get_by_role("dialog")
    field = dialog.get_by_role("spinbutton", name="Spending limit per conversation (DBU)")
    field.select_text()
    field.press_sequentially("2.5")  # Key by key: "2." must not be rewritten to "2".
    expect(field).to_have_value("2.5")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["spending_limit_dbu"] == 2.5


def test_a_limit_changed_elsewhere_refreshes_the_open_panel(ui, page):
    group = open_usage(ui, page, spending())
    expect(group).to_contain_text("1.7 of 2 DBU")
    ui.run("spendingState = { ...spendingState, source: 'chat', limit_dbu: 3, percent: 56.7 }")
    ui.rerender("{ ...dialogProps({ ...session(), spending_limit_dbu: 3 }) }")
    expect(group).to_contain_text("1.7 of 3 DBU")
    expect(group).to_contain_text("This chat's own limit.")
