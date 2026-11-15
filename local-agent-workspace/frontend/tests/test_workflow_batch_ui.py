"""One approval block for a batch of subagents (a workflow): the run head lists them with Allow all, Decline all and
Review one by one, the cards under it wait for that decision, and the head names who started the batch."""
import json

from playwright.sync_api import expect

SETUP = """
window.makeSession = (events, overrides = {}) => ({
  id: 'parent', title: 'Parent task', workspace: '/project', model: 'test-model', status: 'awaiting_approval',
  events, updated: 1, permission_mode: 'manual', allowed_directories: [], ...overrides,
})
window.batchCard = (id, index, task, extra = {}, batch = {}) => ({
  id, type: 'tool', name: 'delegate_task', state: 'pending', call_id: `call-${index + 1}`,
  input: { task, agent_type: 'explore', max_steps: 12, tool_profile: 'read_only', ...(extra.input || {}) },
  preview: `Start a subagent (Explore; separate paid model requests, at most 12 steps)\\n\\nTask: ${task}`,
  rememberable: ['chat', 'project', 'user'], rule_options: null, editable: [],
  delegation_batch: { id: 'b1', index, size: 3, origin: 'agent' },
  batch_approval: { id: 'b1', size: 3, event_ids: ['c1', 'c2', 'c3'], review: false, ...batch },
  ...Object.fromEntries(Object.entries(extra).filter(([key]) => key !== 'input')),
})
window.waitingBatch = (tasks = ['Check the parser', 'Check the lexer', 'Check the tests'], extra = {}) =>
  tasks.map((task, index) => batchCard(`c${index + 1}`, index, task, extra))
fake.handle = () => fake.json({ ok: true })
"""


def show(ui, events: str, overrides: str = "{}"):
    ui.mount("components/Conversation.js", f"{{ session: makeSession({events}, {overrides}), onError: fake.spy('onError') }}", setup=SETUP)


def posts(ui, suffix=""):
    return [json.loads(call["body"] or "{}") for call in ui.requests(f"/api/sessions/parent/approvals/b1{suffix}", "POST")]


def block(page):
    return page.get_by_role("group", name="Approval for 3 subagents")


