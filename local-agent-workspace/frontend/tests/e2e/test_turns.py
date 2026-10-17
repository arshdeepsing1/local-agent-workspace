"""The simulator's scripted turns through the real page and app (M8g3): a reply and its usage, an edit approved in
Manual with Keep and Undo, parallel subagents, ask_user, a rate-limit retry, a spending-limit stop with Continue and
compaction. Each step waits for what the page shows next (the simulator's scripted text), never for a time."""
import re

from playwright.sync_api import expect

from .helpers import send

def summaries(page):
    """The open chat's Turn summary rows ("Turn summary: 1 step · 0 tools · …"): one per finished turn."""
    return page.locator("details.turn-summary > summary")


def finished(page, count):
    """Wait until count turns of the open chat have finished."""
    expect(summaries(page)).to_have_count(count, timeout=30_000)


def approve(page):
    """Approve the one action waiting for approval (a card is waiting once the reply that asks for it shows)."""
    page.get_by_role("button", name="Approve", exact=True).click()


def test_a_reply_streams_and_its_usage_shows_in_the_meter(app_page, stack):
    page = app_page
    send(page, "Hello there")
    expect(page.get_by_text("Simulated reply from")).to_be_visible(timeout=15_000)
    finished(page, 1)
    # The meter shows the input the simulated endpoint reported; the turn summary its cost.
    expect(page.get_by_role("progressbar", name="Reported model input usage")).to_be_visible()
    expect(page.get_by_text(re.compile(r"^Last model input · [\d,]+ tokens · [\d.]+%$"))).to_be_visible()
    expect(summaries(page)).to_have_text(re.compile(r"^Turn summary: 1 step · 0 tools · .* DBU$"))
    # The title came from the simulated title request; the chat is saved idle on the server.
    expect(page.get_by_role("button", name=re.compile("^Simulated conversation"))).to_be_visible()
    [listed] = stack.manager.store.list()
    session = stack.manager.get(listed["id"])
    assert stack.manager.statuses.get(session["id"], "idle") == "idle"
    assert len(session["inference_calls"]) >= 2  # The reply and the title.


def test_an_edit_in_manual_is_approved_kept_and_undone(app_page, stack):
    page = app_page
    send(page, "Please edit a file")
    expect(page.get_by_text("and then change it twice")).to_be_visible(timeout=15_000)
    approve(page)  # write_file
    expect(page.get_by_text("again (step 2).")).to_be_visible(timeout=15_000)
    [path] = (stack.project / "sim-notes").iterdir()
    assert path.read_text() == "Simulated edit demo\nstatus: step 1\n"
    changes = page.get_by_role("region", name="Changed files")
    expect(changes.get_by_role("button", name=re.compile("^1 file changed"))).to_be_visible()
    # Keep while the turn waits for the next approval: the list clears, the file stays.
    changes.get_by_role("button", name="Keep all changes").click()
    expect(changes).to_have_count(0)
    approve(page)  # edit_file, step 2
    expect(page.get_by_text("again (step 3).")).to_be_visible(timeout=15_000)
    approve(page)  # edit_file, step 3
    expect(page.get_by_text("went through three versions")).to_be_visible(timeout=15_000)
    finished(page, 1)
    assert path.read_text() == "Simulated edit demo\nstatus: step 3\n"
    # Undo goes back to the version that was kept, not to before the turn.
    changes.get_by_role("button", name="Undo all changes").click()
    expect(changes).to_have_count(0)
    assert path.read_text() == "Simulated edit demo\nstatus: step 1\n"


def test_parallel_subagents_run_and_report(app_page, stack):
    page = app_page
    send(page, "Delegate this in parallel please")
    page.get_by_role("button", name="Allow subagents in this chat").click(timeout=15_000)
    finished(page, 1)
    expect(page.get_by_text("Subagent reports received:")).to_be_visible()
    for task in ("Survey the project", "Find the README"):
        card = page.get_by_role("button", name=re.compile(f"^delegate task {task}"))
        expect(card).to_be_visible()
    expect(page.get_by_role("button", name="Open subagent")).to_have_count(2)
    expect(page.get_by_text("Subagent: completed", exact=True)).to_have_count(2)
    expect(summaries(page)).to_have_text(re.compile(r" · 2 subagents$"))
    children = [session for session in stack.manager.store.list() if session.get("is_subagent")]
    assert len(children) == 2 and all(child.get("terminal_reason") == "completed" for child in children)


