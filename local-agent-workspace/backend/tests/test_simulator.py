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
    simulator.Gateway.speed, simulator.Gateway.claude_fields, simulator.Gateway.cache_mode = 0.0, "both", "accept"
    simulator.Gateway.prompt_cache = simulator.PromptCache()
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
    assert reasoning.endswith("\n\n")  # Every Claude summary ends with a blank line.
    # Claude's usage shape on the real route: top-level cache counts, no details objects or reasoning tokens.
    usage = parts[-1]["usage"]
    assert set(usage) == {"prompt_tokens", "completion_tokens", "total_tokens", "cache_read_input_tokens",
                          "cache_creation_input_tokens"}
    assert usage["completion_tokens"] > 1_200 and usage["cache_read_input_tokens"] == usage["cache_creation_input_tokens"] == 0
    # GPT OSS keeps the OpenAI-style shape.
    gpt = chunks(httpx.post(f"{gateway}/serving-endpoints/databricks-gpt-oss-120b/invocations",
                            json={**{key: body[key] for key in ("messages", "stream")}, "reasoning_effort": "high"}))
    assert gpt[-1]["usage"]["completion_tokens_details"]["reasoning_tokens"] == 1_200
    assert "prompt_tokens_details" in gpt[-1]["usage"] and "cache_read_input_tokens" not in gpt[-1]["usage"]


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


def test_a_custom_delegation_uses_the_first_agent_of_the_agent_index(gateway):
    url = f"{gateway}/serving-endpoints/databricks-claude-opus-5-5/invocations"
    tools = [{"type": "function", "function": {"name": "delegate_task", "parameters": {
        "type": "object", "properties": {"task": {"type": "string"}, "agent_type": {"type": "string"}}}}}]
    system = ("You are Local.\nCustom subagents: set delegate_task agent_type to one of these names when its description "
              "fits (each brings its own instructions and tools, within this chat's limits):\n"
              "- security-auditor: Reviews code.\n- mapper: Maps modules.")

    def answer(system_text, prompt="delegate custom"):
        messages = [{"role": "system", "content": system_text}, {"role": "user", "content": prompt}]
        parts = chunks(httpx.post(url, json={"messages": messages, "tools": tools, "stream": True}))
        arguments = "".join(call.get("function", {}).get("arguments", "") for part in parts
                            for call in part["choices"][0]["delta"].get("tool_calls") or [])
        pieces = [part["choices"][0]["delta"].get("content") for part in parts]
        return (json.loads(arguments) if arguments else None), "".join(piece for piece in pieces if isinstance(piece, str))
    assert answer(system)[0]["agent_type"] == "security-auditor"
    assert answer(system, "delegate a survey")[0]["agent_type"] == "explore"
    called, content = answer("You are Local.")
    assert called is None and "no custom agents" in content


MARK = {"cache_control": {"type": "ephemeral"}}


def cached_request(system="You are Local. " * 200, prompt="hello", *, split=True):
    stable = [{"type": "text", "text": system, **MARK}, {"type": "text", "text": "\nDate: today"}]
    return {"messages": [{"role": "system", "content": stable if split else system + "\nDate: today"},
                         {"role": "user", "content": [{"type": "text", "text": prompt, **MARK}]}],
            "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
            "stream": True}


def usage_of(gateway, body, endpoint="databricks-claude-opus-5-5"):
    response = httpx.post(f"{gateway}/serving-endpoints/{endpoint}/invocations", json=body)
    assert response.status_code == 200, response.text
    return chunks(response)[-1]["usage"]


def test_a_second_request_reads_the_first_ones_marked_prefix(gateway):
    first = usage_of(gateway, cached_request())
    assert first["cache_read_input_tokens"] == 0 and first["cache_creation_input_tokens"] > 500
    again = usage_of(gateway, cached_request())
    assert again["cache_read_input_tokens"] == first["cache_creation_input_tokens"]
    assert again["cache_creation_input_tokens"] == 0
    # The next step: the earlier messages follow unmarked, a new marked one is written on top of what was read.
    body = cached_request()
    body["messages"][1]["content"] = "hello"
    body["messages"] += [{"role": "assistant", "content": "Hi."}, {"role": "user", "content": [
        {"type": "text", "text": "and more", **MARK}]}]
    step = usage_of(gateway, body)
    assert step["cache_read_input_tokens"] == first["cache_creation_input_tokens"] and step["cache_creation_input_tokens"] > 0
    # A changed system tail still reads the system part; uncached input is what is left.
    changed = cached_request(prompt="other")
    changed["messages"][0]["content"][1]["text"] = "\nDate: tomorrow"
    tail = usage_of(gateway, changed)
    assert 0 < tail["cache_read_input_tokens"] < first["cache_creation_input_tokens"]
    assert tail["total_tokens"] == (tail["prompt_tokens"] + tail["cache_read_input_tokens"]
                                    + tail["cache_creation_input_tokens"] + tail["completion_tokens"])
    # Another endpoint has its own cache, and an unmarked request uses none.
    assert usage_of(gateway, cached_request(), "databricks-claude-opus-4-8")["cache_read_input_tokens"] == 0
    plain = usage_of(gateway, {"messages": [{"role": "user", "content": "hello"}], "stream": True})
    assert plain["cache_read_input_tokens"] == plain["cache_creation_input_tokens"] == 0


