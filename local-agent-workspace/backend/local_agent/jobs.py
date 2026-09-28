import asyncio
import codecs
import copy
import json
import os
import signal
import time
import uuid
from pathlib import Path


MAX_ACTIVE_JOBS = 4
MAX_COMPLETED_JOBS = 100
UPDATE_TIMEOUT_SECONDS = 2
ENVIRONMENT_KEYS = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER", "LOGNAME", "VIRTUAL_ENV"}


def shell_argv(command):
    for executable, options in (("/bin/zsh", ["-f"]), ("/bin/bash", ["--noprofile", "--norc"]), ("/bin/sh", [])):
        if Path(executable).is_file():
            return [executable, *options, "-c", command]
    raise ValueError("No supported POSIX shell was found (zsh, bash, or sh).")


def validate_command_options(command, timeout_seconds=60, max_output_bytes=80_000):
    if not isinstance(command, str) or not command.strip() or len(command) > 50_000:
        raise ValueError("Command must contain text and be at most 50,000 characters.")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise ValueError("Command timeout must be an integer from 1 to 3,600 seconds.")
    if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 1_000_000:
        raise ValueError("Command output limit must be an integer from 1,024 to 1,000,000 bytes.")


class CommandTimeout(TimeoutError):
    def __init__(self, result):
        super().__init__("Command exceeded its time limit and its process group was stopped.")
        self.result = result


async def run_process(command, workspace, timeout_seconds=60, max_output_bytes=80_000, on_output=None):
    """Run a noninteractive command, retaining bounded output while draining its pipe."""
    validate_command_options(command, timeout_seconds, max_output_bytes)
    env = {key: value for key, value in os.environ.items() if key in ENVIRONMENT_KEYS}
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
        *shell_argv(command), cwd=workspace, env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True))
    try:
        process = await asyncio.shield(spawning)
    except asyncio.CancelledError:
        # Process creation can finish after its caller is cancelled. Acquire and
        # reap that process before propagating cancellation.
        process = await spawning
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.communicate()
        raise

    output = ""
    retained = 0
    truncated = False
    finished = False
    changed = asyncio.Event()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def result():
        return {"exit_code": process.returncode, "output": output, "truncated": truncated}

    async def drain():
        nonlocal output, retained, truncated, finished
        while chunk := await process.stdout.read(4096):
            available = max_output_bytes - retained
            kept = chunk[:available]
            retained += len(kept)
            output += decoder.decode(kept)
            truncated = truncated or len(chunk) > available
            changed.set()
        # A byte limit may cut a UTF-8 character in half. Do not add a replacement
        # character for that deliberately omitted suffix.
        output += decoder.decode(b"", final=not truncated)
        await process.wait()
        # Children with redirected stdio may outlive a successful shell without
        # keeping this pipe open. Completion ends the entire managed group.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finished = True
        changed.set()

    async def publish():
        last_time = 0.0
        last_value = None
        while True:
            await changed.wait()
            delay = 0.1 - (time.monotonic() - last_time)
            if delay > 0 and not finished:
                await asyncio.sleep(delay)
            changed.clear()
            value = (output, truncated)
            if on_output is not None and value != last_value:
                try:
                    await on_output(*value)
                except Exception:
                    pass  # A disconnected observer must not stop pipe draining.
            last_value = value
            last_time = time.monotonic()
            if finished:
                return

    reader = asyncio.create_task(drain())
    publisher = asyncio.create_task(publish())
    try:
        async with asyncio.timeout(timeout_seconds):
            await asyncio.shield(reader)
            await publisher
    except BaseException as error:
        # Always kill the group, even when the shell itself has already exited:
        # its children may still be running and holding the output pipe open.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.gather(reader, return_exceptions=True)
        publisher.cancel()
        await asyncio.gather(publisher, return_exceptions=True)
        await process.wait()
        if isinstance(error, TimeoutError):
            raise CommandTimeout(result()) from error
        if isinstance(error, asyncio.CancelledError):
            error.result = result()
        raise
    return result()


