"""Approval answers: per-chat allow rules, decline notes, and naming a busy chat.

An allow rule turns one exact kind of approval card into an automatic approval
for a single conversation. Rules never change Plan mode, folder-access cards,
credential exclusions, or the guidance and file checks around an action.
"""
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

MAX_ALLOW_RULES = 100
MAX_DECLINE_NOTE = 2000
EDIT_RULE_TOOLS = ("write_file", "edit_file", "insert_activity_log", "move_path", "delete_path", "notebook_edit")


def rule_for(name, arguments):
    """The rule that "Allow for this chat" stores for an action, or None when it cannot be remembered.

    Commands match by their exact text (only surrounding whitespace is removed),
    MCP tools by their exact name, and hooks by their exact ID. Folder access and
    every other card has no rule.
    """
    name = name if isinstance(name, str) else ""
    arguments = arguments if isinstance(arguments, dict) else {}
    if name == "run_command":
        command = arguments.get("command")
        return {"tool": "run_command", "command": command.strip()} if isinstance(command, str) and command.strip() else None
    if name in EDIT_RULE_TOOLS:
        return {"tool": "edits"}
    if name.startswith("mcp__"):
        return {"tool": name}
    if name.startswith("hook_"):
        hook = arguments.get("hook_id")
        return {"tool": "hook", "hook": hook} if isinstance(hook, str) and hook else None
    return None


def allowed_by_rule(session, name, arguments):
    rule = rule_for(name, arguments)
    return rule is not None and rule in (session.get("allow_rules") or [])


def declined(answer, subject="this action", follow="Do not retry it without a new instruction."):
    """The model-facing result of a declined card, with the user's note when they wrote one."""
    note = answer.get("decline_note")
    return f"User declined {subject}." + (f" Their note: {note}" if note else "") + f" {follow}"


class AllowRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(min_length=1, max_length=300, strict=True)
    command: str | None = Field(default=None, max_length=50000, strict=True)
    hook: str | None = Field(default=None, max_length=200, strict=True)


# Why a conversation is not idle, and what the user can do about it.
BUSY_REASONS = {
    "awaiting_approval": ("is waiting for your approval", "Answer or stop it"),
    "running": ("is still running", "Stop it"),
    "delegating": ("is waiting for its subagent", "Stop it"),
    "compacting": ("is compacting its context", "Wait or stop it"),
    "naming": ("is still creating its title", "Wait a moment or stop it"),
}


def clip_title(title, limit=80):
    title = " ".join(str(title or "Untitled conversation").split())
    return title if len(title) <= limit else title[:limit - 1] + "…"


def busy_status(manager, session_id):
    status = manager.statuses.get(session_id, "idle")
    task = manager.tasks.get(session_id)
    return "running" if status == "idle" and task is not None and not task.done() else status


def busy_conflict(session, status, action=None):
    """A 409 whose detail names the blocking conversation: {message, session_id, title}."""
    reason, remedy = BUSY_REASONS.get(status, ("is still busy", "Stop it"))
    message = f"“{clip_title(session.get('title'))}” {reason}. {remedy} " + (f"before {action}." if action else "first.")
    return HTTPException(409, detail={"message": message, "session_id": session["id"],
                                      "title": clip_title(session.get("title"), 160)})


def ensure_idle(manager, session, action=None):
    status = busy_status(manager, session["id"])
    if status != "idle":
        raise busy_conflict(session, status, action)


def register_approvals(app, manager, store, session_or_404):
    from .agents import public_session  # agents.py imports this module.

    @app.delete("/api/sessions/{session_id}/allow-rules")
    async def remove_allow_rule(session_id: str, body: AllowRule):
        session = session_or_404(session_id)
        ensure_idle(manager, session, "changing what this chat allows")
        rule = body.model_dump(exclude_none=True)
        session["allow_rules"] = [item for item in session.get("allow_rules", []) if item != rule]
        store.save(session)
        await manager.publish_permissions(session)
        return public_session(session, busy_status(manager, session_id))
