"""GET /api/git/status and /api/git/diff: Git's view of the project as file
entries and per-file diffs, without shells, repository programs or credentials."""
import shutil
import subprocess

import pytest

from local_agent import git_tools
import test_git_changes as base
from test_git_changes import git, program

client, repository = base.client, base.repository  # The fixtures of test_git_changes.py.

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is not installed")


def status(client):
    response = client.get("/api/git/status")
    assert response.status_code == 200, response.text
    return response.json()


def entries(result):
    return {(entry["area"], entry["path"]): entry for entry in result["files"]}


def diff(client, path, area, orig_path=None):
    params = {"path": path, "area": area, **({"orig_path": orig_path} if orig_path else {})}
    return client.get("/api/git/diff", params=params)


def test_staged_unstaged_untracked_deleted_and_renamed_entries(repository):
    client, workspace = repository
    (workspace / "lib.py").write_text("one\ntwo\n")
    (workspace / "gone.txt").write_text("bye\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "more")
    (workspace / "app.py").write_text("print('new')\nprint('more')\n")  # unstaged
    (workspace / "lib.py").write_text("one\n2\ntwo\n")
    git(workspace, "add", "lib.py")  # staged
    (workspace / "lib.py").write_text("one\n2\ntwo\nthree\n")  # and unstaged on top
    git(workspace, "mv", "gone.txt", "moved.txt")
    (workspace / "notes" / "deep").mkdir(parents=True)
    (workspace / "notes/deep/todo.md").write_text("- a\n- b\n")  # untracked, inside a new folder
    git(workspace, "rm", "-q", "--cached", "app.py")  # staged deletion; the file stays untracked
    result = status(client)
    assert result["repository"] is True and result["truncated"] is False
    assert result["branch"] in ("main", "master")
    files = entries(result)
    assert files[("staged", "lib.py")] == {"path": "lib.py", "area": "staged", "status": "modified", "added": 1,
                                           "removed": 0, "hidden": None}
    assert files[("unstaged", "lib.py")]["added"] == 1 and files[("unstaged", "lib.py")]["status"] == "modified"
    assert files[("staged", "moved.txt")] == {"path": "moved.txt", "orig_path": "gone.txt", "area": "staged",
                                              "status": "renamed", "added": 0, "removed": 0, "hidden": None}
    assert files[("staged", "app.py")]["status"] == "deleted" and files[("staged", "app.py")]["removed"] == 1
    assert files[("untracked", "app.py")]["status"] == "untracked"
    assert files[("untracked", "notes/deep/todo.md")] == {"path": "notes/deep/todo.md", "area": "untracked",
                                                          "status": "untracked", "added": None, "removed": None,
                                                          "hidden": None}

    staged = diff(client, "lib.py", "staged").json()
    assert staged["binary"] is False and (staged["added"], staged["removed"]) == (1, 0)
    assert [line["kind"] for line in staged["lines"]] == ["hunk", "context", "added", "context"]
    assert staged["lines"][0]["text"].startswith("@@ -1,2 +1,3 @@")
    assert staged["lines"][2] == {"kind": "added", "text": "2", "new": 2}
    assert staged["lines"][3] == {"kind": "context", "text": "two", "old": 2, "new": 3}
    unstaged = diff(client, "lib.py", "unstaged").json()
    assert [line["text"] for line in unstaged["lines"] if line["kind"] == "added"] == ["three"]
    renamed = diff(client, "moved.txt", "staged", "gone.txt").json()
    assert renamed["lines"] == [] and renamed["binary"] is False
    deleted = diff(client, "app.py", "staged").json()
    assert [line for line in deleted["lines"] if line["kind"] == "removed"] == [
        {"kind": "removed", "text": "print('old')", "old": 1}]
    untracked = diff(client, "notes/deep/todo.md", "untracked").json()
    assert untracked == {"path": "notes/deep/todo.md", "area": "untracked", "binary": False, "truncated": False,
                         "added": 2, "removed": 0, "lines": [{"kind": "added", "text": "- a", "new": 1},
                                                             {"kind": "added", "text": "- b", "new": 2}]}


def test_missing_final_newline_and_binary_files(repository):
    client, workspace = repository
    (workspace / "image.bin").write_bytes(b"\x00\x01\x02")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "binary")
    (workspace / "image.bin").write_bytes(b"\x00\x03\x04\x05")
    (workspace / "app.py").write_text("print('new')")
    (workspace / "raw.dat").write_bytes(b"a\x00b")
    files = entries(status(client))
    assert (files[("unstaged", "image.bin")]["added"], files[("unstaged", "image.bin")]["removed"]) == (None, None)
    assert (files[("unstaged", "app.py")]["added"], files[("unstaged", "app.py")]["removed"]) == (1, 1)
    binary = diff(client, "image.bin", "unstaged").json()
    assert binary["binary"] is True and binary["lines"] == []
    assert diff(client, "raw.dat", "untracked").json()["binary"] is True
    text = diff(client, "app.py", "unstaged").json()
    assert text["lines"][1] == {"kind": "removed", "text": "print('old')", "old": 1}
    assert text["lines"][2] == {"kind": "added", "text": "print('new')", "new": 1, "newline": False}
    (workspace / "draft.txt").write_text("first\nlast")
    assert diff(client, "draft.txt", "untracked").json()["lines"][-1] == {"kind": "added", "text": "last", "new": 2,
                                                                          "newline": False}


