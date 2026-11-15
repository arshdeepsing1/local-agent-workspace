"""The choice before an automatic compaction (FR-028): the meter's note near the threshold, the waiting card with its
two buttons, the hint under the message box, the tab-title count and Settings > Ask before compacting."""
import json

from playwright.sync_api import expect

INFO = """{ estimated_tokens: 52000, input_budget: 59392, context_window: 65536, reply_reserve: 4096,
  compactions: 0, summarized_messages: 0, estimate_method: 'weighted_utf8', instruction_files: [], warnings: [],
  percent_used: 79, compact_at_percent: 82, is_estimate: true, compactable: true }"""
TITLE = "addEventListener('DOMContentLoaded', () => { document.title = 'Local' })\n"
# The answers the fake server gives to the two routes under test.
ROUTES = """
const base = fake.handle
fake.handle = (path, options) => /\\/(compact|compaction-choice)$/.test(path) ? fake.json({ ok: true }) : base(path, options)
"""
USER = {"id": "u1", "type": "user", "text": "Please go on with the next part.", "in_wire": True}


def card(state="pending", answer=None, **fields):
    return {"id": "choice", "type": "notice", "state": state, "created": 1,
            "text": "This chat is at 91% of its context budget, and automatic compaction starts at 90%.",
            "compaction_choice": {"percent_used": 91, "compact_at_percent": 90, "options": ["compact_now", "continue"],
                                  "answer": answer, **fields}}


def waiting_chat(events=None, status="awaiting_approval"):
    """An open chat whose turn waits for the choice."""
    events = [USER, card()] if events is None else events
    return f"""{TITLE}{ROUTES}
fake.sessions.set('a', {{ ...makeSession('a', 'Long chat'), status: {json.dumps(status)}, events: {json.dumps(events)},
  context_info: {INFO} }})"""


def choices(ui):
    return [json.loads(call["body"]) for call in ui.requests("/api/sessions/a/compaction-choice", "POST")]


# The meter's note

def test_the_meter_says_it_is_close_and_offers_compact_now(ui, page):
    ui.mount("components/ContextMeter.js", f"{{ info: {{ ...{INFO}, warn: 'near' }}, onCompact: fake.spy('compact', async () => {{}}) }}",
             setup="fake.handle = () => fake.json({ ok: true })")
    note = page.locator(".context-near")
    expect(note).to_contain_text("Close to automatic compaction")
    button = note.get_by_role("button", name="Compact now")
    expect(button).to_be_visible()
    button.click()
    ui.wait("(fake.spies.compact || []).length === 1")
    assert ui.spy("compact") == [[""]]  # The same action as the Compact now button of the details: no note.


def test_there_is_no_note_below_the_mark_or_while_a_turn_runs(ui, page):
    ui.mount("components/ContextMeter.js", f"{{ info: {{ ...{INFO}, warn: 'none' }}, onCompact: async () => {{}} }}")
    expect(page.locator("details.context-meter")).to_be_visible()
    expect(page.locator(".context-near")).to_have_count(0)
    ui.rerender(f"{{ info: {{ ...{INFO}, warn: 'near' }}, onCompact: async () => {{}}, busy: true }}")
    expect(page.locator(".context-near")).to_have_count(0)
    ui.rerender(f"{{ info: {{ ...{INFO}, warn: 'near' }}, onCompact: async () => {{}}, status: 'awaiting_approval' }}")
    expect(page.locator(".context-near")).to_have_count(0)


def test_at_the_threshold_the_note_says_the_next_message_starts_it(ui, page):
    ui.mount("components/ContextMeter.js", f"{{ info: {{ ...{INFO}, warn: 'at_threshold' }}, onCompact: async () => {{}} }}")
    expect(page.locator(".context-near")).to_contain_text("Your next message starts automatic compaction")
    expect(page.locator(".context-near").get_by_role("button", name="Compact now")).to_be_visible()


# The card

def test_the_waiting_card_shows_the_numbers_and_two_buttons_and_posts_the_choice(ui, page):
    ui.app(session="a", scenario="workspace", setup=waiting_chat())
    ui.show_session("a")
    region = page.get_by_role("region", name="Compact before this message?")
    expect(region).to_be_visible()
    expect(region).to_contain_text("91%")
    expect(region).to_contain_text("90%")
    compact, keep = region.get_by_role("button", name="Compact now"), region.get_by_role("button", name="Continue without compacting")
    expect(compact).to_be_visible()
    expect(keep).to_be_visible()
    expect(region).to_contain_text("Compact now summarizes earlier turns")
    expect(region).to_contain_text("Continue without compacting sends your message as it is")
    keep.click()
    ui.wait("fake.calls.some(call => call.path.endsWith('/compaction-choice'))")
    assert choices(ui) == [{"choice": "continue"}]


def test_compact_now_posts_compact_now(ui, page):
    ui.app(session="a", scenario="workspace", setup=waiting_chat())
    ui.show_session("a")
    page.get_by_role("region", name="Compact before this message?").get_by_role("button", name="Compact now").click()
    ui.wait("fake.calls.some(call => call.path.endsWith('/compaction-choice'))")
    assert choices(ui) == [{"choice": "compact_now"}]


