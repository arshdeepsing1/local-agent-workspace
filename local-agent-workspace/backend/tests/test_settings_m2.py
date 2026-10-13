"""User-level settings added in M2: custom instructions, file write limit,
environment pass-through names and continuing after rate limits."""
import json

import pytest
from fastapi.testclient import TestClient

from local_agent import config
from local_agent.api import create_app
from local_agent.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(tmp_path), env_file="")
    return settings


def test_defaults(settings):
    assert settings.values["user_instructions"] == ""
    assert settings.values["write_limit_kb"] == 80
    assert settings.values["command_env"] == []
    assert settings.values["rate_limit_auto_continue"] is True


@pytest.mark.parametrize("values, message", [
    ({"user_instructions": "x" * 8001}, "Custom instructions must be text of at most 8,000 characters."),
    ({"user_instructions": "a\x00b"}, "Custom instructions must be text of at most 8,000 characters."),
    ({"user_instructions": 5}, "Custom instructions must be text of at most 8,000 characters."),
    ({"write_limit_kb": 15}, "File write limit must be an integer from 16 to 1,000 KB."),
    ({"write_limit_kb": 1001}, "File write limit must be an integer from 16 to 1,000 KB."),
    ({"write_limit_kb": 80.0}, "File write limit must be an integer from 16 to 1,000 KB."),
    ({"write_limit_kb": True}, "File write limit must be an integer from 16 to 1,000 KB."),
    ({"command_env": "JAVA_HOME"}, "List at most 32 environment variable names (letters, digits and _, not starting with a digit)."),
    ({"command_env": [f"NAME_{index}" for index in range(33)]}, "List at most 32 environment variable names"),
    ({"command_env": ["JAVA_HOME", "JAVA_HOME"]}, "List at most 32 environment variable names"),
    ({"command_env": ["1ABC"]}, "List at most 32 environment variable names"),
    ({"command_env": ["A-B"]}, "List at most 32 environment variable names"),
    ({"command_env": ["A" * 65]}, "List at most 32 environment variable names"),
    ({"command_env": ["NAME\n"]}, "List at most 32 environment variable names"),
    ({"command_env": [None]}, "List at most 32 environment variable names"),
    ({"command_env": ["DBRICKS_TOKEN"]}, "DBRICKS_TOKEN cannot be passed to commands."),
    ({"command_env": ["DATABRICKS_HOST"]}, "DATABRICKS_HOST cannot be passed to commands."),
    ({"command_env": ["LOCAL_AGENT_ENV_FILE"]}, "LOCAL_AGENT_ENV_FILE cannot be passed to commands."),
    ({"command_env": ["LD_PRELOAD"]}, "LD_PRELOAD changes how programs start and cannot be passed to commands."),
    ({"command_env": ["DYLD_INSERT_LIBRARIES"]}, "DYLD_INSERT_LIBRARIES changes how programs start"),
    ({"command_env": ["JAVA_HOME", "BASH_ENV"]}, "BASH_ENV changes how programs start and cannot be passed to commands."),
    ({"command_env": ["NODE_OPTIONS"]}, "NODE_OPTIONS changes how programs start"),
    ({"rate_limit_auto_continue": "yes"}, "Continue after rate limits must be on or off."),
    ({"rate_limit_auto_continue": 1}, "Continue after rate limits must be on or off."),
])
def test_every_new_setting_is_validated(settings, values, message):
    before = dict(settings.values)
    with pytest.raises(ValueError) as error:
        settings.update(values)
    assert str(error.value).startswith(message)
    assert settings.values == before
    assert not settings.path.exists()


def test_denied_names_match_the_documented_sets():
    assert config.CREDENTIAL_ENV == {"DBRICKS_TOKEN", "DATABRICKS_TOKEN", "DBRICKS_URL", "DATABRICKS_HOST",
                                     "LOCAL_AGENT_ENV_FILE"}
    assert config.DENIED_ENV == {"BASH_ENV", "ENV", "ZDOTDIR", "PROMPT_COMMAND", "SHELLOPTS", "BASHOPTS", "IFS",
                                 "PYTHONSTARTUP", "PERL5OPT", "RUBYOPT", "NODE_OPTIONS"}
    assert config.DENIED_ENV_PREFIXES == ("LD_", "DYLD_")


def test_new_settings_round_trip_through_settings_json(settings, tmp_path):
    values = {"user_instructions": "Answer briefly.\nUse metric units.", "write_limit_kb": 1000,
              "command_env": ["JAVA_HOME", "NVM_DIR", "_PRIVATE_1"], "rate_limit_auto_continue": False}
    public = settings.update(values)
    assert {key: public[key] for key in values} == values
    saved = json.loads(settings.path.read_text())
    assert {key: saved[key] for key in values} == values
    reloaded = Settings(tmp_path / "state")
    assert {key: reloaded.values[key] for key in values} == values
    settings.update({"write_limit_kb": 16, "user_instructions": "x" * 8000, "command_env": [f"N{i}" for i in range(32)]})


def test_put_settings_accepts_the_new_fields(settings, tmp_path):
    with TestClient(create_app(settings)) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        base = {"workspace": str(tmp_path), "model": "test-model", "env_file": ""}
        body = {**base, "user_instructions": "Be precise.", "write_limit_kb": 200, "command_env": ["JAVA_HOME"],
                "rate_limit_auto_continue": False}
        response = client.put("/api/settings", json=body)
        assert response.status_code == 200
        result = response.json()
        assert (result["user_instructions"], result["write_limit_kb"], result["command_env"],
                result["rate_limit_auto_continue"]) == ("Be precise.", 200, ["JAVA_HOME"], False)
        assert client.get("/api/bootstrap").json()["settings"]["write_limit_kb"] == 200
        # Omitted fields keep their saved values.
        assert client.put("/api/settings", json=base).json()["command_env"] == ["JAVA_HOME"]
        for field, value in (("write_limit_kb", 15), ("write_limit_kb", "80"), ("command_env", "JAVA_HOME"),
                             ("user_instructions", "x" * 8001), ("rate_limit_auto_continue", "true")):
            assert client.put("/api/settings", json={**base, field: value}).status_code == 422
        response = client.put("/api/settings", json={**base, "command_env": ["LD_PRELOAD"]})
        assert response.status_code == 400
        assert response.json()["detail"] == "LD_PRELOAD changes how programs start and cannot be passed to commands."


def test_file_endpoint_returns_the_absolute_path(settings, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("print('hi')\n")
    with TestClient(create_app(settings)) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        result = client.get("/api/file", params={"path": "src/app.py"}).json()
    assert result == {"path": "src/app.py", "content": "print('hi')\n", "absolute": str((tmp_path / "src/app.py").resolve())}
