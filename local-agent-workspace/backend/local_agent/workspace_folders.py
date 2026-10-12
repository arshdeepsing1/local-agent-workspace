"""Folders added to Workspace -> Files, per project (view only).

The list only widens what the user can browse and edit in the Files tab
(GET /api/files, GET/PUT /api/file). It never changes what the agent may read,
write or run: the agent's tools keep using the conversation's own folder grants.
"""
import json
import logging
import os
import tempfile
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

FILE_NAME = "workspace_folders.json"
VERSION = 1
MAX_FOLDERS = 20
MAX_PATH = 4096

log = logging.getLogger(__name__)


class FolderPath(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    path: str = Field(min_length=1, max_length=MAX_PATH)


class UnreadableList(Exception):
    """The folder list file exists but could not be read or parsed."""


def project_key(workspace) -> str:
    return str(Path(workspace).expanduser().resolve())


def _valid(data) -> bool:
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != VERSION \
            or not isinstance(data.get("projects"), dict):
        return False
    return all(isinstance(key, str) and isinstance(folders, list) and all(isinstance(item, str) for item in folders)
               for key, folders in data["projects"].items())


class WorkspaceFolders:
    """Storage and validation for <state_dir>/workspace_folders.json."""

    def __init__(self, settings):
        self.settings = settings
        self.path = Path(settings.state_dir) / FILE_NAME
        self._warned = False

    def _load(self) -> dict:
        """The stored projects, {} when there is no file; raises UnreadableList otherwise."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._warned = False
            return {}
        except (OSError, UnicodeError) as exc:
            raise UnreadableList() from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise UnreadableList() from exc
        if not _valid(data):
            raise UnreadableList()
        self._warned = False
        return data["projects"]

    def _read(self) -> dict:
        """Like _load, but an unreadable file reads as empty (logged once)."""
        try:
            return self._load()
        except UnreadableList:
            if not self._warned:
                self._warned = True
                log.warning("The folder list file %s could not be read; Files shows no added folders.", FILE_NAME)
            return {}

    def _writable(self) -> dict:
        try:
            return self._load()
        except UnreadableList:
            # Never replace a file we could not parse: the user may want to repair it.
            raise HTTPException(409, f"The folder list file could not be read; fix or remove {FILE_NAME} in the state folder.")

    def _save(self, projects: dict):
        descriptor, temporary = tempfile.mkstemp(prefix=".workspace_folders-", suffix=".tmp", dir=self.path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump({"version": VERSION, "projects": projects}, target, indent=2)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def list(self, workspace):
        """Entries in insertion order; a folder that no longer exists is kept and flagged."""
        return [{"path": folder, "missing": not Path(folder).is_dir()}
                for folder in self._read().get(project_key(workspace), [])]

    def paths(self, workspace):
        """The existing added folders of a project, as extra roots for the user's own file browsing."""
        # Checked again on each use: a listed folder may since have been replaced by a symbolic link.
        return [item["path"] for item in self.list(workspace)
                if not item["missing"] and not self.refusal(Path(item["path"]).resolve())]

    def refusal(self, folder: Path) -> str | None:
        """Why a resolved folder must never be browsable from Files, or None."""
        if folder.parent == folder:
            return "The filesystem root cannot be added to Files."
        state = Path(self.settings.state_dir).expanduser().resolve()
        if folder == state or folder.is_relative_to(state):
            return ("This folder is the app's state folder or inside it. It holds conversations and settings "
                    "and is never shown in Files.")
        if state.is_relative_to(folder):
            return ("This folder contains the app's state folder, which holds conversations and settings "
                    "and is never shown in Files. Add a folder inside it instead.")
        return None

    def check(self, workspace, value: str) -> Path:
        """The resolved folder, or ValueError with a plain reason it cannot be added."""
        project = Path(project_key(workspace))
        try:
            requested = Path(value).expanduser()
        except RuntimeError:
            raise ValueError("Enter a folder path; ~name is not a known user here.")
        try:
            folder = (project / requested).resolve(strict=True)
        except FileNotFoundError:
            raise ValueError(f"This folder does not exist: {value}")
        if not folder.is_dir():
            raise ValueError(f"This is not a folder: {folder}")
        if folder == project or folder.is_relative_to(project):
            raise ValueError("This folder is inside the project; it is already in Files.")
        problem = self.refusal(folder)
        if problem:
            raise ValueError(problem)
        if not os.access(folder, os.R_OK | os.X_OK):
            raise ValueError(f"The app cannot list this folder (permission denied): {folder}")
        return folder

    def add(self, workspace, value: str) -> str:
        """Add a folder to the project's list and return its resolved path."""
        folder = str(self.check(workspace, value))
        projects = self._writable()
        key = project_key(workspace)
        folders = projects.get(key, [])
        if folder in folders:
            return folder
        if len(folders) >= MAX_FOLDERS:
            raise ValueError(f"Files can show at most {MAX_FOLDERS} added folders; remove one first.")
        self._save({**projects, key: [*folders, folder]})
        return folder

    def remove(self, workspace, value: str):
        projects = self._writable()
        key = project_key(workspace)
        folders = projects.get(key, [])
        try:
            resolved = str((Path(key) / Path(value).expanduser()).resolve())
        except RuntimeError:
            resolved = value
        entry = value if value in folders else resolved if resolved in folders else None
        if entry is None:
            raise HTTPException(404, "This folder is not in Files.")
        remaining = [folder for folder in folders if folder != entry]
        self._save({name: items for name, items in {**projects, key: remaining}.items() if items})


def register_workspace_folders(app, settings, session_or_404):
    """Add the /api/workspace-folders routes and return the store (for the UI-only file tools)."""
    folders = WorkspaceFolders(settings)

    def project(session_id):
        return session_or_404(session_id)["workspace"] if session_id else settings.values["workspace"]

    def result(workspace, **extra):
        return {"workspace": project_key(workspace), "folders": folders.list(workspace), **extra}

    # No idle requirement: the list never changes what the agent may do.
    @app.get("/api/workspace-folders")
    async def list_workspace_folders(session_id: str | None = None):
        return result(project(session_id))

    @app.post("/api/workspace-folders")
    async def add_workspace_folder(body: FolderPath, session_id: str | None = None):
        workspace = project(session_id)
        added = folders.add(workspace, body.path)
        return result(workspace, added=added)

    @app.delete("/api/workspace-folders")
    async def remove_workspace_folder(body: FolderPath, session_id: str | None = None):
        workspace = project(session_id)
        folders.remove(workspace, body.path)
        return result(workspace)

    return folders
