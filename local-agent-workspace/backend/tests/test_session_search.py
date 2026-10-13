"""User-facing conversation search: a bounded scan of titles and messages with redacted snippets."""
import pytest
from fastapi.testclient import TestClient

from local_agent import conversation_search
from local_agent.api import create_app
from local_agent.config import Settings

TOKEN = "configured-secret-value"
GITHUB = "ghp_" + "A1b2C3d4" * 5


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(workspace), env_file="")
    settings.credentials = lambda: ("https://gateway.example", TOKEN)
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        yield client, app.state.manager


def chat(manager, title, texts, workspace="/srv/project", **fields):
    session = manager.store.create({**manager.settings.values, "workspace": workspace})
    session["title"] = title
    session["events"] = [{"id": f"e{index}", "type": kind, "text": text, "created": float(index)}
                         for index, (kind, text) in enumerate(texts)]
    session["events"] += [{"id": "tool", "type": "tool", "name": "read_file", "output": "needle in a tool output"},
                          {"id": "note", "type": "notice", "text": "needle in a notice"}]
    session["wire"] = [{"role": "user", "content": "needle in the wire only"}]
    session.update(fields)
    manager.store.save(session)
    return session["id"]


def search(client, q, **params):
    response = client.get("/api/session-search", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_titles_and_user_and_assistant_text_match_every_word(client):
    client, manager = client
    first = chat(manager, "Parser rewrite", [("user", "Please fix the parser"), ("assistant", "The lexer is fixed now")])
    second = chat(manager, "Release notes", [("user", "Write release notes for the parser")])
    result = search(client, "parser")
    assert [item["id"] for item in result["results"]] == [second, first]  # Newest first.
    assert result["truncated"] is False
    assert result["results"][1]["matches"] == [{"event_id": "e0", "role": "user", "snippet": "Please fix the parser"}]
    assert result["results"][1]["title"] == "Parser rewrite" and result["results"][1]["archived"] is False
    assert [match["role"] for match in search(client, "LEXER fixed")["results"][0]["matches"]] == ["assistant"]
    # Every word, in one message or in the title.
    assert search(client, "lexer parser")["results"] == []
    titled = search(client, "rewrite parser")["results"]
    assert [item["id"] for item in titled] == [first] and titled[0]["matches"] == []
    # Tool output, notices and model history are not searched.
    assert search(client, "needle")["results"] == []


def test_at_most_three_matches_per_chat_with_snippets_around_the_hit(client):
    client, manager = client
    long = "word " * 200 + "target   here\n\nand " + "more " * 200
    chat(manager, "Chat", [("user", long)] * 5)
    matches = search(client, "target")["results"][0]["matches"]
    assert len(matches) == 3
    snippet = matches[0]["snippet"]
    assert "target here and" in snippet and snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) <= conversation_search.SNIPPET_CHARS + 2


def test_snippets_are_redacted(client):
    client, manager = client
    chat(manager, "Secrets", [("user", f"deploy with {TOKEN} please"), ("assistant", f"deploy uses {GITHUB} now")])
    matches = search(client, "deploy")["results"][0]["matches"]
    text = " ".join(match["snippet"] for match in matches)
    assert TOKEN not in text and GITHUB not in text and GITHUB[:12] not in text
    assert text.count("[REDACTED]") == 2


def test_a_secret_cut_by_the_snippet_edge_is_still_redacted():
    # Cut first, only "ghp_A1b2C3d4A" would remain: too short for the token shape to be recognised.
    text = "x" * 300 + " hit " + "y" * 88 + " " + GITHUB + " " + "z" * 300
    result = conversation_search.snippet(text, ["hit"])
    assert "hit" in result and "ghp_" not in result and "A1b2" not in result and "[REDA" in result


def test_a_tiny_budget_stops_early(client, monkeypatch):
    client, manager = client
    for number in range(3):
        chat(manager, f"Chat {number}", [("user", "common words")])
    monkeypatch.setattr(conversation_search, "MAX_BYTES", 1)
    result = search(client, "common")
    assert result["truncated"] is True and len(result["results"]) == 1
    monkeypatch.setattr(conversation_search, "MAX_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(conversation_search, "MAX_SECONDS", -1)
    assert search(client, "common") == {"results": [], "truncated": True}


def test_subagents_are_left_out_unless_asked_for(client):
    client, manager = client
    child = chat(manager, "Child", [("user", "Delegated task: audit")], is_subagent=True, parent_session_id="p")
    assert search(client, "audit")["results"] == []
    assert [item["id"] for item in search(client, "audit", include_subagents="true")["results"]] == [child]


def test_unreadable_files_are_skipped(client):
    client, manager = client
    good = chat(manager, "Good", [("user", "findable text")])
    folder = manager.store.conversations
    (folder / "garbage.jsonl").write_text("findable text but not json\n")
    (folder / "damaged.jsonl").write_text(
        '{"record":"session","version":1,"data":{"id":"damaged","title":"findable","updated":9e9,'
        '"workspace":"/srv/x","model":"m"}}\n{"record":"event","data":{"id":"x","type":"user","text":"findable text"}}\n'
        '{"record":"event","data":\n')
    result = search(client, "findable text")
    # The damaged body yields what it can before the broken line, and never an error.
    assert [item["id"] for item in result["results"]] == ["damaged", good]


def test_the_workspace_filter_includes_worktree_chats_of_the_project(client):
    client, manager = client
    here = chat(manager, "Here", [("user", "shared phrase")], workspace="/srv/project")
    worktree = chat(manager, "Worktree", [("user", "shared phrase")], workspace="/srv/state/worktrees/abc",
                    worktree={"id": "abc", "branch": "agent/abc", "source_workspace": "/srv/project"})
    chat(manager, "Elsewhere", [("user", "shared phrase")], workspace="/srv/other")
    result = search(client, "shared phrase", workspace="/srv/project")
    assert [item["id"] for item in result["results"]] == [worktree, here]
    assert len(search(client, "shared phrase")["results"]) == 3


def test_archived_chats_are_found_and_marked(client):
    client, manager = client
    chat(manager, "Old", [("user", "archived idea")], archived=True)
    assert search(client, "idea")["results"][0]["archived"] is True


@pytest.mark.parametrize("params,status", [
    ({"q": "a"}, 422), ({"q": "x" * 201}, 422), ({"q": "ok", "limit": 0}, 422), ({"q": "ok", "limit": 51}, 422),
    ({"q": "ok", "workspace": "relative/path"}, 400),
])
def test_search_parameters_are_bounded(client, params, status):
    client, _ = client
    assert client.get("/api/session-search", params=params).status_code == status


def test_the_result_limit(client):
    client, manager = client
    for number in range(5):
        chat(manager, f"Chat {number}", [("user", "limit me")])
    assert len(search(client, "limit", limit=2)["results"]) == 2