def test_a_question_from_the_agent_is_answered_in_its_card(app_page, stack):
    page = app_page
    send(page, "Please ask me first")
    question = page.get_by_role("region", name="Local has a question")
    expect(question).to_be_visible(timeout=15_000)
    question.get_by_role("radio", name="High").check()
    question.get_by_role("button", name="Send answers").click()
    expect(page.get_by_text("Thanks, noted your answer:")).to_be_visible(timeout=15_000)
    expect(page.get_by_text("Thanks, noted your answer:")).to_contain_text("High")
    finished(page, 1)


def test_a_rate_limit_is_retried_after_its_wait(app_page, stack):
    page = app_page
    send(page, "Test the rate limit")
    expect(page.get_by_text(re.compile(r"Databricks rate limited the model request .*Retrying in 2 seconds \(1 of 3\)\."))
           ).to_be_visible(timeout=15_000)
    expect(page.get_by_text("The simulated rate limit was waited out")).to_be_visible(timeout=15_000)
    finished(page, 1)


def test_a_spending_limit_stops_the_chat_until_it_is_raised(app_page, stack):
    page = app_page
    # Settings → Agent: a default limit below one Claude Opus 5.5 request of this chat.
    page.get_by_role("button", name="Settings", exact=True).click()
    settings = page.get_by_role("dialog", name="Settings")
    settings.get_by_role("spinbutton", name="Spending limit per conversation (DBU)").fill("0.5")
    settings.get_by_role("button", name="Save settings").click()
    expect(settings).to_have_count(0)
    send(page, "Please read the project")
    stopped = page.get_by_role("status").filter(has_text="The conversation reached its spending limit.")
    expect(stopped).to_be_visible(timeout=15_000)
    expect(page.get_by_text(re.compile(r"^This conversation has used about [\d.]+ of its 0\.500 DBU spending limit"))).to_be_visible()
    # Agent tools → Usage: this chat's own, higher limit; then Continue finishes the turn.
    page.get_by_role("button", name="Agent tools").click()
    tools = page.get_by_role("dialog", name="Agent tools")
    tools.get_by_role("button", name="Usage", exact=True).click()
    limit = tools.get_by_role("group", name="Spending limit")
    limit.get_by_role("spinbutton", name="Limit for this chat (DBU, 0 = none)").fill("100")
    limit.get_by_role("button", name="Save limit").click()
    expect(limit.get_by_text("This chat's own limit.")).to_be_visible()
    page.keyboard.press("Escape")
    expect(tools).to_have_count(0)
    stopped.get_by_role("button", name="Continue").click()
    expect(page.get_by_text("Continue from where you stopped.")).to_be_visible()
    expect(page.get_by_text("Simulated reply from")).to_be_visible(timeout=15_000)
    finished(page, 2)
    expect(page.get_by_role("status").filter(has_text="spending limit")).to_have_count(0)
    [session] = stack.manager.store.list()
    assert session.get("spending_limit_dbu") == 100 and session.get("terminal_reason") == "completed"


def test_compaction_summarizes_earlier_turns_and_saves_a_handoff(app_page, stack):
    page = app_page
    send(page, "Write something long")
    finished(page, 1)
    send(page, "Another long one")
    finished(page, 2)
    send(page, "/compact keep the table")
    notice = page.get_by_text(re.compile(r"^Context compacted \(compaction 1\): 2 earlier messages were summarized"))
    expect(notice).to_be_visible(timeout=20_000)
    expect(notice).to_contain_text("Saved a detailed handoff of the summarized turns to handoffs/auto/")
    handoffs = list((stack.project / "handoffs" / "auto").glob("*-compaction-1.md"))
    assert len(handoffs) == 1 and handoffs[0].read_text().strip()
