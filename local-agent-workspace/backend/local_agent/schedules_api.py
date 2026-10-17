"""Routes of Agent tools → Schedules (schedules.py): a project's schedules (the chat's project, or the default one
without a chat), and by id: edit, delete, pause, resume, Run now and Skip. Also a preview of a schedule's next
times while it is typed, and the runs waiting for Run over all projects (the page's banner)."""
from fastapi import HTTPException
from pydantic import BaseModel, Field

from . import schedules
from .config import MAX_TURN_MINUTES, MAX_TURN_TOKENS
from .portability_api import root_project
from .user_paths import checked_workspace


class ScheduleEdit(BaseModel):
    """A schedule's fields; schedules.check_schedule checks them (the caps are required there, with their reason)."""
    model_config = {"extra": "forbid"}
    name: str = Field(max_length=400, strict=True)
    prompt: str = Field(max_length=schedules.MAX_PROMPT * 2, strict=True)
    spec: str = Field(max_length=200, strict=True)
    timezone: str = Field(default="", max_length=64, strict=True)
    model: str = Field(default="", max_length=128, strict=True)
    reasoning_effort: str = Field(default="", max_length=16, strict=True)
    permission_mode: str = Field(default="dontAsk", max_length=32, strict=True)
    approval_timeout_minutes: int | None = Field(default=None, strict=True)
    turn_token_limit: int | None = Field(default=None, strict=True)
    turn_time_limit_minutes: int | None = Field(default=None, strict=True)
    spending_limit_dbu: float = Field(default=0, allow_inf_nan=False, strict=True)
    max_agent_steps: int | None = Field(default=None, strict=True)
    run_policy: str = Field(default="ask", max_length=16, strict=True)
    catch_up: str = Field(default="once", max_length=16, strict=True)
    enabled: bool = Field(default=True, strict=True)


class SchedulePreview(BaseModel):
    model_config = {"extra": "forbid"}
    spec: str = Field(max_length=200, strict=True)
    timezone: str = Field(default="", max_length=64, strict=True)


LIMITS = {"max_approval_minutes": schedules.MAX_APPROVAL_MINUTES, "max_turn_tokens": MAX_TURN_TOKENS,
          "max_turn_minutes": MAX_TURN_MINUTES, "min_interval_minutes": schedules.MIN_INTERVAL_MINUTES,
          "max_name": schedules.MAX_NAME, "max_prompt": schedules.MAX_PROMPT, "max_schedules": schedules.MAX_SCHEDULES,
          "max_running": schedules.MAX_RUNNING, "modes": list(schedules.MODES)}


def register_schedules(app, manager, settings, session_or_404, workspace_tools):
    scheduler = manager.scheduler

    def project(session_id):
        """The project folder of the chat (its source project for a worktree chat), or the Settings project."""
        tools = workspace_tools(session_id)
        session = session_or_404(session_id) if session_id else {}
        return str(root_project(manager, str(tools.root), session.get("worktree")))

    def listing(workspace):
        try:
            items, problem = scheduler.store.read(), None
        except ValueError as exc:
            items, problem = [], str(exc)
        if scheduler.enabled() and not scheduler.running:
            scheduler.start()  # Another app instance may have released the lock since.
        status = scheduler.status()
        mine = [scheduler.public(item) for item in items if item.get("workspace") == workspace]
        return {**status, "problem": problem or status["problem"], "project": workspace, "schedules": mine,
                "other_projects": len({item.get("workspace") for item in items} - {workspace}),
                "token_warning": schedules.token_warning(settings.values.get("token_expires_on")), "limits": LIMITS}

    def found(change, *args):
        try:
            return change(*args)
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from None

    @app.get("/api/schedules")
    async def list_schedules(session_id: str | None = None):
        return listing(project(session_id))

    @app.get("/api/schedules/ready")
    async def ready_schedules():
        return scheduler.attention()

    @app.post("/api/schedules/preview")
    async def preview_schedule(body: SchedulePreview):
        return schedules.preview(body.spec, body.timezone.strip())

    @app.post("/api/schedules")
    async def create_schedule(body: ScheduleEdit, session_id: str | None = None):
        workspace = project(session_id)
        checked_workspace(workspace, settings)  # Never the app's state folder.
        scheduler.create(body.model_dump(), workspace)
        return listing(workspace)

    @app.put("/api/schedules/{schedule_id}")
    async def edit_schedule(schedule_id: str, body: ScheduleEdit, session_id: str | None = None):
        found(scheduler.edit, schedule_id, body.model_dump())
        return listing(project(session_id))

    @app.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str, session_id: str | None = None):
        found(scheduler.delete, schedule_id)
        return listing(project(session_id))

    @app.post("/api/schedules/{schedule_id}/pause")
    async def pause_schedule(schedule_id: str, session_id: str | None = None):
        found(scheduler.pause, schedule_id)
        return listing(project(session_id))

    @app.post("/api/schedules/{schedule_id}/resume")
    async def resume_schedule(schedule_id: str, session_id: str | None = None):
        found(scheduler.resume, schedule_id)
        return listing(project(session_id))

    @app.post("/api/schedules/{schedule_id}/skip")
    async def skip_schedule(schedule_id: str, session_id: str | None = None):
        found(scheduler.skip, schedule_id)
        return listing(project(session_id))

    @app.post("/api/schedules/{schedule_id}/run")
    async def run_schedule(schedule_id: str, session_id: str | None = None):
        """Run now, or Run on a run waiting for it: the click is the approval of this run's model calls."""
        item = scheduler.store.get(schedule_id)
        if item is None:
            raise HTTPException(404, "This schedule no longer exists.")
        pending = item.get("pending") if isinstance(item.get("pending"), dict) else None
        started = await scheduler.launch(schedule_id, "ready" if pending else "run_now",
                                         pending.get("due") if pending else None,
                                         pending.get("note") if pending else None, user=True)
        return {**listing(project(session_id)), "session_id": started}
