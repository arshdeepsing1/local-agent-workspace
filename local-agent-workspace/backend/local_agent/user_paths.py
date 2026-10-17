"""The default code folder (LOCAL_AGENT_CODING_DIR) and the Python virtual environment
(LOCAL_AGENT_PYTHON_VENV) set in Settings or .env.

Both are checked every time they are used, so a folder that is removed or changed later
is reported in Settings and not used. Nothing here executes a program.
"""
import glob
import os
import sys
from pathlib import Path

from .tools import excluded, parts_below
from .workspace_folders import _within

SYSTEM_DIRS = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/boot", "/proc", "/sys", "/dev", "/run",
               "/System", "/Library", "/private/etc")
ENV_NAMES = {"coding_dir": "LOCAL_AGENT_CODING_DIR", "python_venv": "LOCAL_AGENT_PYTHON_VENV"}
PYVENV_CFG_LIMIT = 65_536


def _home():
    try:
        return Path.home().resolve()
    except (RuntimeError, KeyError, OSError):
        return None


def _absolute(value):
    if not isinstance(value, str) or len(value) > 4096 or "\0" in value:
        raise ValueError("Enter a folder path of at most 4,096 characters.")
    try:
        expanded = Path(value).expanduser()
    except RuntimeError:
        raise ValueError(f"{value.split('/', 1)[0]} is not a known user here.") from None
    if not expanded.is_absolute():
        raise ValueError("Enter an absolute path (or one starting with ~/).")
    return Path(os.path.abspath(expanded))


def _resolve(path):
    """path.resolve() (non-strict), with a symbolic link loop refused as a ValueError like any other bad path."""
    try:
        return Path(path).resolve()
    except RuntimeError:
        raise ValueError("This path runs through a symbolic link loop.") from None


def _state_checks(resolved, state_dir):
    state = _resolve(Path(state_dir).expanduser())
    if _within(resolved, state):
        raise ValueError("This is the app's state folder or inside it, which holds conversations and settings.")
    if _within(state, resolved):
        raise ValueError("This folder contains the app's state folder; choose a folder that does not.")


def checked_workspace(value, settings):
    """A project folder a script chose for a new chat (POST /api/sessions workspace), checked as Settings checks its
    workspace (an existing folder; an absolute or ~/ path here), and never the app's state folder or a folder inside
    it. The resolved folder, or ValueError."""
    resolved = _resolve(_absolute(value))
    if _within(resolved, _resolve(Path(settings.state_dir).expanduser())):
        raise ValueError("This is the app's state folder or inside it, which holds conversations and settings.")
    if not resolved.is_dir():
        raise ValueError("Choose an existing project directory.")
    return resolved


def check_coding_dir(value, *, state_dir, env_file="", app_root, home):
    """(resolved folder, missing) for a default code folder, or ValueError with the reason it is refused."""
    path = _absolute(value)
    resolved = _resolve(path)
    if resolved.parent == resolved:
        raise ValueError("The filesystem root is too broad; choose a folder for your code.")
    if any(_within(resolved, Path(system)) for system in SYSTEM_DIRS):
        raise ValueError("This is a system folder; choose a folder for your code.")
    if home is not None and _within(home, resolved):
        raise ValueError("Your home folder (or a folder containing it) is too broad; choose a folder inside it.")
    _state_checks(resolved, state_dir)
    if _within(resolved, _resolve(app_root)):
        raise ValueError("This is inside the app's own folder; choose a folder outside it.")
    kind = excluded(resolved.parts) or excluded(path.parts)
    if kind == "credential":
        raise ValueError("This path is a credential location, which file tools never use.")
    if kind == "generated":
        raise ValueError("File tools exclude this internal or generated folder (.git, .venv, node_modules and similar).")
    inside = parts_below(resolved, home) if home is not None else None  # Also under another name for home.
    if inside and inside[0].startswith("."):
        raise ValueError("This is inside a hidden configuration folder in your home folder (such as ~/.config or ~/.ssh).")
    if env_file:
        try:
            credential = Path(env_file).expanduser().resolve()
        except RuntimeError:  # An unknown ~user or a link loop: the file cannot be read, so compare it as written.
            credential = Path(os.path.abspath(env_file))
        if _within(credential, resolved):
            raise ValueError("This folder contains your Databricks credential file.")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("This is not a folder.")
    if not resolved.exists():
        if not resolved.parent.is_dir():
            raise ValueError(f"Its parent folder does not exist: {resolved.parent}")
        if not os.access(resolved.parent, os.W_OK | os.X_OK):
            raise ValueError("The app cannot create this folder: its parent folder is not writable.")
        return resolved, True
    if not os.access(resolved, os.R_OK | os.W_OK | os.X_OK):
        raise ValueError("The app cannot write to this folder (permission denied).")
    return resolved, False


