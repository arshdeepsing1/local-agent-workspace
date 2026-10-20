import asyncio
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import time
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agents import AgentManager, public_session
from .config import APP_ROOT, ENDPOINT_NAME, MAX_APPROVAL_MINUTES, MAX_MAX_AGENT_STEPS, MIN_MAX_AGENT_STEPS, Settings
from .config import MAX_COMMAND_ENV, MAX_USER_INSTRUCTIONS, MAX_WRITE_LIMIT_KB, MIN_WRITE_LIMIT_KB
from .config import MAX_TURN_MINUTES, MAX_TURN_TOKENS
from .context import (
    MAX_CONTEXT_WINDOW, MAX_MAX_OUTPUT_TOKENS, MIN_CONTEXT_WINDOW, MIN_MAX_OUTPUT_TOKENS, SAFETY_MARGIN,
    effective_input_budget, model_context_window,
)
from .context import MAX_AUTO_COMPACT_PERCENT, MIN_AUTO_COMPACT_PERCENT
from .model_profiles import default_limits, effective_limits, limit_warnings, limits_note, public_profile
from . import reasoning, serving, settings_layers
from .user_paths import standing_directories
from .store import Store, UnreadableConversation
from . import conversation_search
from .git_tools import (GH_ENVIRONMENT_KEYS, commit_staged, file_diff, find_git, git_environment, pr_status, stage,
                        staged_files, working_tree_changes, working_tree_status)
from .jobs import NOT_STOPPED, TIMEOUT_NOT_STOPPED
from .tools import NotText, WorkspaceTools, file_error
from .workspace_folders import register_workspace_folders
from .permissions import PermissionMode
from .telemetry import SUMMARY_VERSION, session_metrics, usage_summary
from .usage_overview import MAX_DAYS, overview
from . import spending, turns
from .spending import MAX_LIMIT_DBU
from .usage_export import usage_csv, usage_rows
from .transcript import transcript, transcript_file_name
from .feature_api import register_features
from .portability_api import register_portability
from .approvals import MAX_DECLINE_NOTE, ensure_idle, register_approvals, workspace_idle
from .composer_api import UploadRef, register_composer, upload_refs, upload_refs_field
from .interactions import register_interactions
from .projects import register_projects
from .endpoint_checks_api import register_endpoint_checks
from .stream_api import register_stream, stream_snapshot
from .schedules import Scheduler
from .schedules_api import register_schedules
from .user_paths import checked_workspace


USAGE_SCAN = 50  # Chats without a usage summary loaded per GET /api/usage (each once: its summary is saved).

# Browsers refuse ES modules served with a non-JavaScript type, and some systems
# map .js to text/plain in their mimetypes registry.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/manifest+json", ".webmanifest")  # The web app manifest (Install app).


# GET /api/file/raw serves these inline when a file's bytes start with the signature of its type (WebP: RIFF,
# then WEBP at byte 8); every other file is a download.
INLINE_TYPES = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff", "image/gif": (b"GIF87a", b"GIF89a"),
                "image/webp": b"RIFF", "application/pdf": b"%PDF-"}


INDEX_POLICY = "frame-src blob:; frame-ancestors 'none'"


def content_disposition(disposition, name):
    """A Content-Disposition header for a file name: an ASCII fallback and the UTF-8 name (RFC 6266)."""
    fallback = "".join(char if 32 <= ord(char) < 127 and char not in '"\\;%' else "_" for char in name) or "file"
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe="")}'


class RevalidatedStaticFiles(StaticFiles):
    """Frontend files keep stable names, so browsers must check for edits on each load."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class Prompt(BaseModel):
    text: str = Field(min_length=1, max_length=50000)
    # Sent as typed: no command template, no skill selected by /<id>, no @ file attached.
    verbatim: bool = Field(default=False, strict=True)
    # Files attached from the composer (POST /api/sessions/{id}/uploads), also with plain text.
    uploads: list[UploadRef] = upload_refs_field()
    # A script's token for this message, kept on the user event that sends it (client_refs): its client tells its
    # own turns from others by it. Never sent to the model.
    client_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$", strict=True)


class CompactionRequest(BaseModel):
    preservation_note: str = Field(default="", max_length=1000, strict=True)


class Decision(BaseModel):
    allowed: bool
    remember: bool = Field(default=False, strict=True)  # Approve and store an exact allow rule for this chat.
    note: str | None = Field(default=None, max_length=MAX_DECLINE_NOTE, strict=True)  # Decline only.
    scope: Literal["chat", "project", "user"] | None = None  # Approve and save an allow rule there.
    rule: dict | None = None  # The rule to save (at most 8 KB of JSON); the card's default rule when absent.


Effort = Literal["default", "low", "medium", "high", "xhigh", "max"]  # reasoning.EFFORT_LEVELS
RequestFormat = Literal["auto", "anthropic", "anthropic_budget", "openai", "none"]  # reasoning.REQUEST_FORMATS


class SessionPermissions(BaseModel):
    permission_mode: PermissionMode = "manual"
    reasoning_effort: Effort | None = None  # A new chat's effort; Settings' default when absent.
    model: str | None = Field(default=None, min_length=1, max_length=256, strict=True)  # /clear: the old chat's model.
    # Scripts (client.py): the chat's project folder, checked like Settings' (the Settings workspace when absent),
    # and its own Agent steps per message (Settings' when absent).
    workspace: str | None = Field(default=None, min_length=1, max_length=4096, strict=True)
    max_agent_steps: int | None = Field(default=None, ge=MIN_MAX_AGENT_STEPS, le=MAX_MAX_AGENT_STEPS, strict=True)


class SessionEffort(BaseModel):
    model_config = {"extra": "forbid"}
    reasoning_effort: Effort


class SessionSpendingLimit(BaseModel):
    """The chat's own limit in DBU (0 = none), or null to follow the Settings default."""
    model_config = {"extra": "forbid"}
    spending_limit_dbu: float | None = Field(ge=0, le=MAX_LIMIT_DBU, allow_inf_nan=False, strict=True)


