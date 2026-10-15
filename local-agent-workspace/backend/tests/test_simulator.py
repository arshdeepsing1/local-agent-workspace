"""scripts/simulate_databricks.py: the offline Databricks stand-in for trying the app without paid calls."""
import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "simulate_databricks.py"


@pytest.fixture(scope="module")
def simulator():
    spec = importlib.util.spec_from_file_location("simulate_databricks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gateway(simulator):
    simulator.Gateway.speed, simulator.Gateway.claude_fields = 0.0, "both"
    server = ThreadingHTTPServer(("127.0.0.1", 0), simulator.Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def chunks(response):
    return [json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


def test_reasoning_fields_follow_the_documented_endpoint_rules(simulator):
    read = simulator.reasoning_request
    opus = "databricks-claude-opus-5-5"
    assert read(opus, {}, "both") == ("medium", False)  # Always thinks; summary omitted by default.
    assert read(opus, {"thinking": {"type": "adaptive", "display": "summarized"},
                       "output_config": {"effort": "xhigh"}}, "both") == ("xhigh", True)
    assert read(opus, {"reasoning_effort": "low"}, "both") == ("low", False)
    for body in ({"thinking": {"type": "enabled", "budget_tokens": 2048}}, {"thinking": {"type": "disabled"}},
                 {"output_config": {"effort": "extreme"}}):
        with pytest.raises(simulator.Rejected):
            read(opus, body, "both")
    assert read("databricks-claude-opus-4-8", {}, "both") == (None, False)  # No thinking unless adaptive.
    assert read("databricks-claude-opus-4-8", {"thinking": {"type": "adaptive"}}, "both") == ("high", False)
    assert read("databricks-gpt-oss-120b", {"reasoning_effort": "max"}, "both") == ("high", True)
    with pytest.raises(simulator.Rejected, match="output_config"):
        read("databricks-gpt-oss-120b", {"output_config": {"effort": "low"}}, "both")
    assert read("databricks-gpt-oss-20b", {"reasoning_effort": "low"}, "anthropic") == ("low", True)
    # Other names follow the family the app's Automatic format guesses; plain models refuse the fields.
    assert read("my-claude-endpoint", {}, "both") == (None, False)
    assert read("my-claude-endpoint", {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}, "both") == ("low", False)
    assert read("team-gpt-5-mini", {"reasoning_effort": "high"}, "both") == ("high", True)
    assert read("llama-endpoint", {}, "both") == (None, False)
    with pytest.raises(simulator.Rejected, match="reasoning_effort"):
        read("llama-endpoint", {"reasoning_effort": "low"}, "both")
    # The route modes rehearse a chat-completions route that refuses one style of field.
    with pytest.raises(simulator.Rejected, match="reasoning_effort"):
        read(opus, {"reasoning_effort": "low"}, "anthropic")
    with pytest.raises(simulator.Rejected, match="thinking"):
        read(opus, {"thinking": {"type": "adaptive"}}, "openai")


def test_stream_carries_a_reasoning_summary_text_and_usage(gateway):
    body = {"messages": [{"role": "user", "content": "hello"}], "stream": True, "max_tokens": 1024,
            "thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": "high"}}
    response = httpx.post(f"{gateway}/serving-endpoints/databricks-claude-opus-5-5/invocations", json=body)
    assert response.status_code == 200
    parts = chunks(response)
    reasoning = "".join(item["text"] for part in parts for block in part["choices"][0]["delta"].get("content") or []
                        if isinstance(block, dict) for item in block.get("summary", []))
    text = "".join(part["choices"][0]["delta"]["content"] for part in parts
                   if isinstance(part["choices"][0]["delta"].get("content"), str))
    assert "effort **high**" in reasoning
    assert "output_config" in text and "summary returned" in text
    assert parts[-1]["choices"][0]["finish_reason"] == "stop"
    assert parts[-1]["usage"]["completion_tokens_details"]["reasoning_tokens"] == 1_200


def test_scripted_edit_turn_calls_tools_in_order(gateway):
    url = f"{gateway}/serving-endpoints/databricks-claude-opus-5-5/invocations"
    tools = [{"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}
             for name in ("write_file", "edit_file")]
    messages = [{"role": "user", "content": "edit demo"}]
    names = []
    for _ in range(4):
        parts = chunks(httpx.post(url, json={"messages": messages, "tools": tools, "stream": True}))
        calls = {}
        for part in parts:
            for call in part["choices"][0]["delta"].get("tool_calls") or []:
                entry = calls.setdefault(call["index"], {"id": "", "name": "", "arguments": ""})
                entry["id"] = call.get("id") or entry["id"]
                entry["name"] += call.get("function", {}).get("name", "")
                entry["arguments"] += call.get("function", {}).get("arguments", "")
        if not calls:
            break
        names += [call["name"] for call in calls.values()]
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": call["arguments"]}}
            for call in calls.values()]})
        messages += [{"role": "tool", "tool_call_id": call["id"], "content": "ok"} for call in calls.values()]
    assert names == ["write_file", "edit_file", "edit_file"]


def test_rejections_and_endpoint_listing(gateway, simulator):
    listed = httpx.get(f"{gateway}/api/2.0/serving-endpoints").json()
    assert [item["name"] for item in listed["endpoints"]] == list(simulator.ENDPOINTS)
    refused = httpx.post(f"{gateway}/serving-endpoints/databricks-claude-opus-5-5/invocations",
                         json={"messages": [], "stream": True, "thinking": {"type": "disabled"}})
    assert refused.status_code == 400 and "cannot be disabled" in refused.json()["message"]
    limited = {"messages": [{"role": "user", "content": "please rate limit me"}], "stream": True}
    first = httpx.post(f"{gateway}/serving-endpoints/databricks-claude-opus-5-5/invocations", json=limited)
    assert first.status_code == 429 and first.headers["Retry-After"] == "2"
    second = httpx.post(f"{gateway}/serving-endpoints/databricks-claude-opus-5-5/invocations", json=limited)
    assert second.status_code == 200
