"""State retention across three compactions (US4 scenarios 4 and 5, FR-031, SC-010).

The real app (uvicorn on a free port, its AgentManager, the real HTTP calls) runs against the offline simulator's
long_chat scenario (scripts/simulate_databricks.py): forty "step N" messages with large tool results, facts stated at
fixed steps, and "question: <topic>" messages that the scripted model answers ONLY from the request it receives. So a
question measures what the app keeps in the context it sends after compaction. No network beyond 127.0.0.1, no paid
calls, no sleeps: the tests wait on the chat's own stream (LocalAgentClient.wait_result).
"""
import importlib.util
import time
from pathlib import Path

import pytest

from local_agent.client import LocalAgentClient

APP = Path(__file__).resolve().parents[2]
SIMULATOR = APP / "scripts" / "simulate_databricks.py"
MODEL = "databricks-claude-opus-5-5"
# Measured against the real app: the documented 65,536 gave only two compactions (steps 20 and 36); 49,152 with scale 1.0
# gives four (steps 12, 22, 31 and 39), the steps the scenario estimated for 65,536.
WINDOW, SCALE = 49_152, 1.0
ASKED = ("goal", "open tasks", "decision 1", "decision 2", "decision 3", "decision 4", "instruction 1",
         "instruction 2", "closed task", "last step")
MUST_KEEP = ("goal", "instruction 1", "instruction 2")  # Scenario 5: never lost.


