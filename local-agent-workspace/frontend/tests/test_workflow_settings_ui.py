"""Settings → Subagents: how many subagents run at once and how many one chat may start in one turn."""
import re

from playwright.sync_api import expect


def open_subagents(ui, page, setup=""):
    ui.app(scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("navigation", name="Settings sections").get_by_role("button", name="Subagents").click()
    return dialog, dialog.get_by_role("group", name="Subagents", exact=True)


def test_the_limits_show_their_defaults_and_the_rate_limit_hint(ui, page):
    dialog, group = open_subagents(ui, page)
    expect(group.get_by_label("Subagents at once")).to_have_value("3")
    expect(group.get_by_label("Subagents per turn")).to_have_value("6")
    expect(group.get_by_text(re.compile(
        "More subagents at once means a smaller share of the endpoint's rate limit for each request"))).to_be_visible()
    expect(group.get_by_label("Subagents at once")).to_have_attribute("min", "1")
    expect(group.get_by_label("Subagents at once")).to_have_attribute("max", "8")
    expect(group.get_by_label("Subagents per turn")).to_have_attribute("max", "24")


def test_saved_values_show_and_changes_are_sent(ui, page):
    dialog, group = open_subagents(
        ui, page, "fake.settings = { ...fake.settings, subagents_running_max: 5, subagents_per_turn_max: 12 }")
    expect(group.get_by_label("Subagents at once")).to_have_value("5")
    expect(group.get_by_label("Subagents per turn")).to_have_value("12")
    group.get_by_label("Subagents at once").fill("2")
    group.get_by_label("Subagents per turn").fill("4")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    body = ui.bodies("/api/settings", "PUT")[0]
    assert body["subagents_running_max"] == 2 and body["subagents_per_turn_max"] == 4


def test_a_save_without_the_fields_in_the_settings_sends_the_defaults_shown(ui, page):
    dialog, group = open_subagents(ui, page)
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    body = ui.bodies("/api/settings", "PUT")[0]
    assert body.get("subagents_running_max") in (None, 3) and body.get("subagents_per_turn_max") in (None, 6)
