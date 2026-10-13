"""Question cards (ask_user) and plan cards (submit_plan) in the conversation."""
import json
import re

import pytest
from playwright.sync_api import expect

SETUP = """
window.makeSession = (events, overrides = {}) => ({
  id: 'parent', title: 'Parent task', workspace: '/project', model: 'test-model', status: 'idle',
  events, updated: 1, permission_mode: 'plan', allowed_directories: [], ...overrides,
})
fake.handle = () => fake.json({ ok: true })
"""
QUESTIONS = [
    {"question": "Which database?", "header": "Storage",
     "options": [{"label": "SQLite", "description": "One local file"}, {"label": "Postgres"}]},
    {"question": "Which checks should run?", "options": [{"label": "Lint"}, {"label": "Tests"}, {"label": "Types"}],
     "multi_select": True},
]
PLAN = "# Storage plan\\n\\n- Add the schema\\n- Write the migration\\n\\nRun `make test`."


def question_event(state="pending", questions=QUESTIONS, **fields):
    return json.dumps({"id": "ask", "type": "tool", "name": "ask_user", "state": state,
                       "input": {"questions": questions}, "output": "", **fields})


def plan_event(state="pending", plan=PLAN, **fields):
    return (f"{{ id: 'plan', type: 'tool', name: 'submit_plan', state: '{state}', input: {{ plan: \"{plan}\", "
            f"title: 'Storage plan' }}, output: '', ...{json.dumps(fields)} }}")


def show(ui, events: str, setup=""):
    ui.mount("components/Conversation.js", f"{{ session: makeSession({events}), onError: fake.spy('onError') }}",
             setup=SETUP + setup)


def sent(ui, path):
    return [json.loads(call["body"]) for call in ui.requests(path, "POST")]


# Question cards

def test_single_and_multi_select_answers_with_other(ui, page):
    show(ui, f"[{question_event()}]")
    card = page.get_by_role("region", name="Local has 2 questions")
    expect(card.get_by_role("heading", name="Local has 2 questions")).to_be_visible()
    first = card.get_by_role("group", name="Storage Which database?")
    expect(first).to_be_visible()
    assert first.locator("legend").evaluate("e => e.firstElementChild.textContent") == "Storage"  # The chip comes first.
    expect(first.get_by_role("radio", name="SQLite", exact=True)).to_have_accessible_description("One local file")
    expect(card.get_by_text("Choose one or more.")).to_have_count(1)
    send = card.get_by_role("button", name="Send answers")
    expect(send).to_be_disabled()
    first.get_by_role("radio", name="SQLite", exact=True).check()
    expect(send).to_be_disabled()  # The second question is still open.
    second = card.get_by_role("group", name="Which checks should run?")
    second.get_by_role("checkbox", name="Lint").check()
    second.get_by_role("checkbox", name="Tests").check()
    expect(send).to_be_enabled()
    other = second.get_by_role("textbox", name="Other answer for: Which checks should run?")
    expect(other).to_be_disabled()
    assert other.get_attribute("maxlength") == "1000"
    second.get_by_role("checkbox", name="Other").check()
    expect(other).to_be_enabled()
    expect(other).to_be_focused()
    expect(send).to_be_disabled()  # A chosen Other needs its text.
    page.keyboard.type("  Docs links  ")
    expect(send).to_be_enabled()
    send.click()
    ui.wait("fake.calls.length === 1")
    assert sent(ui, "/api/sessions/parent/questions/ask") == [{"answers": [
        {"selected": ["SQLite"]}, {"selected": ["Lint", "Tests"], "other": "Docs links"}]}]


def test_other_replaces_a_single_select_option(ui, page):
    show(ui, f"[{question_event(questions=QUESTIONS[:1])}]")
    card = page.get_by_role("region", name="Local has a question")
    card.get_by_role("radio", name="Postgres").check()
    card.get_by_role("radio", name="Other").check()
    expect(card.get_by_role("radio", name="Postgres")).not_to_be_checked()
    send = card.get_by_role("button", name="Send answers")
    expect(send).to_be_disabled()
    card.get_by_role("textbox", name="Other answer for: Which database?").fill("DuckDB")
    send.click()
    ui.wait("fake.calls.length === 1")
    assert sent(ui, "/api/sessions/parent/questions/ask") == [{"answers": [{"selected": [], "other": "DuckDB"}]}]


def test_skip_sends_skip(ui, page):
    show(ui, f"[{question_event()}]")
    page.get_by_role("button", name="Skip").click()
    ui.wait("fake.calls.length === 1")
    assert sent(ui, "/api/sessions/parent/questions/ask") == [{"skip": True}]