def _coding_dir(value, settings, env_file):
    from . import config
    return check_coding_dir(value, state_dir=settings.state_dir, env_file=env_file, app_root=config.APP_ROOT,
                            home=_home())


def check_coding_dir_setting(value, settings, candidate=None):
    """The resolved folder to store for a value entered in Settings (ValueError when refused).
    The credential-file check uses the env_file saved in the same request."""
    env_file = (candidate if candidate is not None else settings.values).get("env_file", "")
    return str(_coding_dir(value, settings, env_file)[0])


def env_value(settings, key):
    """The value .env or the process environment sets for coding_dir or python_venv, stripped ("" when unset)."""
    raw = settings.env.get(ENV_NAMES[key], "")
    return raw.strip() if isinstance(raw, str) else ""


def _source(settings, key, value):
    if not value:
        return "none"
    raw = env_value(settings, key)
    if raw == value:
        return "env"
    return "env" if raw and settings.env_equivalent(key, value) else "settings"


def coding_dir_status(settings):
    """{path, state: off|ready|missing|invalid, message, source} for the default code folder."""
    value = settings.values.get("coding_dir", "")
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        return {"path": None, "state": "off", "message": "Not set. New code goes where you ask, or into the project folder.",
                "source": "none"}
    source = _source(settings, "coding_dir", value)
    try:
        path, missing = _coding_dir(value, settings, settings.values.get("env_file", ""))
    except (ValueError, OSError) as exc:
        return {"path": value, "state": "invalid", "message": "Not used: " + str(exc), "source": source}
    if missing:
        return {"path": str(path), "state": "missing", "source": source,
                "message": f"{path} does not exist yet; the agent's first file there creates it."}
    return {"path": str(path), "state": "ready", "message": f"Ready: every conversation can use {path}.", "source": source}


def standing_directories(settings):
    """The default code folder as a folder every conversation may use: [path] when ready or missing."""
    status = coding_dir_status(settings)
    return [status["path"]] if status["state"] in ("ready", "missing") else []


def check_python_venv(value, *, state_dir, app_root):
    """{path, python, version} for a virtual environment folder, or ValueError with the reason it is refused.
    path is absolute but not resolved: a venv's programs find it by the name they were started under."""
    venv = _absolute(value)
    if os.pathsep in str(venv):  # PATH would split <venv>/bin into other entries (CPython's venv refuses it too).
        raise ValueError(f"This path contains '{os.pathsep}', which cannot appear in a PATH entry, so commands could "
                         "not use this environment; move or rename it to a path without it.")
    resolved = _resolve(venv)
    if resolved.parent == resolved:
        raise ValueError("The filesystem root is not a virtual environment.")
    if not resolved.is_dir():
        raise ValueError("This folder does not exist.")
    _state_checks(resolved, state_dir)
    if _within(resolved, _resolve(app_root)):
        raise ValueError("This is the app's own Python environment (or inside the app's folder); create a separate one "
                         "for your projects.")
    if sys.prefix != sys.base_prefix:
        server = Path(sys.prefix).resolve()
        if _within(resolved, server) or _within(server, resolved):
            raise ValueError("This is the Python environment the app server runs in; create a separate one for your "
                             "projects.")
    python = venv / "bin" / "python"
    if not (os.path.isfile(python) and os.access(python, os.X_OK)):
        raise ValueError("No bin/python here; choose the environment's folder (the one that contains bin/ and "
                         "pyvenv.cfg).")
    config_file = venv / "pyvenv.cfg"
    if not config_file.is_file() and not (venv / "conda-meta").is_dir():
        raise ValueError("No pyvenv.cfg (or conda-meta/) here, so this is not a virtual environment.")
    return {"path": str(venv), "python": str(python), "version": _pyvenv_version(config_file)}


