import asyncio
import codecs
import copy
import json
import math
import os
import re
import signal
import statistics
import time
import uuid
from pathlib import Path

import regex as safe_regex


MAX_ACTIVE_JOBS = 4
MAX_COMPLETED_JOBS = 100
UPDATE_TIMEOUT_SECONDS = 2
KILL_GRACE_SECONDS = 5
NOT_STOPPED = ("[The command did not end when it was stopped and may still be running, "
               "for example as another user through sudo.]")
TIMEOUT_NOT_STOPPED = "Command exceeded its time limit and did not end when it was stopped; it may still be running."
DRAINING = set()  # Output readers of commands that did not end when stopped.
ENVIRONMENT_KEYS = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER", "LOGNAME", "VIRTUAL_ENV"}
# Never passed to commands by name (Settings -> Environment variables for commands):
# model credentials, and loader or shell-startup variables that would change what
# even Auto mode's basic commands do. config.py validates the same sets.
CREDENTIAL_ENV = {"DBRICKS_TOKEN", "DATABRICKS_TOKEN", "DBRICKS_URL", "DATABRICKS_HOST", "LOCAL_AGENT_ENV_FILE"}
DENIED_ENV = {"BASH_ENV", "ENV", "ZDOTDIR", "PROMPT_COMMAND", "SHELLOPTS", "BASHOPTS", "IFS", "PYTHONSTARTUP",
              "PERL5OPT", "RUBYOPT", "NODE_OPTIONS"}
DENIED_ENV_PREFIXES = ("LD_", "DYLD_")
TERM_GRACE_SECONDS = 2
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
JOB_NOTICE_STATES = {"completed", "failed", "timed_out"}  # Background jobs that get a finished notice.
HEAD_CHARS = 1_500  # A failed command's first result: up to this much of the start,
TAIL_CHARS = 4_500  # and this much of the end,
MIN_HEAD_CHARS = 200  # or at least this much of the start alone.
WAIT_SCAN_SECONDS = 1.0  # wait_job's regular-expression budget per batch of new output
WAIT_THREAD_CHARS = 65_536  # larger batches are scanned in a worker thread
WAIT_OUTPUT_CHARS = 4_000
PAGE_BYTES = 8_000
MIN_WAIT_SECONDS = 5
MAX_WAIT_SECONDS = 300  # wait_job's timeout_seconds limit
DEFAULT_WAIT_SECONDS = 60  # wait_job without timeout_seconds when no earlier run says how long the job takes


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


def kill_group(pid):
    """Stop a command started in its own session, and anything it started.

    A group with nothing left to stop raises ESRCH, or on macOS EPERM while its
    exited processes wait to be reaped (macOS still signals any that are running).
    EPERM also means every running member belongs to another user, for example
    through sudo, so wait for the command afterwards with settle(), not forever.
    """
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def settle(reader):
    """Give a command whose group was just killed KILL_GRACE_SECONDS to end.

    reader is the task that drains the command's output and then reaps it. Returns
    (ended, cancellation). A command still running after the grace period keeps
    being drained in the background, so a full pipe never blocks it. A cancellation
    of the caller during the wait is returned, not raised, so that the caller can
    finish its cleanup and then raise it.
    """
    cancellation = None
    try:
        await asyncio.wait({reader}, timeout=KILL_GRACE_SECONDS)
    except asyncio.CancelledError as error:
        cancellation = error
    if not reader.done():
        DRAINING.add(reader)
        reader.add_done_callback(drained)
    return reader.done(), cancellation


async def terminate(reader, pid):
    """Ask a command's process group to end (SIGTERM), give it TERM_GRACE_SECONDS,
    then kill what is left and settle(). Returns (ended, cancellation) like settle()."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    cancellation = None
    try:
        await asyncio.wait({reader}, timeout=TERM_GRACE_SECONDS)
    except asyncio.CancelledError as error:
        cancellation = error
    kill_group(pid)
    ended, later = await settle(reader)
    return ended, cancellation or later


def drained(reader):
    DRAINING.discard(reader)
    if not reader.cancelled():
        reader.exception()  # Retrieved, so a failure is not reported as never retrieved.


async def discard_output(process):
    """Read a command's output to the end without keeping it, then reap it."""
    while await process.stdout.read(65536):
        pass
    await process.wait()


