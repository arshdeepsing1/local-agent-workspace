"""GET /api/connection says which step of the Databricks check failed (error_kind)."""
import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent.api import create_app
from local_agent.config import Settings


def check(tmp_path, monkeypatch, gateway, credentials=("https://gateway.example", "fake-token")):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    if credentials:
        settings.credentials = lambda: credentials
    requests = []

    async def handler(request):
        requests.append(str(request.url))
        return await gateway(request)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(handler)))
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        response = client.get("/api/connection", headers=headers)
    assert response.status_code == 200
    return response.json(), requests


async def unused(request):
    raise AssertionError("no request expected")


def test_missing_credentials_are_a_credentials_problem(tmp_path, monkeypatch):
    for name in ("DBRICKS_URL", "DBRICKS_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    result, requests = check(tmp_path, monkeypatch, unused, credentials=None)
    assert result == {"connected": False, "models": [], "error_kind": "credentials",
                      "error": "Set a valid HTTPS Databricks workspace URL in your credential file."}
    assert requests == []


def test_http_error_status_is_reported_with_its_status(tmp_path, monkeypatch):
    async def gateway(request):
        return httpx.Response(401, json={"message": "bad token"})

    result, _ = check(tmp_path, monkeypatch, gateway)
    assert result == {"connected": False, "models": [], "error_kind": "http", "http_status": 401,
                      "error": "Databricks returned HTTP 401. Check your credentials and access."}


@pytest.mark.parametrize("error, text", [
    (httpx.ConnectError("All connection attempts failed"), "All connection attempts failed"),
    (httpx.ReadTimeout(""), "ReadTimeout"),
])
def test_network_failure_is_a_network_problem(tmp_path, monkeypatch, error, text):
    async def gateway(request):
        raise error

    result, _ = check(tmp_path, monkeypatch, gateway)
    assert result == {"connected": False, "models": [], "error_kind": "network", "error": text}


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="<html>Sign in</html>"),
    httpx.Response(200, json=["not", "an", "object"]),
    httpx.Response(200, json={"endpoints": "nope"}),
])
def test_unreadable_answer_is_an_invalid_response(tmp_path, monkeypatch, response):
    async def gateway(request):
        return response

    result, _ = check(tmp_path, monkeypatch, gateway)
    assert result == {"connected": False, "models": [], "error_kind": "invalid_response",
                      "error": "Databricks returned a response the app could not read."}


def test_success_has_no_error_kind(tmp_path, monkeypatch):
    async def gateway(request):
        return httpx.Response(200, json={"endpoints": [{"name": "test-chat"}, {"name": "test-embedding"}, {}]})

    result, requests = check(tmp_path, monkeypatch, gateway)
    assert result == {"connected": True, "models": ["test-chat"], "error": None}
    assert requests == ["https://gateway.example/api/2.0/serving-endpoints"]