def test_the_card_goes_when_the_choice_is_answered_and_the_message_stays(ui, page):
    ui.app(session="a", scenario="workspace", setup=waiting_chat())
    ui.show_session("a")
    expect(page.get_by_role("region", name="Compact before this message?")).to_be_visible()
    ui.emit("a", {"type": "event", "event": card("completed", "continue")})
    ui.emit("a", {"type": "status", "status": "running"})
    expect(page.get_by_role("region", name="Compact before this message?")).to_have_count(0)
    expect(page.get_by_text("Please go on with the next part.")).to_be_visible()
    expect(page.locator(".composer-hint")).to_have_count(0)


def test_a_card_that_was_cancelled_or_expired_shows_no_buttons(ui, page):
    for state, answer in (("cancelled", None), ("completed", "expired")):
        ui.app(session="a", scenario="workspace", setup=waiting_chat([USER, card(state, answer)], status="idle"))
        ui.show_session("a")
        expect(page.get_by_text("Please go on with the next part.")).to_be_visible()
        expect(page.get_by_role("region", name="Compact before this message?")).to_have_count(0)
        expect(page.get_by_role("button", name="Continue without compacting")).to_have_count(0)


def test_an_error_from_the_route_is_shown_and_the_buttons_come_back(ui, page):
    ui.app(session="a", scenario="workspace", setup=waiting_chat() + """
fake.override = (path, options) => path.endsWith('/compaction-choice') ? new Response(JSON.stringify({ detail: 'No compaction choice is waiting for an answer.' }), { status: 400, headers: { 'content-type': 'application/json' } }) : undefined""")
    ui.show_session("a")
    region = page.get_by_role("region", name="Compact before this message?")
    region.get_by_role("button", name="Compact now").click()
    expect(page.get_by_text("No compaction choice is waiting for an answer.")).to_be_visible()
    expect(region.get_by_role("button", name="Compact now")).to_be_enabled()


# The hint, the title and the sidebar

def test_the_hint_under_the_message_box_names_the_choice_and_show_focuses_it(ui, page):
    ui.app(session="a", scenario="workspace", setup=waiting_chat())
    ui.show_session("a")
    hint = page.locator(".composer-hint")
    expect(hint.get_by_role("status")).to_have_text("A choice about compacting is waiting for you above.")
    hint.get_by_role("button", name="Show", exact=True).click()
    expect(page.get_by_role("region", name="Compact before this message?").get_by_role("button", name="Compact now")).to_be_focused()


def test_the_tab_title_and_the_sidebar_count_the_waiting_choice_like_any_card(ui, page):
    setup = waiting_chat() + """
fake.sessions.set('b', { ...makeSession('b', 'Another chat'), status: 'idle' })"""
    ui.app(session="a", scenario="workspace", setup=setup)
    ui.show_session("a")
    expect(page).to_have_title("(1) Local")
    ui.emit("a", {"type": "event", "event": card("completed", "compact_now")})
    ui.run("fake.sessions.set('a', { ...fake.sessions.get('a'), status: 'idle' })")
    ui.emit("a", {"type": "status", "status": "idle"})
    expect(page).to_have_title("Local")


def test_a_chat_that_waits_for_the_choice_is_marked_in_the_list(ui, page):
    setup = TITLE + ROUTES + """
fake.sessions = new Map([['a', { ...makeSession('a', 'Waiting chat'), status: 'awaiting_approval' }],
  ['b', { ...makeSession('b', 'Open chat'), status: 'idle' }]])"""
    ui.app(session="b", scenario="workspace", setup=setup)
    expect(page.get_by_role("button", name="Waiting chat Needs approval")).to_be_visible()


# Settings

def open_settings(ui, page, setup=""):
    ui.app(scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    page.get_by_role("button", name="Settings", exact=True).click()
    return page.get_by_role("dialog")


def test_settings_has_the_ask_checkbox_on_by_default_next_to_the_compaction_settings(ui, page):
    dialog = open_settings(ui, page)
    ask = dialog.get_by_role("checkbox", name="Ask before compacting a long chat")
    expect(ask).to_be_checked()
    order = dialog.locator("label").all_inner_texts()
    assert order.index("Ask before compacting a long chat") == order.index("Save a detailed handoff at each compaction") - 1
    expect(dialog.get_by_text("a card lets you compact first or continue without compacting")).to_be_visible()


def test_the_ask_checkbox_is_saved(ui, page):
    dialog = open_settings(ui, page, "fake.settings = { ...fake.settings, compaction_ask: true }")
    dialog.get_by_role("checkbox", name="Ask before compacting a long chat").uncheck()
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    [saved] = ui.bodies("/api/settings", "PUT")
    assert saved["compaction_ask"] is False


def test_a_start_mark_that_is_not_below_the_compaction_threshold_gets_a_hint_not_an_error(ui, page):
    dialog = open_settings(ui, page, "fake.settings = { ...fake.settings, auto_compact_percent: 60, context_trim_start_percent: 70 }")
    hint = "Trimming starts 5 points below the compaction threshold."
    expect(dialog.get_by_text(hint)).to_be_visible()
    dialog.get_by_role("spinbutton", name="Start trimming at (% of input budget)").fill("50")
    expect(dialog.get_by_text(hint)).to_have_count(0)
    dialog.get_by_role("spinbutton", name="Auto-compact at (% of input budget)").fill("90")
    dialog.get_by_role("spinbutton", name="Start trimming at (% of input budget)").fill("70")
    expect(dialog.get_by_text(hint)).to_have_count(0)
    # Both marks are checked for their own range only: 85 is fine for the target whatever the start is.
    target = dialog.get_by_role("spinbutton", name="Trim down to (% of input budget)")
    expect(target).to_have_attribute("max", "85")