def test_claude_input_counts_are_a_little_above_the_app_estimate(simulator):
    # As on the real route, so a rehearsed cache accounting check ((uncached + reads + writes) / estimate) is near 1.
    from local_agent.context import estimate_tokens
    from local_agent.prompt_cache import unmarked
    rules = "Use the tools to read and change files in the project; say what you changed. " * 120
    source = "def greet(name):\n    return f'Hello, {name}!'\n\n" * 300
    body = {"messages": [
        {"role": "system", "content": [{"type": "text", "text": rules, **MARK}, {"type": "text", "text": "\nDate: today"}]},
        {"role": "user", "content": "Read the app"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": "src/app.py"})}}]},
        {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": source, **MARK}]}],
        "tools": [{"type": "function", "function": {"name": "read_file", "description": "Read a file. " * 40,
                                                    "parameters": {"type": "object"}}}], "stream": True}
    read, written, total = simulator.PromptCache().use("e", body)
    plain = unmarked(body)
    assert (read, written) == (0, total)  # Written up to the last marker: here the whole request.
    assert 1.0 <= total / estimate_tokens(plain["messages"], plain["tools"]) <= 1.2


def test_cache_entries_expire_after_five_minutes_unless_read(simulator):
    cache = simulator.PromptCache()
    body = cached_request()
    _, written, _ = cache.use("e", body, now=1000)
    assert cache.use("e", body, now=1000 + 299)[0] == written  # Read: refreshed for another 5 minutes.
    assert cache.use("e", body, now=1000 + 598)[0] == written
    assert cache.use("e", body, now=1000 + 598 + 300)[0] == 0


def test_cache_markers_are_checked_and_the_modes_rehearse_a_route_that_refuses_them(gateway, simulator):
    url = f"{gateway}/serving-endpoints/databricks-claude-opus-5-5/invocations"
    gpt = httpx.post(f"{gateway}/serving-endpoints/databricks-gpt-oss-120b/invocations", json=cached_request())
    assert gpt.status_code == 400 and "only accepted by Databricks-hosted Claude models" in gpt.json()["message"]
    five = cached_request()
    five["messages"][1]["content"] = [{"type": "text", "text": str(index), **MARK} for index in range(4)]
    assert httpx.post(url, json=five).status_code == 400
    ttl = cached_request()
    ttl["messages"][1]["content"][0]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    assert "only" in httpx.post(url, json=ttl).json()["message"]
    tool_call = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}, **MARK}]}
    documented = {"messages": [{"role": "system", "content": "Rules."}, {"role": "user", "content": "read"}, tool_call,
                               {"role": "tool", "tool_call_id": "c1", "content": "text"}], "stream": True}
    simulator.Gateway.cache_mode = "documented"
    assert httpx.post(url, json=documented).status_code == 200  # A marked tool call is a documented form.
    refused = httpx.post(url, json=cached_request())
    assert refused.status_code == 400 and "messages[0].content" in refused.json()["message"]
    assert httpx.post(url, json=cached_request(split=False)).status_code == 200  # A marked user message is too.
    simulator.Gateway.cache_mode = "reject"
    refused = httpx.post(url, json=cached_request(split=False))
    assert refused.status_code == 400
    assert refused.json()["message"] == "Unrecognized request argument supplied: cache_control"
    assert httpx.post(url, json={"messages": [{"role": "user", "content": "hello"}], "stream": True}).status_code == 200
    simulator.Gateway.cache_mode = "ignore"
    for _ in range(2):
        usage = usage_of(gateway, cached_request())
        assert usage["cache_read_input_tokens"] == usage["cache_creation_input_tokens"] == 0
