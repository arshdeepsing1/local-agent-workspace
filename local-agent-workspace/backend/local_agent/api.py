import asyncio
import mimetypes
import os
import secrets
import sys
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agents import AgentManager, public_session
from .config import APP_ROOT, MAX_APPROVAL_MINUTES, MAX_MAX_AGENT_STEPS, MIN_MAX_AGENT_STEPS, Settings
from .config import MAX_COMMAND_ENV, MAX_USER_INSTRUCTIONS, MAX_WRITE_LIMIT_KB, MIN_WRITE_LIMIT_KB
from .context import (
    MAX_CONTEXT_WINDOW, MAX_MAX_OUTPUT_TOKENS, MIN_CONTEXT_WINDOW, MIN_MAX_OUTPUT_TOKENS, SAFETY_MARGIN,
)
from .model_profiles import default_limits, effective_limits, limit_warnings, limits_note, public_profile
from .user_paths import standing_directories
from .store import Store, UnreadableConversation
from . import conversation_search
from .git_tools import file_diff, working_tree_changes, working_tree_status
from .jobs import NOT_STOPPED, TIMEOUT_NOT_STOPPED
from .tools import WorkspaceTools, file_error
from .workspace_folders import register_workspace_folders
from .permissions import PermissionMode
from .telemetry import session_metrics
from .activity import redact_secrets
from .usage_export import usage_csv, usage_rows
from .transcript import transcript, transcript_file_name
from .feature_api import register_features
from .portability_api import register_portability
from .approvals import MAX_DECLINE_NOTE, ensure_idle, register_approvals
from .composer_api import register_composer
from .interactions import register_interactions
from .projects import register_projects


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
    remember: bool = Field(default=False, strict=True)  # Approve and store an exact allow rule for this chat.
    note: str | None = Field(default=None, max_length=MAX_DECLINE_NOTE, strict=True)  # Decline only.
    scope: Literal["chat", "project", "user"] | None = None  # Approve and save an allow rule there.
    rule: dict | None = None  # The rule to save (at most 8 KB of JSON); the card's default rule when absent.


class SessionPermissions(BaseModel):
    permission_mode: PermissionMode = "manual"


class PermissionChange(BaseModel):
    model_config = {"extra": "forbid"}
    permission_mode: PermissionMode
    confirm: bool = Field(default=False, strict=True)  # Required to switch to Bypass permissions.


class SessionModel(BaseModel):
    model: str = Field(min_length=1, max_length=256, strict=True)


class SessionTitle(BaseModel):
    title: str = Field(min_length=1, max_length=160, strict=True)


class SessionMeta(BaseModel):
    model_config = {"extra": "forbid"}
    pinned: bool | None = Field(default=None, strict=True)
    archived: bool | None = Field(default=None, strict=True)
    tags: list[str] | None = Field(default=None, max_length=10, strict=True)


def clean_tags(tags):
    """NFC, whitespace collapsed, 1 to 32 characters, no control characters; the first of
    tags that differ only in case is kept."""
    result, seen = [], set()
    for raw in tags:
        tag = " ".join(unicodedata.normalize("NFC", raw).split())
        if not 1 <= len(tag) <= 32 or any(unicodedata.category(char) == "Cc" for char in tag):
            raise ValueError("Tags have 1 to 32 characters and no control characters.")
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            result.append(tag)
    return result