def _pyvenv_version(config_file):
    """The Python version pyvenv.cfg names, or None. The file is read as data; python never runs."""
    try:
        if not config_file.is_file() or config_file.stat().st_size > PYVENV_CFG_LIMIT:
            return None
        text = config_file.read_bytes()[:PYVENV_CFG_LIMIT].decode("utf-8", errors="replace")
    except OSError:
        return None
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values.setdefault(key.strip().casefold(), value.strip())
    if values.get("version"):
        return values["version"]
    if values.get("version_info"):
        return ".".join(values["version_info"].split(".")[:3])
    return None


# Where venvs keep site-packages: lib/python3.X (CPython, conda), lib/pypy3.X (PyPy), lib64, or the top level.
SITE_PACKAGES = (("lib", "*", "site-packages"), ("lib64", "*", "site-packages"), ("site-packages",))


def has_pip(venv):
    """Whether the pip module is installed in the virtual environment (uv venv and --without-pip leave it out):
    True or False, or None when no site-packages folder is found (a layout not recognised). Looks for the
    package, as `python -m pip` needs it; nothing runs."""
    root = glob.escape(str(venv))
    folders = [path for parts in SITE_PACKAGES for path in glob.glob(os.path.join(root, *parts))]
    if any(os.path.isfile(os.path.join(folder, "pip", "__init__.py")) for folder in folders):
        return True
    return False if folders else None


def _python_venv(value, settings):
    from . import config
    return check_python_venv(value, state_dir=settings.state_dir, app_root=config.APP_ROOT)


def check_python_venv_setting(value, settings, candidate=None):
    """The absolute path to store for a virtual environment entered in Settings (ValueError when refused)."""
    return _python_venv(value, settings)["path"]


def python_venv_status(settings):
    """{path, state: off|ready|invalid, message, python, version, source} for the Python environment."""
    value = settings.values.get("python_venv", "")
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        return {"path": None, "state": "off", "message": "Not set. Commands use the Python on the server's PATH.",
                "python": None, "version": None, "pip": None, "source": "none"}
    source = _source(settings, "python_venv", value)
    try:
        venv = _python_venv(value, settings)
    except (ValueError, OSError) as exc:
        return {"path": value, "state": "invalid", "message": "Not used: " + str(exc), "python": None, "version": None,
                "pip": None, "source": source}
    version = f" (Python {venv['version']})" if venv["version"] else ""
    pip = has_pip(venv["path"])
    return {"path": venv["path"], "state": "ready",
            "message": f"Ready: commands use {venv['python']}{version}." + (" No pip is installed in it." if pip is False else ""),
            "python": venv["python"], "version": venv["version"], "pip": pip, "source": source}


def usable_python_venv(settings):
    """The virtual environment commands run with, or None. Checked again on every call."""
    status = python_venv_status(settings)
    return status["path"] if status["state"] == "ready" else None


def server_bins():
    """The app server's own interpreter folders when it runs in a virtual environment (run.py puts
    dirname(sys.executable) first on PATH), which commands with a venv set must not see."""
    if sys.prefix == sys.base_prefix:
        return []
    return list(dict.fromkeys([os.path.dirname(sys.executable), os.path.join(sys.prefix, "bin")]))


def venv_environment(env, venv, *, drop=()):
    """A copy of env with venv active: VIRTUAL_ENV set, venv/bin first on PATH, the previous
    VIRTUAL_ENV's bin and the drop entries removed from PATH, and PYTHONHOME removed."""
    result = dict(env)
    bin_dir = os.path.join(venv, "bin")
    old = result.get("VIRTUAL_ENV")
    original = result["PATH"] if "PATH" in result else os.defpath
    removed = {bin_dir, *drop, *([os.path.join(old, "bin")] if old else [])}
    rest = [entry for entry in original.split(os.pathsep) if entry not in removed]
    result["PATH"] = bin_dir if not rest or original == "" else bin_dir + os.pathsep + os.pathsep.join(rest)
    result["VIRTUAL_ENV"] = venv
    result.pop("PYTHONHOME", None)
    return result


def server_python_note(settings):
    """The system prompt line when no venv is set and `python` on PATH may be the app server's own."""
    if python_venv_status(settings)["state"] != "off" or not server_bins():
        return None
    return ("\nPython: no virtual environment is set in Settings, and `python` on PATH may be the app server's own "
            f"environment ({sys.prefix}). Do not install packages into it; ask the user which environment to use.")
