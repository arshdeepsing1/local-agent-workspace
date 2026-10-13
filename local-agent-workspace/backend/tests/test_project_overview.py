"""project_overview: a project summary from names and sizes, with the file tools' exclusions
(u-large-inputs item 4)."""
import json
import os
import shutil
import subprocess

import pytest

from local_agent import overview, search
from local_agent.overview import OVERVIEW_OUTPUT_LIMIT, project_overview
from local_agent.permissions import evaluate
from local_agent.tool_errors import classify
from local_agent.tools import WorkspaceTools, _output_size

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")


def git(folder, *arguments):
    subprocess.run(["git", "-c", "init.defaultBranch=main", *arguments], cwd=folder, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env={"PATH": os.environ["PATH"], "HOME": str(folder), "GIT_CONFIG_NOSYSTEM": "1"})


def files(root, mapping):
    for name, size in mapping.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * size)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    files(root, {
        "README.md": 100, "pyproject.toml": 50, "app/main.py": 3000, "app/util.py": 1000, "app/style.css": 10,
        "web/package.json": 40, "web/src/index.ts": 2000, "web/a/b/c/package.json": 5,
        ".github/workflows/ci.yml": 30, "docs/guide.md": 500, "node_modules/pkg/index.js": 9000,
        ".env": 20, "secrets.cfg": 10, "data/blob.bin": 50_000, "ignored/big.log": 70_000,
    })
    (root / ".gitignore").write_text("ignored/\n")
    os.link(root / "secrets.cfg", root / "copy.txt")  # A hard link to the credential file.
    state = root / "state"
    state.mkdir()
    (state / "conversation.json").write_text("{}")
    return root


def tools_for(root):
    return WorkspaceTools(str(root), credential_file=str(root / "secrets.cfg"), excluded_roots=(root / "state",))


def paths(result, key):
    return [item if isinstance(item, str) else item["path"] for item in result[key]]


@needs_git
def test_the_overview_applies_gitignore_and_every_exclusion(project):
    git(project, "init", "-q")
    result = project_overview(tools_for(project))
    listed = json.dumps(result)
    for hidden in ("node_modules", ".env", "secrets.cfg", "copy.txt", "conversation.json", "ignored/big.log"):
        assert hidden not in listed
    assert result["files"] == 12  # With .gitignore.
    assert result["bytes"] == sum((100, 50, 3000, 1000, 10, 40, 2000, 5, 30, 500, 50_000)) + len("ignored/\n")
    with_ignored = project_overview(tools_for(project), include_ignored=True)
    assert "ignored/big.log" in json.dumps(with_ignored) and "node_modules" not in json.dumps(with_ignored)
    assert result["hint"] == overview.HINT


def test_languages_folders_key_files_and_largest_files(project):
    result = project_overview(tools_for(project))
    languages = {item["language"]: item for item in result["languages"]}
    assert languages["Python"] == {"language": "Python", "files": 2, "bytes": 4000}
    assert languages["TypeScript"]["bytes"] == 2000 and languages["Other"]["files"] >= 2
    assert [item["language"] for item in result["languages"]][-1] == "Other"
    folders = {item["path"]: item for item in result["folders"]}
    assert folders["app"] == {"path": "app", "files": 3, "bytes": 4010, "languages": ["Python", "CSS"]}
    assert folders["(files directly here)"]["files"] == 3  # README.md, pyproject.toml, .gitignore
    assert [item["files"] for item in result["folders"]] == sorted((item["files"] for item in result["folders"]),
                                                                    reverse=True)
    assert result["key_files"] == ["README.md", "pyproject.toml", "web/package.json", ".github/workflows/ci.yml"]
    largest = result["largest_files"]
    assert [item["bytes"] for item in largest] == sorted((item["bytes"] for item in largest), reverse=True)
    assert largest[:2] == [{"path": "ignored/big.log", "bytes": 70_000}, {"path": "data/blob.bin", "bytes": 50_000}]


