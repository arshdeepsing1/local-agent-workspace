"""A background command through the real page, app and simulator: its card reads how the command ended once its
chat has the end notice, also when the job was stopped from Workspace → Progress, and in a fork of the chat. Each
test waits for what the page shows, never for a fixed time."""
import re

import pytest
from playwright.sync_api import expect

from .helpers import send


@pytest.fixture
def server_brain(simulator, stack, monkeypatch):
    """The simulator's model, which also starts a long background command when asked to "start a server" (its own
    job scenario ends within seconds, too soon to stop it from the page)."""
    call = simulator.call

    class Server(simulator.Brain):
        def respond(self, body):
            messages = body.get("messages") or []
            users = [message for message in messages if message.get("role") == "user"]
            if users and "start a server" in simulator.text_of(users[-1].get("content")).lower():
                if simulator.tool_calls_before(messages):
                    return "The server runs in the background.", []
                return "Starting it in the background.", [
                    call("run_command", command="sleep 30", background=True, timeout_seconds=60)]
            return super().respond(body)

    monkeypatch.setattr(simulator.Gateway, "brain", Server())


def finished(page, count):
    """Wait until count turns of the open chat have finished."""
    expect(page.locator("details.turn-summary > summary")).to_have_count(count, timeout=30_000)


def approve(page):
    page.get_by_role("button", name="Approve", exact=True).click(timeout=15_000)


def panel(page):
    return page.get_by_role("complementary", name="Workspace panel")


def command_status(page, command):
    """The status label on the card of the agent's command."""
    return page.locator(".transcript .tool-card").filter(has_text=command).locator(".tool-status")


def test_a_background_command_stopped_from_progress_reads_background_stopped_on_its_card(app_page, stack, server_brain):
    page = app_page
    send(page, "Please start a server")
    approve(page)
    finished(page, 1)
    expect(command_status(page, "sleep 30")).to_have_text("started in background")
    page.get_by_role("button", name="Workspace", exact=True).click()
    panel(page).get_by_role("button", name="Progress", exact=True).click()
    row = panel(page).get_by_role("list", name="Background tasks").get_by_role("listitem").filter(has_text="sleep 30")
    row.get_by_role("button", name="Stop job sleep 30").click()
    expect(row.locator(".progress-status")).to_have_text(re.compile(r"^cancelled"))
    # The card agrees with Progress, and the chat's notice says so.
    expect(command_status(page, "sleep 30")).to_have_text("background · stopped")
    expect(page.locator(".conversation-notice").filter(has_text="Background job stopped: sleep 30")).to_be_visible()


def test_a_fork_keeps_a_background_commands_outcome_on_its_card(app_page, stack):
    page = app_page
    send(page, "Run the background job")
    approve(page)
    finished(page, 1)
    expect(command_status(page, "echo build finished")).to_have_text("background · exit 0")
    # /fork copies the finished conversation through an export and opens the copy (README, slash commands). As in
    # test_slash_commands: Enter runs it once the chat is idle and the / menu has its list.
    box = page.get_by_role("textbox", name="Message")
    box.fill("/fork")
    expect(page.get_by_role("combobox", name="Model")).to_be_enabled(timeout=10_000)
    expect(page.get_by_text("Loading skills…")).to_have_count(0, timeout=10_000)
    box.press("Enter")
    expect(page.locator(".conversation-title")).to_contain_text("(fork)", timeout=15_000)
    expect(page.get_by_text(re.compile(r"^Background job completed \(exit 0\)"))).to_be_visible()
    expect(command_status(page, "echo build finished")).to_have_text("background · exit 0")