def test_the_block_lists_the_three_subagents_with_three_buttons(ui, page):
    show(ui, "waitingBatch()")
    group = block(page)
    expect(group).to_be_visible()
    expect(group.get_by_role("listitem")).to_have_count(3)
    for task in ("Check the parser", "Check the lexer", "Check the tests"):
        expect(group.get_by_text(task)).to_be_visible()
    first = group.get_by_role("listitem").first
    expect(first).to_contain_text("Explore")
    expect(first).to_contain_text("read_only")
    expect(first).to_contain_text("12 steps")
    for name in ("Allow all", "Decline all", "Review one by one"):
        expect(group.get_by_role("button", name=name, exact=True)).to_be_visible()
    expect(group.get_by_role("button", name="Allow subagents in this chat", exact=True)).to_be_visible()
    # The cards under the block wait for it instead of showing their own buttons.
    expect(page.get_by_text("Waiting for the batch decision")).to_have_count(3)
    expect(page.get_by_role("button", name="Approve", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Decline", exact=True)).to_have_count(0)


def test_allow_all_posts_one_decision_and_the_block_goes_away(ui, page):
    show(ui, "waitingBatch()")
    block(page).get_by_role("button", name="Allow all", exact=True).click()
    ui.wait("fake.calls.length === 1")
    assert posts(ui) == [{"allowed": True}]
    assert len(ui.requests("/api/sessions/parent/approvals/c1", "POST")) == 0
    # The server then answers every card: the page drops the block when no card waits for the batch.
    ui.rerender("{ session: makeSession(waitingBatch().map(card => ({ ...card, state: 'running', batch_approval: undefined, "
                "delegation_batch: { ...card.delegation_batch, approval: 'batch' } }))), onError: fake.spy('onError') }")
    expect(block(page)).to_have_count(0)
    expect(page.get_by_text("Started by the agent")).to_be_visible()


def test_decline_all_posts_one_decision_with_the_note(ui, page):
    show(ui, "waitingBatch()")
    group = block(page)
    group.get_by_label("Tell the agent why (optional)").fill("Do it yourself")
    group.get_by_role("button", name="Decline all", exact=True).click()
    ui.wait("fake.calls.length === 1")
    assert posts(ui) == [{"allowed": False, "note": "Do it yourself"}]


def test_allow_subagents_in_this_chat_remembers_one_rule_for_the_batch(ui, page):
    show(ui, "waitingBatch()")
    block(page).get_by_role("button", name="Allow subagents in this chat", exact=True).click()
    ui.wait("fake.calls.length === 1")
    assert posts(ui) == [{"allowed": True, "remember": True}]


def test_review_one_by_one_posts_to_the_review_route_and_shows_per_card_controls(ui, page):
    show(ui, "waitingBatch()")
    block(page).get_by_role("button", name="Review one by one", exact=True).click()
    ui.wait("fake.calls.length === 1")
    assert len(ui.requests("/api/sessions/parent/approvals/b1/review", "POST")) == 1
    assert len(ui.requests("/api/sessions/parent/approvals/b1", "POST")) == 0
    # The server turns the batch into cards: the first asks on its own, the others wait for their turn.
    ui.rerender("{ session: makeSession([{ ...batchCard('c1', 0, 'Check the parser'), batch_approval: undefined }, "
                "batchCard('c2', 1, 'Check the lexer', {}, { review: true }), "
                "batchCard('c3', 2, 'Check the tests', {}, { review: true })]), onError: fake.spy('onError') }")
    expect(block(page)).to_have_count(0)
    expect(page.get_by_role("button", name="Approve", exact=True)).to_have_count(1)
    expect(page.get_by_role("button", name="Decline", exact=True)).to_have_count(1)
    expect(page.get_by_text("Waits for the subagents before it")).to_have_count(2)
    page.get_by_role("button", name="Approve", exact=True).click()
    ui.wait("fake.calls.length === 2")
    assert len(ui.requests("/api/sessions/parent/approvals/c1", "POST")) == 1


def test_the_head_says_who_started_the_batch(ui, page):
    def done(origin):
        return ("waitingBatch().map(card => ({ ...card, state: 'completed', batch_approval: undefined, output: '{}', "
                f"delegation_batch: {{ ...card.delegation_batch, origin: '{origin}', approval: 'batch' }} }}))")
    show(ui, done("user"))
    expect(page.get_by_text("Started by you", exact=True)).to_be_visible()
    ui.rerender(f"{{ session: makeSession({done('agent')}), onError: fake.spy('onError') }}")
    expect(page.get_by_text("Started by the agent", exact=True)).to_be_visible()
    expect(page.get_by_text("Started by you", exact=True)).to_have_count(0)


def test_text_with_hidden_characters_is_shown_escaped_with_the_note(ui, page):
    show(ui, "waitingBatch(['Check \\u202eexe.txt', 'Plain', 'Plain two'])")
    group = block(page)
    expect(group.locator(".hidden-char").first).to_have_text("\\u202e")
    expect(group.locator(".logical-order").first).to_be_visible()
    expect(group.get_by_role("note")).to_have_text("Hidden or direction-changing characters are shown as \\u escapes.")
    expect(group).not_to_contain_text("‮")


def test_the_block_names_a_hidden_agent_type(ui, page):
    show(ui, "waitingBatch().map((card, index) => index ? card : { ...card, input: { ...card.input, agent_type: 'rev\\u202eiew' } })")
    expect(block(page).locator(".hidden-char")).to_have_count(1)
    expect(block(page).get_by_role("note")).to_be_visible()


def test_the_buttons_are_reachable_by_keyboard_in_order(ui, page):
    show(ui, "waitingBatch()")
    group = block(page)
    group.get_by_label("Tell the agent why (optional)").focus()
    names = []
    for _ in range(4):
        page.keyboard.press("Tab")
        names.append(page.evaluate("document.activeElement.textContent.trim()"))
    assert {"Allow all", "Decline all", "Review one by one"} <= set(names)


def test_the_block_has_no_horizontal_overflow_at_390_px(ui, page):
    page.set_viewport_size({"width": 390, "height": 844})
    long_task = "Check the " + "very-long-module-name-" * 8
    show(ui, f"waitingBatch(['{long_task}', 'Check the lexer', 'Check the tests'])")
    expect(block(page)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert page.evaluate("""() => { const box = document.querySelector('.run-batch-approval').getBoundingClientRect()
      return box.left >= 0 && box.right <= window.innerWidth }""")
    assert page.evaluate("""() => [...document.querySelectorAll('.transcript, .transcript-inner')].every(el => el.scrollWidth <= el.clientWidth + 1)""")


def test_a_batch_that_still_waits_after_a_rule_change_keeps_one_block(ui, page):
    show(ui, "waitingBatch()")
    expect(block(page)).to_have_count(1)
    ui.rerender("{ session: makeSession(waitingBatch(), { permission_mode: 'plan' }), onError: fake.spy('onError') }")
    expect(block(page)).to_have_count(1)


# A decided batch: no block, and the cards that wait their turn say why

def decided(first="ask", second="allowed", third="declined"):
    """A decided batch whose first card waits on its own card (or its hook) and whose others wait their release."""
    return ("waitingBatch().map((card, index) => { const { batch_approval, ...rest } = card; "
            f"const decided = [{first!r}, {second!r}, {third!r}][index]; "
            "return decided === 'own' ? rest : { ...rest, batch_decided: decided } })")


def test_a_decided_batch_shows_no_block_and_each_waiting_card_says_why_it_waits(ui, page):
    show(ui, decided("own"))
    expect(block(page)).to_have_count(0)
    expect(page.locator(".run-batch-approval")).to_have_count(0)
    expect(page.get_by_text("Waiting for the batch decision")).to_have_count(0)
    waiting = page.locator(".batch-waiting")
    expect(waiting).to_have_count(2)
    expect(waiting.nth(0)).to_have_text("Approved, starts after the subagents before it")
    expect(waiting.nth(1)).to_have_text("Will not start. The agent is told after the subagents before it.")
    # The card that is up asks on its own, with its own buttons; the two behind it have none.
    expect(page.get_by_role("button", name="Approve", exact=True)).to_have_count(1)
    expect(page.get_by_role("button", name="Decline", exact=True)).to_have_count(1)
    # Only the card that asks needs your answer: the queued ones are not counted as waiting for you.
    expect(page.locator("[data-event-id].needs-approval")).to_have_count(1)


def test_a_card_that_asks_on_its_own_after_a_rule_change_waits_for_the_ones_before_it(ui, page):
    show(ui, decided("own", "ask", "ask"))
    expect(block(page)).to_have_count(0)
    expect(page.locator(".batch-waiting")).to_have_text(["Waits for the subagents before it"] * 2)
    expect(page.get_by_role("button", name="Approve", exact=True)).to_have_count(1)


def test_the_head_names_the_origin_again_once_the_batch_is_decided(ui, page):
    show(ui, decided("own"))
    expect(page.get_by_text("Started by the agent")).to_be_visible()