def test_conflicted_files_are_listed(repository):
    client, workspace = repository
    git(workspace, "checkout", "-qb", "other")
    (workspace / "app.py").write_text("print('other')\n")
    git(workspace, "commit", "-qam", "other")
    git(workspace, "checkout", "-q", "-")
    (workspace / "app.py").write_text("print('mine')\n")
    git(workspace, "commit", "-qam", "mine")
    merge = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "-C", str(workspace),
                            "merge", "-q", "other"], capture_output=True)
    assert merge.returncode != 0
    files = entries(status(client))
    assert files[("unstaged", "app.py")]["status"] == "conflicted"
    assert ("staged", "app.py") not in files


def test_credential_files_are_listed_hidden_and_their_diffs_refused(repository):
    client, workspace = repository
    (workspace / ".env").write_text("TOKEN=new-secret\n")
    (workspace / "config").mkdir()
    (workspace / "config/prod.ENV").write_text("TOKEN=other-secret\n")
    (workspace / "config/.env.example").write_text("TOKEN=example-secret\n")
    (workspace / "keys").mkdir()
    (workspace / "keys/id_rsa").write_text("private-secret\n")
    response = client.get("/api/git/status")
    files = entries(response.json())
    for key in (("unstaged", ".env"), ("untracked", "config/prod.ENV"), ("untracked", "config/.env.example"),
                ("untracked", "keys/id_rsa")):
        assert files[key]["hidden"] == "credential" and files[key]["added"] is None
    assert "secret" not in response.text
    for path, area in ((".env", "unstaged"), (".env", "staged"), ("config/prod.ENV", "untracked"),
                       ("keys/id_rsa", "untracked"), ("CONFIG/../.env", "unstaged")):
        response = diff(client, path, area)
        assert response.status_code == 400
        assert "secret" not in response.text
    assert diff(client, ".env", "unstaged").json()["detail"] == "Credential files are not shown."
    assert diff(client, "keys/id_rsa", "untracked").json()["detail"] == "Credential files are not shown."


def test_a_staged_rename_from_a_credential_file_never_shows_its_lines(repository):
    client, workspace = repository
    git(workspace, "mv", ".env", "notes.txt")
    response = client.get("/api/git/status")
    entry = entries(response.json())[("staged", "notes.txt")]
    assert entry["orig_path"] == ".env" and entry["status"] == "renamed"
    assert entry["hidden"] == "credential" and entry["added"] is None
    assert "TOKEN" not in response.text
    refused = diff(client, "notes.txt", "staged", ".env")
    assert refused.status_code == 400 and refused.json()["detail"] == "Credential files are not shown."
    assert "TOKEN" not in refused.text
    # Also when the rename is modified again in the working tree.
    (workspace / "notes.txt").write_text("TOKEN=old\nmore\n")
    response = client.get("/api/git/status")
    assert entries(response.json())[("unstaged", "notes.txt")]["hidden"] == "credential"
    assert "TOKEN" not in response.text and "TOKEN" not in diff(client, "notes.txt", "unstaged", ".env").text


def test_generated_folders_are_listed_but_not_shown(repository):
    client, workspace = repository
    (workspace / "dist").mkdir()
    (workspace / "dist/bundle.js").write_text("generated()\n")
    entry = entries(status(client))[("untracked", "dist/bundle.js")]
    assert entry["hidden"] == "generated"
    response = diff(client, "dist/bundle.js", "untracked")
    assert response.status_code == 400 and response.json()["detail"] == "Files in generated folders are not shown here."


@pytest.mark.parametrize("path", ["/etc/hosts", "../outside.txt", "a/../../b", "~/notes.txt", ""])
def test_paths_must_stay_inside_the_repository(repository, path):
    client, _ = repository
    assert diff(client, path or "", "unstaged").status_code == 400
    assert diff(client, "app.py", "staged", path or "../x").status_code == 400


