import httpx
import pytest

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.telemetry import LEDGER_VERSION, session_metrics


def test_session_metrics_aggregates_purposes_models_and_documented_dbu_rates():
    session = {
        "id": "metrics-session",
        "created": 1,
        "events": [],
        "inference_ledger_version": LEDGER_VERSION,
        "inference_calls": [
            {"id": "agent-1", "purpose": "agent", "model": "databricks-claude-opus-4-8",
             "created": "2026-09-22T10:00:00Z", "status": "completed", "http_status": 200,
             "usage": {"input_tokens": 1_000_000, "output_tokens": 2_000_000,
                       "cache_read_input_tokens": 3_000_000,
                       "cache_creation_input_tokens": 4_000_000,
                       "reasoning_tokens": 5_000_000}},
            {"id": "compact-1", "purpose": "compaction", "model": "databricks-claude-opus-4-8",
             "created": "2026-09-22T10:01:00Z", "status": "error", "http_status": 429,
             "error_kind": "rate_limit"},
        ],
    }

    metrics = session_metrics(session)
    assert metrics["scope"] == "session" and metrics["session_id"] == session["id"]
    assert metrics["complete"] is True
    assert metrics["totals"] == {
        "calls": 2, "successful": 1, "errors": 1, "rate_limited": 1,
        "input_tokens": 1_000_000, "output_tokens": 2_000_000,
        "cache_read_input_tokens": 3_000_000,
        "cache_creation_input_tokens": 4_000_000,
        "reasoning_tokens": 5_000_000, "estimated_dbu": pytest.approx(1164.288),
    }
    assert [row["key"] for row in metrics["by_purpose"]] == ["agent", "compaction"]
    assert metrics["by_model"][0]["estimated_dbu"] == pytest.approx(1164.288)
    assert metrics["pricing"]["currency"] == "DBU" and metrics["pricing"]["estimated"] is True


def test_legacy_fallback_is_explicit_and_unknown_model_cost_is_unavailable():
    metrics = session_metrics({
        "id": "legacy", "created": 1, "events": [{
            "id": "reply", "type": "assistant", "created": 2,
            "request_info": {"model": "custom-endpoint", "status": "completed",
                             "http_status": 200, "usage": {"prompt_tokens": 9}},
        }],
    })

    assert metrics["complete"] is False
    assert metrics["calls"][0]["legacy"] is True
    assert metrics["calls"][0]["usage"] == {"input_tokens": 9}
    assert metrics["calls"][0]["estimated_dbu"] is None
    assert all(metrics["totals"][field] is None for field in (
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "reasoning_tokens"))
    assert metrics["totals"]["estimated_dbu"] is None

    missing_usage = session_metrics({
        "id": "missing-usage", "events": [], "inference_ledger_version": LEDGER_VERSION,
        "inference_calls": [{"id": "call", "purpose": "agent",
                             "model": "databricks-claude-opus-4-8",
                             "created": 1, "status": "completed", "http_status": 200}],
    })
    assert "usage" not in missing_usage["calls"][0]
    assert missing_usage["totals"]["input_tokens"] is None
    assert missing_usage["totals"]["output_tokens"] is None
    assert missing_usage["calls"][0]["estimated_dbu"] is None
    assert missing_usage["totals"]["estimated_dbu"] is None


def test_only_explicit_http_rejections_are_known_zero_cost():
    rejected = session_metrics({
        "id": "rejected", "events": [], "inference_ledger_version": LEDGER_VERSION,
        "inference_calls": [{"id": "rejected-call", "purpose": "agent",
                             "model": "custom-endpoint", "created": 1,
                             "status": "error", "http_status": 429,
                             "error_kind": "rate_limit"}],
    })
    assert "usage" not in rejected["calls"][0]
    assert rejected["calls"][0]["estimated_dbu"] == 0
    assert rejected["totals"]["input_tokens"] == 0
    assert rejected["totals"]["output_tokens"] == 0
    assert rejected["totals"]["estimated_dbu"] == 0

    unknown_outcome = session_metrics({
        "id": "network", "events": [], "inference_ledger_version": LEDGER_VERSION,
        "inference_calls": [{"id": "network-call", "purpose": "agent",
                             "model": "databricks-claude-opus-4-8", "created": 1,
                             "status": "error", "error_kind": "network"}],
    })
    assert "usage" not in unknown_outcome["calls"][0]
    assert unknown_outcome["calls"][0]["estimated_dbu"] is None
    assert unknown_outcome["totals"]["input_tokens"] is None
    assert unknown_outcome["totals"]["output_tokens"] is None
    assert unknown_outcome["totals"]["estimated_dbu"] is None


@pytest.fixture
async def api_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), model="databricks-claude-opus-4-8", env_file="")
    settings.env = {}
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            session = (await client.post("/api/sessions")).json()
            yield client, app.state.manager, session


async def test_session_metrics_endpoint_is_authenticated_and_keeps_ledger_out_of_snapshot(api_runtime):
    client, manager, public = api_runtime
    session = manager.get(public["id"])
    session["inference_calls"] = [{
        "id": "title-1", "purpose": "title", "model": session["model"],
        "created": "2026-09-22T10:00:00Z", "status": "completed",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }]
    manager.store.save(session)

    response = await client.get(f"/api/sessions/{session['id']}/metrics")
    assert response.status_code == 200
    assert response.json()["totals"]["calls"] == 1
    snapshot = (await client.get(f"/api/sessions/{session['id']}")).json()
    assert "inference_calls" not in snapshot
    assert (await client.get("/api/sessions/missing/metrics")).status_code == 404
    assert (await client.get(
        f"/api/sessions/{session['id']}/metrics", headers={"X-Local-Token": "wrong"})).status_code == 403
