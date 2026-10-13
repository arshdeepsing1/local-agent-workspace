"""Approval answers: permission rules, decline notes, and naming a busy chat.

A rule turns one kind of approval card into an automatic approval (allow), a card
in every mode (ask) or a refusal (deny), for one conversation, one project or all
projects. Rules never change Plan mode, folder-access cards, credential exclusions,
protected paths, or the guidance and file checks around an action.
"""
import json
import re
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .rules import rule_matches, same_rule

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
    if name in ("delegate_task", "stop_job"):
        return {"tool": name}
    return None


def allowed_by_rule(session, name, arguments, resolve_path=None):
    """True when an allow rule of this chat matches the action (see rules.rule_matches)."""
    return any(rule.get("behavior", "allow") == "allow"
               and rule_matches(rule, name, arguments, resolve_path=resolve_path or (lambda value: value))
               for rule in session.get("allow_rules") or [])


def declined(answer, subject="this action", follow="Do not retry it without a new instruction."):
    """The model-facing result of a declined card, with the user's note when they wrote one."""
    note = answer.get("decline_note")
    return f"User declined {subject}." + (f" Their note: {note}" if note else "") + f" {follow}"


class AllowRule(BaseModel):
    """A saved rule as the Permissions menu lists it (M1 chat rules have no behavior)."""
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(min_length=1, max_length=300, strict=True)
    behavior: Literal["allow", "ask", "deny"] | None = None
    command: str | None = Field(default=None, max_length=50000, strict=True)
    hook: str | None = Field(default=None, max_length=200, strict=True)
    argv: list[str] | None = Field(default=None, max_length=64)
    prefix: list[str] | None = Field(default=None, max_length=16)
    flags: list[str] | None = Field(default=None, max_length=64)
    path: str | None = Field(default=None, max_length=4096, strict=True)


class RuleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["chat", "project", "user"]
    session_id: str | None = Field(default=None, min_length=1, max_length=512, strict=True)
    rule: dict


class RuleRemoval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["chat", "project", "user"]
    session_id: str | None = Field(default=None, min_length=1, max_length=512, strict=True)
    rule: AllowRule


class CardEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: dict[str, str]


