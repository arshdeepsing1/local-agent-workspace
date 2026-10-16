import json
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import approvals, custom_agents, mcp_import, memory, output_styles
from .agents import public_session
from .approvals import busy_conflict, busy_status, ensure_idle
from .planning import transcript_name
from .portability_api import root_project
from .tool_profiles import validate_profile


class RestoreRequest(BaseModel):
    expected_current_hash: str = Field(min_length=1, max_length=128)


class ChangeTarget(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    current_hash: str | None = Field(default=None, max_length=128)
    baseline_hash: str | None = Field(default=None, max_length=128)
    hunk: int | None = Field(default=None, ge=0, le=1_000_000)


class ChangeAction(BaseModel):
    files: list[ChangeTarget] = Field(min_length=1, max_length=500)


class WorktreeRequest(BaseModel):
    branch: str = Field(min_length=1, max_length=200)


class TaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    depends_on: list[str] = Field(default_factory=list, max_length=50)


class SkillRequest(BaseModel):
    skill_id: str = Field(min_length=1, max_length=64)
    enabled: bool = True


class SubagentProfileRequest(BaseModel):
    tool_profile: str = Field(strict=True, max_length=32)


class MemoryNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000, strict=True)  # memory.check_note keeps 500 characters.


class ImportRequest(BaseModel):
    source: Literal["project", "desktop", "text"]
    text: str = Field(default="", max_length=mcp_import.MAX_FILE_BYTES)


