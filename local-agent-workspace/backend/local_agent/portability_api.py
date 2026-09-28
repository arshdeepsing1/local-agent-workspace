"""Authenticated routes for local conversation copies (no model or tool calls)."""
from pathlib import Path
import sqlite3

from fastapi import HTTPException, Request

from .agents import public_session
from .portability import MAX_BUNDLE_BYTES, MAX_SESSIONS, completed_for_fork, export_bundle, fresh_copies, parse_bundle
from .tools import WorkspaceTools


def register_portability(app, manager, settings, store, session_or_404):
    def idle(session_id):
        session = session_or_404(session_id)
        task = manager.tasks.get(session_id)
        if manager.statuses.get(session_id, "idle") != "idle" or task and not task.done():
            raise HTTPException(409, "Stop the current response before exporting or forking this conversation.")
        return session

    def destination(value):
        if not value or len(value) > 4096 or not Path(value).expanduser().is_absolute():
            raise ValueError("Explicitly choose an absolute destination workspace path.")
        folder = WorkspaceTools(value, settings.values["env_file"]).resolve(".")
        if folder.is_relative_to(settings.state_dir.resolve()):
            raise ValueError("Choose a project folder, not the application's private state directory.")
        if not folder.is_dir():
            raise ValueError("Choose an existing destination workspace folder.")
        next(folder.iterdir(), None)
        manager.workspace_available(str(folder))
        return str(folder)

    def insert(bundle, workspace, kind):
        copies, root_id = fresh_copies(bundle, workspace, kind=kind)
        try:
            store.insert_sessions(copies)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "A generated conversation ID already exists. Retry to create a fresh copy.") from exc
        return {"session": public_session(next(session for session in copies if session["id"] == root_id)),
                "imported_count": len(copies)}

    @app.get("/api/sessions/{session_id}/export")
    async def export(session_id: str, include_children: bool = False):
        sessions = [idle(session_id)]
        if include_children:
            included = {session_id}
            metadata = store.list()
            for parent in sessions:
                for child in metadata:
                    if child.get("parent_session_id") == parent["id"] and child["id"] not in included:
                        if len(sessions) >= MAX_SESSIONS:
                            raise ValueError("Export at most 32 conversations at once. Exclude subagents or export a child separately.")
                        sessions.append(idle(child["id"]))
                        included.add(child["id"])
        return export_bundle(sessions, session_id)

    @app.post("/api/sessions/import", status_code=201)
    async def import_conversations(request: Request, workspace: str):
        folder = destination(workspace)
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > MAX_BUNDLE_BYTES):
            raise HTTPException(413, "Conversation bundle exceeds the 16 MiB limit.")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BUNDLE_BYTES:
                raise HTTPException(413, "Conversation bundle exceeds the 16 MiB limit.")
            raw.extend(chunk)
        return insert(parse_bundle(raw), folder, "import")

    @app.post("/api/sessions/{session_id}/fork", status_code=201)
    async def fork(session_id: str):
        source = idle(session_id)
        completed_for_fork(source)
        result = insert(export_bundle([source], session_id), destination(source["workspace"]), "fork")
        return result["session"]