def test_others_are_summed_beyond_fifteen_languages(tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    extensions = [".py", ".js", ".ts", ".go", ".rs", ".java", ".kt", ".c", ".cpp", ".rb", ".php", ".sh", ".sql",
                  ".md", ".json", ".yml", ".toml", ".css"]
    files(root, {f"f{index}{suffix}": 1000 - index for index, suffix in enumerate(extensions)})
    result = project_overview(WorkspaceTools(str(root)))
    assert len(result["languages"]) == 16 and result["languages"][-1] == {"language": "Other", "files": 3,
                                                                           "bytes": 3 * 1000 - 15 - 16 - 17}


def test_three_hundred_folders_fit_the_output_limit(tmp_path):
    root = tmp_path / "wide"
    root.mkdir()
    files(root, {f"package-number-{index:03d}/module.py": 10 for index in range(300)})
    result = project_overview(WorkspaceTools(str(root)))
    assert _output_size(result) <= OVERVIEW_OUTPUT_LIMIT
    assert result["omitted"]["folders"] == 300 - len(result["folders"]) and len(result["folders"]) <= 40


def test_a_folder_too_large_to_list_gives_a_partial_overview(project, monkeypatch):
    monkeypatch.setattr(search, "UNIVERSE_LIMIT", 5)
    result = project_overview(tools_for(project), include_ignored=True)
    assert result["partial"] is True
    assert result["note"].startswith("More than 5 files under this path; narrow the path. Showing top-level entries")
    folder_paths = paths(result, "folders")
    assert folder_paths == sorted(folder_paths) and {"app", "web", "docs", ".github"} <= set(folder_paths)
    assert all(item["files"] is None for item in result["folders"])
    listed = json.dumps(result)
    for hidden in ("node_modules", ".env", "state", "secrets.cfg", "copy.txt"):
        assert f'"{hidden}"' not in listed
    assert result["key_files"] == ["README.md", "pyproject.toml"]


def test_a_file_path_is_the_wrong_type(project):
    with pytest.raises(ValueError) as caught:
        project_overview(tools_for(project), "README.md")
    assert classify(caught.value, str(caught.value))[0] == "wrong_type"
    with pytest.raises(ValueError, match="include_ignored must be a boolean"):
        project_overview(tools_for(project), include_ignored="yes")


@pytest.mark.parametrize("mode", ["plan", "dontAsk", "manual"])
def test_read_modes_allow_it_without_a_card(mode):
    assert evaluate(mode, "project_overview", {"path": "."}).decision == "allow"
    assert evaluate(mode, "outline_file", {"path": "a.md"}).decision == "allow"


def test_key_file_matches_the_fnmatch_patterns_in_one_compiled_pattern(monkeypatch):
    """Integration review B, finding 18: the same answers as fnmatchcase over each KEY_FILES pattern."""
    import fnmatch
    from pathlib import PurePosixPath

    from local_agent import overview
    from local_agent.overview import KEY_FILES, WORKFLOWS, key_file

    def expected(name):
        lower = name.lower()
        return len(PurePosixPath(lower).parts) <= 3 and (lower.startswith(WORKFLOWS) or any(
            fnmatch.fnmatchcase(PurePosixPath(lower).name, pattern) for pattern in KEY_FILES))
    examples = [pattern.replace("*", "x") for pattern in KEY_FILES] + [pattern.replace("*", "") for pattern in KEY_FILES]
    names = [*examples, "README.md", "Dockerfile", "compose.yzml", "requirements.txt.bak", "[x].sln", "a.sln",
             "docker-compose.yaml", "x\nreadme", "readme\n", ".github/workflows/ci.yml", ".GitHub/Workflows/CI.yml",
             ".github/workflows/sub/ci.yml", "notes.txt", "src/main.py"]
    paths = [prefix + name for name in names for prefix in ("", "pkg/", "pkg/mod/", "pkg/mod/deep/")]
    assert [key_file(path) for path in paths] == [expected(path) for path in paths]
    calls = []
    match = fnmatch.fnmatchcase
    monkeypatch.setattr(overview.fnmatch, "fnmatchcase", lambda *args: calls.append(args) or match(*args))
    assert key_file("pkg/mod/f1.py") is False and key_file("README.md") is True and calls == []
