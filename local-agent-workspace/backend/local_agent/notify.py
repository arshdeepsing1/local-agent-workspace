"""Optional desktop notifications from the server, for when no page is open.

A notification names only the status and the conversation title, never transcript
text. It is started without a shell and without waiting for it, so a slow or missing
notifier never delays the agent loop.
"""
import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "Local"
TITLE_LIMIT = 60
TIMEOUT_SECONDS = 5
OSASCRIPT = "/usr/bin/osascript"
# "interrupted": the server stopped during the turn.
ERROR_REASONS = frozenset({"error", "inference_error", "interrupted"})
# The notifier needs the desktop session, not the server's credentials.
ENVIRONMENT_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "DISPLAY",
                    "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")


def short_title(title, limit=TITLE_LIMIT):
    """One line of at most `limit` characters."""
    text = "".join(char for char in " ".join(str(title or "").split()) if char.isprintable())
    text = text or "Untitled conversation"
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def attention_message(session, previous, status, turn_ended=False):
    """The notification for a status change, or None. Only a status and the title.

    turn_ended marks the idle status that ends a turn, including one that failed
    during an automatic compaction; the end of a manual compaction is not a reply.
    """
    if status == previous:
        return None
    title = short_title(session.get("title"))
    if status == "awaiting_approval":
        return f"Needs your approval: {title}"
    # A turn that delegate_task started is reported by its parent's turn (a subagent chat
    # you continued yourself is not); a stop was asked for.
    if status != "idle" or not turn_ended or session.get("turn_delegated"):
        return None
    reason = session.get("terminal_reason")
    if reason == "stopped":
        return None
    if reason in ERROR_REASONS:
        return f"Stopped with an error: {title}"
    if reason == "step_limit":  # Not finished: the chat offers Continue.
        return f"Reached the step limit: {title}"
    return f"Finished: {title}"


def notification_argv(message, platform=None, which=shutil.which, osascript=OSASCRIPT):
    """The command that shows `message`, as an argument list (no shell), or None."""
    platform = platform or sys.platform
    if platform == "darwin":
        if not Path(osascript).is_file():
            return None
        # The title and message are arguments of the script, never part of its source.
        return [osascript, "-e", "on run argv",
                "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e", "end run", APP_NAME, message]
    if platform.startswith("linux"):
        program = which("notify-send")
        # The message starts with a fixed status word, so it cannot be read as an option.
        # Notification servers may read the body as markup: show a title's <, > and & as text.
        body = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return [program, APP_NAME, body] if program else None
    return None


async def run_notification(argv, timeout=TIMEOUT_SECONDS):
    """Run one notifier process; failures are logged and ignored."""
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={key: os.environ[key] for key in ENVIRONMENT_KEYS if key in os.environ})
        code = await asyncio.wait_for(process.wait(), timeout)
        if code:
            log.warning("Desktop notification command exited with status %s.", code)
    except TimeoutError:
        log.warning("Desktop notification command did not finish within %s seconds.", timeout)
        try:
            process.kill()
            await process.wait()
        except (OSError, AttributeError):
            pass
    except Exception as exc:  # noqa: BLE001 - a notification must never affect a conversation.
        log.warning("Desktop notification failed: %s", type(exc).__name__)


class SystemNotifier:
    """Starts notifications in the background; `runner`, `platform` and `which` are replaceable in tests."""

    def __init__(self, runner=run_notification, platform=None, which=shutil.which):
        self.runner, self.platform, self.which = runner, platform, which
        self.tasks = set()

    def send(self, message):
        try:
            argv = notification_argv(message, self.platform, self.which)
            if not argv:
                return None
            task = asyncio.get_running_loop().create_task(self.runner(argv))
        except Exception as exc:  # noqa: BLE001
            log.warning("Desktop notification failed: %s", type(exc).__name__)
            return None
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task