def with_note(output, result):
    """The (already redacted) output, followed by NOT_STOPPED if the command did not end."""
    return output + ("\n" if output else "") + NOT_STOPPED if result.get("stopped") is False else output


class CommandTimeout(TimeoutError):
    def __init__(self, result, stopped=True):
        super().__init__("Command exceeded its time limit and its process group was stopped." if stopped
                         else TIMEOUT_NOT_STOPPED)
        self.result = result


def command_environment(extra_env=None):
    """The environment commands run with: the variables named in ENVIRONMENT_KEYS, plus
    extra_env, the variables passed through by name (the caller has filtered them)."""
    env = {key: value for key, value in os.environ.items() if key in ENVIRONMENT_KEYS}
    env.update(extra_env or {})
    return env


# A command given to run without a shell has at most 64 tokens (command_exec.prepare_command
# checks it). The argument vector that runs may be longer: read-only Git adds its own options
# and pathspecs.
ARGV_LIMIT = 64
PREPARED_ARGV_LIMIT = 256


def validate_argv(argv, limit=ARGV_LIMIT):
    """An argument vector run without a shell: 1 to limit text tokens, each without NUL
    and at most 1,000 characters."""
    if (not isinstance(argv, list) or not 1 <= len(argv) <= limit
            or not all(isinstance(token, str) and "\0" not in token and len(token) <= 1000 for token in argv)
            or not argv[0]):
        raise ValueError(f"A command run without a shell needs 1 to {limit} arguments, each text of at most "
                         "1,000 characters without NUL.")


async def spawn(argv, workspace, env):
    """Start argv in its own session with no stdin and stdout and stderr on one pipe.

    Process creation can finish after its caller is cancelled. That process is then
    acquired and reaped before the cancellation propagates."""
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, cwd=workspace, env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True))
    try:
        return await asyncio.shield(spawning)
    except asyncio.CancelledError as error:
        process = await spawning
        kill_group(process.pid)
        ended, cancellation = await settle(asyncio.ensure_future(discard_output(process)))
        error = cancellation or error
        if not ended:
            error.result = {"exit_code": None, "output": "", "truncated": False, "stopped": False}
        raise error


async def run_process(command, workspace, timeout_seconds=60, max_output_bytes=80_000, on_output=None,
                      extra_env=None, *, argv=None, env=None):
    """Run a noninteractive command, retaining bounded output while draining its pipe.

    command is the text run by the shell, or with argv the display text of argv,
    which then runs without a shell. extra_env holds variables passed through by
    name; the caller has filtered them. env, when given, replaces the whole
    environment (command_environment(extra_env) otherwise)."""
    validate_command_options(command, timeout_seconds, max_output_bytes)
    if argv is not None:
        validate_argv(argv, PREPARED_ARGV_LIMIT)
    env = env if env is not None else command_environment(extra_env)
    process = await spawn(list(argv) if argv is not None else shell_argv(command), workspace, env)

    output = ""
    retained = 0
    truncated = False
    finished = False
    changed = asyncio.Event()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def result(stopped=True):
        values = {"exit_code": process.returncode, "output": output, "truncated": truncated}
        return values if stopped else {**values, "stopped": False}

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
        kill_group(process.pid)
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
        # Always stop the group, even when the shell itself has already exited:
        # its children may still be running and holding the output pipe open.
        # SIGTERM first, so that they can clean up; SIGKILL after the grace period.
        stopped, cancellation = await terminate(reader, process.pid)
        publisher.cancel()
        await asyncio.gather(publisher, return_exceptions=True)
        if stopped:
            await asyncio.gather(reader, return_exceptions=True)
            await process.wait()
        # A Stop during the grace period after a timeout ends as a cancellation.
        if cancellation is not None:
            cancellation.result = result(stopped)
            raise cancellation from error
        if isinstance(error, TimeoutError):
            raise CommandTimeout(result(stopped), stopped) from error
        if isinstance(error, asyncio.CancelledError):
            error.result = result(stopped)
        raise
    return result()


