from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .agents import public_session
from .tool_profiles import validate_profile


class RestoreRequest(BaseModel):
    expected_current_hash: str = Field(min_length=1, max_length=128)


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


def register_features(app, manager, settings, store, session_or_404, workspace_tools):
    def idle(session):
        if manager.statuses.get(session["id"], "idle") != "idle":
            raise HTTPException(409, "Stop the current response before changing this conversation.")

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
        return manager.checkpoints.preview(checkpoint_id, workspace_tools(session_id), session_id=session_id)

    @app.get("/api/checkpoint-turns/{turn_id}/preview")
    async def preview_turn(turn_id: str, session_id: str, offset: int = 0):
        session_or_404(session_id)
        result = manager.checkpoints.preview_turn(turn_id, workspace_tools(session_id), session_id, offset)
        for preview in result["previews"]:
            if "error" in preview:
                preview["error"] = settings.redact(preview["error"])
        return result

    @app.post("/api/checkpoints/{checkpoint_id}/restore")
    async def restore(checkpoint_id: str, body: RestoreRequest, session_id: str | None = None):
        tools = workspace_tools(session_id)
        # A restore is an explicit user action, guarded against active writers in this workspace.
        for summary in store.list():
            if Path(summary["workspace"]).resolve() == tools.root:
                idle(summary)
        if any(job["state"] == "running" for job in manager.jobs.list(workspace=str(tools.root))):
            raise HTTPException(409, "Stop workspace commands before restoring a checkpoint.")
        return manager.checkpoints.restore(checkpoint_id, tools, body.expected_current_hash, session_id=session_id)

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
        return manager.task_board.create(session_id, **body.model_dump())

    @app.patch("/api/sessions/{session_id}/tasks/{task_id}")
    async def update_task(session_id: str, task_id: str, body: dict):
        session_or_404(session_id)
        return manager.task_board.update(session_id, task_id, body)

    @app.get("/api/sessions/{session_id}/children")
    async def children(session_id: str):
        session_or_404(session_id)
        return [{**child, "status": manager.statuses.get(child["id"], "idle")}
                for child in manager.delegates.children(session_id)]

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
        if any(not task.done() for task in manager.tasks.values()):
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
