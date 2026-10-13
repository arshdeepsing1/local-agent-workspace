"""Composer routes: queued messages, ! shell commands, /btw side questions and the @ file menu.

Messages themselves still go through POST /api/sessions/{id}/messages (api.py), which
starts them or, while the chat is busy, queues them (AgentManager.submit_message).
"""
import asyncio
import copy

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .approvals import ensure_idle
from .context import is_continuation

MAX_QUEUED = 3
MAX_QUEUED_CHARS = 50_000
STEER_PREFIX = ("[The user sent this while you were working. Read it before your next step; it may change the "
                "task.]\n\n")
SHELL_ONE_LINE = "Shell mode runs one line. Remove the line breaks or use Workspace → Terminal."
SIDE_QUESTION_PREFIX = ("Side question from the user. Answer it briefly from the conversation so far. Do not call "
                        "tools. Your answer is not saved in the conversation.\n\n")
SIDE_TOO_LONG = "This conversation is too long for a side question right now. Compact it first."
SIDE_TOOL_CALLS = ("The model tried to use a tool, which side questions cannot do. Ask in the conversation "
                   "instead.")
SIDE_BUSY = "A side question is already being answered in this conversation."


def side_history(wire):
    """A copy of the model history for a side question: without a trailing app-generated
    prompt, and cut before a last tool request that does not have every result yet (a turn
    waiting for approval or running a tool), together with its partial results."""
    history = copy.deepcopy(wire)
    last = next((index for index in range(len(history) - 1, -1, -1)
                 if history[index].get("role") == "assistant" and history[index].get("tool_calls")), None)
    if last is not None:
        wanted = {call.get("id") for call in history[last]["tool_calls"]}
        answered = {message.get("tool_call_id") for message in history[last + 1:] if message.get("role") == "tool"}
        if not wanted <= answered:
            del history[last:]
    # The merge switches this to context.is_app_prompt.
    while history and is_continuation(history[-1]):
        history.pop()
    return history


class ShellCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1, max_length=2000, strict=True)
    timeout_seconds: int = Field(default=60, ge=1, le=3600, strict=True)
    max_output_bytes: int = Field(default=80000, ge=1024, le=1000000, strict=True)


class SideQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000, strict=True)


def register_composer(app, manager, settings, store, session_or_404):
    from .mentions import find_files  # mentions is imported by agents.py, which imports this module.
    from .tools import WorkspaceTools, write_limit_setting

    @app.post("/api/sessions/{session_id}/shell", status_code=202)
    async def shell(session_id: str, body: ShellCommand):
        session = session_or_404(session_id)
        if "\n" in body.command or "\r" in body.command:
            raise ValueError(SHELL_ONE_LINE)
        if not body.command.strip():
            raise ValueError("Enter a command after !.")
        ensure_idle(manager, session, "running a command")
        manager.workspace_available(session["workspace"])
        manager.start_shell(session_id, body.command.strip(), body.timeout_seconds, body.max_output_bytes)
        return {"ok": True}

    @app.delete("/api/sessions/{session_id}/queue/{item_id}")
    async def remove_queued(session_id: str, item_id: str):
        session_or_404(session_id)
        try:
            queue = await manager.remove_queued(session_id, item_id)
        except KeyError:
            raise HTTPException(404, "This queued message was already sent.") from None
        return {"queued_messages": queue}

    @app.post("/api/sessions/{session_id}/queue/send")
    async def send_queued(session_id: str):
        session = session_or_404(session_id)
        ensure_idle(manager, session, "sending queued messages")
        if not manager.queue_of(session):
            raise ValueError("No messages are queued.")
        await manager.send_queued(session_id)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/side-question")
    async def side_question(session_id: str, body: SideQuestion):
        session_or_404(session_id)
        if not body.question.strip():
            raise ValueError("Enter a question after /btw.")
        if session_id in manager.side_questions:
            raise HTTPException(409, SIDE_BUSY)
        return await manager.side_question(session_id, body.question.strip())

    def mention_tools(session):
        if session is not None:
            return manager.session_tools(session)
        return WorkspaceTools(settings.values["workspace"], settings.values["env_file"],
                              write_limit=write_limit_setting(settings.values), excluded_roots=(settings.state_dir,))

    @app.get("/api/sessions/{session_id}/mention-files")
    async def session_mention_files(session_id: str, q: str = Query(default="", max_length=200)):
        return await asyncio.to_thread(find_files, mention_tools(session_or_404(session_id)), q)

    @app.get("/api/mention-files")
    async def mention_files(q: str = Query(default="", max_length=200)):
        return await asyncio.to_thread(find_files, mention_tools(None), q)
