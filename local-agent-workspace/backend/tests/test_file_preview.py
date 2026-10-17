"""The Workspace preview and Download (M8g2): GET /api/file/raw serves a file's bytes with the file tools'
exclusions, inline only for images and PDF whose bytes match their type, and never as a page of the app's
origin; GET /api/file says when a file is not text."""
import os
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from local_agent import tools as tools_module
from local_agent.api import INDEX_POLICY, content_disposition, create_app
from local_agent.config import Settings
from local_agent.tools import NotText, WorkspaceTools

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
PDF = b"%PDF-1.4\n%%EOF\n"


@pytest.fixture
def project(tmp_path):
    workspace = tmp_path / "demo"
    (workspace / "img").mkdir(parents=True)
    (workspace / "img" / "logo.png").write_bytes(PNG)
    (workspace / "paper.pdf").write_bytes(PDF)
    (workspace / "page.html").write_text("<script>alert(1)</script>")
    (workspace / "drawing.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    (workspace / "fake.png").write_text("<html><script>alert(1)</script></html>")  # An HTML page named .png.
    (workspace / "archive.zip").write_bytes(b"PK\x03\x04" + bytes(range(256)))
    (workspace / "notes.md").write_text("# Notes\n")
    return workspace


@pytest.fixture
def client(tmp_path, project):
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        client.app_settings = settings
        yield client


def raw(client, path, **params):
    return client.get("/api/file/raw", params={"path": path, **params})


@pytest.mark.parametrize("path, kind", [("img/logo.png", "image/png"), ("paper.pdf", "application/pdf")])
def test_images_and_pdf_are_served_inline_with_their_type(client, project, path, kind):
    response = raw(client, path)
    assert response.status_code == 200
    assert response.content == (project / path).read_bytes()
    assert response.headers["content-type"] == kind
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    # download=1 makes any file an attachment.
    download = raw(client, path, download="1")
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["content-disposition"].startswith("attachment;")


@pytest.mark.parametrize("path", ["page.html", "drawing.svg", "fake.png", "archive.zip", "notes.md"])
def test_every_other_file_is_an_attachment_that_cannot_run_as_a_page(client, project, path):
    # HTML and SVG can hold scripts, and a .png that is really HTML must not become a page of this origin.
    response = raw(client, path)
    assert response.status_code == 200 and response.content == (project / path).read_bytes()
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_the_file_name_is_given_as_ascii_and_utf8(client, project):
    (project / 'résumé "v2".png').write_bytes(PNG)
    header = raw(client, 'résumé "v2".png').headers["content-disposition"]
    assert header.startswith('inline; filename="r_sum_ _v2_.png"; filename*=UTF-8\'\'')
    assert unquote(header.split("UTF-8''", 1)[1]) == 'résumé "v2".png'
    assert content_disposition("attachment", "a;b%c\\d") == "attachment; filename=\"a_b_c_d\"; filename*=UTF-8''a%3Bb%25c%5Cd"


def test_credential_files_the_state_folder_and_outside_paths_are_refused(client, project, tmp_path):
    (project / ".env").write_text("DBRICKS_TOKEN=x\n")
    (project / "secrets").mkdir()
    (project / "secrets" / "id_rsa").write_text("key\n")
    outside = tmp_path / "other.png"
    outside.write_bytes(PNG)
    (project / "link.png").symlink_to(outside)
    state = client.app_settings.state_dir
    (state / "shot.png").write_bytes(PNG)
    for path, message in ((".env", "Credential files are excluded from file tools."),
                          ("secrets/id_rsa", "Credential files are excluded from file tools."),
                          (str(state / "settings.json"), "The app's settings and conversation folder is excluded"),
                          (str(state / "shot.png"), "The app's settings and conversation folder is excluded"),
                          (str(outside), "This folder needs access approval"),
                          ("link.png", "This folder needs access approval"),
                          ("../other.png", "This folder needs access approval")):
        response = raw(client, path)
        assert response.status_code == 400, path
        assert response.json()["detail"].startswith(message), path
    assert raw(client, "img").json()["detail"] == "Only files can be previewed or downloaded."
    assert raw(client, "missing.png").json()["detail"].startswith("missing.png does not exist in")


def test_the_size_limit(client, project, monkeypatch):
    assert tools_module.MAX_RAW_BYTES == 20_000_000
    (project / "big.pdf").write_bytes(b"%PDF-" + b"x" * 1_000_000)
    with pytest.raises(ValueError, match=r"^File is too large to preview or download here \(1 MB limit\)\.$"):
        WorkspaceTools(str(project)).read_bytes("big.pdf", 1_000_000)
    assert WorkspaceTools(str(project)).read_bytes("paper.pdf", 1_000_000)[1] == PDF
    monkeypatch.setattr(WorkspaceTools.read_bytes, "__defaults__", (1_000_000,))
    response = raw(client, "big.pdf")
    assert response.status_code == 400
    assert response.json()["detail"] == "File is too large to preview or download here (1 MB limit)."


def test_a_link_swapped_in_after_the_check_is_not_followed(project, tmp_path, monkeypatch):
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    tools = WorkspaceTools(str(project))
    checked = tools.path

    def swap(value):
        target = checked(value)
        target.unlink()
        target.symlink_to(secret)  # Replaced between the check and the open.
        return target
    monkeypatch.setattr(tools, "path", swap)
    with pytest.raises(OSError):
        tools.read_bytes("img/logo.png")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_named_pipe_does_not_block(project):
    os.mkfifo(project / "pipe.png")
    with pytest.raises(ValueError, match="Only files"):
        WorkspaceTools(str(project)).read_bytes("pipe.png")


@pytest.mark.parametrize("mode", ["manual", "bypassPermissions"])
def test_a_worktree_chat_reads_its_own_copy(tmp_path, project, mode):
    worktree = tmp_path / "state" / "worktrees" / "1234"
    (worktree / "img").mkdir(parents=True)
    (worktree / "img" / "logo.png").write_bytes(PNG + b"worktree")
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
        response = client.get("/api/file/raw", params={"path": str(project / "img" / "logo.png"),
                                                       "session_id": created["id"]}, headers=headers)
        assert response.status_code == 200 and response.content == PNG + b"worktree"
        assert response.headers["x-opened-path"] == "img/logo.png"


def test_the_text_route_says_when_a_file_is_not_text(client, project):
    (project / "latin1.txt").write_bytes("caf\xe9\n".encode("latin-1"))
    (project / "nul.txt").write_bytes(b"a\x00b")
    for path, message in (("archive.zip", "This file is not UTF-8 text, so the text editor cannot open it."),
                          ("latin1.txt", "This file is not UTF-8 text, so the text editor cannot open it."),
                          ("nul.txt", "Binary files cannot be opened in the text editor.")):
        response = client.get("/api/file", params={"path": path})
        assert response.status_code == 400
        assert response.json()["detail"] == {"message": message, "code": "not_text"}
    assert issubclass(NotText, ValueError)  # Callers that catch ValueError keep working.
    assert client.get("/api/file", params={"path": "notes.md"}).json()["content"] == "# Notes\n"


def test_the_page_lets_preview_frames_reach_no_address(client):
    page = client.get("/")
    assert page.headers["content-security-policy"] == INDEX_POLICY == "frame-src blob:; frame-ancestors 'none'"
