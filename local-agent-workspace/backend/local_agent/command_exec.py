"""Commands run from an argument vector, without a shell.

The caller decides whether a command may run; this module only prepares it. A
command allowed by a rule runs the program found on PATH (absolute PATH entries
only). A read-only command (pwd, ls or read-only Git) runs no project code: pwd
and ls come from /bin, and Git is found outside the project and runs with the
programs a repository's configuration can name switched off (git_tools).
"""
import os
import shutil
from pathlib import Path

from . import git_tools
from .jobs import command_environment, validate_argv

READ_ONLY_PROGRAMS = {"pwd": "/bin/pwd", "/bin/pwd": "/bin/pwd", "ls": "/bin/ls", "/bin/ls": "/bin/ls"}


def absolute_path_entries(env):
    """The entries of env['PATH'] that are absolute paths. Empty and relative entries
    (which name the working folder, that is the project) are dropped."""
    return [entry for entry in str((env or {}).get("PATH") or "").split(os.pathsep) if os.path.isabs(entry)]


def resolve_executable(name, env, cwd):
    """The program argv[0] names: a path (with '/') relative to cwd, or a bare name
    looked up on the absolute PATH entries of env. A path keeps its own name, not the
    target of a symbolic link: a virtual environment's python finds its environment, and
    a multi-call program its role, from the name it was started under."""
    if "/" in name:
        path = Path(cwd, name).absolute()
        target = path.resolve()
        if not target.is_file() or not os.access(target, os.X_OK):
            raise ValueError(f"Command not found: {name}")
        return str(path)
    found = shutil.which(name, path=os.pathsep.join(absolute_path_entries(env)))
    if not found:
        raise ValueError(f"Command not found on PATH: {name}")
    return found


async def prepare_command(argv, *, workspace, forbidden_roots, read_only, env=None):
    """(argv, env) to run without a shell. read_only commands are hardened; forbidden_roots
    are the folders (the project and its granted folders) Git must not be found in."""
    validate_argv(argv)
    env = command_environment() if env is None else env
    if not read_only:
        return [resolve_executable(argv[0], env, workspace), *argv[1:]], env
    program = argv[0]
    if program in READ_ONLY_PROGRAMS:
        binary = READ_ONLY_PROGRAMS[program]
        if not Path(binary).is_file():
            raise ValueError(f"{binary} is not available, so this read-only command does not run automatically.")
        return [binary, *argv[1:]], env
    if program == "git":
        return await git_tools.hardened_git_command(workspace, list(argv[1:]), forbidden_roots, env=env)
    raise ValueError("Not a read-only command.")
