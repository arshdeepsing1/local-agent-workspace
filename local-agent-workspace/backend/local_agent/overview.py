"""project_overview: a summary of a project folder from file names and sizes only.

Files are never read. The file list is search.list_universe's, the one find_files and
search_files use: the file tools' credential and generated-folder exclusions, the
configured credential file (by identity, hard links included), the app's state folder
and .gitignore (inside a Git repository that starts within the permitted folder) apply.
A folder too large to list gives a partial overview of its top level instead of an error.
"""
import fnmatch
import os
import re
import time
from pathlib import Path, PurePosixPath

from . import search
from . import tools as tool_rules
from .tools import _output_size

OVERVIEW_OUTPUT_LIMIT = 12_000
TOP_LANGUAGES = 15
KEY_FILE_DEPTH = 3
KEY_FILE_LIMIT = 40
LARGEST_FILES = 10
PARTIAL_FOLDERS = 60
FOLDER_LANGUAGES = 3
DIRECT_FILES = "(files directly here)"
HINT = ("Locate code with find_files and search_files (output_mode=files_with_matches; search_files scans at most "
        "20,000 files and 64 MB per call, so narrow path and glob in large projects), see a file's structure with "
        "outline_file, and read only the line ranges you need.")
# List-limit errors of search.list_universe that give a partial overview instead.
LISTING_LIMITS = ("More than", "Directory scan limit reached", "Listing files took longer than")

LANGUAGES = {
    ".py": "Python", ".pyi": "Python", ".ipynb": "Jupyter Notebook",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala",
    ".cs": "C#", ".swift": "Swift", ".dart": "Dart", ".c": "C", ".h": "C/C++ header", ".cc": "C++",
    ".cpp": "C++", ".cxx": "C++", ".hpp": "C/C++ header", ".hh": "C/C++ header", ".rb": "Ruby", ".php": "PHP",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".ps1": "PowerShell", ".sql": "SQL", ".r": "R",
    ".jl": "Julia", ".lua": "Lua", ".pl": "Perl", ".ex": "Elixir", ".exs": "Elixir", ".erl": "Erlang",
    ".hs": "Haskell", ".clj": "Clojure", ".vue": "Vue", ".svelte": "Svelte", ".html": "HTML", ".htm": "HTML",
    ".css": "CSS", ".scss": "SCSS", ".less": "Less", ".md": "Markdown", ".mdx": "Markdown", ".rst": "reStructuredText",
    ".txt": "Text", ".json": "JSON", ".yml": "YAML", ".yaml": "YAML", ".toml": "TOML", ".ini": "INI", ".cfg": "INI",
    ".xml": "XML", ".csv": "CSV", ".tf": "Terraform", ".proto": "Protocol Buffers", ".graphql": "GraphQL",
}
NAMED_LANGUAGES = {"dockerfile": "Dockerfile", "makefile": "Makefile", "gnumakefile": "Makefile"}
KEY_FILES = (
    "readme*", "changelog*", "contributing*", "license*", "agents.md", "claude.md",
    "pyproject.toml", "setup.py", "setup.cfg", "requirements*.txt", "constraints*.txt", "pipfile", "environment.yml",
    "tox.ini", "noxfile.py", "pytest.ini", "manage.py",
    "package.json", "tsconfig.json", "deno.json", "vite.config.*", "webpack.config.*",
    "cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle*",
    "makefile", "cmakelists.txt", "meson.build", "dockerfile", "docker-compose.y*ml", "compose.y*ml", "justfile",
    "gemfile", "composer.json", "*.sln", "*.csproj", "databricks.yml", ".env.example", ".pre-commit-config.yaml",
    "mkdocs.yml",
)
WORKFLOWS = ".github/workflows/"
# One compiled alternation of the fnmatch patterns: the same answers as fnmatchcase over each
# pattern, about 36 times faster (key_file runs for every file of a large tree, after the listing).
KEY_FILE_RE = re.compile("|".join(fnmatch.translate(pattern) for pattern in KEY_FILES))


def language_of(name):
    lower = name.lower()
    return NAMED_LANGUAGES.get(lower) or LANGUAGES.get(PurePosixPath(lower).suffix, "Other")


def key_file(relative):
    lower = relative.lower()
    # relative is a normalised relative path (list_universe) or a bare name (_partial).
    if lower.count("/") >= KEY_FILE_DEPTH:
        return False
    return lower.startswith(WORKFLOWS) or KEY_FILE_RE.match(lower.rpartition("/")[2]) is not None


def _languages(sizes, limit):
    """[{language, files, bytes}] by bytes, the languages after limit summed as Other."""
    ranked = sorted(sizes.items(), key=lambda item: (-item[1][1], item[0]))
    named = [(name, value) for name, value in ranked if name != "Other"]
    other = [value for name, value in ranked if name == "Other"] + [value for _, value in named[limit:]]
    result = [{"language": name, "files": files, "bytes": size} for name, (files, size) in named[:limit]]
    if other:
        result.append({"language": "Other", "files": sum(files for files, _ in other),
                       "bytes": sum(size for _, size in other)})
    return result


