"""Per-project settings kept in app state: the Verify command and its limits.

A project is a resolved workspace folder, so a worktree is its own project. The
settings live in <state dir>/projects.json, never in the repository, and are not
part of a conversation export.
"""
import json
import os
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .jobs import validate_command_options


VERSION = 1
MAX_PROJECTS = 500
MAX_VERIFY_COMMAND = 2000
DEFAULTS = {"verify_command": "", "verify_timeout_seconds": 600, "verify_max_rounds": 2}
TIMEOUT_RANGE = (10, 3600)
ROUNDS_RANGE = (1, 3)


def validate_values(values):
    """The checked, normalised project settings, or ValueError."""
    command = values.get("verify_command", "")
    if not isinstance(command, str) or len(command) > MAX_VERIFY_COMMAND:
        raise ValueError(f"The verify command must be text of at most {MAX_VERIFY_COMMAND:,} characters.")
    if "\0" in command:
        raise ValueError("The verify command cannot contain a NUL character.")
    command = command.strip()
    timeout = values.get("verify_timeout_seconds", DEFAULTS["verify_timeout_seconds"])
    if type(timeout) is not int or not TIMEOUT_RANGE[0] <= timeout <= TIMEOUT_RANGE[1]:
        raise ValueError("The verify time limit must be an integer from 10 to 3,600 seconds.")
    rounds = values.get("verify_max_rounds", DEFAULTS["verify_max_rounds"])
    if type(rounds) is not int or not ROUNDS_RANGE[0] <= rounds <= ROUNDS_RANGE[1]:
        raise ValueError("Fix rounds must be an integer from 1 to 3.")
    if command:
        validate_command_options(command, timeout)
    return {"verify_command": command, "verify_timeout_seconds": timeout, "verify_max_rounds": rounds}


class ProjectSettings:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / "projects.json"

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        projects = data.get("projects") if isinstance(data, dict) and data.get("version") == VERSION else None
        return {key: value for key, value in projects.items() if isinstance(value, dict)} if isinstance(projects, dict) else {}

    def get(self, workspace):
        """The project's settings; a missing, corrupt or invalid entry reads as the defaults."""
        stored = self._read().get(str(workspace))
        if not stored:
            return dict(DEFAULTS)
        try:
            return validate_values({**DEFAULTS, **{key: stored[key] for key in DEFAULTS if key in stored}})
        except ValueError:
            return dict(DEFAULTS)

    def update(self, workspace, values):
        current = self.get(workspace)
        checked = validate_values({**current, **{key: values[key] for key in DEFAULTS if key in values}})
        projects = self._read()
        projects.pop(str(workspace), None)
        projects[str(workspace)] = {**checked, "updated": time.time()}
        if len(projects) > MAX_PROJECTS:
            def updated(item):
                value = item[1].get("updated")
                return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
            projects = dict(sorted(projects.items(), key=updated)[-MAX_PROJECTS:])
        self._write({"version": VERSION, "projects": projects})
        return checked

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(prefix="projects-", suffix=".tmp", dir=self.path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump(data, target, indent=2)
            os.replace(filename, self.path)
        finally:
            Path(filename).unlink(missing_ok=True)


class ProjectSettingsEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    verify_command: str | None = Field(default=None, max_length=MAX_VERIFY_COMMAND)
    verify_timeout_seconds: int | None = None
    verify_max_rounds: int | None = None


def register_projects(app, manager, settings, workspace_tools):
    def project(session_id):
        return str(workspace_tools(session_id).root)

    @app.get("/api/project-settings")
    async def get_project_settings(session_id: str | None = None):
        workspace = project(session_id)
        return {"workspace": workspace, **manager.projects.get(workspace)}

    @app.put("/api/project-settings")
    async def put_project_settings(body: ProjectSettingsEdit, session_id: str | None = None):
        workspace = project(session_id)
        values = manager.projects.update(workspace, body.model_dump(exclude_none=True))
        return {"workspace": workspace, **values}
