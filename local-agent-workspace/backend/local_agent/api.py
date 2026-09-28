import asyncio
import mimetypes
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agents import AgentManager, public_session
from .config import APP_ROOT, MAX_MAX_AGENT_STEPS, MIN_MAX_AGENT_STEPS, Settings
from .context import (
    MAX_CONTEXT_WINDOW, MAX_MAX_OUTPUT_TOKENS, MIN_CONTEXT_WINDOW, MIN_MAX_OUTPUT_TOKENS,
)
from .store import Store
from .tools import WorkspaceTools, file_error
from .permissions import PermissionMode
from .telemetry import session_metrics
from .activity import redact_secrets
from .usage_export import usage_csv, usage_rows
from .feature_api import register_features
from .portability_api import register_portability


# Browsers refuse ES modules served with a non-JavaScript type, and some systems
# map .js to text/plain in their mimetypes registry.
mimetypes.add_type("text/javascript", ".js")


class RevalidatedStaticFiles(StaticFiles):
    """Frontend files keep stable names, so browsers must check for edits on each load."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class Prompt(BaseModel):
    text: str = Field(min_length=1, max_length=50000)


class CompactionRequest(BaseModel):
    preservation_note: str = Field(default="", max_length=1000, strict=True)


class Decision(BaseModel):
    allowed: bool


class SessionPermissions(BaseModel):
    permission_mode: PermissionMode = "manual"


class SessionModel(BaseModel):
    model: str = Field(min_length=1, max_length=256, strict=True)


class SessionTitle(BaseModel):
    title: str = Field(min_length=1, max_length=160, strict=True)


class FolderAccess(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class FileEdit(BaseModel):
    path: str
    content: str = Field(max_length=80000)
    original: str
    session_id: str | None = None


class SettingsEdit(BaseModel):
    workspace: str
    model: str
    env_file: str
    context_window: int | None = Field(default=None, ge=MIN_CONTEXT_WINDOW, le=MAX_CONTEXT_WINDOW, strict=True)
    max_output_tokens: int | None = Field(default=None, ge=MIN_MAX_OUTPUT_TOKENS, le=MAX_MAX_OUTPUT_TOKENS, strict=True)
    max_agent_steps: int | None = Field(default=None, ge=MIN_MAX_AGENT_STEPS, le=MAX_MAX_AGENT_STEPS, strict=True)
    compaction_handoffs: bool | None = Field(default=None, strict=True)


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=50000)
    timeout_seconds: int = Field(default=60, ge=1, le=3600, strict=True)
    max_output_bytes: int = Field(default=80000, ge=1024, le=1000000, strict=True)
    background: bool = Field(default=False, strict=True)


def create_app(settings=None):
    settings = settings or Settings()
    store = Store(settings.state_dir / "conversations.sqlite3")
    manager = AgentManager(store, settings)
    local_token = secrets.token_urlsafe(32)
    deleting_sessions = set()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.gather(*(manager.stop(sid) for sid in list(manager.tasks)))
        await manager.jobs.shutdown()
        store.db.close()

    app = FastAPI(title="Local workspace", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
    app.state.manager, app.state.settings = manager, settings

    def origin_allowed(origin):
        if not origin:
            return True  # API clients still need the unguessable local token.
        parsed = urlsplit(origin)
        return parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        if not origin_allowed(request.headers.get("origin")):
            return JSONResponse({"detail": "Cross-origin access is not allowed."}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path != "/api/bootstrap":
            if not secrets.compare_digest(request.headers.get("x-local-token", ""), local_token):
                return JSONResponse({"detail": "Refresh the app to reconnect.", "code": "reconnect_required"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, error):
        return JSONResponse({"detail": settings.redact(str(error))}, status_code=400)

    @app.exception_handler(OSError)
    async def os_error(request, error):
        return JSONResponse({"detail": settings.redact(file_error(error))}, status_code=400)

    def session_or_404(session_id):
        if session_id in deleting_sessions:
            raise HTTPException(404, "Conversation is being deleted.")
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, "Conversation not found.")
        return session

    def workspace_tools(session_id=None):
        session = session_or_404(session_id) if session_id else {}
        workspace = session.get("workspace", settings.values["workspace"])
        return WorkspaceTools(workspace, settings.values["env_file"], session.get("allowed_directories", []),
                              session.get("permission_mode") == "bypassPermissions")

    @app.get("/api/bootstrap")
    async def bootstrap():
        return {"token": local_token, "settings": settings.public()}

    def check_folder(path, workspace):
        folder = WorkspaceTools(workspace, settings.values["env_file"]).resolve(path)
        if not folder.is_dir():
            raise ValueError("Choose an existing folder.")
        # Probe directory access without reading any file contents or returning names.
        next(folder.iterdir(), None)
        return folder

    @app.post("/api/folder-access/check")
    async def folder_access_check(body: FolderAccess):
        try:
            folder = check_folder(body.path, settings.values["workspace"])
            return {"accessible": True, "path": str(folder), "error": None}
        except (ValueError, OSError) as exc:
            return {"accessible": False, "path": body.path, "error": settings.redact(file_error(exc)),
                    "python_executable": str(Path(sys.executable).resolve())}

    @app.get("/api/connection")
    async def connection():
        try:
            host, token = settings.credentials()
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.get(host + "/api/2.0/serving-endpoints", headers={"Authorization": f"Bearer {token}"})
            if response.status_code != 200:
                return {"connected": False, "models": [], "error": f"Databricks returned HTTP {response.status_code}. Check your credentials and access."}
            data = response.json()
            models = [item.get("name", "") for item in data.get("endpoints", [])]
            models = [name for name in models if name and not any(term in name for term in ("embedding", "gte-", "bge-"))]
            return {"connected": True, "models": models, "error": None}
        except (ValueError, httpx.HTTPError) as exc:
            return {"connected": False, "models": [], "error": settings.redact(str(exc))}

    @app.put("/api/settings")
    async def update_settings(body: SettingsEdit):
        manager.workspace_available(body.workspace)
        return settings.update(body.model_dump(exclude_none=True))

    @app.get("/api/sessions")
    async def list_sessions():
        return [{**s, "status": manager.statuses.get(s["id"], "idle")} for s in store.list()]

    @app.post("/api/sessions")
    async def create_session(body: SessionPermissions | None = None):
        manager.workspace_available(settings.values["workspace"])
        session = store.create(settings.values)
        session["permission_mode"] = body.permission_mode if body else "manual"
        store.save(session)
        return public_session(session)

    @app.put("/api/sessions/{session_id}/permissions")
    async def permissions(session_id: str, body: SessionPermissions):
        session = session_or_404(session_id)
        if manager.statuses.get(session_id, "idle") != "idle":
            raise HTTPException(409, "Stop the current response before changing permissions.")
        session["permission_mode"] = body.permission_mode
        store.save(session)
        result = public_session(session)
        await manager.broadcast(session_id, {"type": "snapshot", "session": result})
        return result

    @app.put("/api/sessions/{session_id}/model")
    async def change_model(session_id: str, body: SessionModel):
        session = session_or_404(session_id)
        if manager.statuses.get(session_id, "idle") != "idle":
            raise HTTPException(409, "Stop the current response before changing the model.")
        model = body.model.strip()
        if not model:
            raise ValueError("Enter a model endpoint name.")
        session["model"] = model
        store.save(session)
        result = public_session(session)
        await manager.broadcast(session_id, {"type": "model", "model": model})
        return result

    @app.put("/api/sessions/{session_id}/title")
    async def rename_session(session_id: str, body: SessionTitle):
        session = session_or_404(session_id)
        title = " ".join(body.title.split())
        if not title or any(ord(char) < 32 or ord(char) == 127 for char in title):
            raise ValueError("Enter a conversation title without control characters.")
        # Persist first so a failed save cannot change a running conversation.
        # The existing flag also prevents an in-flight generated title replacing
        # the user's choice, even when they keep the previous title unchanged.
        updated = {**session, "title": title, "title_generated": True}
        store.save(updated)
        session.update(title=title, title_generated=True, updated=updated["updated"])
        await manager.broadcast(session_id, {"type": "title", "title": title})
        return public_session(session, manager.statuses.get(session_id, "idle"))

    @app.post("/api/sessions/{session_id}/folders")
    async def allow_folder(session_id: str, body: FolderAccess):
        session = session_or_404(session_id)
        if manager.statuses.get(session_id, "idle") != "idle":
            raise HTTPException(409, "Stop the current response before changing folder access.")
        folder = str(check_folder(body.path, session["workspace"]))
        allowed = session.setdefault("allowed_directories", [])
        if folder not in allowed and folder != session["workspace"]:
            allowed.append(folder)
        store.save(session)
        result = public_session(session)
        await manager.broadcast(session_id, {"type": "snapshot", "session": result})
        return result

    @app.delete("/api/sessions/{session_id}/folders")
    async def remove_folder(session_id: str, body: FolderAccess):
        session = session_or_404(session_id)
        if manager.statuses.get(session_id, "idle") != "idle":
            raise HTTPException(409, "Stop the current response before changing folder access.")
        folder = str(Path(body.path).expanduser().resolve())
        session["allowed_directories"] = [p for p in session.get("allowed_directories", []) if p != folder]
        store.save(session)
        result = public_session(session)
        await manager.broadcast(session_id, {"type": "snapshot", "session": result})
        return result

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        return public_session(session_or_404(session_id), manager.statuses.get(session_id, "idle"))

    @app.get("/api/sessions/{session_id}/metrics")
    async def get_session_metrics(session_id: str):
        return session_metrics(session_or_404(session_id))

    @app.get("/api/sessions/{session_id}/usage.csv")
    async def export_session_usage(session_id: str):
        session = session_or_404(session_id)
        text = usage_csv(usage_rows(session, redact=lambda value: settings.redact(redact_secrets(value))))
        return Response(text, media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="usage-{session_id[:8]}.csv"',
            "Cache-Control": "no-store"})

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        session_or_404(session_id)
        deleting_sessions.add(session_id)
        try:
            await manager.stop(session_id)
            await manager.jobs.remove_session(session_id)
            manager.task_board.delete_session(session_id)
            manager.checkpoints.delete_session(session_id)
            store.delete(session_id)
            manager.live.pop(session_id, None)
            manager.statuses.pop(session_id, None)
        finally:
            deleting_sessions.discard(session_id)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/messages")
    async def message(session_id: str, body: Prompt):
        session_or_404(session_id)
        if not body.text.strip():
            raise ValueError("Enter a message.")
        manager.start(session_id, body.text.strip())
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/stop")
    async def stop(session_id: str):
        session_or_404(session_id)
        await manager.stop(session_id)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/compact", status_code=202)
    async def compact(session_id: str, body: CompactionRequest):
        session_or_404(session_id)
        if manager.statuses.get(session_id, "idle") != "idle" or (session_id in manager.tasks and not manager.tasks[session_id].done()):
            raise HTTPException(409, "Stop the current response before compacting context.")
        manager.start_compaction(session_id, body.preservation_note)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/approvals/{event_id}")
    async def approval(session_id: str, event_id: str, body: Decision):
        session_or_404(session_id)
        manager.decide(session_id, event_id, body.allowed)
        return {"ok": True}

    @app.websocket("/api/sessions/{session_id}/stream")
    async def stream(socket: WebSocket, session_id: str):
        protocols = [p.strip() for p in socket.headers.get("sec-websocket-protocol", "").split(",")]
        if len(protocols) != 2 or protocols[0] != "local-workspace" or not secrets.compare_digest(protocols[1], local_token) or not origin_allowed(socket.headers.get("origin")):
            await socket.close(code=1008)
            return
        session = manager.get(session_id)
        if not session:
            await socket.close(code=1008)
            return
        await socket.accept(subprotocol="local-workspace")
        manager.listeners.setdefault(session_id, set()).add(socket)
        try:
            await socket.send_json({"type": "snapshot", "session": public_session(session, manager.statuses.get(session_id, "idle"))})
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            manager.listeners.get(session_id, set()).discard(socket)

    @app.get("/api/files")
    async def files(path: str = ".", session_id: str | None = None):
        return workspace_tools(session_id).list_files(path)

    @app.get("/api/file")
    async def file(path: str, session_id: str | None = None):
        return {"path": path, "content": workspace_tools(session_id).read_file(path)}

    @app.put("/api/file")
    async def save_file(body: FileEdit):
        tools = workspace_tools(body.session_id)
        if tools.read_file(body.path) != body.original:
            raise HTTPException(409, "This file changed on disk. Reopen it before saving.")
        manager.checkpoints.apply_edit(tools, "write_file", {"path": body.path, "content": body.content}, session_id=body.session_id)
        return {"ok": True}

    @app.get("/api/git")
    async def git_changes(session_id: str | None = None):
        tools = workspace_tools(session_id)
        result = await tools.run_command("git status --short --untracked-files=normal && git diff --no-ext-diff --no-textconv -- . ':!*.env' ':!.env*' ':!env_vars.txt'")
        return result

    @app.post("/api/command")
    async def command(body: Prompt, session_id: str | None = None):
        # Compatibility for existing API clients; new UI uses observable /jobs.
        workspace = str(workspace_tools(session_id).root)
        manager.workspace_available(workspace)
        job = await manager.jobs.start(body.text, workspace, session_id=session_id)
        try:
            job = await manager.jobs.wait(job["id"])
        except asyncio.CancelledError:
            await manager.jobs.stop(job["id"])
            raise
        if job["state"] == "timed_out":
            raise HTTPException(408, "Command stopped after 60 seconds.")
        return {"job_id": job["id"], "exit_code": job["exit_code"], "output": job["output"], "truncated": job["truncated"]}

    def job_scope(session_id):
        session = session_or_404(session_id) if session_id else None
        return str(Path(session["workspace"] if session else settings.values["workspace"]).expanduser().resolve())

    def scoped_job(job_id, session_id):
        workspace = job_scope(session_id)
        job = manager.jobs.get(job_id)
        if not job or job["session_id"] != session_id or job["workspace"] != workspace:
            raise HTTPException(404, "Job not found in this conversation or workspace.")
        return job

    @app.get("/api/jobs")
    async def list_jobs(session_id: str | None = None):
        workspace = job_scope(session_id)
        return [{key: value for key, value in job.items() if key != "output"}
                for job in manager.jobs.list(session_id=session_id, workspace=workspace)
                if job["session_id"] == session_id]

    @app.post("/api/jobs")
    async def start_job(body: CommandRequest, session_id: str | None = None):
        # Clicking Run explicitly authorizes this command, independent of agent mode.
        workspace = job_scope(session_id)
        manager.workspace_available(workspace)
        return await manager.jobs.start(workspace=workspace, session_id=session_id, **body.model_dump())

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, session_id: str | None = None):
        return scoped_job(job_id, session_id)

    @app.post("/api/jobs/{job_id}/stop")
    async def stop_job(job_id: str, session_id: str | None = None):
        scoped_job(job_id, session_id)
        return await manager.jobs.stop(job_id)

    register_features(app, manager, settings, store, session_or_404, workspace_tools)
    register_portability(app, manager, settings, store, session_or_404)

    # The browser loads the frontend source files directly; there is no build step.
    frontend = APP_ROOT / "frontend"
    if (frontend / "static").is_dir():
        app.mount("/static", RevalidatedStaticFiles(directory=frontend / "static"), name="static")

    @app.get("/")
    async def index():
        if (frontend / "index.html").exists():
            return FileResponse(frontend / "index.html", headers={"Cache-Control": "no-cache"})
        return JSONResponse({"message": "The frontend folder is missing. See README.md."}, status_code=503)

    return app
