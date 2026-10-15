"""Run the app offline against a simulated Databricks workspace: no network, no paid calls.

    .venv/bin/python scripts/simulate_databricks.py            # app on http://127.0.0.1:8766
    .venv/bin/python scripts/simulate_databricks.py --workspace ~/code/demo --speed 0.5

A fake gateway on 127.0.0.1 answers the same routes the app calls on Databricks
(/api/2.0/serving-endpoints and /serving-endpoints/<name>/invocations) with OpenAI-style
chat-completion chunks, and the app is started with its credentials pointed at it. The
gateway checks reasoning fields the way the endpoints document them and prints what each
request asked for, so effort levels, the Thinking display and the request-format fallback
can be rehearsed before using a paid workspace:

- databricks-claude-opus-5-5: thinking is always on; effort low|medium|high|xhigh|max
  (default medium) in output_config.effort; thinking.display "summarized" returns a
  summary, anything else returns empty reasoning text. budget_tokens or a disabled
  thinking type is refused with HTTP 400.
- databricks-claude-opus-4-8: no thinking unless thinking.type is "adaptive"; same levels.
- databricks-gpt-oss-120b and -20b: reasoning_effort low|medium|high (xhigh and max count as high).
- Any other endpoint name (Settings → Other endpoint…) follows the family the app guesses from
  the name (claude: thinking only when adaptive; gpt-oss, gpt-5, o1/o3/o4: reasoning_effort);
  other names are plain models that refuse reasoning fields.

--claude-fields chooses which reasoning fields the simulated chat-completions route accepts
for Claude endpoints (the real route is not verified yet): both (default), anthropic
(refuses reasoning_effort), openai (refuses thinking and output_config) or none.

Scripted turns, chosen by words in your message (the agent only calls tools the chat offers):
  edit / keep      writes a file, then edits it twice with pauses (try Keep in between)
  job / background starts a background command and waits for it with wait_job
  delegate         delegates a read-only survey to a subagent; "parallel" delegates two;
                   "custom" uses the first custom agent of the system message's agent index
  ask              asks you a question with ask_user
  task             creates and completes tasks
  read             project_overview and read_file
  rate limit       the first request is answered with HTTP 429 and Retry-After: 2
  long             a long Markdown reply
anything else gets a short reply that says which effort and fields the request carried.

Conversations and settings live in a separate state folder (default .local/simulator), so
simulated chats never mix with real ones. Your credential file is never read.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
LEVELS = ("low", "medium", "high", "xhigh", "max")
# Simulated thinking per effort: seconds of streamed reasoning (before --speed) and reported tokens.
THINKING = {"low": (0.6, 120), "medium": (1.5, 400), "high": (3.0, 1_200), "xhigh": (5.0, 2_500), "max": (8.0, 5_000)}
ENDPOINTS = {
    "databricks-claude-opus-5-5": {"family": "claude", "always_thinks": True, "default": "medium"},
    "databricks-claude-opus-4-8": {"family": "claude", "always_thinks": False, "default": "high"},
    "databricks-gpt-oss-120b": {"family": "openai", "always_thinks": True, "default": "medium"},
    "databricks-gpt-oss-20b": {"family": "openai", "always_thinks": True, "default": "medium"},
}


def endpoint_profile(endpoint):
    """A listed endpoint's rules; for another name (Settings → Other endpoint…) the family the app's
    Automatic format guesses from the name, and a plain model for anything else."""
    if endpoint in ENDPOINTS:
        return ENDPOINTS[endpoint]
    name = endpoint.lower()
    if "claude" in name:
        return {"family": "claude", "always_thinks": False, "default": "high"}
    if "gpt-oss" in name or re.search(r"(?:^|[-_.])(?:gpt-5|o[134])(?:[-_.]|$)", name):
        return {"family": "openai", "always_thinks": True, "default": "medium"}
    return {"family": "plain", "always_thinks": False, "default": None}
SCENARIO_HINT = ("Try a message with one of these words: edit, job, delegate, parallel, ask, task, read, "
                 "rate limit, long.")


class Rejected(Exception):
    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


def log(text):
    print(f"[simulator] {text}", flush=True)


def reasoning_request(endpoint, body, claude_fields):
    """(effort or None, summarized) as the simulated endpoint reads the request; Rejected for invalid fields."""
    profile = endpoint_profile(endpoint)
    thinking, output_config, effort = body.get("thinking"), body.get("output_config"), body.get("reasoning_effort")
    if profile["family"] == "plain":
        present = [key for key in ("thinking", "output_config", "reasoning_effort") if key in body]
        if present:
            raise Rejected(400, f"Unrecognized request argument supplied: {present[0]}")
        return None, False
    if profile["family"] == "openai":
        if thinking is not None or output_config is not None:
            field = "thinking" if thinking is not None else "output_config"
            raise Rejected(400, f"Unrecognized request argument supplied: {field}")
        if effort is None:
            return profile["default"], True
        if effort in ("minimal", "low", "medium", "high", "xhigh", "max"):
            return {"minimal": "low", "xhigh": "high", "max": "high"}.get(effort, effort), True
        raise Rejected(400, f"Invalid value for reasoning_effort: {effort!r}. Supported: low, medium, high.")
    if effort is not None and claude_fields in ("anthropic", "none"):
        raise Rejected(400, "Unrecognized request argument supplied: reasoning_effort")
    if (thinking is not None or output_config is not None) and claude_fields in ("openai", "none"):
        field = "thinking" if thinking is not None else "output_config"
        raise Rejected(400, f"Unrecognized request argument supplied: {field}")
    if thinking is not None:
        if not isinstance(thinking, dict):
            raise Rejected(400, "thinking must be an object.")
        if "budget_tokens" in thinking or thinking.get("type") == "enabled":
            raise Rejected(400, "thinking.type 'enabled' with budget_tokens is not supported for this model. "
                                "Use thinking.type 'adaptive' and output_config.effort.")
        if thinking.get("type") == "disabled" and profile["always_thinks"]:
            raise Rejected(400, "thinking cannot be disabled for this model. Lower output_config.effort instead.")
        if thinking.get("type") not in ("adaptive", "disabled"):
            raise Rejected(400, f"Invalid thinking.type: {thinking.get('type')!r}")
    if output_config is not None:
        if not isinstance(output_config, dict) or set(output_config) - {"effort"}:
            raise Rejected(400, "output_config accepts only effort here.")
        if output_config.get("effort") not in LEVELS:
            raise Rejected(400, f"Invalid output_config.effort: {output_config.get('effort')!r}. "
                                "Supported: low, medium, high, xhigh, max.")
    level = (output_config or {}).get("effort") or effort
    if level in ("minimal",):
        level = "low"
    if level is not None and level not in LEVELS:
        raise Rejected(400, f"Invalid effort: {level!r}")
    thinking_on = profile["always_thinks"] or (isinstance(thinking, dict) and thinking.get("type") == "adaptive")
    if isinstance(thinking, dict) and thinking.get("type") == "disabled":
        thinking_on = False
    if not thinking_on:
        return None, False
    return level or profile["default"], isinstance(thinking, dict) and thinking.get("display") == "summarized"


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def tool_calls_before(messages):
    """Tool calls the simulated model made since the last user message, in order."""
    calls = []
    for message in messages:
        if message.get("role") == "user":
            calls = []
        elif message.get("role") == "assistant":
            calls.extend(message.get("tool_calls") or [])
    return calls


def tool_results(messages):
    """{tool_call_id: content} of tool results since the last user message."""
    results = {}
    for message in messages:
        if message.get("role") == "user":
            results = {}
        elif message.get("role") == "tool":
            results[message.get("tool_call_id")] = text_of(message.get("content"))
    return results


def call(name, **arguments):
    return {"name": name, "arguments": arguments}


class Brain:
    """Decides the next simulated response from the request alone (no hidden state but rate-limit marks)."""

    def __init__(self):
        self.limited = set()
        self.lock = threading.Lock()

    def respond(self, body):
        """(text, [calls]) for an agent request."""
        messages = body.get("messages") or []
        tools = {item.get("function", {}).get("name"): item.get("function", {}).get("parameters", {})
                 for item in body.get("tools") or [] if isinstance(item, dict)}
        users = [message for message in messages if message.get("role") == "user"]
        first = text_of(users[0].get("content")) if users else ""
        prompt = text_of(users[-1].get("content")) if users else ""
        made = tool_calls_before(messages)
        results = tool_results(messages)
        words = prompt.lower()
        system = text_of(messages[0].get("content")) if messages and messages[0].get("role") == "system" else ""
        if first.startswith("Delegated task:") and len(users) == 1:
            return self.subagent(prompt, made, results, tools)
        if "rate limit" in words or "429" in words:
            key = hashlib.sha256(prompt.encode()).hexdigest()
            with self.lock:
                if key not in self.limited:
                    self.limited.add(key)
                    raise Rejected(429, json.dumps({"error_code": "REQUEST_LIMIT_EXCEEDED",
                                                    "message": "Simulated rate limit: output tokens per minute.",
                                                    "limit_type": "output_tokens_per_minute"}), retry_after=2)
            return "The simulated rate limit was waited out and this request went through.", []
        for name, scenario in (("edit", self.edit), ("keep", self.edit), ("background", self.job), ("job", self.job),
                               ("parallel", self.delegate), ("delegate", self.delegate), ("subagent", self.delegate),
                               ("ask", self.ask), ("task", self.tasks), ("read", self.read)):
            if re.search(rf"\b{name}\b", words):
                return scenario(words, made, results, tools, **({"system": system} if scenario == self.delegate else {}))
        if "long" in words:
            return self.long(), []
        return None, []

    def edit(self, words, made, results, tools):
        if not {"write_file", "edit_file"} <= tools.keys():
            return "write_file and edit_file are turned off in this chat, so the edit scenario cannot run.", []
        step = len(made)
        path = (json.loads(made[0]["function"]["arguments"])["path"] if made
                else f"sim-notes/edit-demo-{time.strftime('%H%M%S')}.txt")
        if step == 0:
            return (f"I'll write `{path}` and then change it twice, pausing in between so you can press Keep.",
                    [call("write_file", path=path, content="Simulated edit demo\nstatus: step 1\n")])
        if step in (1, 2):
            time.sleep(4 * Gateway.speed)  # A pause in which Keep can be pressed while the turn still runs.
            return (f"Changing `{path}` again (step {step + 1}).",
                    [call("edit_file", path=path, old_text=f"status: step {step}", new_text=f"status: step {step + 1}")])
        return f"Done: `{path}` went through three versions. Review it in the changes bar.", []

    def job(self, words, made, results, tools):
        if not {"run_command", "wait_job"} <= tools.keys():
            return "run_command or wait_job is turned off in this chat.", []
        if not made:
            command = "for i in 1 2 3; do echo tick $i; sleep 1; done; echo build finished"
            return "Starting a short background job, then waiting only as long as it needs.", [
                call("run_command", command=command, background=True, timeout_seconds=60)]
        if len(made) == 1:
            output = results.get(made[0].get("id"), "")
            match = re.search(r'"(?:job_id|id)"\s*:\s*"([^"]+)"', output)
            if not match:
                return "The job did not start: " + output[:300], []
            arguments = {"job_id": match.group(1), "timeout_seconds": 15}
            if "until_regex" in tools.get("wait_job", {}).get("properties", {}):
                arguments["until_regex"] = "build finished"
            return "Waiting for the job to print `build finished` (it needs about 3 seconds).", [
                call("wait_job", **arguments)]
        output = results.get(made[-1].get("id"), "")
        state = re.search(r'"(?:state|reason)"\s*:\s*"([^"]+)"', output)
        return f"The background job finished ({state.group(1) if state else 'see the card'}).", []

    def delegate(self, words, made, results, tools, system=""):
        if "delegate_task" not in tools:
            return "delegate_task is not available in this chat (subagents cannot delegate further).", []
        if made:
            return "Subagent reports received:\n\n" + "\n\n".join(
                f"- {results.get(item.get('id'), '')[:400]}" for item in made), []
        properties = tools["delegate_task"].get("properties", {})
        extra = {"agent_type": "explore"} if "agent_type" in properties else (
            {"tool_profile": "read_only"} if "tool_profile" in properties else {})
        # "custom": the first agent the app's agent index lists ("- <name>: <description>" lines).
        index = system.split("Custom subagents:", 1)[1] if "Custom subagents:" in system else ""
        custom = re.findall(r"^- ([a-z][a-z0-9-]{0,39}): ", index, re.MULTILINE)
        if re.search(r"\bcustom\b", words) and "agent_type" in properties:
            if not custom:
                return ("This project has no custom agents: add one in .agents/agents/<name>.md "
                        "(see Custom agents in the README).", [])
            extra = {"agent_type": custom[0]}
        tasks = ["Survey the project: list its top-level folders and name the three most important files.",
                 "Find the README and summarize what the project does in three bullet points."]
        if "parallel" not in words:
            tasks = tasks[:1]
        return ("Delegating " + ("two independent surveys in parallel." if len(tasks) > 1 else "a read-only survey."),
                [call("delegate_task", task=task, context="Simulated run: be brief.", max_steps=4, **extra)
                 for task in tasks])

    def subagent(self, prompt, made, results, tools):
        if not made and "project_overview" in tools:
            return "Looking at the project layout.", [call("project_overview", path=".")]
        if not made and "list_files" in tools:
            return "Listing the project.", [call("list_files", path=".")]
        if len(made) == 1 and "read_file" in tools:
            listing = results.get(made[0].get("id"), "")
            name = next((candidate for candidate in ("README.md", "readme.md", "README") if candidate in listing), None)
            if name:
                return f"Reading {name}.", [call("read_file", path=name, max_lines=40)]
        task = prompt.split("\n", 2)[1] if "\n" in prompt else prompt
        return (f"## Report\n\n**Task:** {task[:200]}\n\n- Summary: simulated subagent finished after "
                f"{len(made)} tool call(s).\n- Findings: see the tool results in this transcript.\n"
                "- Files changed: none.\n- Open questions: none."), []

    def ask(self, words, made, results, tools):
        if "ask_user" not in tools:
            return "ask_user is turned off in this chat.", []
        if made:
            return "Thanks, noted your answer: " + results.get(made[0].get("id"), "")[:300], []
        return "One question first.", [call("ask_user", questions=[{
            "question": "Which effort level should the simulated follow-up use?", "header": "Effort",
            "options": [{"label": "High", "description": "More thinking"}, {"label": "Fast", "description": "Low effort"}]}])]

    def tasks(self, words, made, results, tools):
        if not {"create_task", "update_task"} <= tools.keys():
            return "Task tools are turned off in this chat.", []
        if not made:
            return "Recording two tasks.", [call("create_task", title="Simulated task one"),
                                           call("create_task", title="Simulated task two")]
        if len(made) == 2:
            ids = [re.search(r'"id"\s*:\s*"([^"]+)"', results.get(item.get("id"), "")) for item in made]
            ids = [match.group(1) for match in ids if match]
            return "Completing them.", [call("update_task", task_id=task_id, status="in_progress") for task_id in ids]
        if len(made) <= 4:
            ids = [json.loads(item["function"]["arguments"]).get("task_id") for item in made[2:]]
            return "Done.", [call("update_task", task_id=task_id, status="completed") for task_id in ids if task_id]
        return "Both simulated tasks are complete.", []

    def read(self, words, made, results, tools):
        if not made and "project_overview" in tools:
            return "Getting an overview first.", [call("project_overview", path=".")]
        if len(made) == 1 and "read_file" in tools:
            return "Reading the README.", [call("read_file", path="README.md", max_lines=30)]
        return "I looked at the project overview and the README (simulated).", []

    def long(self):
        rows = "\n".join(f"| {index} | `item_{index}` | {index * 7 % 13} |" for index in range(1, 9))
        return ("## A long simulated answer\n\n" + "This paragraph is filler to make the reply long. " * 12
                + "\n\n| # | Name | Value |\n|---|---|---|\n" + rows
                + "\n\n```python\ndef simulated():\n    return 42\n```\n\nThat is all.")


def reasoning_text(endpoint, effort, body, prompt):
    tools = len(body.get("tools") or [])
    steps = {"low": 1, "medium": 2, "high": 4, "xhigh": 6, "max": 8}[effort]
    lines = [f"The user wrote: \"{prompt[:120]}\". This request runs at effort **{effort}** on {endpoint}, "
             f"with {tools} tools and {len(body.get('messages') or [])} messages."]
    ideas = ["Check what the project already contains before changing anything.",
             "Prefer one focused edit over rewriting the file.",
             "Keep the reply short; the details are in the tool cards.",
             "Consider whether a subagent could read the large files instead.",
             "Make sure the command's exit code is checked.",
             "Look for tests that cover this behaviour.",
             "Weigh the simplest change that satisfies the request.",
             "Double-check the edge cases before answering."]
    return "\n\n".join(lines + ideas[:steps])


def words_of(text):
    return re.findall(r"\S+\s*", text)


class Gateway(BaseHTTPRequestHandler):
    brain = Brain()
    claude_fields = "both"
    speed = 1.0
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def send_json(self, status, data, headers=None):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.rstrip("/") == "/api/2.0/serving-endpoints":
            self.send_json(200, {"endpoints": [{"name": name, "state": {"ready": "READY"}} for name in ENDPOINTS]})
        else:
            self.send_json(404, {"error_code": "NOT_FOUND", "message": "Not found in the simulator."})

    def do_POST(self):
        match = re.fullmatch(r"/serving-endpoints/([^/]+)/invocations", self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_json(400, {"error_code": "BAD_REQUEST", "message": "Invalid JSON."})
            return
        if not match:
            self.send_json(404, {"error_code": "NOT_FOUND", "message": "Unknown route."})
            return
        endpoint = match.group(1)
        try:
            effort, summarized = reasoning_request(endpoint, body, self.claude_fields)
            fields = {key: body[key] for key in ("thinking", "output_config", "reasoning_effort") if key in body}
            kind = "agent" if body.get("stream") else "auxiliary"
            log(f"{endpoint} {kind} request: fields {json.dumps(fields) if fields else 'none'} -> "
                f"{'effort ' + effort if effort else 'no thinking'}{', summary shown' if summarized else ''}")
            if body.get("stream"):
                self.stream(endpoint, body, effort, summarized)
            else:
                self.complete(endpoint, body)
        except Rejected as rejected:
            log(f"{endpoint}: HTTP {rejected.status} {rejected}")
            headers = {"Retry-After": str(rejected.retry_after)} if rejected.retry_after else None
            try:
                message = json.loads(str(rejected))
            except json.JSONDecodeError:
                message = {"error_code": "BAD_REQUEST", "message": str(rejected)}
            self.send_json(rejected.status, message, headers)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def complete(self, endpoint, body):
        """A title, compaction summary or side question: one JSON response."""
        messages = body.get("messages") or []
        request = "\n".join(text_of(message.get("content")) for message in messages)
        if "Create a short title" in request:
            text = "Simulated conversation"
        else:
            headings = list(dict.fromkeys(re.findall(r"^## (.+)$", request, flags=re.M)))
            text = ("\n\n".join(f"## {name}\nSimulated summary content for {name.lower()}." for name in headings)
                    if headings else "Simulated answer to a side question.")
        self.send_json(200, {"id": f"sim-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "model": endpoint,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                          "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": len(request) // 4 + 20, "completion_tokens": len(text) // 4 + 5,
                                       "total_tokens": (len(request) + len(text)) // 4 + 25}})

    def chunk(self, response_id, endpoint, delta, finish=None, usage=None):
        data = {"id": response_id, "object": "chat.completion.chunk", "model": endpoint,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage:
            data["usage"] = usage
        line = f"data: {json.dumps(data)}\n\n".encode()
        self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
        self.wfile.flush()

    def stream(self, endpoint, body, effort, summarized):
        prompt = next((text_of(message.get("content")) for message in reversed(body.get("messages") or [])
                       if message.get("role") == "user"), "")
        text, calls = self.brain.respond(body)
        if text is None:
            fields = {key: body[key] for key in ("thinking", "output_config", "reasoning_effort") if key in body}
            text = (f"Simulated reply from **{endpoint}**.\n\n- Reasoning fields in this request: "
                    f"`{json.dumps(fields) if fields else 'none'}`\n- Thinking: "
                    + (f"effort **{effort}**" + (", summary returned" if summarized else ", summary omitted (empty text)")
                       if effort else "off") + f"\n\n{SCENARIO_HINT}")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        response_id = f"sim-{uuid.uuid4().hex[:12]}"
        self.chunk(response_id, endpoint, {"role": "assistant", "content": ""})
        reasoning_tokens = 0
        if effort:
            seconds, reasoning_tokens = THINKING[effort]
            thought = reasoning_text(endpoint, effort, body, prompt) if summarized else ""
            pieces = words_of(thought) or [""] * 4
            for piece in pieces:
                self.chunk(response_id, endpoint, {"content": [{"type": "reasoning", "summary": [
                    {"type": "summary_text", "text": piece, "signature": ""}]}]})
                time.sleep(seconds * self.speed / len(pieces))
            self.chunk(response_id, endpoint, {"content": [{"type": "reasoning", "summary": [
                {"type": "summary_text", "text": "", "signature": "simulated-signature"}]}]})
        for piece in words_of(text):
            self.chunk(response_id, endpoint, {"content": piece})
            time.sleep(0.015 * self.speed)
        for index, item in enumerate(calls):
            arguments = json.dumps(item["arguments"])
            self.chunk(response_id, endpoint, {"tool_calls": [{"index": index, "id": f"call_{uuid.uuid4().hex[:10]}",
                                                               "type": "function", "function": {"name": item["name"],
                                                                                                "arguments": ""}}]})
            for start in range(0, len(arguments), 40):
                self.chunk(response_id, endpoint, {"tool_calls": [{"index": index,
                                                                   "function": {"arguments": arguments[start:start + 40]}}]})
        prompt_tokens = len(json.dumps(body.get("messages"))) // 4 + len(json.dumps(body.get("tools") or [])) // 4
        completion = len(text) // 4 + 10 * len(calls) + reasoning_tokens
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion, "total_tokens": prompt_tokens + completion,
                 "prompt_tokens_details": {"cached_tokens": prompt_tokens // 2},
                 "completion_tokens_details": {"reasoning_tokens": reasoning_tokens}}
        self.chunk(response_id, endpoint, {}, finish="tool_calls" if calls else "stop", usage=usage)
        done = b"data: [DONE]\n\n"
        self.wfile.write(f"{len(done):x}\r\n".encode() + done + b"\r\n0\r\n\r\n")
        self.wfile.flush()


def demo_workspace(state):
    """A small Git project to try the scenarios in, created once."""
    project = state / "demo-project"
    if project.exists():
        return project
    (project / "src").mkdir(parents=True)
    (project / "README.md").write_text("# Demo project\n\nA tiny project for trying the app against the simulator.\n")
    (project / "src" / "app.py").write_text("def greet(name):\n    return f\"Hello, {name}!\"\n")
    (project / "tests").mkdir()
    (project / "tests" / "test_app.py").write_text("from src.app import greet\n\n\ndef test_greet():\n"
                                                   "    assert greet('x') == 'Hello, x!'\n")
    import subprocess
    try:
        for command in (["git", "init", "-q"], ["git", "add", "."],
                        ["git", "-c", "user.name=Simulator", "-c", "user.email=simulator@example.invalid",
                         "commit", "-q", "-m", "Demo project"]):
            subprocess.run(command, cwd=project, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        pass  # The scenarios also work without Git.
    return project


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8766, help="app port (default 8766; the real app uses 8765)")
    parser.add_argument("--gateway-port", type=int, default=0, help="fake Databricks port (default: any free port)")
    parser.add_argument("--state-dir", default=str(APP / ".local" / "simulator"),
                        help="state folder for simulated chats and settings (default .local/simulator)")
    parser.add_argument("--workspace", help="project folder (default: a demo Git project in the state folder)")
    parser.add_argument("--claude-fields", choices=("both", "anthropic", "openai", "none"), default="both",
                        help="reasoning fields the simulated route accepts for Claude endpoints")
    parser.add_argument("--speed", type=float, default=1.0, help="multiplies simulated thinking and typing time")
    parser.add_argument("--gateway-only", action="store_true", help="run only the fake gateway (for your own launcher)")
    args = parser.parse_args()

    Gateway.claude_fields, Gateway.speed = args.claude_fields, max(0.0, args.speed)
    server = ThreadingHTTPServer(("127.0.0.1", args.gateway_port), Gateway)
    server.daemon_threads = True
    gateway = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"fake Databricks gateway on {gateway} (Claude reasoning fields accepted: {args.claude_fields})")
    if args.gateway_only:
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            return

    state = Path(args.state_dir).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else demo_workspace(state)
    # Set before the app reads its settings; the process environment wins over .env.
    os.environ.update(LOCAL_AGENT_STATE_DIR=str(state), LOCAL_AGENT_WORKSPACE=str(workspace), LOCAL_AGENT_ENV_FILE="")
    sys.path.insert(0, str(APP / "backend"))
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    from local_agent import config
    config.Settings.credentials = lambda self: (gateway, "simulated-token")
    import uvicorn
    log(f"app on http://127.0.0.1:{args.port}  workspace {workspace}  state {state}")
    log(SCENARIO_HINT)
    uvicorn.run("local_agent.api:create_app", factory=True, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