def test_buttons_wait_for_the_server_and_errors_are_shown(ui, page):
    held = "const answer = fake.defer('answer'); fake.override = path => path.includes('/questions/') ? answer.promise : undefined"
    show(ui, f"[{question_event(questions=QUESTIONS[:1])}]", setup=held)
    page.get_by_role("radio", name="SQLite").check()
    page.get_by_role("button", name="Send answers").click()
    expect(page.get_by_role("button", name="Send answers")).to_be_disabled()
    expect(page.get_by_role("button", name="Skip")).to_be_disabled()
    ui.run("fake.deferreds.answer.resolve(fake.json({ detail: 'This question is no longer waiting for an answer.' }, 400))")
    ui.wait("(fake.spies.onError || []).length === 1")
    assert ui.spy("onError") == [["This question is no longer waiting for an answer."]]
    expect(page.get_by_role("button", name="Skip")).to_be_enabled()


def test_the_expiry_sentence(ui, page):
    expires = "new Date().setHours(15, 42, 0, 0) / 1000"
    show(ui, f"[{{ ...{question_event()}, approval_expires: {expires} }}]")
    time = ui.js(f"new Date({expires} * 1000).toLocaleTimeString([], {{ hour: '2-digit', minute: '2-digit' }})")
    expect(page.get_by_text(f"If you do not answer by {time}, the agent continues without your answer.", exact=True)).to_be_visible()


@pytest.mark.parametrize("state, fields, texts", [
    ("completed", {"question_outcome": "answered", "answers": [
        {"question": "Which database?", "selected": ["SQLite"], "other": None},
        {"question": "Which checks should run?", "selected": ["Lint", "Tests"], "other": "Docs links"}]},
     ["You answered: SQLite", "You answered: Lint, Tests, Other: Docs links"]),
    ("completed", {"question_outcome": "skipped"}, ["You skipped these questions."]),
    ("completed", {"question_outcome": "expired"}, ["Not answered in time."]),
    ("cancelled", {}, ["Question cancelled."]),
])
def test_answered_questions_show_a_summary(ui, page, state, fields, texts):
    show(ui, f"[{question_event(state, **fields)}]")
    card = page.get_by_role("region", name="Local has 2 questions")
    expect(card.get_by_text("Which database?")).to_be_visible()
    for text in texts:
        expect(card.get_by_text(text, exact=True)).to_be_visible()
    expect(card.get_by_role("radio")).to_have_count(0)
    expect(card.get_by_role("button")).to_have_count(0)


# Plan cards

def test_a_plan_renders_markdown_and_sends_each_choice_with_feedback(ui, page):
    show(ui, f"[{plan_event()}]")
    card = page.get_by_role("region", name="Storage plan")
    expect(card.locator(".plan-body").get_by_role("heading", name="Storage plan")).to_be_visible()
    expect(card.locator(".plan-body").get_by_role("listitem")).to_have_text(["Add the schema", "Write the migration"])
    expect(card.locator(".plan-body code")).to_have_text("make test")
    feedback = card.get_by_role("textbox", name="Feedback for the agent (optional)")
    assert feedback.get_attribute("maxlength") == "2000"
    choices = [("Keep planning", "keep_planning", "  Split step two.  "),
               ("Approve and use Manual", "manual", ""), ("Approve and use Accept edits", "acceptEdits", "Go ahead.")]
    for count, (button, _, note) in enumerate(choices, start=1):
        feedback.fill(note)
        card.get_by_role("button", name=button, exact=True).click()
        ui.wait(f"fake.calls.length === {count}")
    assert sent(ui, "/api/sessions/parent/plans/plan") == [
        {"choice": "keep_planning", "feedback": "Split step two."}, {"choice": "manual"},
        {"choice": "acceptEdits", "feedback": "Go ahead."}]


def test_a_plan_without_a_title_and_its_expiry(ui, page):
    expires = "new Date().setHours(9, 5, 0, 0) / 1000"
    show(ui, f"[{{ ...{plan_event()}, input: {{ plan: 'Short plan.' }}, approval_expires: {expires} }}]")
    time = ui.js(f"new Date({expires} * 1000).toLocaleTimeString([], {{ hour: '2-digit', minute: '2-digit' }})")
    card = page.get_by_role("region", name="Plan ready for review")
    expect(card.get_by_role("heading", name="Plan ready for review")).to_be_visible()
    expect(card.get_by_text(f"If you do not review the plan by {time}, the agent continues without your review.", exact=True)).to_be_visible()


def test_script_in_a_plan_is_sanitised(ui, page):
    plan = ("<script>window.pwned = 1</script>\\n\\n<img src=x onerror=\\\"window.pwned = 2\\\">\\n\\n"
            "[link](javascript:window.pwned=3)\\n\\n![image](x \\\"title\\\")")
    show(ui, f"[{plan_event(plan=plan)}]")
    body = page.locator(".plan-body")
    expect(body).to_be_visible()
    # Raw HTML stays text, and no element can run script.
    expect(body).to_contain_text("<script>window.pwned = 1</script>")
    assert body.evaluate("""e => [e.querySelectorAll('script').length,
      [...e.querySelectorAll('*')].filter(n => [...n.attributes].some(a => a.name.startsWith('on'))).length,
      [...e.querySelectorAll('a')].filter(a => (a.getAttribute('href') || '').startsWith('javascript')).length]""") == [0, 0, 0]
    page.wait_for_timeout(100)
    assert ui.js("window.pwned === undefined")