def register_features(app, manager, settings, store, session_or_404, workspace_tools, ui_tools=None):
    # Recovery previews and restores are the user's own actions, like editor saves: they may reach the
    # folders added to Workspace -> Files (ui_tools). The agent's tools never do.
    recovery_tools = ui_tools or workspace_tools

    def idle(session, action="changing this conversation"):
        ensure_idle(manager, session, action)

    def scoped_worktree(worktree_id, tools):
        record = next((item for item in manager.worktrees.list(str(tools.root)) if item["id"] == worktree_id), None)
        if not record:
            raise HTTPException(404, "Managed worktree not found in this workspace.")
        return record

    @app.get("/api/checkpoints")
    async def checkpoints(session_id: str | None = None):
        tools = workspace_tools(session_id)
        return [item for item in manager.checkpoints.list(session_id, str(tools.root)) if item["session_id"] == session_id]

    @app.get("/api/checkpoints/{checkpoint_id}/preview")
    async def preview(checkpoint_id: str, session_id: str | None = None):
        return manager.checkpoints.preview(checkpoint_id, recovery_tools(session_id), session_id=session_id)

    @app.get("/api/checkpoint-turns/{turn_id}/preview")
    async def preview_turn(turn_id: str, session_id: str, offset: int = 0):
        session_or_404(session_id)
        result = manager.checkpoints.preview_turn(turn_id, recovery_tools(session_id), session_id, offset)
        for preview in result["previews"]:
            if "error" in preview:
                preview["error"] = settings.redact(preview["error"])
        return result

    def workspace_idle(tools, action):
        approvals.workspace_idle(manager, store, tools.root, action)

    @app.post("/api/checkpoints/{checkpoint_id}/restore")
    async def restore(checkpoint_id: str, body: RestoreRequest, session_id: str | None = None):
        tools = recovery_tools(session_id)
        workspace_idle(tools, "restoring a checkpoint")
        result = manager.checkpoints.restore(checkpoint_id, tools, body.expected_current_hash, session_id=session_id)
        if session_id:
            await manager.publish_changes(session_or_404(session_id), only_tracked=True)
        return result

    @app.get("/api/sessions/{session_id}/changes")
    async def changes(session_id: str):
        session_or_404(session_id)
        return manager.changes.list(session_id, manager.review_tools(session_or_404(session_id)))

    @app.get("/api/sessions/{session_id}/changes/diff")
    async def change_diff(session_id: str, path: str):
        session_or_404(session_id)
        return manager.changes.diff(session_id, manager.review_tools(session_or_404(session_id)), path)

    @app.post("/api/sessions/{session_id}/changes/{action}")
    async def review_changes(session_id: str, action: str, body: ChangeAction):
        session = session_or_404(session_id)
        if action not in ("keep", "undo"):
            raise HTTPException(404, "Unknown review action.")
        tools = manager.review_tools(session)
        if action == "undo":
            workspace_idle(tools, "undoing agent changes")
        try:
            return manager.changes.act(session_id, tools, action, [item.model_dump() for item in body.files])
        finally:
            # Other open pages of this conversation see the outcome, including a partial undo.
            await manager.publish_changes(session)

    @app.get("/api/worktrees")
    async def worktrees(session_id: str | None = None):
        return manager.worktrees.list(str(workspace_tools(session_id).root))

    @app.post("/api/worktrees")
    async def create_worktree(body: WorktreeRequest, session_id: str | None = None):
        workspace = str(workspace_tools(session_id).root)
        manager.workspace_available(workspace)
        return await manager.worktrees.create(workspace, body.branch)

    @app.post("/api/worktrees/{worktree_id}/session")
    async def open_worktree(worktree_id: str, session_id: str | None = None):
        record = scoped_worktree(worktree_id, workspace_tools(session_id))
        record = manager.worktrees.validate(record["id"])
        manager.workspace_available(record["path"])
        parent = session_or_404(session_id) if session_id else {}
        session = store.create({**settings.values, "workspace": record["path"], "model": parent.get("model", settings.values["model"])})
        # A new worktree chat starts with normal manual permissions and no inherited external grants.
        session["title"] = f"Worktree: {record['branch']}"[:100]
        # The root project, also when the worktree was made from a worktree chat (sidebar grouping).
        source = root_project(manager, record["source_workspace"], parent.get("worktree"))
        session["worktree"] = {"id": record["id"], "branch": record["branch"], "source_workspace": source}
        store.save(session)
        return public_session(session)

    @app.delete("/api/worktrees/{worktree_id}")
    async def remove_worktree(worktree_id: str, session_id: str | None = None):
        record = scoped_worktree(worktree_id, workspace_tools(session_id))
        if Path(settings.values["workspace"]).expanduser().resolve().is_relative_to(Path(record["path"])):
            raise HTTPException(409, "Choose a different default workspace in Settings before removing this worktree.")
        return await manager.worktrees.remove(worktree_id)

    @app.get("/api/sessions/{session_id}/tasks")
    async def tasks(session_id: str):
        session_or_404(session_id)
        return manager.task_board.list(session_id)

    @app.post("/api/sessions/{session_id}/tasks")
    async def create_task(session_id: str, body: TaskRequest):
        session_or_404(session_id)
        task = manager.task_board.create(session_id, **body.model_dump())
        await manager.publish_tasks(session_id)
        return task

    @app.patch("/api/sessions/{session_id}/tasks/{task_id}")
    async def update_task(session_id: str, task_id: str, body: dict):
        session_or_404(session_id)
        task = manager.task_board.update(session_id, task_id, body)
        await manager.publish_tasks(session_id)
        return task

    @app.get("/api/sessions/{session_id}/children")
    async def children(session_id: str):
        """The chat's subagent chats (headers), with their status, transcript file and, for those
        started since the parent keeps a subagents index, that entry (task, steps_used, tool_calls)."""
        index = {item.get("id"): item for item in session_or_404(session_id).get("subagents") or [] if isinstance(item, dict)}
        return [{**child, "status": manager.statuses.get(child["id"], "idle"), "transcript": transcript_name(child["id"]),
                 **({"subagent": index[child["id"]]} if child["id"] in index else {})}
                for child in manager.delegates.children(session_id)]

    @app.get("/api/agents")
    async def agents(session_id: str | None = None):
        """The subagent types delegate_task offers in the chat's workspace: the built-in ones with their
        Settings → Subagents defaults, the custom agents of its agent files (without their instructions)
        and the files that could not be used, with the reason."""
        catalog = custom_agents.discover(workspace_tools(session_id).root, custom_agents.display_redactor(settings))
        defaults = settings.values.get("subagent_defaults")
        # As delegate_task resolves them: an agent file's model must be an endpoint the user already uses.
        used = custom_agents.used_endpoints(settings, manager.store.list())
        return {"built_in": custom_agents.built_in_agents(defaults if isinstance(defaults, dict) else {}),
                "custom": [custom_agents.public_agent(custom_agents.limit_model(agent, used)) for agent in catalog["agents"]],
                "errors": catalog["errors"], "folders": list(custom_agents.AGENT_ROOTS),
                "index_bytes": len(custom_agents.agent_index(catalog["agents"]).encode("utf-8"))}

    @app.put("/api/sessions/{session_id}/subagent-profile")
    async def subagent_profile(session_id: str, body: SubagentProfileRequest):
        session = session_or_404(session_id)
        idle(session)
        if session.get("is_subagent"):
            raise HTTPException(400, "Subagents cannot delegate or change their inherited tool profile.")
        session["subagent_tool_profile"] = validate_profile(body.tool_profile)
        store.save(session)
        result = public_session(session)
        await manager.broadcast(session_id, {"type": "snapshot", "session": result})
        return result

    def extensions_idle():
        active = next((session_id for session_id, task in manager.tasks.items() if not task.done()), None)
        session = active and manager.get(active)
        if session:
            raise busy_conflict(session, busy_status(manager, active), "changing or testing extensions")
        if active:
            raise HTTPException(409, "Stop active responses before changing or testing extensions.")

    @app.get("/api/extensions")
    async def extensions():
        return manager.extensions.public_config()

    @app.put("/api/extensions")
    async def save_extensions(body: dict):
        extensions_idle()
        return manager.extensions.update_config(body)

    @app.post("/api/extensions/test")
    async def test_extensions():
        extensions_idle()
        return await manager.extensions.test()

    # Agent tools → Memory: the notes of the chat's project (the default project without a chat).
    def memory_listing(workspace):
        current = manager.memory.read(workspace)
        return {**current, "enabled": settings.values.get("project_memory", True) is True,
                "max_bytes": memory.MAX_MEMORY_BYTES, "max_notes": memory.MAX_NOTES, "max_note_chars": memory.MAX_NOTE_CHARS}

    def note_id(value):
        if not memory.NOTE_ID.match(value):
            raise HTTPException(404, "This memory note no longer exists.")
        return value

    def memory_change(change):
        try:
            change()
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from None

    @app.get("/api/memory")
    async def memory_notes(session_id: str | None = None):
        return memory_listing(workspace_tools(session_id).root)

    @app.post("/api/memory")
    async def add_memory_note(body: MemoryNote, session_id: str | None = None):
        workspace = workspace_tools(session_id).root
        manager.memory.add(workspace, body.text, settings.redact)
        return memory_listing(workspace)

    @app.patch("/api/memory/{entry}")
    async def edit_memory_note(entry: str, body: MemoryNote, session_id: str | None = None):
        workspace = workspace_tools(session_id).root
        memory_change(lambda: manager.memory.edit(workspace, note_id(entry), body.text, settings.redact))
        return memory_listing(workspace)

    @app.delete("/api/memory/{entry}")
    async def delete_memory_note(entry: str, session_id: str | None = None):
        workspace = workspace_tools(session_id).root
        memory_change(lambda: manager.memory.delete(workspace, note_id(entry)))
        return memory_listing(workspace)

    @app.delete("/api/memory")
    async def clear_memory(session_id: str | None = None):
        workspace = workspace_tools(session_id).root
        manager.memory.clear(workspace)
        return memory_listing(workspace)

    # Settings → Output style: the built-in styles and those of the chat's (or the default) project and home folder.
    @app.get("/api/output-styles")
    async def list_output_styles(session_id: str | None = None):
        found = output_styles.discover(workspace_tools(session_id), manager.home_tools())
        return {"styles": [{key: value for key, value in style.items() if key != "text"} for style in found["styles"]],
                "errors": found["errors"]}

    @app.post("/api/extensions/import")
    async def import_extensions(body: ImportRequest, session_id: str | None = None):
        """A preview of the MCP servers in this chat's project .mcp.json, the Claude desktop app's
        configuration or pasted JSON, as disabled entries: nothing is saved or started, and no
        environment or header value is returned (each becomes a secret reference). present: the
        needed keys the credential file already has (names only), which an enabled server would get."""
        existing = [server["id"] for server in manager.extensions.public_config()["servers"]]
        tokens = manager.redaction_tokens()
        shown = None

        def preview(found):
            keys = manager.extensions.credential_keys()
            return {**found, "present": [name for name in found["needs"] if name in keys]}
        try:
            if body.source == "text":
                return {"source": "text", **preview(mcp_import.parse_text(body.text, existing, tokens))}
            if body.source == "project":
                tools = workspace_tools(session_id)
                path, shown = tools.root / ".mcp.json", ".mcp.json"
                if path.is_symlink():
                    raise ValueError(".mcp.json is a symbolic link; only a regular file is imported.")
                tools.path(".mcp.json")  # Credential and excluded-folder checks.
            else:
                path = mcp_import.desktop_config_path()
                shown = str(path)
            text = mcp_import.read_config_file(path, settings.values.get("env_file"))
            data = json.loads(text)
        except FileNotFoundError:
            raise HTTPException(404, f"No {shown or 'configuration file'} was found.") from None
        except json.JSONDecodeError as error:
            raise HTTPException(400, f"{shown} is not valid JSON ({error.msg} at line {error.lineno}).") from None
        except (ValueError, UnicodeError, OSError) as error:
            raise HTTPException(400, settings.redact(str(error))) from None
        try:
            return {"source": body.source, "path": shown, **preview(mcp_import.parse(data, existing, tokens))}
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.get("/api/sessions/{session_id}/tool-images/{image_id}")
    async def tool_image(session_id: str, image_id: str):
        """An image an MCP tool returned in this chat, kept in memory for its card ({mime_type, data})."""
        session_or_404(session_id)
        image = manager.results.image(session_id, image_id)
        if image is None:
            raise HTTPException(404, "This image is no longer available: images are kept in memory until the app restarts.")
        return image

    @app.get("/api/skills")
    async def skills(session_id: str | None = None):
        return manager.extensions.skills(workspace_tools(session_id))

    @app.post("/api/sessions/{session_id}/skills")
    async def select_skill(session_id: str, body: SkillRequest):
        session = session_or_404(session_id)
        idle(session)
        result = manager.select_skill(session, workspace_tools(session_id), body.skill_id, body.enabled)
        await manager.broadcast(session_id, {"type": "snapshot", "session": public_session(session)})
        return result
