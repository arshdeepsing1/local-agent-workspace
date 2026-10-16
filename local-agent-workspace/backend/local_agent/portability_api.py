"""Authenticated routes for local conversation copies, forks and rewinds (no model or tool calls,
except the explicit resend after a rewind)."""
import copy
from pathlib import Path
import sqlite3
import time
import uuid

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import approvals, mentions
from .agents import delegated_prompt, public_session
from .approvals import busy_status, ensure_idle
from .history import HistoryError, cut_points, cut_session, rewind_turn_ids
from .portability import MAX_BUNDLE_BYTES, MAX_SESSIONS, completed_for_fork, export_bundle, fresh_copies, parse_bundle
from .recovery import rewind_refusal
from .tools import WorkspaceTools, error_clause, file_error


class ForkRequest(BaseModel):
    """Fork from a point: before_event_id (a user message; none = the whole chat), an optional title,
    and optionally a new Git worktree on this branch."""
    model_config = ConfigDict(extra="forbid")
    before_event_id: str | None = Field(default=None, min_length=1, max_length=512, strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=160, strict=True)
    worktree_branch: str | None = Field(default=None, min_length=1, max_length=200, strict=True)


class RewindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    before_event_id: str = Field(min_length=1, max_length=512, strict=True)
    conversation: bool = Field(default=False, strict=True)
    code: bool = Field(default=False, strict=True)
    resend_text: str | None = Field(default=None, min_length=1, max_length=50000, strict=True)
    resend_verbatim: bool = Field(default=False, strict=True)  # Resent as plain text, as the message was sent.


# Never copied into a rewind backup: the usage ledger and jobs stay with the live chat, and authority,
# links and local state are not carried (its subagents stay linked to the live chat, so the backup has
# no subagents index). The worktree header stays: it is display metadata (grouping under the project).
BACKUP_DROPPED = ("pinned", "inference_calls", "command_jobs", "context_info", "file_views", "instruction_directories",
                  "turn_running", "queued_messages", "pending_context", "parent_session_id", "parent_event_id",
                  "parent_call_id", "delegation", "subagents")


def backup_copy(session):
    """An archived full copy of a chat, written before its conversation is rewound in place."""
    backup = copy.deepcopy(session)
    for key in BACKUP_DROPPED:
        backup.pop(key, None)
    backup.update(id=str(uuid.uuid4()), title=(str(session.get("title") or "") + " (before rewind)")[:160],
                  archived=True, is_subagent=False, permission_mode="manual", allowed_directories=[], allow_rules=[],
                  active_skills=[], provenance={"kind": "rewind_backup", "source_session_id": session["id"],
                                                "copied_at": time.time()})
    return backup


def managed_worktree_of(manager, workspace):
    """The app's worktree record whose folder is this workspace, or None."""
    folder = str(Path(workspace).expanduser().resolve())
    return next((record for record in manager.worktrees.list() if record["path"] == folder), None)


def root_project(manager, workspace, header=None):
    """The project a chat in this workspace belongs to, never an app-internal worktree folder: the chat's
    worktree header, else the managed worktree's source (chats made before the header existed), else the
    workspace itself. Used by Fork into worktree and New chat in worktree."""
    record = managed_worktree_of(manager, workspace)
    return (header or {}).get("source_workspace") or (record or {}).get("source_workspace") or workspace


def clean_title(value):
    """Like renaming: whitespace collapsed, no control characters."""
    title = " ".join(value.split())
    if not title or any(ord(char) < 32 or ord(char) == 127 for char in title):
        raise ValueError("Enter a conversation title without control characters.")
    return title