class SessionTurnLimits(BaseModel):
    """The chat's own turn limits (whole tokens or minutes, 0 = none), or null to follow the Settings default.
    A field left out keeps its value."""
    model_config = {"extra": "forbid"}
    turn_token_limit: int | None = Field(default=None, ge=0, le=MAX_TURN_TOKENS, strict=True)
    turn_time_limit_minutes: int | None = Field(default=None, ge=0, le=MAX_TURN_MINUTES, strict=True)


class ReasoningQuery(BaseModel):
    """What agent requests to an endpoint would carry (Settings → Reasoning and the effort picker)."""
    model_config = {"extra": "forbid"}
    model: str = Field(min_length=1, max_length=256, strict=True)
    reasoning_effort: Effort = "default"
    reasoning_format: RequestFormat = "auto"
    thinking_summaries: bool = Field(default=True, strict=True)


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
    auto_compact_percent: int | None = Field(default=None, ge=MIN_AUTO_COMPACT_PERCENT, le=MAX_AUTO_COMPACT_PERCENT,
                                             strict=True)
    approval_timeout_minutes: int | None = Field(default=None, ge=0, le=MAX_APPROVAL_MINUTES, strict=True)
    system_notifications: bool | None = Field(default=None, strict=True)
    auto_keep_changes: bool | None = Field(default=None, strict=True)
    user_instructions: str | None = Field(default=None, max_length=MAX_USER_INSTRUCTIONS, strict=True)
    user_agents_file: bool | None = Field(default=None, strict=True)
    user_claude_file: bool | None = Field(default=None, strict=True)
    project_memory: bool | None = Field(default=None, strict=True)
    git_context: bool | None = Field(default=None, strict=True)  # A Git snapshot in each turn's first message.
    output_style: str | None = Field(default=None, min_length=1, max_length=40, strict=True)  # output_styles.STYLE_ID
    write_limit_kb: int | None = Field(default=None, ge=MIN_WRITE_LIMIT_KB, le=MAX_WRITE_LIMIT_KB, strict=True)
    command_env: list[str] | None = Field(default=None, max_length=MAX_COMMAND_ENV, strict=True)
    secret_file_patterns: list[str] | None = Field(default=None, max_length=32, strict=True)  # secret_paths
    rate_limit_auto_continue: bool | None = Field(default=None, strict=True)
    reasoning_effort: Effort | None = None
    reasoning_format: RequestFormat | None = None
    thinking_summaries: bool | None = Field(default=None, strict=True)
    thinking_replay: bool | None = Field(default=None, strict=True)  # Send earlier thinking back (reasoning_replay.py).
    prompt_caching: Literal["auto", "messages", "off"] | None = None  # config.PROMPT_CACHING
    slash_commands: bool | None = Field(default=None, strict=True)
    spending_limit_dbu: float | None = Field(default=None, ge=0, le=MAX_LIMIT_DBU, allow_inf_nan=False, strict=True)
    turn_token_limit: int | None = Field(default=None, ge=0, le=MAX_TURN_TOKENS, strict=True)  # turns.py; 0 = none
    turn_time_limit_minutes: int | None = Field(default=None, ge=0, le=MAX_TURN_MINUTES, strict=True)
    coding_dir: str | None = Field(default=None, max_length=4096, strict=True)
    python_venv: str | None = Field(default=None, max_length=4096, strict=True)
    # Settings → Subagents: {explore|general|editor: {model, effort}}; config.subagent_defaults checks it.
    subagent_defaults: dict[str, dict[str, str]] | None = None
    background_subagent_wake: bool | None = Field(default=None, strict=True)  # planning.DelegateManager.maybe_wake
    # Settings → Model and context: "" = none; config.Settings.update and AgentManager.fallback_allowed check it.
    fallback_model: str | None = Field(default=None, max_length=128, strict=True)
    # Settings → Web access: config.Settings.check_web_settings checks them; the key is a credential-file entry name.
    web_fetch: bool | None = Field(default=None, strict=True)
    web_search_provider: Literal["", "brave", "tavily", "searxng"] | None = None
    web_search_url: str | None = Field(default=None, max_length=2048, strict=True)
    web_search_key: str | None = Field(default=None, max_length=64, strict=True)
    # Settings → Agent → Scheduled runs and Settings → Credentials → Token expires on ("" or YYYY-MM-DD): schedules.py.
    schedules_enabled: bool | None = Field(default=None, strict=True)
    token_expires_on: str | None = Field(default=None, max_length=10, strict=True)