class FolderAccess(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class FileEdit(BaseModel):
    path: str
    content: str = Field(max_length=1_000_000)  # The write limit (bytes) is checked by apply_edit.
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
    approval_timeout_minutes: int | None = Field(default=None, ge=0, le=MAX_APPROVAL_MINUTES, strict=True)
    system_notifications: bool | None = Field(default=None, strict=True)
    auto_keep_changes: bool | None = Field(default=None, strict=True)
    user_instructions: str | None = Field(default=None, max_length=MAX_USER_INSTRUCTIONS, strict=True)
    write_limit_kb: int | None = Field(default=None, ge=MIN_WRITE_LIMIT_KB, le=MAX_WRITE_LIMIT_KB, strict=True)
    command_env: list[str] | None = Field(default=None, max_length=MAX_COMMAND_ENV, strict=True)
    rate_limit_auto_continue: bool | None = Field(default=None, strict=True)
    coding_dir: str | None = Field(default=None, max_length=4096, strict=True)
    python_venv: str | None = Field(default=None, max_length=4096, strict=True)


class LimitsQuery(BaseModel):
    """Budgets to check against an endpoint's limits (Settings shows the warnings while editing)."""
    model_config = {"extra": "forbid"}
    model: str = Field(min_length=1, max_length=256, strict=True)
    context_window: int = Field(ge=MIN_CONTEXT_WINDOW, le=MAX_CONTEXT_WINDOW, strict=True)
    max_output_tokens: int = Field(ge=MIN_MAX_OUTPUT_TOKENS, le=MAX_MAX_OUTPUT_TOKENS, strict=True)

    @model_validator(mode="after")
    def output_below_context(self):
        if self.max_output_tokens >= self.context_window - SAFETY_MARGIN:
            raise ValueError("Max output tokens must be below the context budget minus the safety margin.")
        return self


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
        manager.closing = True  # A Stop during shutdown records the reply as interrupted.
        await asyncio.gather(*(manager.stop(sid) for sid in list(manager.tasks)))
        await manager.jobs.shutdown()
        # Everything stopped: the next start reads only conversation headers. If a stop raised,
        # no marker is written and the next start loads every conversation (the safe direction).
        try:
            os.close(os.open(store.conversations / ".clean-shutdown",
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600))
        except OSError:
            pass
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
        try:
            session = manager.get(session_id)
        except UnreadableConversation as exc:
            # After a clean shutdown startup reads only headers, so a damaged body is found when the
            # chat is opened: hide it and report it like a file found unreadable at startup.
            store.record_problem(exc.file, exc.reason)
            raise
        if not session:
            raise HTTPException(404, "Conversation not found.")
        return session

    def workspace_tools(session_id=None):
        from .tools import write_limit_setting
        session = session_or_404(session_id) if session_id else {}
        workspace = session.get("workspace", settings.values["workspace"])
        return WorkspaceTools(workspace, settings.values["env_file"],
                              [*session.get("allowed_directories", []), *standing_directories(settings)],
                              session.get("permission_mode") == "bypassPermissions",
                              write_limit=write_limit_setting(settings.values), excluded_roots=(settings.state_dir,))

    def ui_tools(session_id=None):
        """workspace_tools plus the folders added to Files, for the user's own browsing, editor saves and Recovery only.
        The agent's tools and every other route keep the conversation's own grants."""
        session = session_or_404(session_id) if session_id else {}
        workspace = session.get("workspace", settings.values["workspace"])
        from .tools import write_limit_setting
        # workspace_folders is assigned by register_workspace_folders below, before any request.
        return WorkspaceTools(workspace, settings.values["env_file"],
                              [*session.get("allowed_directories", []), *standing_directories(settings),
                               *workspace_folders.paths(workspace)],
                              session.get("permission_mode") == "bypassPermissions",
                              write_limit=write_limit_setting(settings.values), excluded_roots=(settings.state_dir,))

    @app.get("/api/bootstrap")
    async def bootstrap():
        store.list()  # Refreshes the report of unreadable conversation files.
        return {"token": local_token, "settings": settings.public(), "storage_problems": store.problems()}

    def check_folder(path, workspace):
        folder = WorkspaceTools(workspace, settings.values["env_file"], excluded_roots=(settings.state_dir,)).resolve(path)
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

    @app.get("/api/settings")
    async def get_settings():
        # Read again so the Settings dialog and Folder access show current folder states.
        return settings.public()

    @app.post("/api/settings/limits")
    async def check_limits(body: LimitsQuery):
        model = body.model.strip()
        profile = public_profile(model)
        recommended = None
        if profile and profile["recommended_context_window"] and profile["recommended_max_output_tokens"]:
            recommended = {"context_window": profile["recommended_context_window"],
                           "max_output_tokens": profile["recommended_max_output_tokens"]}
        defaults = default_limits(model)
        return {"profile": profile, "recommended": recommended,
                "defaults": {"context_window": defaults[0], "max_output_tokens": defaults[1]},
                "effective": effective_limits(model, body.context_window, body.max_output_tokens),
                "warnings": limit_warnings(model, body.context_window, body.max_output_tokens),
                "note": limits_note(model)}

    @app.get("/api/sessions")
    async def list_sessions():
        counts = manager.changes.counts()
        return [{**s, "status": manager.statuses.get(s["id"], "idle"), "unreviewed_changes": counts.get(s["id"], 0)}
                for s in store.list()]

    @app.post("/api/sessions")
    async def create_session(body: SessionPermissions | None = None):
        manager.workspace_available(settings.values["workspace"])
        session = store.create(settings.values)
        session["permission_mode"] = body.permission_mode if body else "manual"
        store.save(session)
        return public_session(session)

    @app.put("/api/sessions/{session_id}/permissions")
    async def permissions(session_id: str, body: PermissionChange):
        # Allowed while a turn runs: waiting cards are re-checked and a running subagent follows.
        from .approvals import busy_status
        session = session_or_404(session_id)
        if (body.permission_mode == "bypassPermissions" and not body.confirm
                and session.get("permission_mode", "manual") != "bypassPermissions"):
            raise HTTPException(400, "Confirm switching to Bypass permissions.")
        await manager.change_mode(session, body.permission_mode)
        return public_session(session, busy_status(manager, session_id))

    @app.put("/api/sessions/{session_id}/model")
    async def change_model(session_id: str, body: SessionModel):
        session = session_or_404(session_id)
        ensure_idle(manager, session, "changing the model")
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

    @app.get("/api/sessions/{session_id}/info")
    async def session_info(session_id: str):
        """The conversation's metadata and status without loading its events."""
        if session_id in deleting_sessions:
            raise HTTPException(404, "Conversation not found.")
        live = manager.live.get(session_id)
        header = Store.summary(live) if live else store.header(session_id)
        if header is None:
            raise HTTPException(404, "Conversation not found.")
        return {**header, "status": manager.statuses.get(session_id, "idle")}

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str, before: str, limit: int = Query(default=100, ge=1, le=500)):
        """The page of up to limit events just before the event `before` (earlier history)."""
        events = session_or_404(session_id)["events"]
        index = next((number for number, event in enumerate(events) if event.get("id") == before), None)
        if index is None:
            raise ValueError("That message is no longer in this conversation.")
        start = max(0, index - limit)
        return {"events": events[start:index], "start": start, "total": len(events)}

    @app.put("/api/sessions/{session_id}/meta")
    async def session_meta(session_id: str, body: SessionMeta):
        """Pin, archive or tag a chat. Allowed during a response; keeps the sort order and unread state."""
        session = session_or_404(session_id)
        changes = body.model_dump(exclude_none=True)
        if not changes:
            raise HTTPException(400, "Change pin, archive or tags.")
        if "tags" in changes:
            changes["tags"] = clean_tags(changes["tags"])
        # Persist first, like a rename, so a failed save changes nothing.
        updated = {**session, **changes}
        store.save(updated, touch=False)
        session.update(changes)
        await manager.broadcast(session_id, {"type": "meta", "pinned": session.get("pinned") is True,
                                             "archived": session.get("archived") is True,
                                             "tags": session.get("tags") or []})
        return public_session(session, manager.statuses.get(session_id, "idle"))

    @app.get("/api/session-search")
    async def session_search(q: str = Query(min_length=2, max_length=200), workspace: str | None = None,
                             include_subagents: bool = False, limit: int = Query(default=20, ge=1, le=50)):
        if workspace is not None and not Path(workspace).is_absolute():
            raise ValueError("Choose a project by its absolute path.")
        return await asyncio.to_thread(conversation_search.search, store, q, workspace=workspace,
                                       include_subagents=include_subagents, limit=limit, redact=settings.redact)

    @app.post("/api/sessions/{session_id}/folders")
    async def allow_folder(session_id: str, body: FolderAccess):
        session = session_or_404(session_id)
        ensure_idle(manager, session, "changing folder access")
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
        ensure_idle(manager, session, "changing folder access")
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

    @app.get("/api/sessions/{session_id}/transcript.md")
    async def export_session_transcript(session_id: str):
        session = session_or_404(session_id)
        text = transcript(session, redact=settings.redact)
        return Response(text, media_type="text/markdown; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{transcript_file_name(session, redact=settings.redact)}"',
            "Cache-Control": "no-store"})

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, children: Literal["keep", "delete"] = "keep"):
        """children=delete also deletes its subagent chats; keep (the default) detaches them."""
        session_or_404(session_id)
        family = [session_id]
        deleting_sessions.add(session_id)
        try:
            await manager.stop(session_id)  # Also stops its active subagent.
            # Listed after the stop, so a subagent started meanwhile is included.
            if children == "delete":
                family = [*manager.descendants(session_id), session_id]  # Deepest first.
                deleting_sessions.update(family)
            for member in family:
                await manager.stop(member)
                await manager.jobs.remove_session(member)
                manager.task_board.delete_session(member)
                manager.checkpoints.delete_session(member)
                manager.changes.delete_session(member)
                store.delete(member)
                manager.live.pop(member, None)
                manager.statuses.pop(member, None)
        finally:
            deleting_sessions.difference_update(family)
        if children == "keep":
            for summary in store.list():
                if summary.get("parent_session_id") != session_id:
                    continue
                try:
                    child = manager.get(summary["id"])  # The live dict while it runs.
                except (ValueError, OSError):
                    continue  # Unreadable: left unchanged and reported by the store.
                if not child:
                    continue
                for key in ("parent_session_id", "parent_event_id", "parent_call_id"):
                    child.pop(key, None)
                child["parent_deleted"] = True
                delegation = child.get("delegation")
                if isinstance(delegation, dict) and not delegation.get("terminal_reason"):
                    child["delegation"] = {**delegation, "status": "cancelled", "terminal_reason": "stopped"}
                store.save(child, touch=False)
        return {"ok": True, "deleted": family}

    @app.post("/api/sessions/{session_id}/messages")
    async def message(session_id: str, body: Prompt):
        session_or_404(session_id)
        if not body.text.strip():
            raise ValueError("Enter a message.")
        # Starts the message, or queues it while a turn, compaction or ! command runs.
        return await manager.submit_message(session_id, body.text.strip())

    @app.post("/api/sessions/{session_id}/stop")
    async def stop(session_id: str):
        session_or_404(session_id)
        await manager.stop(session_id)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/compact", status_code=202)
    async def compact(session_id: str, body: CompactionRequest):
        ensure_idle(manager, session_or_404(session_id), "compacting context")
        manager.start_compaction(session_id, body.preservation_note)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/approvals/{event_id}")
    async def approval(session_id: str, event_id: str, body: Decision):
        session = session_or_404(session_id)
        manager.decide(session_id, event_id, body.allowed, body.remember, body.note, body.scope, body.rule)
        if body.scope in ("project", "user"):
            await manager.rules_changed(body.scope, session)
        return {"ok": True}

    @app.websocket("/api/sessions/{session_id}/stream")
    async def stream(socket: WebSocket, session_id: str):
        protocols = [p.strip() for p in socket.headers.get("sec-websocket-protocol", "").split(",")]
        if len(protocols) != 2 or protocols[0] != "local-workspace" or not secrets.compare_digest(protocols[1], local_token) or not origin_allowed(socket.headers.get("origin")):
            await socket.close(code=1008)
            return
        try:
            session = manager.get(session_id)
        except UnreadableConversation as exc:
            store.record_problem(exc.file, exc.reason)  # Its next /info answers 400 instead of reconnecting.
            session = None
        except (ValueError, OSError):
            session = None  # An unreadable conversation file.
        if not session:
            await socket.close(code=1008)
            return
        await socket.accept(subprotocol="local-workspace")
        manager.listeners.setdefault(session_id, set()).add(socket)
        try:
            # The first snapshot carries the latest 200 events, extended to the whole latest turn
            # (at most 1,000); the page loads earlier ones from /events. Later snapshots are complete.
            snapshot = public_session(session, manager.statuses.get(session_id, "idle"))
            events, total = snapshot["events"], len(snapshot["events"])
            start = max(0, total - 200)
            latest_user = next((index for index in range(total - 1, -1, -1) if events[index].get("type") == "user"), None)
            if latest_user is not None and latest_user < start:
                start = max(latest_user, total - 1000)
            snapshot.update(events=events[start:], events_start=start, events_total=total)
            await socket.send_json({"type": "snapshot", "session": snapshot})
            await manager.publish_changes(session)
            try:
                tasks = manager.task_board.list(session_id)
            except ValueError:  # Deleted while connecting.
                tasks = []
            if tasks:
                await socket.send_json({"type": "tasks", "tasks": tasks})
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            manager.listeners.get(session_id, set()).discard(socket)

    @app.websocket("/api/presence")
    async def presence(socket: WebSocket):
        """Held open by every page, so desktop notifications know a page is open."""
        protocols = [p.strip() for p in socket.headers.get("sec-websocket-protocol", "").split(",")]
        if len(protocols) != 2 or protocols[0] != "local-workspace" or not secrets.compare_digest(protocols[1], local_token) or not origin_allowed(socket.headers.get("origin")):
            await socket.close(code=1008)
            return
        await socket.accept(subprotocol="local-workspace")
        manager.pages.add(socket)
        try:
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            manager.pages.discard(socket)

    @app.get("/api/files")
    async def files(path: str = ".", session_id: str | None = None):
        return ui_tools(session_id).list_files(path)

    @app.get("/api/file")
    async def file(path: str, session_id: str | None = None):
        tools = ui_tools(session_id)
        # The absolute path lets the editor offer an "Open in VS Code" link.
        return {"path": path, "content": tools.read_file(path), "absolute": str(tools.path(path))}

    @app.put("/api/file")
    async def save_file(body: FileEdit):
        tools = ui_tools(body.session_id)
        if tools.read_file(body.path) != body.original:
            raise HTTPException(409, "This file changed on disk. Reopen it before saving.")
        manager.checkpoints.apply_edit(tools, "write_file", {"path": body.path, "content": body.content}, session_id=body.session_id)
        if body.session_id:
            await manager.publish_changes(session_or_404(body.session_id), only_tracked=True)
        return {"ok": True}

    @app.get("/api/git")
    async def git_changes(session_id: str | None = None):
        # No shell, and no programs named by the repository's configuration.
        return await working_tree_changes(workspace_tools(session_id).root)

    def git_classifier(tools):
        """'credential', 'generated' or None for a workspace-relative path, as the file tools see it."""
        def classify(path):
            name = PurePosixPath(path).name.casefold()
            if name.endswith(".env") or name.startswith(".env."):
                return "credential"  # Also left out of every diff by git_tools.DIFF_EXCLUDES.
            try:
                tools.resolve(path)
            except ValueError as exc:
                return "credential" if str(exc).startswith("Credential") else "generated"
            except (OSError, RuntimeError):
                pass
            return None
        return classify

    @app.get("/api/git/status")
    async def git_status(session_id: str | None = None):
        tools = workspace_tools(session_id)
        return await working_tree_status(tools.root, git_classifier(tools))

    @app.get("/api/git/diff")
    async def git_diff(path: str, area: Literal["staged", "unstaged", "untracked"], session_id: str | None = None,
                       orig_path: str | None = None):
        tools = workspace_tools(session_id)
        paths = [path] if orig_path is None or area == "untracked" else [path, orig_path]
        for value in paths:
            relative = PurePosixPath(value)
            if not value or "\0" in value or relative.is_absolute() or ".." in relative.parts or value.startswith("~"):
                raise ValueError("Choose a file inside this project's repository.")
        kinds = [git_classifier(tools)(value) for value in paths]
        if "credential" in kinds:
            raise ValueError("Credential files are not shown.")
        if "generated" in kinds:
            raise ValueError("Files in generated folders are not shown here.")
        for value in paths:
            target = tools.root / value
            if value.endswith("/") or PurePosixPath(value) == PurePosixPath(".") or (target.is_dir() and not target.is_symlink()):
                raise ValueError("Choose one changed file, not a folder.")
        # Leave the configured credential file out of the diff even under another name.
        credential = tools.credential_file
        excludes = ([f":(exclude,literal){credential.relative_to(tools.root).as_posix()}"]
                    if credential and credential.is_relative_to(tools.root) and credential != tools.root else [])
        return await file_diff(tools.root, path, area, paths[1] if len(paths) > 1 else None, tools=tools, excludes=excludes)

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
            raise HTTPException(408, TIMEOUT_NOT_STOPPED if job["output"].endswith(NOT_STOPPED) else "Command stopped after 60 seconds.")
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

    register_features(app, manager, settings, store, session_or_404, workspace_tools, ui_tools)
    register_portability(app, manager, settings, store, session_or_404)
    register_approvals(app, manager, store, session_or_404)
    register_composer(app, manager, settings, store, session_or_404)
    register_interactions(app, manager, session_or_404)
    register_projects(app, manager, settings, workspace_tools)
    workspace_folders = register_workspace_folders(app, settings, session_or_404)

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
