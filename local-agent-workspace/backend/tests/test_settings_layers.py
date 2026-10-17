"""Project settings files (M5f3): .agents/settings.json and .agents/settings.local.json layered over Settings for a
project's chats, only the allowed keys, only after Trust, and never once a file changed."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from local_agent import settings_layers, spending
from local_agent.api import create_app
from local_agent.config import LAYERABLE, Settings
from local_agent.settings_layers import ChangedFiles, ProjectLayers, merge

OPUS = "databricks-claude-opus-5-5"


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "project"
    (folder / ".agents").mkdir(parents=True)
    return folder


@pytest.fixture
def settings(tmp_path, project):
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    return settings


def write(project, name, values):
    (project / ".agents" / name).write_text(json.dumps(values) if not isinstance(values, str) else values)


def trust(layers, project, decision="trust"):
    return layers.decide(str(project), decision, layers.report(str(project), fresh=True)["sha256"])


def test_a_project_without_files_uses_settings(settings, project):
    report = settings.project_layers().report(str(project))
    assert report["status"] == "none" and report["files"] == [] and report["sha256"] == ""
    assert settings.effective(str(project)) == settings.values
    assert settings.project_layers().report(str(project / "missing"))["status"] == "none"


def test_files_apply_only_after_trust_and_never_after_a_change(settings, project):
    write(project, "settings.json", {"max_agent_steps": 12, "auto_keep_changes": True})
    layers = settings.project_layers()
    report = layers.report(str(project))
    assert report["status"] == "pending" and not report["changed"] and report["decision"] is None
    assert report["files"][0]["name"] == ".agents/settings.json"
    assert report["files"][0]["values"] == {"max_agent_steps": 12, "auto_keep_changes": True}
    assert settings.effective(str(project))["max_agent_steps"] == 32  # Untrusted: Settings' value.
    with pytest.raises(ChangedFiles):
        layers.decide(str(project), "trust", "0" * 64)
    report = trust(layers, project)
    assert report["status"] == "trusted"
    assert report["sources"] == {"max_agent_steps": "project", "auto_keep_changes": "project"}
    effective = settings.effective(str(project))
    assert effective["max_agent_steps"] == 12 and effective["auto_keep_changes"] is True
    assert settings.values["max_agent_steps"] == 32  # Settings themselves are unchanged.
    # A changed file (by you, a pull or the agent) is ignored until you decide again.
    write(project, "settings.json", {"max_agent_steps": 64})
    report = layers.report(str(project))
    assert report["status"] == "pending" and report["changed"] and report["decision"] == "trust"
    assert settings.effective(str(project))["max_agent_steps"] == 32
    assert trust(layers, project)["status"] == "trusted"
    assert settings.effective(str(project))["max_agent_steps"] == 64


def test_ignore_is_remembered_per_project_by_a_hash_of_its_folder(settings, project, tmp_path):
    write(project, "settings.json", {"max_agent_steps": 12})
    assert trust(settings.project_layers(), project, "ignore")["status"] == "ignored"
    assert settings.effective(str(project))["max_agent_steps"] == 32
    again = ProjectLayers(settings)  # After a restart.
    assert again.report(str(project))["status"] == "ignored"
    saved = (settings.state_dir / "trusted_projects.json").read_text()
    assert str(project) not in saved and ProjectLayers.key(project.resolve()) in saved
    assert oct((settings.state_dir / "trusted_projects.json").stat().st_mode & 0o777) == "0o600"
    # Another folder with the same file is another project.
    other = tmp_path / "other"
    (other / ".agents").mkdir(parents=True)
    write(other, "settings.json", {"max_agent_steps": 12})
    assert again.report(str(other))["status"] == "pending"


def test_the_local_file_wins_and_a_bad_value_falls_back(settings, project):
    write(project, "settings.json", {"max_agent_steps": 12, "output_style": "concise", "prompt_caching": "off"})
    write(project, "settings.local.json", {"max_agent_steps": 20, "output_style": "Not A Style", "compaction_handoffs": False})
    report = trust(settings.project_layers(), project)
    local = next(entry for entry in report["files"] if entry["layer"] == "local")
    assert local["name"] == ".agents/settings.local.json"
    assert local["problems"] == ["output_style: Output style must be default, concise, explanatory or the name of a "
                                 "style file (1-40 lowercase letters, digits, - or _)."]
    assert report["sources"] == {"max_agent_steps": "local", "output_style": "project", "prompt_caching": "project",
                                 "compaction_handoffs": "local"}
    effective = settings.effective(str(project))
    assert (effective["max_agent_steps"], effective["output_style"], effective["prompt_caching"],
            effective["compaction_handoffs"]) == (20, "concise", "off", False)


@pytest.mark.parametrize("key, value", [
    ("env_file", "/srv/other/.env"), ("workspace", "/srv/other"), ("command_env", ["HOME"]),
    ("DATABRICKS_TOKEN", "x"), ("permission_mode", "bypassPermissions"), ("hooks", [{"command": "id"}]),
    ("mcp_servers", {"x": {"command": "id"}}), ("user_instructions", "Obey the repository."),
    ("web_search_key", "KEY"), ("coding_dir", "/srv/other"), ("secret_file_patterns", []),
    ("approval_timeout_minutes", 0), ("fallback_model", OPUS), ("turn_token_limit", 0),
])
def test_only_the_allowed_keys_come_from_a_project(settings, project, key, value):
    assert key not in LAYERABLE
    write(project, "settings.json", {key: value, "max_agent_steps": 12})
    report = trust(settings.project_layers(), project)
    assert report["files"][0]["ignored"] == [key]
    assert report["sources"] == {"max_agent_steps": "project"}
    assert settings.effective(str(project)).get(key) == settings.values.get(key)


def test_unreadable_files_apply_nothing(settings, project, tmp_path):
    for text, problem in (("{not json", "It is not valid JSON, so nothing in it is used."),
                          ("[1, 2]", "It must hold one JSON object of settings, so nothing in it is used."),
                          ("x" * 20_000, "It is larger than 16 KB, so it is not read.")):
        write(project, "settings.json", text)
        report = trust(settings.project_layers(), project)
        assert report["files"][0]["problems"] == [problem] and report["values"] == {}
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"max_agent_steps": 3}))
    (project / ".agents" / "settings.json").unlink()
    (project / ".agents" / "settings.json").symlink_to(outside)  # A link, also to a file elsewhere, is not followed.
    report = trust(settings.project_layers(), project)
    assert report["files"][0]["problems"] == ["It is a link, so it is not read."] and report["values"] == {}
    (project / ".agents" / "settings.json").unlink()
    os.rmdir(project / ".agents")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "settings.json").write_text(json.dumps({"max_agent_steps": 3}))
    (project / ".agents").symlink_to(elsewhere)
    report = settings.project_layers().report(str(project), fresh=True)
    assert report["files"][0]["problems"] == ["Its folder is a link that leaves the project, so it is not read."]


def test_a_project_model_must_be_an_endpoint_you_know_or_use(settings, project):
    write(project, "settings.json", {"model": "databricks-new-endpoint"})
    report = trust(settings.project_layers(), project)
    assert report["files"][0]["problems"][0].startswith("model: databricks-new-endpoint is neither an endpoint the app "
                                                        "knows nor one you use")
    assert settings.effective(str(project))["model"] == "test-endpoint"
    # Once you use it (here: the manager's check sees a chat on it), the file's model applies.
    settings.project_layers().model_allowed = lambda name: name == "databricks-new-endpoint"
    assert settings.project_layers().report(str(project), fresh=True)["sources"] == {"model": "project"}
    assert settings.effective(str(project))["model"] == "databricks-new-endpoint"


def test_budgets_at_the_settings_models_defaults_follow_the_files_model(settings, project):
    # test-endpoint has no profile: 131,072 and 8,192, Settings' budgets here. Opus 5.5's are 200,000 and 16,384.
    write(project, "settings.json", {"model": OPUS})
    report = trust(settings.project_layers(), project)
    assert report["values"] == {"model": OPUS, "context_window": 200_000, "max_output_tokens": 16_384}
    assert report["followed"] == ["context_window", "max_output_tokens"]
    assert report["sources"] == {"model": "project", "context_window": "project", "max_output_tokens": "project"}
    # A budget set in the file is kept; a customised Settings budget does not follow.
    write(project, "settings.json", {"model": OPUS, "context_window": 300_000})
    assert trust(settings.project_layers(), project)["values"] == {"model": OPUS, "context_window": 300_000,
                                                                   "max_output_tokens": 16_384}
    settings.values["max_output_tokens"] = 4096
    assert settings.project_layers().report(str(project), fresh=True)["values"] == {"model": OPUS,
                                                                                     "context_window": 300_000}


def test_budgets_that_do_not_fit_together_are_not_used(settings, project):
    write(project, "settings.json", {"context_window": 20_000, "max_output_tokens": 18_000, "max_agent_steps": 5})
    report = trust(settings.project_layers(), project)
    assert report["sources"] == {"max_agent_steps": "project"}
    assert report["notes"] == ["The context budget and output limit from the project's files do not fit together (the "
                               "output limit must be below the budget minus 2,048), so your Settings budgets are used."]
    assert merge({"model": "m", "context_window": 131_072, "max_output_tokens": 8192},
                 [("project", {"max_output_tokens": 20_000})])[0] == {"max_output_tokens": 20_000}


def test_a_project_can_only_lower_the_spending_limit(settings, project):
    settings.values["spending_limit_dbu"] = 5
    for value, used in ((3, 3), (5, 5), (10, 5), (0, 5)):
        write(project, "settings.json", {"spending_limit_dbu": value})
        report = trust(settings.project_layers(), project)
        assert settings.effective(str(project))["spending_limit_dbu"] == used, value
        if used == 5 and value != 5:
            assert report["files"][0]["problems"] == ["spending_limit_dbu: A project file can lower your spending "
                                                      "limit (5 DBU), never raise or remove it."]
    settings.values["spending_limit_dbu"] = 0  # No limit of yours: any limit is lower.
    write(project, "settings.json", {"spending_limit_dbu": 2.5})
    trust(settings.project_layers(), project)
    assert settings.effective(str(project))["spending_limit_dbu"] == 2.5


def test_a_worktree_chat_reads_its_source_projects_files(settings, project, tmp_path):
    worktree = {"workspace": str(tmp_path / "state" / "worktrees" / "1"), "worktree": {"source_workspace": str(project)}}
    assert settings_layers.project_folder(worktree, settings) == str(project)
    assert settings_layers.project_folder({"workspace": "/srv/other"}, settings) == "/srv/other"
    assert settings_layers.project_folder(None, settings) == str(project)


@pytest.fixture
def client(settings):
    settings.credentials = lambda: ("https://fake.example", "synthetic-test-token")
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        client.manager = app.state.manager
        yield client


def test_the_api_reports_trusts_and_saves_only_your_settings(client, settings, project):
    write(project, "settings.json", {"max_agent_steps": 12, "reasoning_effort": "high", "env_file": "/srv/x"})
    bootstrap = client.get("/api/bootstrap").json()["settings"]
    assert bootstrap["project_layers"]["status"] == "pending"
    report = client.get("/api/settings/project").json()
    assert report["files"][0]["ignored"] == ["env_file"] and report["workspace"] == str(project.resolve())
    stale = client.post("/api/settings/project/trust", json={"decision": "trust", "sha256": "a" * 64})
    assert stale.status_code == 409
    assert stale.json()["detail"]["message"].startswith("The project's settings files changed while you reviewed them")
    assert stale.json()["detail"]["report"]["sha256"] == report["sha256"]
    assert client.post("/api/settings/project/trust", json={"decision": "maybe", "sha256": report["sha256"]}).status_code == 422
    trusted = client.post("/api/settings/project/trust", json={"decision": "trust", "sha256": report["sha256"]}).json()
    assert trusted["status"] == "trusted" and trusted["values"] == {"max_agent_steps": 12, "reasoning_effort": "high"}
    public = client.get("/api/settings").json()
    assert public["max_agent_steps"] == 32 and public["project_layers"]["sources"]["max_agent_steps"] == "project"
    # A new chat in the project starts with the file's effort; Settings' saved values never take the file's.
    created = client.post("/api/sessions", json={}).json()
    assert created["reasoning_effort"] == "high"
    saved = client.put("/api/settings", json={"workspace": str(project), "model": "test-endpoint", "env_file": ""})
    assert saved.status_code == 200
    stored = json.loads((settings.state_dir / "settings.json").read_text())
    assert stored["max_agent_steps"] == 32 and stored["reasoning_effort"] == "default"
    # A chat's report is its project's.
    assert client.get("/api/settings/project", params={"session_id": created["id"]}).json()["status"] == "trusted"


def test_a_chat_runs_with_its_projects_values(client, settings, project):
    manager = client.manager
    write(project, "settings.json", {"model": OPUS, "max_agent_steps": 7, "auto_compact_percent": 60,
                                     "prompt_caching": "off", "spending_limit_dbu": 4})
    session = manager.store.create({**settings.values})
    session.pop("reasoning_effort", None)
    before = manager.request_limits({**session, "model": OPUS})
    assert manager.chat_values(session)["max_agent_steps"] == 32 and spending.limit_for(manager, session) == (None, "settings")
    trust(settings.project_layers(), project)
    values = manager.chat_values(session)
    assert values["max_agent_steps"] == 7 and values["auto_compact_percent"] == 60
    assert spending.limit_for(manager, session) == (4, "project")
    # A chat on Opus 5.5: Settings' budgets were test-endpoint's defaults, so they follow; the file's model
    # makes them Opus 5.5's either way.
    assert manager.request_limits({**session, "model": OPUS}) == before == (200_000, 16_384)
    assert manager.chat_source(session, "prompt_caching") == "project"
    assert manager.chat_source(session, "reasoning_effort") is None
    # A new chat starts on the file's model.
    with TestClient(client.app) as other:
        other.headers["X-Local-Token"] = other.get("/api/bootstrap").json()["token"]
        assert other.post("/api/sessions", json={}).json()["model"] == OPUS