def job_command(command, limit):
    """A job's command on one line, at most limit characters."""
    text = " ".join(str(command).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def job_ending(job):
    """How a finished job ended, for notices: 'completed (exit 0)', 'failed (exit 2)' or 'timed out'."""
    if job["state"] == "timed_out":
        return "timed out"
    exit_code = f" (exit {job['exit_code']})" if type(job.get("exit_code")) is int else ""
    return ("completed" if job["state"] == "completed" else "failed") + exit_code


# Compiling expands counted repeats by their minimum counts, multiplied when nested:
# '(?:a{3000}){3000}' takes seconds and gigabytes before any timeout can apply.
REPEAT_EXPANSION_LIMIT = 100_000


def _repeat_expansion(node):
    """How far the counted repeats in a parsed pattern expand: the minimum counts, multiplied
    through nesting and added up."""
    from regex import _regex_core
    children = []
    for value in vars(node).values():
        if isinstance(value, _regex_core.RegexBase):
            children.append(value)
        elif isinstance(value, (list, tuple)):
            children.extend(item for item in value if isinstance(item, _regex_core.RegexBase))
    inner = sum(_repeat_expansion(child) for child in children)
    if isinstance(node, _regex_core.GreedyRepeat):  # Also the lazy and possessive repeats.
        return node.min_count * max(1, inner)
    return inner


def bounded_regex(text, flags):
    """compile text with the regex module (the engine with match timeouts) unless its counted
    repeats would make compiling itself take seconds and gigabytes. Raises regex.error."""
    from regex import _regex_core
    try:
        # The parse that regex.compile starts with; it expands nothing.
        global_flags = flags
        while True:
            source = _regex_core.Source(text)
            info = _regex_core.Info(global_flags, source.char_type, {})
            info.guess_encoding = _regex_core.UNICODE
            source.ignore_space = bool(info.flags & safe_regex.VERBOSE)
            try:
                parsed = _regex_core._parse_pattern(source, info)
                break
            except _regex_core._UnscopedFlagSet:
                global_flags = info.global_flags
        expansion = _repeat_expansion(parsed)
    except (AttributeError, ImportError, TypeError, KeyError, ValueError):
        # A regex release with another parser, or flags it refuses: compiling reports the latter.
        expansion = 0
    if expansion > REPEAT_EXPANSION_LIMIT:
        raise safe_regex.error(f"its counted repeats ({{n}}) add up to more than {REPEAT_EXPANSION_LIMIT:,} repeated "
                               "items, nested counts multiplied; use smaller counts, or * and +")
    try:
        return safe_regex.compile(text, flags)
    except MemoryError as error:
        raise safe_regex.error("it is too large to compile") from error


def until_pattern(text):
    """wait_job's until_regex, compiled with the same safe engine as search_files."""
    if not isinstance(text, str) or len(text) > 500:
        raise ValueError("until_regex must be text of at most 500 characters.")
    try:
        return bounded_regex(text, safe_regex.VERSION1)
    except (safe_regex.error, RecursionError) as error:
        raise ValueError(f"Invalid until_regex: {error}") from error


def scan_lines(text, start, pattern, timeout):
    """The first line of text that pattern matches, as (line, offset of the line), or None.

    text holds only complete lines, except at the end of a finished job's output.
    start is the offset of text in the job output."""
    from .tools import REGEX_TIMEOUT
    deadline = time.monotonic() + timeout
    position = 0
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()  # A final LF ends the last line; it does not start an empty one.
    for line in lines:
        stripped = line[:-1] if line.endswith("\r") else line
        remaining = deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise TimeoutError
            if pattern.search(stripped, timeout=min(REGEX_TIMEOUT, remaining), concurrent=True):
                return stripped, start + position
        except TimeoutError:
            raise ValueError("until_regex took too long; simplify it.") from None
        position += len(line) + 1
    return None


def job_runtime(job, now=None):
    """Seconds a job has run: until now while it runs, else until its last update (its end)."""
    end = (time.time() if now is None else now) if job["state"] == "running" else job["updated"]
    return max(0.0, end - job["created"])


def suggested_wait(expected, elapsed, previous=None):
    """wait_job seconds for a job: what an earlier run's duration says is left (a quarter more, at
    least 5), else twice the previous wait (from 10), at most 300; None without either."""
    if expected is not None and expected > elapsed:
        return int(min(MAX_WAIT_SECONDS, max(MIN_WAIT_SECONDS, math.ceil((expected - elapsed) * 1.25) + 2)))
    if previous is not None:
        return int(min(MAX_WAIT_SECONDS, max(10, previous * 2)))
    return None


def wait_page(job, reason, match, from_offset, extra=None):
    """wait_job's result: the new output since from_offset (its end when long), at most 8,000 bytes of JSON.
    extra (timing: elapsed, expected and the suggested next wait) is part of every page."""
    from .tools import _excerpt
    output = job["output"]
    total = len(output)

    def page(begin):
        return {"job_id": job["id"], "state": job["state"], "exit_code": job["exit_code"], "reason": reason,
                "match": match and {"line": _excerpt(match[0])[0], "offset": match[1]},
                "output": output[begin:], "output_start": begin, "next_offset": total, "total_chars": total,
                **(extra or {})}

    low = min(total, max(from_offset, total - WAIT_OUTPUT_CHARS))
    high = total
    while low < high:  # The smallest start whose page fits.
        middle = (low + high) // 2
        if len(json.dumps(page(middle), indent=2).encode("utf-8")) <= PAGE_BYTES:
            high = middle
        else:
            low = middle + 1
    return page(low)


class JobManager:
    def __init__(self, store, redact, on_update, redaction_tokens=None, environment=None, mirror_finished=True,
                 venv=None):
        self.store = store
        self.redact = redact
        self.on_update = on_update
        self.redaction_tokens = redaction_tokens or (lambda: ())
        self.environment = environment  # Variables passed to commands by name, read at job start.
        self.venv = venv  # The Python virtual environment commands run with (a path or None), read at job start.
        self._signal = asyncio.Condition()  # Notified after every change to a job (wait_job).
        self._changes = 0
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
            elif mirror_finished:
                # Finished jobs were mirrored when they ended; after a clean shutdown that holds, and
                # mirroring again would fully read each owning conversation (header-only startup).
                self.store.save_job(job)
            self._jobs[job["id"]] = job
        self._prune()

    def _persist(self, job, *, mirror=False):
        self.store.db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?)",
                              (job["id"], job["session_id"], job["workspace"], json.dumps(job)))
        self.store.db.commit()
        if mirror:
            self.store.save_job(job)

    async def _changed(self):
        self._changes += 1
        async with self._signal:
            self._signal.notify_all()

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

    def command_environment(self, *, named=True, python_venv=True):
        """The environment a command started now runs with: with named, the variables
        passed through by name in Settings; with python_venv, the Python virtual
        environment from Settings active (user_paths.venv_environment)."""
        extra_env = {}
        if named and self.environment is not None:
            try:
                extra_env = dict(self.environment() or {})
            except Exception:
                extra_env = {}  # A broken setting must not stop the command.
        env = command_environment(extra_env)
        if python_venv and self.venv is not None:
            try:
                path = self.venv()
            except Exception:
                path = None  # A broken setting must not stop the command.
            if isinstance(path, str) and path:
                from .user_paths import server_bins, venv_environment  # tools imports this module.
                env = venv_environment(env, path, drop=server_bins())
        return env

    async def start(self, command, workspace, session_id=None, timeout_seconds=60,
                    max_output_bytes=80_000, background=False, *, argv=None, env=None):
        """Start a job. command is the shell text, or with argv its display text: argv
        then runs without a shell, with env as its whole environment when given.
        argv and env are never stored or published."""
        validate_command_options(command, timeout_seconds, max_output_bytes)
        if argv is not None:
            validate_argv(argv, PREPARED_ARGV_LIMIT)
            argv = list(argv)
        if env is not None and (not isinstance(env, dict)
                                or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())):
            raise ValueError("A command environment must map names to text.")
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
        if argv is not None:
            job["without_shell"] = True
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
            task = asyncio.create_task(self._run(job, command, sanitize, argv, env and dict(env)))
            self._tasks[job["id"]] = task
            task.add_done_callback(lambda done: self._tasks.pop(job["id"], None))
        return copy.deepcopy(job)

    async def _run(self, job, command, sanitize, argv=None, env=None):
        async def output(text, truncated):
            job.update(output=sanitize(text, partial=True), truncated=truncated, updated=time.time())
            self._persist(job)
            await self._changed()
            await self._notify(job)

        result = None
        try:
            if env is None:
                env = self.command_environment()
            result = await run_process(command, job["workspace"], job["timeout_seconds"],
                                       job["max_output_bytes"], output, argv=argv, env=env)
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
                partial = result["truncated"] or job["state"] in {"cancelled", "timed_out"}
                job.update(exit_code=result["exit_code"], truncated=result["truncated"],
                           output=with_note(sanitize(result["output"], partial=partial), result))
            job["updated"] = time.time()
            self._persist(job, mirror=True)
            self._prune()
            await self._changed()
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
                await self._changed()
                await self._notify(job)
        return copy.deepcopy(job)

    def expected_seconds(self, job):
        """How long this command usually runs: the median duration of the latest runs (at most five)
        of the same command text in the same folder that completed, or, when none did, that failed
        (a run that fails at once says little about a full one). Stopped and timed-out runs never
        count. None when there is no such run."""
        same = [other for other in self._jobs.values() if other["id"] != job["id"]
                and other["command"] == job["command"] and other["workspace"] == job["workspace"]]
        for states in (("completed",), ("failed",)):
            runs = sorted((other for other in same if other["state"] in states),
                          key=lambda other: other["updated"], reverse=True)[:5]
            if runs:
                return statistics.median(job_runtime(other) for other in runs)
        return None

    async def watch(self, job_id, offset, pattern, timeout, idle=None):
        """Wait until the job ends, a new complete output line after offset matches pattern,
        it prints nothing for idle seconds (when given), or timeout seconds pass. Returns
        (job copy, reason, (line, offset) or None); reason is matched, exited, idle or timeout.
        Each wake-up scans only output not scanned before. Cancelling the wait leaves the job running."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        started = time.time()
        cursor = offset
        while True:
            seen = self._changes
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError("The job is no longer available.")
            running = job["state"] == "running"
            output = job["output"]
            cursor = min(cursor, len(output))  # Retained output can shrink (a withheld secret prefix).
            if pattern is not None:
                end = len(output) if not running else output.rfind("\n", cursor) + 1
                if end > cursor:
                    text = output[cursor:end]
                    if len(text) > WAIT_THREAD_CHARS:
                        found = await asyncio.to_thread(scan_lines, text, cursor, pattern, WAIT_SCAN_SECONDS)
                    else:
                        found = scan_lines(text, cursor, pattern, WAIT_SCAN_SECONDS)
                    if found is not None:
                        return copy.deepcopy(job), "matched", found
                    cursor = end
            if not running:
                return copy.deepcopy(job), "exited", None
            remaining = deadline - loop.time()
            if remaining <= 0:
                return copy.deepcopy(job), "timeout", None
            if idle is not None:
                # Quiet since its last output, or since the wait began if that was later.
                quiet = time.time() - max(job["updated"], started)
                if quiet >= idle:
                    return copy.deepcopy(job), "idle", None
                remaining = min(remaining, idle - quiet)
            async with self._signal:
                try:
                    await asyncio.wait_for(self._signal.wait_for(lambda: self._changes != seen), remaining)
                except TimeoutError:
                    pass

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