def register_portability(app, manager, settings, store, session_or_404):
    def idle(session_id):
        session = session_or_404(session_id)
        ensure_idle(manager, session, "exporting or forking this conversation")
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
    async def fork(session_id: str, body: ForkRequest | None = None):
        if body is None:
            source = idle(session_id)
            completed_for_fork(source)
            result = insert(export_bundle([source], session_id), destination(source["workspace"]), "fork")
            return result["session"]
        live = session_or_404(session_id)
        # Like the other history actions: no running turn and no waiting approval, question or plan card.
        ensure_idle(manager, live, "forking from this point")
        title = clean_title(body.title) if body.title is not None else None
        # Copied before any await, so later changes to the live chat never reach the fork.
        source = copy.deepcopy(live)
        try:
            cut, _ = cut_session(source, body.before_event_id)
            bundle = export_bundle([cut], session_id)
        except HistoryError as exc:
            raise HTTPException(400, str(exc)) from exc
        project = root_project(manager, source["workspace"], source.get("worktree"))
        worktree, created = None, None
        if body.worktree_branch is not None:
            # A new branch from the source chat's own checkout at its last commit.
            created = await manager.worktrees.create(source["workspace"], body.worktree_branch)
            worktree, folder = created, created["path"]
            try:
                manager.workspace_available(folder)
            except BaseException:
                await remove_worktree(created)
                raise
        elif (worktree := managed_worktree_of(manager, source["workspace"])) is not None:
            # destination() refuses the state folder, where managed worktrees live.
            worktree = manager.worktrees.validate(worktree["id"])
            folder = worktree["path"]
            manager.workspace_available(folder)
        else:
            folder = destination(source["workspace"])
        try:
            result = insert(bundle, folder, "fork")
        except BaseException:
            if created is not None:
                await remove_worktree(created)
            raise
        session = store.get(result["session"]["id"])
        if title is not None:
            session.update(title=title, title_generated=True)
        if worktree is not None:
            session["worktree"] = {"id": worktree["id"], "branch": worktree["branch"], "source_workspace": project}
        if title is not None or worktree is not None:
            store.save(session)
        return public_session(session)

    async def remove_worktree(record):
        try:
            await manager.worktrees.remove(record["id"])
        except (ValueError, OSError):
            pass  # Best effort: the worktree stays listed in Agent tools and can be removed there.

    def checkpoint_records(session, tools):
        return [item for item in manager.checkpoints.list(session["id"], str(tools.root))
                if item["session_id"] == session["id"]]

    def redacted(files):
        return [{**item, "error": settings.redact(item["error"])} if "error" in item else item for item in files]

    @app.get("/api/sessions/{session_id}/rewind-preview")
    async def rewind_preview(session_id: str, before_event_id: str):
        """What a rewind to this user message would remove and restore. Read-only, also while busy."""
        session = session_or_404(session_id)
        conversation = {"possible": False, "reason": None, "events_removed": 0, "messages_removed": 0,
                        "summary_dropped": False}
        try:
            _, info = cut_session(session, before_event_id, keep_local=True)
            conversation.update(possible=True, **info)
        except HistoryError as exc:
            conversation["reason"] = str(exc)
        code = {"possible": False, "reason": None, "files": []}
        try:
            tools = manager.session_tools(session)
            turns = rewind_turn_ids(session, before_event_id, checkpoint_records(session, tools))
            plan = manager.checkpoints.rewind_plan(tools, session_id, turns)
            code.update(possible=plan["can_rewind"] and bool(plan["files"]), files=redacted(plan["files"]))
            if not plan["files"]:
                code["reason"] = "No file edits by the agent from this message on."
            elif not plan["can_rewind"]:
                code["reason"] = rewind_refusal([item["path"] for item in plan["files"]
                                                 if item["status"] not in ("restore", "unchanged")])
        except HistoryError as exc:
            code["reason"] = str(exc)
        except (ValueError, OSError) as exc:
            code["reason"] = settings.redact(file_error(exc))
        return {"before_event_id": before_event_id, "conversation": conversation, "code": code}

    @app.post("/api/sessions/{session_id}/rewind")
    async def rewind(session_id: str, body: RewindRequest):
        """Rewind the conversation in place (after an archived backup), the agent's file edits, or both,
        then optionally send a new message. Steps 1 to 6 run without an await."""
        if not body.conversation and not body.code:
            raise HTTPException(400, "Choose what to rewind.")
        if body.resend_text is not None and not body.conversation:
            raise HTTPException(400, "Resending needs a conversation rewind.")
        if body.resend_text is not None and not body.resend_text.strip():
            raise HTTPException(400, "Enter a message.")
        session = session_or_404(session_id)
        ensure_idle(manager, session, "rewinding this conversation")
        try:
            if body.conversation:
                event_cut, _ = cut_points(session, body.before_event_id)
            if body.code:
                tools = manager.session_tools(session)
                approvals.workspace_idle(manager, store, tools.root, "rewinding files")
                turns = rewind_turn_ids(session, body.before_event_id, checkpoint_records(session, tools))
        except HistoryError as exc:
            raise HTTPException(400, str(exc)) from exc
        if body.resend_text is not None:
            # The resend's own checks (a refused @ file, the attachment limits, a missing project folder,
            # a command template or skill that cannot be used) run before anything changes, as start runs
            # them, so a refusal leaves the chat as it was and the edit open.
            remaining = {**session, "events": session["events"][:event_cut]}
            try:
                manager.workspace_available(session["workspace"])
                if not delegated_prompt(remaining):
                    text, resend_tools = body.resend_text.strip(), manager.session_tools(session)
                    prepared = manager.prepared_message(resend_tools, text, body.resend_verbatim)
                    mentions.expand_context(manager, remaining, resend_tools, text, include_pending=False,
                                            scan=manager.message_parts([prepared])[0])
                    if not body.resend_verbatim and manager.slash_commands():
                        manager.message_skills(remaining, resend_tools, text)
            except ValueError as exc:
                raise HTTPException(400, settings.redact(str(exc))) from exc
        files = []
        if body.code:
            plan = manager.checkpoints.rewind_plan(tools, session_id, turns)
            if not plan["can_rewind"]:
                refused = [item["path"] for item in plan["files"] if item["status"] not in ("restore", "unchanged")]
                raise HTTPException(409, detail={"message": settings.redact(rewind_refusal(refused)), "files": refused})
            files = [{"path": item["path"], "status": item["status"]} for item in plan["files"]]
        backup_id = None
        if body.conversation:
            backup = backup_copy(session)
            try:
                store.insert_sessions([backup])
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "A generated conversation ID already exists. Retry the rewind.") from exc
            backup_id = backup["id"]

        def drop_backup():
            if backup_id:
                try:
                    store.delete(backup_id)
                except OSError:
                    pass

        if body.code:
            try:
                manager.checkpoints.rewind(tools, session_id, turns)
            except BaseException:
                drop_backup()
                raise
        if body.conversation:
            cut, _ = cut_session(session, body.before_event_id, keep_local=True)
            session.clear()
            session.update(cut)
            try:
                store.save(session)
            except Exception as exc:
                if body.code:
                    raise HTTPException(500, detail={"message": settings.redact(
                        f"Files were rewound, but the conversation could not be saved: {error_clause(exc)}. "
                        "Rewound files can be restored from Agent tools → Checkpoints.")}) from exc
                drop_backup()
                raise
        await manager.broadcast(session_id, {"type": "snapshot", "session": public_session(session, "idle")})
        await manager.publish_changes(session)
        result = {"backup_session_id": backup_id, "files": files, "resent": False}
        if body.resend_text is not None:
            try:
                manager.start(session_id, body.resend_text.strip(), verbatim=body.resend_verbatim)
                result["resent"] = True
            except ValueError as exc:
                result["resend_error"] = settings.redact(str(exc))
        return {"session": public_session(session, busy_status(manager, session_id)), **result}