class ProjectTrust(BaseModel):
    """Trust or Ignore a project's settings files (settings_layers.py), as reviewed: sha256 is the report's."""
    model_config = {"extra": "forbid"}
    decision: Literal["trust", "ignore"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)


class GitStage(BaseModel):
    """Changes view: stage (staged true) or unstage these workspace-relative files."""
    model_config = {"extra": "forbid"}
    paths: list[str] = Field(min_length=1, max_length=4_000, strict=True)  # Both sides of 2,000 renames.
    staged: bool = Field(strict=True)


class GitCommit(BaseModel):
    model_config = {"extra": "forbid"}
    message: str = Field(min_length=1, max_length=10_000, strict=True)


# The pull request chip (git_tools.pr_status): one gh call per project a minute, or every 5 s with Refresh.
PR_CACHE_SECONDS = 60
PR_REFRESH_SECONDS = 5
GIT_PATH_BYTES = 100_000  # The paths of one staging request, in total (a command line has a size limit).


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


# A command's leading `cd <folder> &&` (or `;`, or a new line): where the model ran it.
LEADING_CD = re.compile(r"""\s*cd\s+(?:"([^"\n]+)"|'([^'\n]+)'|([^\s;&|'"]+))\s*(?:&&|;|\n)""")
# Tools whose path argument names a folder; the others name a file.
FOLDER_TOOLS = {"list_files", "search_files", "find_files", "access_directory"}
CHAT_FOLDERS = 40
# The latest events chat_folders reads: a long chat's oldest folders matter least.
CHAT_FOLDER_EVENTS = 2000


def chat_folders(session, root):
    """The folders a chat worked in, most recent first, as workspace paths: where its commands ran (cwd, or a
    leading cd), the folders its tools named and those of the files they named, then its instruction folders,
    and after all of them their parent folders. At most CHAT_FOLDERS before the parents; the project root and
    folders outside it are left out."""
    root, found = PurePosixPath(str(root)), []

    def add(value, folder):
        if not isinstance(value, str) or not value.strip() or "\x00" in value or len(found) >= CHAT_FOLDERS:
            return
        path = PurePosixPath(value.strip().replace("\\", "/"))
        if path.is_absolute():
            try:
                path = path.relative_to(root)
            except ValueError:
                return
        path = path if folder else path.parent
        if ".." not in path.parts and str(path) != "." and str(path) not in found:
            found.append(str(path))

    for event in reversed((session.get("events") or [])[-CHAT_FOLDER_EVENTS:]):
        arguments = event.get("input") if isinstance(event, dict) and event.get("type") == "tool" else None
        if len(found) >= CHAT_FOLDERS:
            break
        if not isinstance(arguments, dict):
            continue
        cwd = arguments.get("cwd") if isinstance(arguments.get("cwd"), str) and arguments["cwd"].strip() else None
        command = arguments.get("command")
        if isinstance(command, str) and (match := LEADING_CD.match(command)):
            target = next(group for group in match.groups() if group)
            # A relative cd starts from the command's cwd.
            add(target if cwd is None or PurePosixPath(target).is_absolute() else f"{cwd.rstrip('/')}/{target}", True)
        add(cwd, True)
        add(arguments.get("path"), event.get("name") in FOLDER_TOOLS)
    for folder in reversed(session.get("instruction_directories") or ()):  # Added as the chat reached them.
        add(folder, True)
    parents = dict.fromkeys(str(parent) for folder in found for parent in PurePosixPath(folder).parents)
    return found + [parent for parent in parents if parent != "." and parent not in found]


def locate_file(tools, path, worktree=None, folders=()):
    """The project file a not-found path most likely names, as a workspace path, or None: a path that
    starts with the project's own last folders (`<project>/src/app.py`, or `<sub>/src/app.py` from the
    Git repository's root) without them, in a worktree chat the same file under the worktree
    instead of its source project (by absolute path or by the source folder's name), or a relative
    path that names a file under one of folders (chat_folders: where the chat worked), the first first."""
    value = PurePosixPath(path.replace("\\", "/"))
    candidates = []
    source = worktree.get("source_workspace") if isinstance(worktree, dict) else None
    roots = [PurePosixPath(str(tools.root)).parts]
    if isinstance(source, str) and source:
        roots.append(PurePosixPath(source).parts)
        if value.is_absolute():
            try:
                candidates.append(str(value.relative_to(PurePosixPath(source))))
            except ValueError:
                pass
    if not value.is_absolute():
        parts = tuple(part for part in value.parts if part != ".")
        candidates += ["/".join(parts[count:]) for root in roots for count in range(1, min(4, len(parts)))
                       if len(root) > count and parts[:count] == root[-count:]]
        candidates += [f"{folder}/{'/'.join(parts)}" for folder in folders if parts]
    for candidate in candidates:
        if not candidate or ".." in PurePosixPath(candidate).parts:
            continue
        try:
            if tools.path(candidate).is_file():
                return candidate
        except (ValueError, OSError):
            continue
    return None