@pytest.mark.parametrize("state, fields, texts", [
    ("completed", {"plan_outcome": "approved", "plan_mode": "acceptEdits"}, ["Approved · switched to Accept edits"]),
    ("completed", {"plan_outcome": "approved", "plan_mode": "manual", "feedback": "Go slowly."},
     ["Approved · switched to Manual", "Your feedback: Go slowly."]),
    ("completed", {"plan_outcome": "keep_planning", "feedback": "Split step two."},
     ["You asked to keep planning", "Your feedback: Split step two."]),
    ("completed", {"plan_outcome": "expired"}, ["Not reviewed in time"]),
    ("completed", {"plan_outcome": "superseded"}, ["Not reviewed: you switched modes"]),
    ("cancelled", {}, ["Plan review cancelled."]),
    ("error", {"output": "submit_plan is only available in Plan mode. Describe the plan in your reply instead."},
     ["submit_plan is only available in Plan mode. Describe the plan in your reply instead."]),
])
def test_reviewed_plans_show_the_outcome(ui, page, state, fields, texts):
    show(ui, f"[{plan_event(state, **fields)}]")
    card = page.get_by_role("region", name="Storage plan")
    expect(card.locator(".plan-body").get_by_role("heading", name="Storage plan")).to_be_visible()
    for text in texts:
        expect(card.get_by_text(text, exact=True)).to_be_visible()
    expect(card.get_by_role("button")).to_have_count(0)


# The composer hint names what is waiting

def test_the_composer_hint_names_the_waiting_card(ui, page):
    events = [{"id": "u", "type": "user", "text": "Plan storage"}, json.loads(question_event())]
    ui.app(session="a", scenario="workspace",
           setup=f"fake.sessions.set('a', {{ ...makeSession('a', 'Planning'), events: {json.dumps(events)} }})")
    ui.show_session("a")
    ui.emit("a", {"type": "status", "status": "awaiting_approval"})
    hint = page.locator(".composer-hint")
    expect(hint).to_have_text("The agent is waiting for your answer above.")
    ui.emit("a", {"type": "event", "event": {**json.loads(question_event("completed")), "question_outcome": "skipped"}})
    ui.emit("a", {"type": "event", "event": {"id": "plan", "type": "tool", "name": "submit_plan", "state": "pending",
                                             "input": {"plan": "Do it."}, "output": ""}})
    expect(hint).to_have_text("A plan is waiting for your review above.")
    ui.emit("a", {"type": "event", "event": {"id": "plan", "type": "tool", "name": "submit_plan", "state": "completed",
                                             "input": {"plan": "Do it."}, "output": "", "plan_outcome": "approved",
                                             "plan_mode": "manual"}})
    ui.emit("a", {"type": "event", "event": {"id": "w", "type": "tool", "name": "write_file", "state": "pending",
                                             "input": {"path": "a.txt"}, "preview": "diff"}})
    expect(hint).to_have_text("An action is waiting for your approval above.")


# Phones

@pytest.mark.parametrize("card", ["question", "plan"])
def test_cards_fit_a_phone_without_horizontal_scroll(ui, page, card):
    page.set_viewport_size({"width": 390, "height": 844})
    long_question = {"question": "Which " + "very " * 30 + "long option?", "header": "A header",
                     "options": [{"label": "An option with a long label " * 2, "description": "word " * 50},
                                 {"label": "Unbreakable" + "x" * 60}]}
    event = question_event(questions=[long_question, QUESTIONS[1]]) if card == "question" else plan_event(
        plan="A long line " + "x" * 300 + "\\n\\n```\\n" + "y" * 300 + "\\n```")
    events = [{"id": "u", "type": "user", "text": "Plan storage"}]
    ui.app(session="a", scenario="workspace",
           setup=f"fake.sessions.set('a', {{ ...makeSession('a', 'Planning'), events: [...{json.dumps(events)}, {event}] }})")
    ui.show_session("a")
    target = page.locator(".question-card" if card == "question" else ".plan-card")
    expect(target).to_be_visible()
    widths = ui.js("[document.documentElement.scrollWidth, document.documentElement.clientWidth, "
                   "document.querySelector('.transcript').scrollWidth, document.querySelector('.transcript').clientWidth]")
    assert widths[0] <= widths[1] and widths[2] <= widths[3], widths
    box = target.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390
    for button in target.get_by_role("button").all():
        area = button.bounding_box()
        assert area["x"] >= 0 and area["x"] + area["width"] <= 390, button.text_content()
    expect(target.get_by_role("button", name=re.compile("Send answers|Approve and use Accept edits"))).to_be_visible()
