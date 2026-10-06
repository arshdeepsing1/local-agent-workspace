"""Git run without a shell and without repository-configured programs.

A repository's own configuration can name programs that ordinary commands run:
`core.fsmonitor` (run by `git status`), hooks, and clean/smudge filters (run by
`git status` and `git diff` on changed files). Every app-initiated Git command goes
through run_git, which disables these, limits the environment, and bounds time and
output. Filter overrides travel in GIT_CONFIG_COUNT/KEY/VALUE environment variables:
unlike `-c name=value` they are not split at an `=` inside a driver name, and child
Git processes inherit them.
"""
import asyncio
import codecs
import os
import shutil
import signal

from .jobs import ENVIRONMENT_KEYS
from .tools import SECRET_NAMES

OUTPUT_LIMIT = 80_000
SAFE_OPTIONS = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                "-c", "submodule.recurse=false", "-c", "core.pager=cat")
FILTER_KEYS = r"^filter\..*\.(clean|smudge|process|required)$"
# The file tools' credential names and `*.env`, at any depth and in any letter case.
DIFF_EXCLUDES = (":(exclude,glob,icase)**/*.env", *(f":(exclude,glob,icase){pattern}" for name in sorted(SECRET_NAMES)
                              for pattern in (f"**/{name}", f"**/{name}/**")),
                 ":(exclude,glob,icase)**/.env.*")
CHANGES_TIMEOUT = 60


def filter_settings(names):
    """(name, value) pairs that switch off the named filter driver settings."""
    return [(key, "false" if key.endswith(".required") else "") for key in names]


def git_environment(filters=()):
    env = {key: value for key, value in os.environ.items() if key in ENVIRONMENT_KEYS}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
    settings = filter_settings(filters)
    if settings:
        env["GIT_CONFIG_COUNT"] = str(len(settings))
        for index, (key, value) in enumerate(settings):
            env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"] = key, value
    return env


def filter_overrides(names):
    """`-c` options for the same settings, for Git older than 2.31. A name that
    contains `=` cannot be given this way; the environment covers it."""
    options = []
    for key, value in filter_settings(names):
        if "=" not in key:
            options.extend(["-c", f"{key}={value}"])
    return options


async def run_git(workspace, arguments, *, filters=(), timeout=30, limit=None, git=None):
    """Run `git <arguments>` in workspace. Returns (exit code, output bytes, truncated).

    Output beyond `limit` bytes stops the command; truncated is then true.
    """
    limit = OUTPUT_LIMIT if limit is None else max(limit, 0)
    git = git or shutil.which("git")
    if not git:
        raise ValueError("Git is not installed or not on PATH.")
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
        git, *SAFE_OPTIONS, *filter_overrides(filters), "-C", str(workspace), *arguments, env=git_environment(filters),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True))
    process = None
    truncated = False
    try:
        process = await asyncio.shield(spawning)
        async with asyncio.timeout(timeout):
            raw = await process.stdout.read(limit + 1)
            # read(n) can return early; continue to EOF while keeping a hard bound.
            while len(raw) <= limit:
                chunk = await process.stdout.read(limit + 1 - len(raw))
                if not chunk:
                    break
                raw += chunk
            if len(raw) > limit:
                raw, truncated = raw[:limit], True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
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
    return process.returncode, raw, truncated


async def configured_filters(workspace, *, timeout=30, git=None):
    _, names, _ = await run_git(workspace, ["config", "--name-only", "-z", "--get-regexp", FILTER_KEYS],
                                timeout=timeout, git=git)
    return [name for name in names.decode("utf-8", errors="surrogateescape").split("\0") if name]


async def working_tree_changes(workspace):
    """`git status --short` followed by `git diff`, as {exit_code, output, truncated}.

    --ignore-submodules=dirty keeps Git from running inside submodules, whose own
    configuration may name filters; changed submodule commits are still listed."""
    try:
        async with asyncio.timeout(CHANGES_TIMEOUT):
            filters = await configured_filters(workspace, timeout=CHANGES_TIMEOUT)
            code, output, truncated = await run_git(
                workspace, ["--no-optional-locks", "status", "--short", "--untracked-files=normal",
                            "--ignore-submodules=dirty"],
                filters=filters, timeout=CHANGES_TIMEOUT)
            if code == 0 and not truncated:
                code, diff, truncated = await run_git(
                    workspace, ["--no-optional-locks", "diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=dirty",
                              "--", ".", *DIFF_EXCLUDES],
                    filters=filters, timeout=CHANGES_TIMEOUT, limit=OUTPUT_LIMIT - len(output))
                output += diff
    except TimeoutError as exc:
        raise ValueError(f"Git did not finish within {CHANGES_TIMEOUT} seconds.") from exc
    # A byte limit may cut a UTF-8 character in half; drop only that partial character.
    text = codecs.getincrementaldecoder("utf-8")(errors="replace").decode(output, final=not truncated)
    if truncated:
        text += f"\n[Output limit reached: showing the first {OUTPUT_LIMIT // 1000} KB.]"
    return {"exit_code": None if truncated else code, "output": text, "truncated": truncated}
