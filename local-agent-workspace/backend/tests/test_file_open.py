"""The editor's GET /api/file: files named from another folder, and a clear message for a missing file."""
import pytest
from fastapi.testclient import TestClient

from local_agent.api import create_app, locate_file
from local_agent.config import Settings
from local_agent.tools import WorkspaceTools


@pytest.fixture
def project(tmp_path):
    workspace = tmp_path / "repo" / "demo"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.py").write_text("print('hi')\n")
    (workspace / "README.md").write_text("# demo\n")
    return workspace


def test_a_path_named_from_the_project_folder_opens_the_project_file(tmp_path, project):
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        exact = client.get("/api/file", params={"path": "src/app.py"}, headers=headers).json()
        assert exact["path"] == "src/app.py" and exact["content"] == "print('hi')\n"
        # The project's own folder name, or the folders above it from the repository root.
        for named in ("demo/src/app.py", "repo/demo/src/app.py", "./demo/src/app.py"):
            found = client.get("/api/file", params={"path": named}, headers=headers)
            assert found.status_code == 200, named
            assert found.json()["path"] == "src/app.py"
            assert found.json()["absolute"] == str(project / "src" / "app.py")
        missing = client.get("/api/file", params={"path": "other/src/app.py"}, headers=headers)
        assert missing.status_code == 400
        assert missing.json()["detail"] == (f"other/src/app.py does not exist in {project}. It may have been moved "
                                            "or deleted, or it was named relative to another folder.")
        # Only leading folders that are the project's own are dropped.
        assert client.get("/api/file", params={"path": "docs/README.md"}, headers=headers).status_code == 400


def test_a_worktree_chat_maps_the_source_projects_paths_to_its_worktree(tmp_path, project):
    worktree = tmp_path / "state" / "worktrees" / "1234"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "app.py").write_text("print('worktree')\n")
    tools = WorkspaceTools(str(worktree))
    record = {"branch": "chat/x", "source_workspace": str(project)}
    assert locate_file(tools, str(project / "src" / "app.py"), record) == "src/app.py"
    assert locate_file(tools, "demo/src/app.py", record) == "src/app.py"
    assert locate_file(tools, str(project / "README.md"), record) is None  # Not in the worktree.
    assert locate_file(tools, str(project / "src" / "app.py"), None) is None
    assert locate_file(tools, "demo/../src/app.py", record) is None


@pytest.mark.parametrize("mode", ["manual", "bypassPermissions"])
def test_a_worktree_chat_opens_its_own_copy_of_a_source_project_path(tmp_path, project, mode):
    # The source project's absolute path names the worktree's copy, in every mode: the editor must
    # never read (and later save) the main checkout's file from a worktree chat.
    (project / "src" / "app.py").write_text("print('source')\n")
    worktree = tmp_path / "state" / "worktrees" / "1234"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "app.py").write_text("print('worktree')\n")
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        created = client.post("/api/sessions", json={}, headers=headers).json()
        manager = app.state.manager
        session = manager.get(created["id"])
        session.update(workspace=str(worktree), permission_mode=mode,
                       worktree={"id": "1234", "branch": "chat/x", "source_workspace": str(project)})
        manager.store.save(session)
        opened = client.get("/api/file", params={"path": str(project / "src" / "app.py"), "session_id": created["id"]},
                            headers=headers)
        assert opened.status_code == 200, opened.json()
        assert opened.json()["path"] == "src/app.py" and opened.json()["content"] == "print('worktree')\n"
        assert opened.json()["absolute"] == str(worktree / "src" / "app.py")


def test_a_path_named_relative_to_where_the_chat_worked_opens_that_file(tmp_path, project):
    # A reply names template/pipeline.json after its commands ran in a repository below the project.
    repository = project / "repos" / "pipeline"
    (repository / "template").mkdir(parents=True)
    (repository / "config").mkdir()
    (repository / "template" / "pipeline.json").write_text("{}\n")
    (project / "notes").mkdir()
    (project / "notes" / "pipeline.json").write_text("[]\n")  # Older work: the latest folders come first.
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        created = client.post("/api/sessions", json={}, headers=headers).json()
        manager = app.state.manager
        session = manager.get(created["id"])

        def opened(path):
            return client.get("/api/file", params={"path": path, "session_id": created["id"]}, headers=headers)

        assert opened("template/pipeline.json").status_code == 400  # Nothing done yet: not found.
        session["events"] += [
            {"id": "1", "type": "tool", "name": "read_file", "input": {"path": str(project / "notes" / "pipeline.json")}},
            {"id": "2", "type": "tool", "name": "run_command", "input": {"command": f"cd {repository / 'config'} && ls"}},
        ]
        manager.store.save(session)
        # The folder the command ran in, then its parent: the repository's own template folder.
        found = opened("template/pipeline.json")
        assert found.status_code == 200, found.json()
        assert found.json()["path"] == "repos/pipeline/template/pipeline.json" and found.json()["content"] == "{}\n"
        assert opened("pipeline.json").json()["path"] == "notes/pipeline.json"
        assert opened("../pipeline/template/pipeline.json").status_code == 400  # Never outside a named folder.
        assert opened("missing.json").json()["detail"].startswith("missing.json does not exist")


def test_chat_folders_reads_commands_tools_and_instruction_folders_most_recent_first(tmp_path):
    from local_agent.api import chat_folders
    root = tmp_path / "project"
    session = {"instruction_directories": ["docs", "a/b"], "events": [
        {"type": "tool", "name": "read_file", "input": {"path": "a/b/c.py"}},
        {"type": "tool", "name": "list_files", "input": {"path": "lib/sub"}},
        {"type": "tool", "name": "run_command", "input": {"command": 'cd "x y/z"; make', "cwd": "w"}},
        {"type": "tool", "name": "run_command", "input": {"command": f"cd '{tmp_path}/elsewhere' && ls"}},  # Outside.
        {"type": "tool", "name": "run_command", "input": {"command": "echo cd later && ls"}},  # Not leading.
        {"type": "tool", "name": "read_file", "input": {"path": f"{root}/top.md"}},  # The project root.
        {"type": "user", "text": "cd nowhere && ls"},
    ]}
    assert chat_folders(session, root) == ["w", "x y/z", "lib/sub", "a/b", "docs", "x y", "lib", "a"]