def test_names_with_spaces_and_glob_characters_are_literal(repository):
    client, workspace = repository
    for name in ("a b.py", "*.py", "x.py"):
        (workspace / name).write_text("old\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "names")
    for name in ("a b.py", "*.py", "x.py"):
        (workspace / name).write_text(f"new {name}\n")
    files = entries(status(client))
    assert files[("unstaged", "a b.py")]["added"] == 1 and files[("unstaged", "*.py")]["added"] == 1
    spaced = diff(client, "a b.py", "unstaged").json()
    assert [line["text"] for line in spaced["lines"] if line["kind"] == "added"] == ["new a b.py"]
    star = diff(client, "*.py", "unstaged").json()
    assert [line["text"] for line in star["lines"] if line["kind"] == "added"] == ["new *.py"]
    assert star["added"] == 1 and star["removed"] == 1


def test_fsmonitor_and_clean_filter_programs_do_not_run(repository, tmp_path):
    client, workspace = repository
    monitor, filtered = tmp_path / "fsmonitor-ran", tmp_path / "filter-ran"
    git(workspace, "config", "core.fsmonitor", str(program(tmp_path / "fsmonitor.sh", monitor)))
    git(workspace, "config", "filter.demo.clean", str(program(tmp_path / "clean.sh", filtered)))
    git(workspace, "config", "filter.demo.required", "true")
    (workspace / ".gitattributes").write_text("*.py filter=demo\n")
    (workspace / "app.py").write_text("print('filtered')\n")
    files = entries(status(client))
    assert files[("unstaged", "app.py")]["added"] == 1
    lines = diff(client, "app.py", "unstaged").json()["lines"]
    assert {"kind": "added", "text": "print('filtered')", "new": 1} in lines
    assert not monitor.exists() and not filtered.exists()


def test_a_folder_that_is_not_a_repository(client):
    client, _ = client
    assert status(client) == {"repository": False, "branch": None, "files": [], "truncated": False}
    assert client.get("/api/git").status_code == 200  # The older endpoint is unchanged.


def test_status_is_truncated_by_entries_and_output(repository, monkeypatch):
    client, workspace = repository
    for index in range(12):
        (workspace / f"file-{index:02d}.txt").write_text("data\n")
    monkeypatch.setattr(git_tools, "STATUS_ENTRY_LIMIT", 5)
    result = status(client)
    assert result["truncated"] is True and len(result["files"]) == 5
    monkeypatch.setattr(git_tools, "STATUS_ENTRY_LIMIT", 2000)
    monkeypatch.setattr(git_tools, "STATUS_OUTPUT_LIMIT", 200)
    result = status(client)
    assert result["truncated"] is True and 0 < len(result["files"]) < 12
    assert all(entry["path"].startswith("file-") and entry["path"].endswith(".txt") for entry in result["files"])


def test_a_long_diff_is_truncated_at_a_line(repository, monkeypatch):
    client, workspace = repository
    (workspace / "app.py").write_text("".join(f"line {index}\n" for index in range(500)))
    monkeypatch.setattr(git_tools, "FILE_DIFF_LIMIT", 1000)
    result = diff(client, "app.py", "unstaged").json()
    assert result["truncated"] is True
    assert all(line["text"].startswith("line ") for line in result["lines"] if line["kind"] == "added")
    assert 10 < result["added"] < 120


async def test_a_workspace_inside_a_repository_sees_paths_relative_to_itself(tmp_path):
    repository = tmp_path / "repo"
    (repository / "sub").mkdir(parents=True)
    git(repository, "init", "-q")
    (repository / "sub/a.txt").write_text("a\n")
    (repository / "top.txt").write_text("top\n")
    git(repository, "add", "-A")
    git(repository, "commit", "-qm", "initial")
    (repository / "sub/a.txt").write_text("a\nb\n")
    (repository / "top.txt").write_text("changed\n")
    result = await git_tools.working_tree_status(repository / "sub", lambda path: None)
    assert [(entry["area"], entry["path"]) for entry in result["files"]] == [("unstaged", "a.txt")]
    assert result["files"][0]["added"] == 1
    lines = (await git_tools.file_diff(repository / "sub", "a.txt", "unstaged"))["lines"]
    assert {"kind": "added", "text": "b", "new": 2} in lines


def test_parsers_handle_renames_and_tabs():
    raw = b"1\t0\tplain.txt\x00-\t-\tpic.png\x002\t1\t\x00old name.txt\x00new\tname.txt\x00"
    assert git_tools.parse_numstat(raw) == {"plain.txt": (1, 0), "pic.png": (None, None), "new\tname.txt": (2, 1)}
    raw = (b"# branch.oid (initial)\x00# branch.head (detached)\x00"
           b"2 RM N... 100644 100644 100644 a b R90 new name.txt\x00old name.txt\x00? space dir/x y\x00")
    branch, found = git_tools.parse_status(raw)
    assert branch is None
    assert found == [{"path": "new name.txt", "area": "staged", "status": "renamed", "orig_path": "old name.txt"},
                     {"path": "new name.txt", "area": "unstaged", "status": "modified", "orig_path": "old name.txt"},
                     {"path": "space dir/x y", "area": "untracked", "status": "untracked"}]
