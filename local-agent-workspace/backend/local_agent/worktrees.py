"""App-owned Git worktrees; removing one always preserves its branch."""
import asyncio
import json
import os
import shutil
import signal
import time
import uuid
from pathlib import Path

from .jobs import ENVIRONMENT_KEYS


class WorktreeManager:
    def __init__(self, store, state_dir):
        self.store = store
        self.base = Path(state_dir).expanduser().resolve() / "worktrees"
        self.git = shutil.which("git")
        self.removing_paths = set()
        store.db.execute("CREATE TABLE IF NOT EXISTS worktrees (id TEXT PRIMARY KEY, data TEXT)")
        store.db.commit()

    async def _git(self, workspace, arguments, *, allowed=(0,), filters=()):
        if not self.git:
            raise ValueError("Git is required to create or remove a worktree.")
        env = {key: value for key, value in os.environ.items() if key in ENVIRONMENT_KEYS}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
        options = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false"]
        for key in filters:
            options.extend(["-c", f"{key}={'false' if key.endswith('.required') else ''}"])
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            self.git, *options, "-C", str(workspace), *arguments, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True))
        process = None
        try:
            process = await asyncio.shield(spawning)
            async with asyncio.timeout(30):
                raw = await process.stdout.read(80_001)
                # read(n) can return early; continue to EOF while keeping a hard bound.
                while len(raw) <= 80_000:
                    chunk = await process.stdout.read(80_001 - len(raw))
                    if not chunk:
                        break
                    raw += chunk
                if len(raw) > 80_000:
                    raise ValueError("Git output limit reached; inspect the repository directly.")
                await process.wait()
        except BaseException:
            if process is None:
                process = await spawning
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()
            raise
        text = raw.decode("utf-8", errors="replace").strip()
        if process.returncode not in allowed:
            raise ValueError(text or f"Git exited with status {process.returncode}.")
        return process.returncode, text

    async def _filters(self, workspace):
        _, names = await self._git(workspace, ["config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$"], allowed=(0, 1))
        return names.splitlines()

    def list(self, workspace=None):
        workspace = str(Path(workspace).expanduser().resolve()) if workspace is not None else None
        records = [json.loads(row[0]) for row in self.store.db.execute("SELECT data FROM worktrees")]
        return [record for record in sorted(records, key=lambda record: record["created"], reverse=True)
                if workspace is None or record["source_workspace"] == workspace or record["path"] == workspace]

    def _owned(self, record):
        target = self.base / record["id"]
        if (self.base.is_symlink() or self.base.resolve() != self.base or target.is_symlink()
                or target.resolve() != target or str(target) != record["path"]):
            raise ValueError("Worktree path is not an app-owned directory; refusing removal.")
        return target

    def validate(self, worktree_id):
        row = self.store.db.execute("SELECT data FROM worktrees WHERE id=?", (worktree_id,)).fetchone()
        if not row:
            raise ValueError("Managed worktree not found.")
        record = json.loads(row[0])
        target = self._owned(record)
        if not target.is_dir():
            raise ValueError("Managed worktree is missing.")
        return record

    async def create(self, workspace, branch):
        if not isinstance(branch, str) or not branch or len(branch) > 200 or branch.startswith("-"):
            raise ValueError("Choose a valid new Git branch name of at most 200 characters.")
        source = Path(workspace).expanduser().resolve()
        if not source.is_dir():
            raise ValueError("Choose an existing Git workspace.")
        _, checked = await self._git(source, ["check-ref-format", "--branch", branch])
        if checked != branch:
            raise ValueError("Use a literal new branch name, not a checkout shorthand.")
        _, bare = await self._git(source, ["rev-parse", "--is-bare-repository"])
        if bare == "true":
            raise ValueError("Choose a Git working directory, not a bare repository.")
        head_code, head = await self._git(source, ["rev-parse", "--verify", "HEAD^{commit}"], allowed=(0, 128))
        if head_code:
            raise ValueError("Create a commit in the source repository before creating a worktree (HEAD is unborn).")
        code, _ = await self._git(source, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], allowed=(0, 1))
        if code == 0:
            raise ValueError("That branch already exists; choose a fresh branch name.")
        self.base.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = {"id": str(uuid.uuid4()), "branch": branch, "source_workspace": str(source),
                  "created": time.time(), "head": head}
        record["path"] = str(self.base / record["id"])
        target = self._owned(record)
        filters = await self._filters(source)
        async def create_and_register():
            await self._git(source, ["worktree", "add", "--no-track", "-b", branch, str(target), head], filters=filters)
            self.store.db.execute("INSERT INTO worktrees VALUES (?, ?)", (record["id"], json.dumps(record)))
            self.store.db.commit()
            return record

        # Git creation and durable app ownership form one bounded operation.
        # Cancellation must not strand a completed worktree outside the catalog.
        creating = asyncio.create_task(create_and_register())
        try:
            return await asyncio.shield(creating)
        except asyncio.CancelledError:
            await asyncio.gather(creating, return_exceptions=True)
            raise

    def _in_use(self, target):
        for session in self.store.list():
            workspace = Path(session["workspace"]).expanduser().resolve()
            if workspace.is_relative_to(target):
                return True
        exists = self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
        if exists:
            for (data,) in self.store.db.execute("SELECT data FROM jobs"):
                job = json.loads(data)
                if job["state"] == "running" and Path(job["workspace"]).expanduser().resolve().is_relative_to(target):
                    return True
        return False

    async def remove(self, worktree_id):
        row = self.store.db.execute("SELECT data FROM worktrees WHERE id=?", (worktree_id,)).fetchone()
        if not row:
            raise ValueError("Managed worktree not found.")
        record = json.loads(row[0])
        target = self._owned(record)
        if str(target) in self.removing_paths:
            raise ValueError("This worktree is already being removed.")
        self.removing_paths.add(str(target))
        try:
            return await self._remove(record, target)
        finally:
            self.removing_paths.discard(str(target))

    async def _remove(self, record, target):
        if not target.is_dir():
            raise ValueError("Managed worktree is missing; no directory was removed.")
        if self._in_use(target):
            raise ValueError("This worktree is used by a conversation or running command. Delete those conversations and stop commands first.")
        _, top = await self._git(target, ["rev-parse", "--show-toplevel"])
        if Path(top).resolve() != target:
            raise ValueError("The managed directory is no longer this Git worktree.")
        source = Path(record["source_workspace"])
        _, common = await self._git(source, ["rev-parse", "--git-common-dir"])
        _, current_common = await self._git(target, ["rev-parse", "--git-common-dir"])
        if (source / common).resolve() != (target / current_common).resolve():
            raise ValueError("The managed directory now belongs to a different repository.")
        filters = await self._filters(target)
        _, dirty = await self._git(target, ["status", "--porcelain=v1", "--untracked-files=normal", "--ignored=matching"], filters=filters)
        if dirty:
            raise ValueError("The worktree has modified, untracked, or ignored files. Preserve or remove them before removing the worktree.")
        # Recheck ownership/use after the subprocess awaits; never force cleanup.
        self._owned(record)
        if self._in_use(target):
            raise ValueError("The worktree became used by a conversation or running command.")
        await self._git(record["source_workspace"], ["worktree", "remove", str(target)], filters=filters)
        self.store.db.execute("DELETE FROM worktrees WHERE id=?", (record["id"],))
        self.store.db.commit()
        return record