class JobManager:
    def __init__(self, store, redact, on_update, redaction_tokens=None):
        self.store = store
        self.redact = redact
        self.on_update = on_update
        self.redaction_tokens = redaction_tokens or (lambda: ())
        self._tasks = {}
        self._jobs = {}
        self._removing_sessions = set()
        self._closed = False
        store.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, session_id TEXT, workspace TEXT, data TEXT)")
        store.db.commit()
        for (data,) in store.db.execute("SELECT data FROM jobs"):
            job = json.loads(data)
            if job["state"] == "running":
                job.update(state="interrupted", updated=time.time(),
                           output=job["output"] + "\n[Server restarted; command outcome is unknown. The command was not restarted.]")
                self._persist(job, mirror=True)
            else:
                self.store.save_job(job)
            self._jobs[job["id"]] = job
        self._prune()

    def _persist(self, job, *, mirror=False):
        self.store.db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?)",
                              (job["id"], job["session_id"], job["workspace"], json.dumps(job)))
        self.store.db.commit()
        if mirror:
            self.store.save_job(job)

    async def _notify(self, job):
        if self.on_update is not None:
            try:
                async with asyncio.timeout(UPDATE_TIMEOUT_SECONDS):
                    await self.on_update(copy.deepcopy(job))
            except Exception:
                pass

    def _prune(self):
        completed = sorted((job for job in self._jobs.values() if job["state"] != "running"),
                           key=lambda job: job["updated"], reverse=True)
        for job in completed[MAX_COMPLETED_JOBS:]:
            self.store.db.execute("DELETE FROM jobs WHERE id=?", (job["id"],))
            self._jobs.pop(job["id"], None)
        self.store.db.commit()

    def get(self, job_id):
        job = self._jobs.get(job_id)
        return copy.deepcopy(job)

    def list(self, session_id=None, workspace=None):
        workspace = str(Path(workspace).expanduser().resolve()) if workspace is not None else None
        return [copy.deepcopy(job) for job in sorted(self._jobs.values(), key=lambda item: item["created"], reverse=True)
                if (session_id is None or job["session_id"] == session_id)
                and (workspace is None or job["workspace"] == workspace)]

    async def start(self, command, workspace, session_id=None, timeout_seconds=60,
                    max_output_bytes=80_000, background=False):
        validate_command_options(command, timeout_seconds, max_output_bytes)
        if self._closed:
            raise ValueError("Command manager is shutting down.")
        if session_id in self._removing_sessions:
            raise ValueError("This conversation is being deleted; no new commands can start.")
        if sum(job["state"] == "running" for job in self._jobs.values()) >= MAX_ACTIVE_JOBS:
            raise ValueError("At most four commands can run at once. Stop or wait for a running command.")
        workspace = str(Path(workspace).expanduser().resolve())
        if not Path(workspace).is_dir():
            raise ValueError("Choose an existing command working directory.")
        tokens = tuple(token for token in self.redaction_tokens() if isinstance(token, str) and token)

        def sanitize(text, partial=False):
            # Keep incomplete tokens out of every partial snapshot. Recompute
            # from cumulative raw output so the next snapshot can reveal safe text.
            for token in sorted(tokens, key=len, reverse=True):
                text = text.replace(token, "[REDACTED]")
            if partial:
                held = max((size for token in tokens for size in range(1, min(len(token), len(text) + 1))
                            if text.endswith(token[:size])), default=0)
                if held:
                    text = text[:-held]
            return self.redact(text)

        now = time.time()
        job = {"id": str(uuid.uuid4()), "session_id": session_id, "workspace": workspace,
               "command": sanitize(command), "state": "running", "created": now, "updated": now,
               "exit_code": None, "output": "", "truncated": False, "timeout_seconds": timeout_seconds,
               "max_output_bytes": max_output_bytes, "background": bool(background)}
        self._jobs[job["id"]] = job
        self._persist(job, mirror=True)
        # Publication precedes execution. A concurrent stop during publication
        # can cancel this queued job without ever starting a process.
        try:
            await self._notify(job)
        except asyncio.CancelledError:
            await self.stop(job["id"])
            raise
        if job["state"] == "running":
            task = asyncio.create_task(self._run(job, command, sanitize))
            self._tasks[job["id"]] = task
            task.add_done_callback(lambda done: self._tasks.pop(job["id"], None))
        return copy.deepcopy(job)

    async def _run(self, job, command, sanitize):
        async def output(text, truncated):
            job.update(output=sanitize(text, partial=True), truncated=truncated, updated=time.time())
            self._persist(job)
            await self._notify(job)

        result = None
        try:
            result = await run_process(command, job["workspace"], job["timeout_seconds"],
                                       job["max_output_bytes"], output)
            job["state"] = "completed" if result["exit_code"] == 0 else "failed"
        except CommandTimeout as error:
            result = error.result
            job["state"] = "timed_out"
        except asyncio.CancelledError as error:
            result = getattr(error, "result", None)
            job["state"] = "cancelled"
        except Exception as error:
            job.update(state="failed", output=sanitize(str(error)))
        finally:
            if result is not None:
                job.update(exit_code=result["exit_code"], truncated=result["truncated"],
                           output=sanitize(result["output"], partial=result["truncated"] or job["state"] in {"cancelled", "timed_out"}))
            job["updated"] = time.time()
            self._persist(job, mirror=True)
            self._prune()
            await self._notify(job)

    async def wait(self, job_id):
        if self.get(job_id) is None:
            raise ValueError("Command job not found.")
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.shield(task)
        return self.get(job_id)

    async def stop(self, job_id):
        job = self._jobs.get(job_id)
        if job is None:
            raise ValueError("Command job not found.")
        task = self._tasks.get(job_id)
        if job["state"] == "running":
            if task is not None and not task.done():
                if not task.cancelling():
                    task.cancel()
                await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            # A task cancelled before its first instruction never enters _run.
            if job["state"] == "running":
                job.update(state="cancelled", updated=time.time())
                self._persist(job, mirror=True)
                self._prune()
                await self._notify(job)
        return copy.deepcopy(job)

    async def shutdown(self):
        self._closed = True
        await asyncio.gather(*(self.stop(job["id"]) for job in list(self._jobs.values()) if job["state"] == "running"))

    async def remove_session(self, session_id):
        self._removing_sessions.add(session_id)
        try:
            jobs = [job for job in self._jobs.values() if job["session_id"] == session_id]
            await asyncio.gather(*(self.stop(job["id"]) for job in jobs if job["state"] == "running"))
            self.store.db.execute("DELETE FROM jobs WHERE session_id=?", (session_id,))
            self.store.db.commit()
            for job in jobs:
                self._jobs.pop(job["id"], None)
        finally:
            self._removing_sessions.discard(session_id)