def create_app(settings=None):
    settings = settings or Settings()
    store = Store(settings.state_dir / "conversations.sqlite3")
    manager = AgentManager(store, settings)
    scheduler = manager.scheduler = Scheduler(manager, settings)
    local_token = secrets.token_urlsafe(32)
    deleting_sessions = set()

    @asynccontextmanager
    async def lifespan(app):
        await scheduler.sync()  # Settings → Agent → Scheduled runs (off by default).
        yield
        await scheduler.stop()  # No new scheduled run starts during the shutdown.
        manager.closing = True  # A Stop during shutdown records the reply as interrupted.
        await asyncio.gather(*(manager.stop(sid) for sid in list(manager.tasks)))
        await manager.endpoint_runner.stop()  # An endpoint check keeps what came back so far.
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
        # error_kind says which step failed: 'credentials' (the credential file), 'http' (Databricks answered
        # with an error status), 'network' (no answer) or 'invalid_response' (an answer the app cannot read).
        # An unreadable credential file (OSError) still reaches the global handler (HTTP 400).
        try:
            host, token = manager.serving.credentials()
        except ValueError as exc:
            return {"connected": False, "models": [], "error": settings.redact(str(exc)), "error_kind": "credentials"}
        try:
            async with manager.serving.client(serving.LISTING_TIMEOUT) as client:
                response = await client.get(serving.endpoints_url(host), headers=serving.request_headers(token))
        except httpx.HTTPError as exc:
            return {"connected": False, "models": [], "error": settings.redact(str(exc) or type(exc).__name__),
                    "error_kind": "network"}
        except ValueError as exc:
            # The request could not be built from the credentials, for example a token with a non-ASCII character
            # (not allowed in an HTTP header). A UnicodeError's message quotes that character, so it is not shown.
            error = ("The Databricks host or token in your credential file contains characters that cannot be sent in a request."
                     if isinstance(exc, UnicodeError) else settings.redact(str(exc)))
            return {"connected": False, "models": [], "error": error, "error_kind": "credentials"}
        if response.status_code != 200:
            status = response.status_code
            # A server problem or a rate limit usually passes; other statuses point at the credentials or access.
            error = (f"Databricks had a server problem (HTTP {status}). Try again in a moment." if status >= 500
                     else f"Databricks rate limited the check (HTTP {status}). Try again in a moment." if status == 429
                     else f"Databricks returned HTTP {status}. Check your credentials and access.")
            return {"connected": False, "models": [], "error": error, "error_kind": "http", "http_status": status}
        try:
            data = response.json()
        except ValueError:
            data = None
        endpoints = data.get("endpoints", []) if isinstance(data, dict) else None
        if not isinstance(endpoints, list):
            return {"connected": False, "models": [], "error": "Databricks returned a response the app could not read.",
                    "error_kind": "invalid_response"}
        models = [item.get("name", "") if isinstance(item, dict) else "" for item in endpoints]
        models = [name for name in models if isinstance(name, str) and name and not any(term in name for term in ("embedding", "gte-", "bge-"))]
        return {"connected": True, "models": models, "error": None}

    @app.put("/api/settings")
    async def update_settings(body: SettingsEdit):
        manager.workspace_available(body.workspace)
        fallback = (body.fallback_model or "").strip()
        # A new fallback endpoint is one the app knows or the user already uses (this save's models count).
        sent = [entry.get("model") for entry in (body.subagent_defaults or {}).values() if isinstance(entry, dict)]
        if (ENDPOINT_NAME.match(fallback) and fallback != settings.values.get("fallback_model")
                and not manager.fallback_allowed(fallback, [body.model, *sent])):
            raise ValueError(f"The fallback endpoint {fallback} is neither an endpoint the app knows nor one "
                             "you use (the Settings model, Settings → Subagents or one of your chats). Requests carry "
                             "the conversation, so the app sends them only to endpoints you already chose.")
        result = settings.update(body.model_dump(exclude_none=True))
        await scheduler.sync()  # Scheduled runs turned on or off.
        # A request format Databricks rejected, and cache markers and images it rejected, are tried again after any
        # Settings save.
        manager.reasoning_rejections.clear()
        manager.cache_levels.clear()
        manager.replay_rejections.clear()
        manager.vision_rejections.clear()
        await refresh_open_meters()
        return result

    async def refresh_open_meters():
        """Open chats' meters follow new budgets and thresholds; others follow when opened (stream)."""
        for session_id in [key for key, sockets in manager.listeners.items() if sockets]:
            try:
                session = manager.get(session_id)
            except (ValueError, OSError):
                continue
            if session:
                await manager.refresh_meter_limits(session)

    def layers_folder(session_id):
        """The project whose settings files a chat (or, without one, a new chat in the Settings project) uses."""
        return settings_layers.project_folder(session_or_404(session_id) if session_id else None, settings)

    @app.get("/api/settings/project")
    async def project_settings_files(session_id: str | None = None):
        """The project's settings files (.agents/settings.json and .agents/settings.local.json): what they set and
        whether they apply (settings_layers.ProjectLayers.report)."""
        return settings.project_layers().report(layers_folder(session_id), fresh=True)

    @app.post("/api/settings/project/trust")
    async def trust_project_settings(body: ProjectTrust, session_id: str | None = None):
        """Trust or Ignore the project's settings files as reviewed (body.sha256: the report's). HTTP 409 with the
        new report when they changed meanwhile."""
        folder = layers_folder(session_id)
        try:
            report = settings.project_layers().decide(folder, body.decision, body.sha256)
        except settings_layers.ChangedFiles as exc:
            raise HTTPException(409, {"message": str(exc),
                                      "report": settings.project_layers().report(folder, fresh=True)}) from None
        await refresh_open_meters()
        return report

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
        if profile is not None and profile.get("vision") and not manager.supports(model, "vision"):
            profile["vision"] = False  # An endpoint check found image parts rejected.
        defaults = default_limits(model)
        effective = effective_limits(model, body.context_window, body.max_output_tokens)
        # The input budget requests use (AgentManager.input_budget): the capped budgets, with the
        # estimate margin near the endpoint's window.
        effective["input_budget"] = effective_input_budget(effective["context_window"], effective["max_output_tokens"],
                                                           model_context_window(model))
        return {"profile": profile, "recommended": recommended,
                "defaults": {"context_window": defaults[0], "max_output_tokens": defaults[1]},
                "effective": effective,
                "warnings": limit_warnings(model, body.context_window, body.max_output_tokens),
                "note": limits_note(model)}

    @app.post("/api/settings/reasoning")
    async def reasoning_preview(body: ReasoningQuery):
        """The reasoning fields agent requests to this endpoint carry for these choices, computed by the
        code that builds the requests, and each level's label for the effort picker."""
        model = body.model.strip()
        if not model:
            raise ValueError("Enter a model endpoint name.")
        # What requests use after a rejection: a thinking budget instead of the adaptive fields, or none.
        setting, rejected, stepped = manager.reasoning_ladder(model, body.reasoning_format)
        limits = effective_limits(model, settings.values["context_window"], settings.values["max_output_tokens"])
        return reasoning.preview(model, body.reasoning_effort, setting, body.thinking_summaries, rejected,
                                 limits["max_output_tokens"], stepped)

    @app.get("/api/sessions")
    async def list_sessions():
        counts = manager.changes.counts()
        # A parent's subagents index (up to 200 entries) is left out: the list is fetched often while
        # subagents work, and /sessions/<id>/children lists them. So are the last turn's MCP server status
        # (errors and stderr tails), which the open chat has, its result record (/info has it) and the usage
        # summary (GET /api/usage).
        return [{**{k: v for k, v in s.items() if k not in ("subagents", "mcp_status", "usage_summary", "last_turn_result")},
                 "status": manager.statuses.get(s["id"], "idle"),
                 "unreviewed_changes": counts.get(s["id"], 0)}
                for s in store.list()]

    @app.post("/api/sessions")
    async def create_session(body: SessionPermissions | None = None):
        workspace = settings.values["workspace"]
        if body and body.workspace is not None:
            workspace = str(checked_workspace(body.workspace, settings))
        manager.workspace_available(workspace)
        model = body.model.strip() if body and body.model else ""
        if body and body.model and not model:
            raise ValueError("Enter a model endpoint name.")
        # The project's trusted settings files can set a new chat's model and effort (settings_layers.py).
        session = store.create({**settings.effective(workspace), "workspace": workspace})
        session["permission_mode"] = body.permission_mode if body else "manual"
        if body and body.reasoning_effort:
            session["reasoning_effort"] = body.reasoning_effort
        if body and body.max_agent_steps is not None:
            session["max_agent_steps"] = body.max_agent_steps
        if model:
            session["model"] = model
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
        await manager.broadcast(session_id, {"type": "model", "model": model})
        await manager.refresh_meter_limits(session)  # The meter follows the new endpoint's budgets.
        return public_session(session)

    @app.put("/api/sessions/{session_id}/reasoning-effort")
    async def change_effort(session_id: str, body: SessionEffort):
        # Allowed while a turn runs: the agent's next model request uses it (and a running subagent's).
        from .approvals import busy_status
        session = session_or_404(session_id)
        await manager.change_effort(session, body.reasoning_effort)
        return public_session(session, busy_status(manager, session_id))

    @app.put("/api/sessions/{session_id}/spending-limit")
    async def change_spending_limit(session_id: str, body: SessionSpendingLimit):
        # Allowed while a turn runs: the agent's next model request checks it (and a running subagent's).
        from .approvals import busy_status
        session = session_or_404(session_id)
        await manager.change_spending_limit(session, body.spending_limit_dbu)
        return public_session(session, busy_status(manager, session_id))

    @app.put("/api/sessions/{session_id}/turn-limits")
    async def change_turn_limits(session_id: str, body: SessionTurnLimits):
        # Allowed while a turn runs: the agent's next model request checks them (and a running subagent's).
        from .approvals import busy_status
        session = session_or_404(session_id)
        changes = {key: getattr(body, key) for key in body.model_fields_set}
        if not changes:
            raise HTTPException(400, "Send turn_token_limit or turn_time_limit_minutes.")
        await manager.change_turn_limits(session, changes)
        return public_session(session, busy_status(manager, session_id))

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
        return public_session(session_or_404(session_id), manager.statuses.get(session_id, "idle"),
                              manager.compaction_view(session_id))

    @app.get("/api/sessions/{session_id}/context")
    async def get_context(session_id: str):
        """The chat's context meter as last prepared (estimate, breakdown with tool groups, instruction
        sources, the last report and the next-request estimate), without building a request."""
        info = session_or_404(session_id).get("context_info")
        progress = manager.compaction_view(session_id)
        return {"status": manager.statuses.get(session_id, "idle"),
                "context_info": info if isinstance(info, dict) else None,
                **({"compaction_progress": progress} if progress else {})}

    @app.get("/api/sessions/{session_id}/metrics")
    async def get_session_metrics(session_id: str):
        session = session_or_404(session_id)
        metrics = {**session_metrics(session), "spending": spending.status(manager, session),
                   "turn_limits": turns.status(manager, session)}
        if session.get("subagents"):
            # This chat's subagents (Agent tools → Usage: the chat and its subagents, and each one).
            metrics["subagents"] = manager.delegates.usage_rollup(session)
        return metrics

    @app.get("/api/usage")
    async def usage_across_chats(days: int = Query(default=30, ge=1, le=MAX_DAYS)):
        """Every chat's model calls in the last `days` local days (Agent tools → Usage → All chats), from the usage
        summaries in their headers. A chat saved before summaries existed is loaded once and its summary saved, at
        most USAGE_SCAN of them per request; pending_chats counts those left for the next one."""
        chats, scanned, pending = [], 0, 0
        for header in store.list():
            if header["id"] in deleting_sessions:
                continue
            if header["id"] in manager.live:  # A running turn: its ledger is newer than its header.
                chats.append((header, usage_summary(manager.live[header["id"]])))
                continue
            summary = header.get("usage_summary")
            if not isinstance(summary, dict) or summary.get("version") != SUMMARY_VERSION:
                if scanned >= USAGE_SCAN:
                    pending += 1
                    continue
                scanned += 1
                try:
                    full = store.get(header["id"])
                except (ValueError, OSError):
                    continue  # Unreadable: reported by the store, not counted.
                if not full:
                    continue
                summary = full["usage_summary"] = usage_summary(full)
                try:
                    store.save(full, touch=False)
                except (OSError, sqlite3.Error):
                    pass  # Counted now and computed again next time.
            chats.append((header, summary))
        return {**overview(chats, days), "pending_chats": pending}

    @app.get("/api/sessions/{session_id}/usage.csv")
    async def export_session_usage(session_id: str, include_subagents: bool = False):
        """include_subagents adds the rows of the chat's subagent chats that still exist (conversation_id
        tells them apart), numbered on after the chat's own."""
        session = session_or_404(session_id)
        redact = manager.redact_sent
        rows = usage_rows(session, redact=redact)
        if include_subagents:
            for entry in session.get("subagents") or []:
                if not isinstance(entry, dict) or entry.get("deleted") or not isinstance(entry.get("id"), str):
                    continue
                try:
                    child = manager.get(entry["id"])
                except (ValueError, OSError):
                    continue  # An unreadable subagent chat is reported by the store.
                if child and child.get("parent_session_id") == session_id:
                    rows += [{**row, "n": len(rows) + number} for number, row in enumerate(usage_rows(child, redact=redact), 1)]
        text = usage_csv(rows)
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
                manager.delegates.forget(member, keep=family)  # A deleted subagent stays in its parent's index, marked.
                await manager.jobs.remove_session(member)
                manager.task_board.delete_session(member)
                manager.checkpoints.delete_session(member)
                manager.changes.delete_session(member)
                manager.results.delete_session(member)
                manager.uploads.delete_session(member)
                store.delete(member)
                manager.live.pop(member, None)
                manager.statuses.pop(member, None)
                manager.work_statuses.pop(member, None)
                for remembered in (manager.estimate_anchors, manager.anchor_candidates, manager.compactions):
                    remembered.pop(member, None)
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
        return await manager.submit_message(session_id, body.text.strip(), verbatim=body.verbatim,
                                            uploads=upload_refs(body.uploads), client_ref=body.client_ref)

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
        await manager.refresh_meter_limits(session)  # Settings may have changed since it was last open.
        manager.listeners.setdefault(session_id, set()).add(socket)
        try:
            # The first snapshot carries the latest 200 events, extended to the whole latest turn
            # (at most 1,000); the page loads earlier ones from /events. Later snapshots are complete.
            snapshot = stream_snapshot(session, manager.statuses.get(session_id, "idle"), manager.compaction_view(session_id))
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

    def opened_file(path, session_id, read):
        """(the path opened, read(tools, path)) for a file the Workspace panel opens by a path a reply, a tool card
        or Files named."""
        tools = ui_tools(session_id)
        session = session_or_404(session_id) if session_id else {}
        worktree = session.get("worktree")
        source = worktree.get("source_workspace") if isinstance(worktree, dict) else None
        if isinstance(source, str) and source and PurePosixPath(path).is_absolute() \
                and PurePosixPath(path).is_relative_to(PurePosixPath(source)):
            # A worktree chat opens its own copy of a file named by the source project's path,
            # so the editor never saves into the main checkout by mistake.
            path = locate_file(tools, path, worktree) or path
        try:
            return tools, path, read(tools, path)
        except FileNotFoundError:
            # Replies and tool cards name files in other ways: from the project's own folder name
            # (demo/src/app.py), from the Git repository's root, or in a worktree chat by the source
            # project's absolute path. Open the one existing file such a name means, else say so.
            found = locate_file(tools, path, session.get("worktree"), chat_folders(session, tools.root) if session else ())
            if found is None:
                raise ValueError(f"{path} does not exist in {tools.root}. It may have been moved or deleted, "
                                 "or it was named relative to another folder.") from None
            return tools, found, read(tools, found)

    @app.get("/api/file")
    async def file(path: str, session_id: str | None = None):
        def read(tools, path):
            try:
                return tools.read_file(path)
            except NotText as exc:
                # Named by the file opened, like a text file, so the panel's Download view names that file.
                raise HTTPException(400, {"message": settings.redact(str(exc)), "code": "not_text",
                                          "path": path, "absolute": str(tools.path(path))}) from None
        tools, path, content = opened_file(path, session_id, read)
        # The absolute path lets the editor offer an "Open in VS Code" link.
        return {"path": path, "content": content, "absolute": str(tools.path(path))}

    @app.get("/api/file/raw")
    async def raw_file(path: str, session_id: str | None = None, download: bool = False):
        """A file's bytes for the Workspace preview and Download (at most 20 MB). Only PNG, JPEG, GIF, WebP and
        PDF files whose bytes start as that type says are served inline; everything else (HTML and SVG included)
        is an attachment of type application/octet-stream. Neither ever runs as a page of this origin."""
        tools, path, (target, data) = opened_file(path, session_id, lambda tools, path: tools.read_bytes(path))
        kind = mimetypes.guess_type(target.name)[0]
        inline = not download and kind in INLINE_TYPES and data.startswith(INLINE_TYPES[kind]) \
            and (kind != "image/webp" or data[8:12] == b"WEBP")
        return Response(data, media_type=kind if inline else "application/octet-stream", headers={
            "Content-Disposition": content_disposition("inline" if inline else "attachment", target.name),
            "Content-Security-Policy": "sandbox; default-src 'none'", "X-Opened-Path": quote(path)})

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
            if name.endswith(".env") or name.startswith(".env.") and name != ".env.example":
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

    def checked_git_paths(tools, paths, shown):
        """Refuse paths outside the project, credential files, generated folders and folders. shown: the verb
        of the refusals ('shown', 'staged')."""
        for value in paths:
            relative = PurePosixPath(value)
            if not value or "\0" in value or relative.is_absolute() or ".." in relative.parts or value.startswith("~"):
                raise ValueError("Choose a file inside this project's repository.")
        kinds = [git_classifier(tools)(value) for value in paths]
        if "credential" in kinds:
            raise ValueError(f"Credential files are not {shown}.")
        if "generated" in kinds:
            raise ValueError(f"Files in generated folders are not {shown} here.")
        for value in paths:
            target = tools.root / value
            if value.endswith("/") or PurePosixPath(value) == PurePosixPath(".") or (target.is_dir() and not target.is_symlink()):
                raise ValueError("Choose one changed file, not a folder.")

    def credential_excludes(tools):
        """The configured credential file as an exclude pathspec, so Git leaves it out even under another name."""
        credential = tools.credential_file
        return ([f":(exclude,literal){credential.relative_to(tools.root).as_posix()}"]
                if credential and credential.is_relative_to(tools.root) and credential != tools.root else [])

    def git_program(tools):
        """The git program for the user's own Git actions: never one inside the project or a granted folder."""
        return find_git(git_environment(), [tools.root, *tools.allowed_directories,
                                            *(Path(path) for path in standing_directories(settings))])

    @app.get("/api/git/diff")
    async def git_diff(path: str, area: Literal["staged", "unstaged", "untracked"], session_id: str | None = None,
                       orig_path: str | None = None):
        tools = workspace_tools(session_id)
        paths = [path] if orig_path is None or area == "untracked" else [path, orig_path]
        checked_git_paths(tools, paths, "shown")
        return await file_diff(tools.root, path, area, paths[1] if len(paths) > 1 else None, tools=tools,
                               excludes=credential_excludes(tools))

    @app.post("/api/git/stage")
    async def git_stage(body: GitStage, session_id: str | None = None):
        """Stage or unstage the files the user chose in the Changes view: their own action, so no card, but
        not while a chat or a command in this project runs (it could be changing the same index)."""
        tools = workspace_tools(session_id)
        paths = list(dict.fromkeys(body.paths))
        if sum(len(path.encode("utf-8", errors="replace")) + 1 for path in paths) > GIT_PATH_BYTES:
            raise ValueError("Too many files at once: stage fewer.")
        checked_git_paths(tools, paths, "staged")
        workspace_idle(manager, store, tools.root, "staging files")
        await stage(tools.root, paths, body.staged, git=git_program(tools), excludes=credential_excludes(tools))
        return await working_tree_status(tools.root, git_classifier(tools))

    @app.post("/api/git/commit")
    async def git_commit(body: GitCommit, session_id: str | None = None):
        """Commit what is staged with the user's message (no hooks or signing program run). Refused when a
        staged file is outside this project folder or is a credential file."""
        tools = workspace_tools(session_id)
        message = body.message.strip()
        if not message or "\0" in message:
            raise ValueError("Write a commit message.")
        workspace_idle(manager, store, tools.root, "committing")
        git = git_program(tools)
        prefix, staged = await staged_files(tools.root, git=git)
        if not staged:
            raise ValueError("Nothing is staged. Stage the files to commit first.")
        outside = [path for _, *paths in staged for path in paths if path is not None and not path.startswith(prefix)]
        if outside:
            raise ValueError(f"{len(outside):,} staged {'file is' if len(outside) == 1 else 'files are'} outside this "
                             f"project folder ({outside[0]}). Commit in a terminal, or unstage {'it' if len(outside) == 1 else 'them'} there.")
        classify = git_classifier(tools)
        for status, path, original in staged:
            path, original = path[len(prefix):], original and original[len(prefix):]
            # A deleted credential file may be committed (git rm --cached .env stops tracking it); its content
            # under another name may not.
            if status != "D" and classify(path) == "credential":
                raise ValueError(f"{path} is a credential file and it is staged. Unstage it in a terminal "
                                 f"(git restore --staged -- <path>) before committing.")
            if original and classify(original) == "credential":
                raise ValueError(f"{path} is staged as a rename or copy of the credential file {original}. Unstage "
                                 f"it in a terminal (git restore --staged -- <path>) before committing.")
        commit = await commit_staged(tools.root, message, git=git)
        return {"commit": commit, "status": await working_tree_status(tools.root, classify)}

    pr_cache, pr_locks = {}, {}

    @app.get("/api/git/pr")
    async def git_pr(session_id: str | None = None, refresh: bool = False,
                     branch: str | None = Query(default=None, max_length=300)):
        """The current branch's pull request from the GitHub CLI, if it is installed and signed in (it calls
        GitHub, never Databricks), at most once a minute per project and branch (the branch the Changes view
        shows; gh itself reads the checked-out one) unless refreshed."""
        tools = workspace_tools(session_id)
        key = (str(tools.root), branch)
        if len(pr_cache) > 256:
            pr_cache.clear()
        if len(pr_locks) > 64 and not any(lock.locked() for lock in pr_locks.values()):
            pr_locks.clear()
        # One gh at a time per project: a second request waits and reads the first one's answer.
        async with pr_locks.setdefault(key[0], asyncio.Lock()):
            entry = pr_cache.get(key)
            if entry is None or time.monotonic() - entry[0] >= (PR_REFRESH_SECONDS if refresh else PR_CACHE_SECONDS):
                env = {**{key: os.environ[key] for key in GH_ENVIRONMENT_KEYS if os.environ.get(key)},
                       **manager.jobs.command_environment(python_venv=False)}
                result = await pr_status(tools.root, env=env,
                                         forbidden_roots=[*tools.allowed_directories,
                                                          *(Path(path) for path in standing_directories(settings))],
                                         redact=lambda text: tools.redact(settings.redact(text)))
                entry = pr_cache[key] = (time.monotonic(), result)
        return entry[1]

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

    def with_origin(jobs, session_id):
        """The job rows with origin, who started each. A job recorded before origin existed is the agent's when
        its chat has the job's run_command card (as for its end notice), else the user's."""
        session = manager.get(session_id) if session_id and any(not job.get("origin") for job in jobs) else None
        return [job if job.get("origin") else
                {**job, "origin": "agent" if session and manager.agent_job(session, job["id"]) else "user"}
                for job in jobs]

    @app.get("/api/jobs")
    async def list_jobs(session_id: str | None = None):
        workspace = job_scope(session_id)
        return with_origin([{key: value for key, value in job.items() if key != "output"}
                            for job in manager.jobs.list(session_id=session_id, workspace=workspace)
                            if job["session_id"] == session_id], session_id)

    @app.post("/api/jobs")
    async def start_job(body: CommandRequest, session_id: str | None = None):
        # Clicking Run explicitly authorizes this command, independent of agent mode.
        workspace = job_scope(session_id)
        manager.workspace_available(workspace)
        return await manager.jobs.start(workspace=workspace, session_id=session_id, **body.model_dump())

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, session_id: str | None = None):
        return with_origin([scoped_job(job_id, session_id)], session_id)[0]

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
    register_endpoint_checks(app, manager, settings)
    register_stream(app, manager, session_or_404)
    register_schedules(app, manager, settings, session_or_404, workspace_tools)
    workspace_folders = register_workspace_folders(app, settings, session_or_404)

    # The browser loads the frontend source files directly; there is no build step.
    frontend = APP_ROOT / "frontend"
    if (frontend / "static").is_dir():
        app.mount("/static", RevalidatedStaticFiles(directory=frontend / "static"), name="static")

    @app.get("/")
    async def index():
        if (frontend / "index.html").exists():
            # frame-src: the Workspace preview's frames (a PDF from a blob, an HTML file in a sandboxed srcdoc
            # frame) cannot navigate to any address, so a previewed page reaches no network.
            return FileResponse(frontend / "index.html", headers={"Cache-Control": "no-cache",
                                                                  "Content-Security-Policy": INDEX_POLICY})
        return JSONResponse({"message": "The frontend folder is missing. See README.md."}, status_code=503)

    return app
