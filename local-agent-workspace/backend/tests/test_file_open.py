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