class DisabledTools(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools: list[str] = Field(max_length=64)


MAX_RULE_JSON = 8 * 1024
MAX_EDIT_JSON = 600 * 1024
MCP_TOOL = re.compile(r"^mcp__[A-Za-z0-9_-]{1,64}__[A-Za-z0-9_.-]{1,128}$")


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


def short_description(text, limit=160):
    """A tool description for the Tools panel: cut at a word boundary with an ellipsis, at most limit characters."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    kept = text[:limit]
    kept = kept[:kept.rfind(" ")] if " " in kept else kept[:limit - 1]
    return kept.rstrip(" ,;:(-") + "…"


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


def workspace_idle(manager, store, root, action):
    """Writing files back is an explicit user action, guarded against active writers in this workspace:
    every chat in the workspace root must be idle and no command may run there (HTTP 409 otherwise)."""
    from pathlib import Path
    for summary in store.list():
        if Path(summary["workspace"]).resolve() == root:
            ensure_idle(manager, summary, action)
    job = next((job for job in manager.jobs.list(workspace=str(root)) if job["state"] == "running"), None)
    owner = job and job.get("session_id") and store.get(job["session_id"])
    if owner:
        raise HTTPException(409, detail={
            "message": f"A command started in “{clip_title(owner.get('title'))}” is still running. Stop it in Terminal before {action}.",
            "session_id": owner["id"], "title": clip_title(owner.get("title"), 160)})
    if job:
        raise HTTPException(409, f"Stop workspace commands before {action}.")


def known_tool_names():
    from .feature_tools import FEATURE_TOOLS
    from .tools import TOOL_DEFINITIONS
    return [item["function"]["name"] for item in [*TOOL_DEFINITIONS, *FEATURE_TOOLS]]


def register_approvals(app, manager, store, session_or_404):
    from .agents import public_session  # agents.py imports this module.
    from .feature_tools import FEATURE_TOOLS
    from .rules import normalize_rule, project_key
    from .tools import TOOL_DEFINITIONS, WorkspaceTools

    def rule_tools(session):
        if session is not None:
            return manager.session_tools(session)
        return WorkspaceTools(manager.settings.values["workspace"], manager.settings.values["env_file"])

    def listing(session):
        workspace = session["workspace"] if session is not None else manager.settings.values["workspace"]
        saved = manager.rules.rules(workspace)
        return {"chat": list(session.get("allow_rules", [])) if session is not None else [],
                "project": saved["project"], "user": saved["user"], "project_path": project_key(workspace),
                "problem": manager.rules.problem}

    def scoped_session(scope, session_id):
        if scope in ("chat", "project") and not session_id:
            raise ValueError("Choose a conversation for a rule in this chat or project.")
        return session_or_404(session_id) if session_id else None

    @app.delete("/api/sessions/{session_id}/allow-rules")
    async def remove_allow_rule(session_id: str, body: AllowRule):
        # Allowed while the chat is busy: removing a rule only makes the agent ask again.
        session = session_or_404(session_id)
        rule = body.model_dump(exclude_none=True)
        session["allow_rules"] = [item for item in session.get("allow_rules", []) if not same_rule(item, rule)]
        store.save(session)
        await manager.rules_changed("chat", session)
        return public_session(session, busy_status(manager, session_id))

    @app.get("/api/permission-rules")
    async def permission_rules(session_id: str | None = None):
        return listing(session_or_404(session_id) if session_id else None)

    @app.post("/api/permission-rules")
    async def add_permission_rule(body: RuleChange):
        session = scoped_session(body.scope, body.session_id)
        if len(json.dumps(body.rule)) > MAX_RULE_JSON:
            raise ValueError("This rule is too large.")
        tools = rule_tools(session)
        rule = normalize_rule(body.rule, **manager.rule_context(tools))
        if body.scope == "chat":
            rules = session.setdefault("allow_rules", [])
            if not any(same_rule(rule, item) for item in rules):
                if len(rules) >= MAX_ALLOW_RULES:
                    raise ValueError(f"This chat already has {MAX_ALLOW_RULES} rules. Remove one in the Permissions menu first.")
                rules.append(rule)
                store.save(session)
        else:
            manager.rules.add(body.scope, (session or {}).get("workspace", manager.settings.values["workspace"]), rule)
        await manager.rules_changed(body.scope, session)
        return listing(session)

    @app.delete("/api/permission-rules")
    async def remove_permission_rule(body: RuleRemoval):
        session = scoped_session(body.scope, body.session_id)
        rule = body.rule.model_dump(exclude_none=True)
        if body.scope == "chat":
            session["allow_rules"] = [item for item in session.get("allow_rules", []) if not same_rule(item, rule)]
            store.save(session)
        else:
            manager.rules.remove(body.scope, (session or {}).get("workspace", manager.settings.values["workspace"]), rule)
        await manager.rules_changed(body.scope, session)
        return listing(session)

    @app.post("/api/permission-rules/reset")
    async def reset_permission_rules(session_id: str | None = None):
        session = session_or_404(session_id) if session_id else None
        manager.rules.reset()
        await manager.rules_changed("user", session)
        return listing(session)

    @app.post("/api/sessions/{session_id}/approvals/{event_id}/edit")
    async def edit_approval(session_id: str, event_id: str, body: CardEdit):
        session_or_404(session_id)
        if len(json.dumps(body.input)) > MAX_EDIT_JSON:
            raise ValueError("The edited text is too large.")
        await manager.revise_card(session_id, event_id, body.input)
        return {"ok": True}

    @app.put("/api/sessions/{session_id}/disabled-tools")
    async def set_disabled_tools(session_id: str, body: DisabledTools):
        session = session_or_404(session_id)
        known = {*known_tool_names(), "ask_user", "submit_plan"}
        names = []
        for name in body.tools:
            if not isinstance(name, str) or not (name in known or MCP_TOOL.match(name)):
                raise ValueError(f"Unknown tool: {str(name)[:80]}.")
            if name not in names:
                names.append(name)
        await manager.set_disabled_tools(session, names)
        return public_session(session, busy_status(manager, session_id))

    @app.get("/api/tools")
    async def list_tools(session_id: str | None = None):
        session = session_or_404(session_id) if session_id else None
        tools = [{"name": item["function"]["name"], "description": short_description(item["function"].get("description", ""))}
                 for item in [*TOOL_DEFINITIONS, *FEATURE_TOOLS]]
        return {"tools": tools, "disabled": list((session or {}).get("disabled_tools", []))}
