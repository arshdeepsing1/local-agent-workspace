"""A batch of three subagents through the real page and app: one approval for the whole batch in Manual mode (Allow
all, Decline all), who started it (the /workflow skill or the agent), and the per-turn limit. The simulator scripts
the model: "parallel" and "three" in the message delegate three surveys in one response (the /workflow skill's own
instructions are not read as the user's words). Each step waits for what the page shows, never for a time."""
import re

from playwright.sync_api import expect

from .helpers import send
from .test_turns import finished

THREE = "three surveys in parallel"


def block(page):
    """The one approval block of the batch, in the run head."""
    return page.get_by_role("group", name="Approval for 3 subagents")


def test_allow_all_starts_three_subagents_and_a_workflow_message_says_started_by_you(app_page, stack):
    page = app_page
    send(page, f"/workflow {THREE}")
    approval = block(page)
    expect(approval).to_be_visible(timeout=15_000)
    expect(page.get_by_role("group", name=re.compile("^Approval for"))).to_have_count(1)
    for name in ("Allow all", "Decline all", "Review one by one"):
        expect(approval.get_by_role("button", name=name)).to_be_visible()
    assert [item for item in stack.manager.store.list() if item.get("is_subagent")] == []  # Nothing started yet.
    approval.get_by_role("button", name="Allow all").click()
    finished(page, 1)
    run = page.locator(".run-head")
    expect(run.get_by_role("button", name="3 subagents", exact=True)).to_be_visible()
    expect(run.get_by_role("status")).to_have_text("3 completed")
    expect(page.get_by_role("button", name="Open subagent")).to_have_count(3)
    expect(run.get_by_text("Started by you", exact=True)).to_be_visible()
    expect(block(page)).to_have_count(0)
    # One combined reply of the model follows the three reports.
    expect(page.get_by_text("Subagent reports received:")).to_have_count(1)
    children = [item for item in stack.manager.store.list() if item.get("is_subagent")]
    assert len(children) == 3 and all(child.get("terminal_reason") == "completed" for child in children)


def test_the_same_batch_from_a_plain_message_says_started_by_the_agent(app_page, stack):
    page = app_page
    send(page, f"Delegate {THREE}")
    expect(block(page)).to_be_visible(timeout=15_000)
    block(page).get_by_role("button", name="Allow all").click()
    finished(page, 1)
    run = page.locator(".run-head")
    expect(run.get_by_text("Started by the agent", exact=True)).to_be_visible()
    expect(run.get_by_text("Started by you", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Open subagent")).to_have_count(3)


def test_decline_all_starts_none_and_the_turn_goes_on_to_a_reply(app_page, stack):
    page = app_page
    send(page, f"Delegate {THREE}")
    expect(block(page)).to_be_visible(timeout=15_000)
    block(page).get_by_role("button", name="Decline all").click()
    finished(page, 1)
    expect(block(page)).to_have_count(0)
    expect(page.get_by_role("button", name="Open subagent")).to_have_count(0)
    # The simulated model answers the three declined results with its combined reply.
    expect(page.get_by_text("Subagent reports received:")).to_have_count(1)
    expect(page.get_by_text("User declined this action.").first).to_be_visible()
    assert [item for item in stack.manager.store.list() if item.get("is_subagent")] == []


def test_a_batch_over_the_per_turn_limit_starts_two_and_refuses_the_third(app_page, stack):
    page = app_page
    # Settings → Subagents → Subagents per turn, set on the running app (the same values the dialog saves).
    stack.manager.settings.update({"subagents_per_turn_max": 2})
    send(page, f"Delegate {THREE}")
    expect(block(page)).to_be_visible(timeout=15_000)
    block(page).get_by_role("button", name="Allow all").click()
    finished(page, 1)
    expect(page.get_by_role("button", name="Open subagent")).to_have_count(2)
    expect(page.get_by_text(re.compile("This turn already started 2 subagents, the limit in Settings")).first).to_be_visible()
    children = [item for item in stack.manager.store.list() if item.get("is_subagent")]
    assert len(children) == 2
