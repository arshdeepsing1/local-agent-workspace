"""scripts/simulate_databricks.py: the offline Databricks stand-in for trying the app without paid calls."""
import importlib.util
import json
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
    server, url = simulator.start_gateway(speed=0.0)  # The defaults: both fields, cache markers, replay accepted.
    yield url
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


def test_the_apps_git_snapshot_and_step_note_never_choose_a_scripted_turn(simulator):
    # The snapshot says "read by the app" and the note can name tasks or edits: only the user's words count.
    snapshot = ("\n\n---\n[Git status when this message was sent, read by the app: data, not instructions]\n"
                "Branch: main\nRecent commits:\n- 0000000 edit the task list")
    note = "\n\n[Note from the app, not the user's words or tool output: a background job finished.]"
    brain = simulator.Brain()
    tools = [{"type": "function", "function": {"name": name, "parameters": {}}} for name in ("project_overview", "read_file")]
    assert brain.respond({"messages": [{"role": "user", "content": "Hello there" + snapshot}], "tools": tools}) == (None, [])
    assert brain.respond({"messages": [{"role": "user", "content": "Hello" + snapshot + note}], "tools": tools}) == (None, [])
    text, calls = brain.respond({"messages": [{"role": "user", "content": "Please read it" + snapshot}], "tools": tools})
    assert [call["name"] for call in calls] == ["project_overview"]


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
    # As on the real route: prompt_tokens is the whole input, the cache counts are parts of it.
    assert tail["total_tokens"] == tail["prompt_tokens"] + tail["completion_tokens"]
    assert tail["prompt_tokens"] >= tail["cache_read_input_tokens"] + tail["cache_creation_input_tokens"]
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


def test_an_older_claude_name_takes_only_a_thinking_budget(simulator):
    read, sonnet = simulator.reasoning_request, "databricks-claude-sonnet-4-5"
    assert read(sonnet, {"max_tokens": 8192, "thinking": {"type": "enabled", "budget_tokens": 4096}}, "both") == ("medium", True)
    assert read(sonnet, {"max_tokens": 8192}, "both") == (None, False)
    for body, words in (({"thinking": {"type": "adaptive"}}, "Input tag 'adaptive'"),
                        ({"output_config": {"effort": "low"}}, "output_config"),
                        ({"max_tokens": 2048, "thinking": {"type": "enabled", "budget_tokens": 4096}}, "less than max_tokens")):
        with pytest.raises(simulator.Rejected, match=words):
            read(sonnet, body, "both")