@pytest.fixture(scope="module")
def simulator():
    spec = importlib.util.spec_from_file_location("simulate_databricks_retention", SIMULATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Chat:
    """One chat of the real app, driven over HTTP with the scenario's messages."""

    def __init__(self, simulator, client, manager, session_id):
        self.simulator, self.client, self.manager, self.id = simulator, client, manager, session_id
        self.step_run = 0
        self.compaction_steps = []  # The step after which each new compaction was counted.

    def compactions(self):
        return int((self.manager.get(self.id).get("context_state") or {}).get("compactions", 0))

    def say(self, text):
        """Send a message, wait for the turn (and any compaction) to end; the final reply."""
        before = self.compactions()
        started = self.client.session(self.id).get("last_turn_result") or {}
        self.client.send(self.id, text, verbatim=True)
        record = None
        deadline = time.time() + 120
        while time.time() < deadline:
            record = self.client.wait_result(self.id, after=started.get("started"))
            if record is not None:
                break
        assert record is not None, f"No turn ran for {text!r}."
        assert record["terminal_reason"] == "completed", (text, record["terminal_reason"])
        return before, record["result"]

    def step(self, number):
        sim = self.simulator
        text = f"step {number}"
        if number in (4, 8):
            text = sim.LONG_CHAT_INSTRUCTIONS[0 if number == 4 else 1]
        before, reply = self.say(text)
        self.step_run = number
        if self.compactions() > before:
            self.compaction_steps.extend([number] * (self.compactions() - before))
        return reply

    def run_to(self, number):
        while self.step_run < number:
            self.step(self.step_run + 1)

    def run_until_compactions(self, count, limit=40):
        while self.compactions() < count:
            assert self.step_run < limit, f"Only {self.compactions()} compactions in {limit} steps: {self.compaction_steps}"
            self.step(self.step_run + 1)

    def ask(self, topic):
        return self.say(f"question: {topic}")[1].strip()

    def expected(self, topic):
        """The fixed text the answer must equal after the steps run so far (None: not applicable yet)."""
        sim, ran = self.simulator, self.step_run
        if topic == "goal":
            return sim.LONG_CHAT_GOAL if ran >= 1 else None
        if topic == "open tasks":
            return sim.LONG_CHAT_OPEN_TASKS if ran >= 9 else None  # Tasks 1 and 3 (task 2 closed at step 20).
        if topic.startswith("decision"):
            index = int(topic[-1]) - 1
            return sim.LONG_CHAT_DECISIONS[index] if ran >= (3, 7, 12, 18)[index] else None
        if topic.startswith("instruction"):
            index = int(topic[-1]) - 1
            return sim.LONG_CHAT_INSTRUCTIONS[index] if ran >= (4, 8)[index] else None
        if topic == "closed task":
            return sim.LONG_CHAT_CLOSED if ran >= 20 else None
        return f"Last step: {ran}"

    def score(self):
        """Ask the ten questions: (correct, applicable, {topic: (expected, answer, ok)})."""
        results = {}
        for topic in ASKED:
            wanted = self.expected(topic)
            if wanted is None:
                continue
            answer = self.ask(topic)
            results[topic] = (wanted, answer, answer == wanted)
        return sum(ok for *_, ok in results.values()), len(results), results


@pytest.fixture
def start(simulator, tmp_path, monkeypatch):
    """start(window, scale, handoffs) -> a Chat in a fresh app, state folder and workspace; stopped afterwards."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LOCAL_AGENT_CODING_DIR", "")
    monkeypatch.setenv("LOCAL_AGENT_PYTHON_VENV", "")
    monkeypatch.setenv("LOCAL_AGENT_MODEL", MODEL)
    from local_agent import extensions, instructions
    monkeypatch.setattr(extensions, "USER_HOME", home)
    monkeypatch.setattr(instructions, "user_home", lambda: home)
    running = []

    def begin(window=WINDOW, scale=SCALE, handoffs=True, trim=False):
        gateway, gateway_url = simulator.start_gateway(speed=0.0, long_chat_scale=scale)
        state, workspace = tmp_path / "state", tmp_path / "project"
        state.mkdir(mode=0o700)
        workspace.mkdir()
        simulator.long_chat_files(workspace)
        context = simulator.patched_settings(gateway_url, state, workspace)
        context.__enter__()
        server, thread, url = simulator.start_app()
        running.append((gateway, context, server, thread))
        manager = server.config.app.state.manager
        # The no-trimming baseline: with the trim on (the default) compactions come later (see the trim-on variant below).
        manager.settings.values.update(context_window=window, compaction_handoffs=handoffs, context_trim=trim)
        client = LocalAgentClient(url)
        session = client.create_session(workspace=workspace, permission_mode="plan", model=MODEL)
        client.set_permission_mode(session["id"], "bypassPermissions", confirm=True)
        return Chat(simulator, client, manager, session["id"])

    yield begin
    for gateway, context, server, thread in running:
        server.should_exit = True
        thread.join(15)
        context.__exit__(None, None, None)
        gateway.shutdown()
        gateway.server_close()


def test_the_long_chat_compacts_at_least_three_times(start):
    chat = start()
    chat.run_to(40)
    print(f"compactions: {chat.compactions()} during steps {chat.compaction_steps}")
    assert chat.compactions() >= 3, f"compactions during steps {chat.compaction_steps}"


@pytest.mark.parametrize("handoffs", [True, False], ids=["handoffs-on", "handoffs-off"])
@pytest.mark.parametrize("compactions", [1, 2, 3], ids=["after-1st", "after-2nd", "after-3rd"])
def test_state_survives_one_two_and_three_compactions(start, compactions, handoffs):
    chat = start(handoffs=handoffs)
    chat.run_until_compactions(compactions)
    steps = list(chat.compaction_steps)
    correct, applicable, results = chat.score()
    lines = "\n".join(f"  {'ok ' if ok else 'BAD'} {topic}: {answer!r}" for topic, (_, answer, ok) in results.items())
    message = (f"{correct} of {applicable} correct after {chat.step_run} steps and {compactions} compactions "
               f"(during steps {steps}), handoffs {'on' if handoffs else 'off'}:\n{lines}")
    print(message)
    assert chat.compactions() >= compactions
    assert correct >= min(9, applicable), message
    kept = {topic for topic, (_, _, ok) in results.items() if ok}
    assert set(MUST_KEEP) & set(results) <= kept, message


@pytest.mark.parametrize("handoffs", [True, False], ids=["handoffs-on", "handoffs-off"])
def test_state_survives_two_compactions_with_the_trim_on(start, handoffs):
    """The same state check with the live trim of old tool results on: it compacts during steps 19 and 35."""
    chat = start(handoffs=handoffs, trim=True)
    chat.run_until_compactions(2)
    steps = list(chat.compaction_steps)
    correct, applicable, results = chat.score()
    lines = "\n".join(f"  {'ok ' if ok else 'BAD'} {topic}: {answer!r}" for topic, (_, answer, ok) in results.items())
    message = (f"{correct} of {applicable} correct after {chat.step_run} steps and 2 compactions "
               f"(during steps {steps}), trim on, handoffs {'on' if handoffs else 'off'}:\n{lines}")
    print(message)
    assert chat.compactions() >= 2
    assert correct >= min(9, applicable), message
    kept = {topic for topic, (_, _, ok) in results.items() if ok}
    assert set(MUST_KEEP) & set(results) <= kept, message


@pytest.mark.parametrize("handoffs", [True, False], ids=["handoffs-on", "handoffs-off"])
def test_the_user_does_not_need_to_repeat_an_instruction_after_three_compactions(start, handoffs):
    chat = start(handoffs=handoffs)
    chat.run_until_compactions(3)
    chat.run_to(chat.step_run + 5)
    sim = chat.simulator
    answers = {topic: chat.ask(topic) for topic in ("instruction 1", "instruction 2")}
    wanted = {"instruction 1": sim.LONG_CHAT_INSTRUCTIONS[0], "instruction 2": sim.LONG_CHAT_INSTRUCTIONS[1]}
    assert answers == wanted, f"after {chat.step_run} steps, compactions during steps {chat.compaction_steps}: {answers}"


def test_questions_are_not_answered_from_outside_the_context(start):
    chat = start()
    chat.run_to(2)
    assert chat.ask("decision 1") == chat.simulator.LONG_CHAT_NOT_IN_CONTEXT
    chat.run_to(3)  # Step 3 states decision 1: now the same question finds it.
    assert chat.ask("decision 1") == chat.simulator.LONG_CHAT_DECISIONS[0]


# --- A fact that only an old tool result holds (the trim touches tool results, so the scenarios above cannot fail on it) ---

FACT = "DEPLOY_KEY_NAME=zebra-17-orchard"


async def test_a_fact_only_an_old_tool_result_holds_can_be_read_again_as_the_notice_says_and_stays_in_the_chat(
        tmp_path, monkeypatch):
    import json

    import httpx
    from local_agent.context import estimate_tokens
    from local_agent.prompt_cache import unmarked
    from test_context_trim import REAL_CLIENT, load, long_history, runtime as trim_runtime  # noqa: F401

    # The runtime fixture of test_context_trim, used by name: a chat with a 65,536-token window and the trim on.
    from test_context_trim import AgentManager, Settings, Store, WINDOW
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    body = "\n".join(f"{number}: setting_{number} = 'value-{number}' " + "x" * 30 for number in range(1, 100))
    lines = body.split("\n")
    lines[48] = f"49: {FACT}"  # In the middle: a shortened result keeps only its head and tail.
    (project / "src" / "config.py").write_text("\n".join(lines))
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", compaction_handoffs=False, context_window=WINDOW,
                           max_output_tokens=4_096)
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    try:
        manager = AgentManager(store, settings)
        session = store.create(settings.values)
        session.update(title="Retention", title_generated=True, permission_mode="bypassPermissions")
        first_read = json.dumps({"path": "src/config.py", "content": "\n".join(lines), "start_line": 1, "total_lines": 99},
                                indent=2)
        history = long_history(8, first=lambda h: h.tool("read_file", {"path": "src/config.py"}, first_read))
        requests = []

        def handle(request):
            payload = json.loads(request.content)
            if payload.get("stream") is not True:
                return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}]})
            sent = unmarked(payload)["messages"]
            requests.append(sent)
            usage = {"prompt_tokens": estimate_tokens(sent, unmarked(payload)["tools"]), "completion_tokens": 5}
            noticed = any(m.get("role") == "tool" and "by the app to save context" in m["content"]
                          and "src/config.py" in m["content"] for m in sent)
            if sent[-1].get("role") == "user" and "deploy key" in sent[-1]["content"] and noticed:
                # What the notice says to do: read the file again.
                delta = {"tool_calls": [{"index": 0, "id": "call_again", "type": "function", "function": {
                    "name": "read_file", "arguments": json.dumps({"path": "src/config.py"})}}]}
                chunk = {"choices": [{"delta": delta, "finish_reason": "tool_calls"}], "usage": usage}
            else:
                chunk = {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}], "usage": usage}
            return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(
            **kwargs, transport=httpx.MockTransport(handle)))
        await manager.run(load(session, history), "Turn 9: what is the deploy key name? Look at the deploy key again.")
        # The first request: the trim replaced the old read, so the request no longer holds the fact. The notice names
        # the file and says how to get it back.
        notice = next(m["content"] for m in requests[0] if m.get("role") == "tool" and "src/config.py" in m["content"])
        assert "by the app to save context" in notice and "read_file result for src/config.py" in notice
        assert "Read the file again" in notice and FACT not in json.dumps(requests[0])
        # The saved conversation holds the fact verbatim, on the wire and on the event.
        saved = manager.store.get(session["id"])
        assert any(FACT in message.get("content", "") for message in saved["wire"] if message["role"] == "tool")
        assert any(FACT in (event.get("output") or "") for event in saved["events"] if event.get("name") == "read_file")
        # Following the notice, the model reads the file again and the next request carries the fact.
        assert len(requests) == 2 and requests[1][-1]["role"] == "tool" and FACT in requests[1][-1]["content"]
    finally:
        store.db.close()