def project_overview(tools, path=".", *, include_ignored=False):
    if type(include_ignored) is not bool:
        raise ValueError("include_ignored must be a boolean.")
    base = tools.path(path)
    if not base.is_dir():
        if base.exists():
            raise ValueError("Not a directory: project_overview needs a folder; use outline_file for a file.")
        raise ValueError(f"{tools.display_path(base)} was not found.")
    try:
        entries, note = search.list_universe(tools, base, include_ignored=include_ignored,
                                             deadline=time.monotonic() + search.ENUMERATION_TIMEOUT)
    except ValueError as error:
        if not str(error).startswith(LISTING_LIMITS):
            raise
        return _partial(tools, base, str(error))
    total_bytes = sum(entry.size for entry in entries)
    languages, folders = {}, {}
    for entry in entries:
        language = language_of(PurePosixPath(entry.relative).name)
        files, size = languages.get(language, (0, 0))
        languages[language] = (files + 1, size + entry.size)
        parts = entry.relative.split("/")
        folder = parts[0] if len(parts) > 1 else None
        record = folders.setdefault(folder, {"files": 0, "bytes": 0, "languages": {}})
        record["files"] += 1
        record["bytes"] += entry.size
        record["languages"][language] = record["languages"].get(language, 0) + entry.size
    folder_list = []
    for folder, record in sorted(folders.items(), key=lambda item: (-item[1]["files"], item[0] or "")):
        top = [name for name, _ in sorted(record["languages"].items(), key=lambda item: (-item[1], item[0]))]
        folder_list.append({"path": tools.display_path(base / folder) if folder else DIRECT_FILES,
                            "files": record["files"], "bytes": record["bytes"], "languages": top[:FOLDER_LANGUAGES]})
    keys = sorted((entry for entry in entries if key_file(entry.relative)),
                  key=lambda entry: (len(PurePosixPath(entry.relative).parts), entry.relative))
    largest = sorted(entries, key=lambda entry: (-entry.size, entry.relative))[:LARGEST_FILES]
    result = {"path": tools.display_path(base), "files": len(entries), "bytes": total_bytes,
              "languages": _languages(languages, TOP_LANGUAGES), "folders": folder_list,
              "key_files": [tools.display_path(base / entry.relative) for entry in keys[:KEY_FILE_LIMIT]],
              "largest_files": [{"path": tools.display_path(base / entry.relative), "bytes": entry.size}
                                for entry in largest],
              "hint": HINT}
    if len(keys) > KEY_FILE_LIMIT:
        result["omitted"] = {"key_files": len(keys) - KEY_FILE_LIMIT}
    if note:
        result["note"] = note
    return _fit(result, languages)


def _fit(result, languages):
    """Shorten the lists, largest first, until the output fits OVERVIEW_OUTPUT_LIMIT."""
    steps = (("folders", 40), ("folders", 20), ("folders", 10), ("key_files", 20), ("largest_files", 5),
             ("languages", 8))
    for key, keep in steps:
        if _output_size(result) <= OVERVIEW_OUTPUT_LIMIT:
            break
        if key == "languages":
            if len(result["languages"]) > keep + 1:
                result["languages"] = _languages(languages, keep)
            continue
        if len(result[key]) > keep:
            omitted = result.setdefault("omitted", {})
            omitted[key] = omitted.get(key, 0) + len(result[key]) - keep
            result[key] = result[key][:keep]
    while _output_size(result) > OVERVIEW_OUTPUT_LIMIT and result["folders"]:
        result["folders"].pop()
        omitted = result.setdefault("omitted", {})
        omitted["folders"] = omitted.get("folders", 0) + 1
    return result


def _partial(tools, base, message):
    """The top level of a folder too large to list completely."""
    folders, files = [], []
    try:
        listing = sorted(os.scandir(base), key=lambda item: item.name)
    except OSError as error:
        raise ValueError(message) from error
    for item in listing:
        if tool_rules.excluded((item.name,)):
            continue
        path = Path(item.path)
        if tools.in_excluded_root(path):
            continue
        try:
            tools.path(str(path))
            if item.is_dir(follow_symlinks=False):
                folders.append(item.name)
            elif item.is_dir():
                continue  # A symbolic link to a folder.
            elif item.is_file():
                files.append((item.name, item.stat().st_size))
        except (OSError, ValueError):
            continue
    # tools.path() above refused credential files, the configured one included (also by identity).
    shown = [{"path": tools.display_path(base / name), "bytes": size} for name, size in files]
    result = {"path": tools.display_path(base), "partial": True,
              "folders": [{"path": tools.display_path(base / name), "files": None} for name in folders[:PARTIAL_FOLDERS]],
              "files_directly_here": shown[:LARGEST_FILES * 4],
              "key_files": [tools.display_path(base / name) for name, _ in files if key_file(name)][:KEY_FILE_LIMIT],
              "hint": HINT,
              "note": (f"{message} Showing top-level entries only. Run project_overview on one folder at a time, and "
                       "narrow find_files/search_files with path and glob (search_files scans at most 20,000 files and "
                       "64 MB per call).")}
    omitted = {}
    if len(folders) > PARTIAL_FOLDERS:
        omitted["folders"] = len(folders) - PARTIAL_FOLDERS
    if len(shown) > LARGEST_FILES * 4:
        omitted["files_directly_here"] = len(shown) - LARGEST_FILES * 4
    if omitted:
        result["omitted"] = omitted
    while _output_size(result) > OVERVIEW_OUTPUT_LIMIT and (result["files_directly_here"] or result["folders"]):
        key = "files_directly_here" if result["files_directly_here"] else "folders"
        result[key].pop()
        omitted = result.setdefault("omitted", {})
        omitted[key] = omitted.get(key, 0) + 1
    return result