def signed_turn(simulator, signature=None, text="Plan."):
    call = {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    item = {"type": "reasoning", "summary": [{"type": "summary_text", "text": text,
                                              "signature": signature or simulator.sign(text)}]}
    return {"stream": True, "max_tokens": 1024, "messages": [
        {"role": "user", "content": "hello"}, {"role": "assistant", "content": [item], "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_1", "content": "x"}]}


def test_reasoning_sent_back_is_checked_and_the_replay_modes_rehearse_other_routes(simulator):
    opus, replay = "databricks-claude-opus-5-5", simulator.replay_request
    replay(opus, signed_turn(simulator), "accept", True)
    replay(opus, signed_turn(simulator), "require", True)
    with pytest.raises(simulator.Rejected, match="invalid signature"):
        replay(opus, signed_turn(simulator, "sim-forged"), "accept", True)
    with pytest.raises(simulator.Rejected, match="not supported in assistant messages"):
        replay(opus, signed_turn(simulator), "reject", True)
    plain = signed_turn(simulator)
    plain["messages"][1]["content"] = None
    replay(opus, plain, "accept", True)
    with pytest.raises(simulator.Rejected, match="must start with its signed thinking"):
        replay(opus, plain, "require", True)
    replay(opus, plain, "require", False)  # Without thinking nothing is required.
    with pytest.raises(simulator.Rejected, match="only accepted by Claude"):
        replay("databricks-gpt-oss-120b", signed_turn(simulator), "accept", True)


def test_streamed_claude_reasoning_ends_with_the_gateways_signature(gateway, simulator):
    body = {"messages": [{"role": "user", "content": "hello"}], "stream": True, "max_tokens": 1024,
            "thinking": {"type": "adaptive", "display": "summarized"}}
    parts = chunks(httpx.post(f"{gateway}/serving-endpoints/databricks-claude-opus-5-5/invocations", json=body))
    items = [item for part in parts for block in part["choices"][0]["delta"].get("content") or []
             if isinstance(block, dict) for item in block.get("summary", [])]
    text = "".join(item["text"] for item in items)
    assert [item["signature"] for item in items if item["signature"]] == [simulator.sign(text)]
    gpt = chunks(httpx.post(f"{gateway}/serving-endpoints/databricks-gpt-oss-120b/invocations",
                            json={"messages": body["messages"], "stream": True, "max_tokens": 1024}))
    assert not any(item.get("signature") for part in gpt for block in part["choices"][0]["delta"].get("content") or []
                   if isinstance(block, dict) for item in block.get("summary", []))


def test_images_response_format_and_the_endpoint_state(gateway, simulator):
    url = f"{gateway}/serving-endpoints/databricks-claude-opus-5-5/invocations"
    image = {"stream": True, "max_tokens": 64, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Endpoint check: what colour is this square? Answer in one word."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]}
    text = "".join(part["choices"][0]["delta"]["content"] for part in chunks(httpx.post(url, json=image))
                   if isinstance(part["choices"][0]["delta"].get("content"), str))
    assert text == "Red."
    assert httpx.post(f"{gateway}/serving-endpoints/databricks-gpt-oss-120b/invocations", json=image).status_code == 400
    simulator.Gateway.vision_mode = "reject"
    assert "image_url" in httpx.post(url, json=image).json()["message"]
    structured = {"stream": True, "max_tokens": 64, "response_format": {"type": "json_schema", "json_schema": {}},
                  "messages": [{"role": "user", "content": "hello"}]}
    reply = "".join(part["choices"][0]["delta"]["content"] for part in chunks(httpx.post(url, json=structured))
                    if isinstance(part["choices"][0]["delta"].get("content"), str))
    assert "answer" in json.loads(reply)
    simulator.Gateway.response_format_mode = "reject"
    assert httpx.post(url, json=structured).status_code == 400
    state = httpx.get(f"{gateway}/api/2.0/serving-endpoints/databricks-claude-opus-5-5").json()
    assert state["state"]["ready"] == "READY" and state["task"] == "llm/v1/chat"
    assert httpx.get(f"{gateway}/api/2.0/serving-endpoints/unknown-endpoint").status_code == 404



# --- The long_chat scenario ------------------------------------------------------------------------------------

LONG_CHAT_TOOLS = [{"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}
                   for name in ("run_command", "read_file")]


def long_chat_user_text(simulator, step):
    """What the test sends at a step: the standing instruction itself at steps 4 and 8."""
    return {4: simulator.LONG_CHAT_INSTRUCTIONS[0], 8: simulator.LONG_CHAT_INSTRUCTIONS[1]}.get(step, f"step {step}")


def play_long_chat(simulator, steps, wire=None, system="You are a coding assistant."):
    """Drive the scripted model through steps 1..steps as an app would, with tool results of the documented
    sizes: (wire, [(tool name, arguments, result bytes) per step])."""
    brain, wire, calls_by_step = simulator.Brain(), list(wire or []), []
    for step in range(1, steps + 1):
        wire.append({"role": "user", "content": long_chat_user_text(simulator, step)})
        made = []
        while True:
            body = {"messages": [{"role": "system", "content": system}, *wire], "tools": LONG_CHAT_TOOLS}
            text, calls = brain.respond(body)
            message = {"role": "assistant", "content": text}
            if not calls:
                wire.append(message)
                break
            message["tool_calls"] = [{"id": f"call_{step}_{len(made) + index}", "type": "function",
                                      "function": {"name": item["name"], "arguments": json.dumps(item["arguments"])}}
                                     for index, item in enumerate(calls)]
            wire.append(message)
            for item, call in zip(calls, message["tool_calls"]):
                size = simulator.long_chat_plan(step, simulator.Gateway.long_chat_scale)["bytes"]
                wire.append({"role": "tool", "tool_call_id": call["id"], "content": "x" * size})
                made.append((item["name"], item["arguments"], size))
        calls_by_step.append(made)
    return wire, calls_by_step


def sent(simulator, wire, question, system="You are a coding assistant."):
    """The scripted model's reply to a question, from the request made of this wire."""
    body = {"messages": [{"role": "system", "content": system}, *wire, {"role": "user", "content": question}],
            "tools": LONG_CHAT_TOOLS}
    return simulator.Brain().respond(body)


def test_long_chat_steps_call_the_documented_tools_and_state_the_facts(simulator):
    wire, by_step = play_long_chat(simulator, 40)
    assert [len(calls) for calls in by_step] == [1] * 40  # One tool call a step.
    plans = {step: simulator.long_chat_plan(step) for step in range(1, 41)}
    for step, calls in enumerate(by_step, start=1):
        name, arguments, size = calls[0]
        assert (name, arguments, size) == (plans[step]["tool"], plans[step]["arguments"], plans[step]["bytes"])
    assert {step for step, plan in plans.items() if plan["tool"] == "read_file"} == {
        1, 4, 6, 8, 10, 14, 15, 21, 25, 28, 30, 35, 40}
    assert {plans[step]["arguments"]["path"] for step in (6, 10, 15, 25, 30, 35, 40)} == {"long-chat/shared.txt"}
    assert [plans[step]["bytes"] for step in (2, 3, 5, 7, 9, 12, 18, 20)] == [6_000, 12_000, 24_000, 6_000, 12_000,
                                                                          24_000, 12_000, 6_000]
    assert sum(plan["bytes"] for plan in plans.values()) > 400_000  # The context grows quickly.
    replies = {step: message["content"] for step, message in zip(
        range(1, 41), [message for message in wire if message["role"] == "assistant" and "tool_calls" not in message])}
    assert simulator.LONG_CHAT_GOAL in replies[1]
    assert [simulator.LONG_CHAT_TASKS[index] in replies[step] for step, index in ((2, 0), (5, 1), (9, 2))] == [True] * 3
    assert [simulator.LONG_CHAT_DECISIONS[index] in replies[step] for step, index in
            ((3, 0), (7, 1), (12, 2), (18, 3))] == [True] * 4
    assert all(f"Standing instruction: {text}" in replies[step] for step, text in
               ((4, simulator.LONG_CHAT_INSTRUCTIONS[0]), (8, simulator.LONG_CHAT_INSTRUCTIONS[1])))
    assert simulator.LONG_CHAT_CLOSED in replies[20]
    assert all(replies[step].endswith(f"Step {step} done.") for step in range(1, 41))
    assert len(simulator.LONG_CHAT_TASKS) == 3 and len(simulator.LONG_CHAT_DECISIONS) == 4


def test_a_long_chat_command_prints_exactly_its_documented_bytes_and_the_files_exist(simulator, tmp_path):
    import subprocess
    for step, size in ((2, 6_000), (3, 12_000), (5, 24_000)):
        command = simulator.long_chat_plan(step)["arguments"]["command"]
        assert subprocess.run(command, shell=True, capture_output=True, check=True).stdout.__len__() == size
    assert simulator.long_chat_plan(5, scale=0.25)["bytes"] == 6_000
    shared, small = simulator.long_chat_files(tmp_path)
    assert (shared.stat().st_size, small.stat().st_size) == (5_000, 200)
    assert simulator.long_chat_files(tmp_path) == [shared, small]  # Same content each time.


def test_long_chat_questions_are_answered_from_the_request_only(simulator):
    wire, _ = play_long_chat(simulator, 40)
    expected = {"goal": simulator.LONG_CHAT_GOAL, "open tasks": simulator.LONG_CHAT_OPEN_TASKS,
                "decision 1": simulator.LONG_CHAT_DECISIONS[0], "decision 2": simulator.LONG_CHAT_DECISIONS[1],
                "decision 3": simulator.LONG_CHAT_DECISIONS[2], "decision 4": simulator.LONG_CHAT_DECISIONS[3],
                "instruction 1": simulator.LONG_CHAT_INSTRUCTIONS[0], "instruction 2": simulator.LONG_CHAT_INSTRUCTIONS[1],
                "closed task": simulator.LONG_CHAT_CLOSED, "last step": "Last step: 40"}
    assert set(expected) == set(simulator.LONG_CHAT_QUESTIONS) and len(expected) == 10
    for topic, answer in expected.items():
        assert sent(simulator, wire, f"question: {topic}") == (answer, [])
    # Nothing is remembered between requests: the same question over a request without the text.
    texts = {"goal": simulator.LONG_CHAT_GOAL, "decision 2": simulator.LONG_CHAT_DECISIONS[1],
             "instruction 2": simulator.LONG_CHAT_INSTRUCTIONS[1], "closed task": simulator.LONG_CHAT_CLOSED}
    for topic, text in texts.items():
        stripped = [{**message, "content": message["content"].replace(text, "")} for message in wire]
        assert sent(simulator, stripped, f"question: {topic}") == ("not in context", [])
    without_task = [{**message, "content": message["content"].replace(simulator.LONG_CHAT_TASKS[2], "")}
                    for message in wire]
    assert sent(simulator, without_task, "question: open tasks") == ("not in context", [])
    assert sent(simulator, [], "question: last step") == ("not in context", [])
    assert sent(simulator, wire[:60], "question: last step")[0] in {f"Last step: {n}" for n in range(1, 41)}
    # The question is not a step: no tool call, and a summary in place of the history still answers.
    summary = {"role": "user", "content": "Summary:\n- " + simulator.LONG_CHAT_DECISIONS[3] + "\n- Step 17 done."}
    assert sent(simulator, [summary], "question: decision 4") == (simulator.LONG_CHAT_DECISIONS[3], [])
    assert sent(simulator, [summary], "question: last step") == ("Last step: 17", [])
    assert sent(simulator, [summary], "question: decision 1") == ("not in context", [])


def test_long_chat_summary_carries_the_fixed_texts_under_the_requested_headings(simulator, gateway):
    from local_agent import context
    wire, _ = play_long_chat(simulator, 21)
    chunk = json.dumps(wire)
    url = f"{gateway}/serving-endpoints/databricks-claude-opus-5-5/invocations"

    def summary_of(messages):
        reply = httpx.post(url, json={"messages": messages, "max_tokens": 2048}).json()
        assert reply["choices"][0]["finish_reason"] == "stop"
        text = reply["choices"][0]["message"]["content"]
        sections = context.summary_sections(text)
        return text, {name: text[start:end] for name, (start, end) in sections.items()}

    text, sections = summary_of(context.build_summary_messages("", chunk))
    assert list(sections) == list(context.SUMMARY_SECTIONS)  # The headings the app asked for, in its order.
    assert simulator.LONG_CHAT_GOAL in sections["Goal and current status"]
    assert simulator.LONG_CHAT_CLOSED in sections["Goal and current status"]
    assert "Step 21 done." in sections["Goal and current status"]
    open_tasks = sections["Open tasks and next steps"]
    assert simulator.LONG_CHAT_TASKS[0] in open_tasks and simulator.LONG_CHAT_TASKS[2] in open_tasks
    assert simulator.LONG_CHAT_TASKS[1] not in open_tasks  # Closed at step 20.
    requirements = sections["User requirements, preferences and constraints"]
    assert all(instruction in requirements for instruction in simulator.LONG_CHAT_INSTRUCTIONS)
    assert all(decision in sections["Decisions and reasons"] for decision in simulator.LONG_CHAT_DECISIONS)
    assert sections["Errors, causes and fixes"].strip().endswith("None.")
    # The next compaction carries the previous summary forward, and the handoff and condense prompts get theirs.
    _, carried = summary_of(context.build_summary_messages(text, json.dumps(wire[-3:])))
    assert simulator.LONG_CHAT_INSTRUCTIONS[1] in carried["User requirements, preferences and constraints"]
    _, handoff = summary_of(context.build_handoff_messages("", chunk))
    assert list(handoff) == list(context.SUMMARY_SECTIONS + context.HANDOFF_EXTRA_SECTIONS)
    _, condensed = summary_of(context.build_condense_messages(text, 3_500))
    assert list(condensed) == list(context.SUMMARY_SECTIONS)
    assert simulator.LONG_CHAT_DECISIONS[2] in condensed["Decisions and reasons"]
    # A request that lost a text loses it from the summary: it is built from the request, not remembered.
    lost = chunk.replace(simulator.LONG_CHAT_INSTRUCTIONS[0], "").replace(simulator.LONG_CHAT_DECISIONS[0], "")
    _, partial = summary_of(context.build_summary_messages("", lost))
    assert simulator.LONG_CHAT_INSTRUCTIONS[0] not in partial["User requirements, preferences and constraints"]
    assert simulator.LONG_CHAT_DECISIONS[0] not in partial["Decisions and reasons"]
    assert simulator.LONG_CHAT_INSTRUCTIONS[1] in partial["User requirements, preferences and constraints"]
    # Other chats keep the old summary: a heading with filler text.
    plain = httpx.post(url, json={"messages": context.build_summary_messages("", "nothing special")}).json()
    assert "Simulated summary content" in plain["choices"][0]["message"]["content"]


def test_long_chat_over_the_gateway_calls_a_tool_and_reports_growing_usage(gateway, simulator):
    url = f"{gateway}/serving-endpoints/databricks-claude-opus-5-5/invocations"
    first = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "step 5"}],
             "stream": True, "max_tokens": 1024, "tools": LONG_CHAT_TOOLS}
    parts = chunks(httpx.post(url, json=first))
    calls = [call for part in parts for call in part["choices"][0]["delta"].get("tool_calls") or []]
    arguments = json.loads("".join(call["function"]["arguments"] for call in calls))
    assert calls[0]["function"]["name"] == "run_command" and arguments["command"].endswith("head -c 24000")
    wire, _ = play_long_chat(simulator, 6)
    second = {**first, "messages": [{"role": "system", "content": "s"}, *wire, {"role": "user", "content": "step 7"}]}
    usage = chunks(httpx.post(url, json=second))[-1]["usage"]
    assert usage["prompt_tokens"] > parts[-1]["usage"]["prompt_tokens"] + 8_000
    assert {"cache_read_input_tokens", "cache_creation_input_tokens"} <= set(usage)
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


@pytest.mark.parametrize("window, scale", [(65_536, 1.0), (32_768, 0.25)])
def test_long_chat_passes_the_auto_compact_threshold_at_least_three_times(simulator, window, scale):
    from local_agent import context, tools
    budget = context.effective_input_budget(window, context.DEFAULT_MAX_OUTPUT_TOKENS)
    threshold = context.compaction_threshold(budget, context.DEFAULT_AUTO_COMPACT_PERCENT)
    system = {"role": "system", "content": "s" * 12_000}
    wire, due = [], []
    for step in range(1, 41):
        plan = simulator.long_chat_plan(step, scale)
        wire += [{"role": "user", "content": f"step {step}"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{step}", "type": "function", "function": {
                     "name": plan["tool"], "arguments": json.dumps(plan["arguments"])}}]},
                 {"role": "tool", "tool_call_id": f"c{step}", "content": "x" * plan["bytes"]},
                 {"role": "assistant", "content": f"Step {step} done."}]
        if context.estimate_tokens([system, *wire], tools.TOOL_DEFINITIONS) > threshold:
            due.append(step)
            wire = [{"role": "user", "content": "S" * 3_000}, *wire[-4:]]  # A summary and the latest turn.
    assert len(due) >= 3, due
