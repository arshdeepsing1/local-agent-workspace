"""The long chat with and without the live trim (US4 scenario 3, FR-029, FR-030, SC-011).

The real app and page against the offline simulator's long_chat scenario (scripts/simulate_databricks.py): forty
"step N" messages with large tool results, facts stated at fixed steps, and "question: <topic>" messages that the
scripted model answers ONLY from the request it receives. The same 40 steps run twice at the settings the backend's
test_compaction_retention.py uses (a 49,152-token context budget, scale 1.0, which compacts during steps 12, 22, 31
and 39 without the trim): once with context_trim off, once with it on at its defaults (start 70, target 55).

With the trim on, the first compaction comes at least 30% later in steps (SC-011), the chat still answers its ten
questions from what it kept, and the collapsed meter and the usage card show the same percentage all along. The
cached tokens each run's requests read and wrote are printed (pytest -s) so the cost effect shows.
"""
import re
import time
from dataclasses import dataclass

import pytest
from playwright.sync_api import expect

from local_agent.client import LocalAgentClient


MODEL = "databricks-claude-opus-5-5"
WINDOW, SCALE = 49_152, 1.0
ASKED = ("goal", "open tasks", "decision 1", "decision 2", "decision 3", "decision 4", "instruction 1",
         "instruction 2", "closed task", "last step")
CHECK_STEPS = (3, 10, 20, 30, 40)  # Where the meter and the card are compared during the trimmed run.
FIRST_GAIN = 1.3  # SC-011: the first compaction at least 30% later.


@dataclass
class Run:
    simulator: object
    client: LocalAgentClient
    manager: object
    url: str
    id: str
    step_run: int = 0

    def __post_init__(self):
        self.compaction_steps = []  # The step after which each new compaction was counted.

    def compactions(self):
        return int((self.manager.get(self.id).get("context_state") or {}).get("compactions", 0))

    def say(self, text):
        started = self.client.session(self.id).get("last_turn_result") or {}
        self.client.send(self.id, text, verbatim=True)
        deadline = time.time() + 120
        record = None
        while time.time() < deadline and record is None:
            record = self.client.wait_result(self.id, after=started.get("started"))
        assert record is not None, f"No turn ran for {text!r}."
        assert record["terminal_reason"] == "completed", (text, record["terminal_reason"])
        return record["result"]

    def step(self, number):
        text = f"step {number}"
        if number in (4, 8):
            text = self.simulator.LONG_CHAT_INSTRUCTIONS[0 if number == 4 else 1]
        before = self.compactions()
        self.say(text)
        self.step_run = number
        self.compaction_steps.extend([number] * (self.compactions() - before))

    def first_compaction(self):
        return self.compaction_steps[0] if self.compaction_steps else None

    def expected(self, topic):
        sim, ran = self.simulator, self.step_run
        if topic == "goal":
            return sim.LONG_CHAT_GOAL if ran >= 1 else None
        if topic == "open tasks":
            return sim.LONG_CHAT_OPEN_TASKS if ran >= 9 else None
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
        """The ten questions: {topic: (expected, answer)} for those that apply after the steps run so far."""
        answers = {}
        for topic in ASKED:
            wanted = self.expected(topic)
            if wanted is not None:
                answers[topic] = (wanted, self.say(f"question: {topic}").strip())
        return answers

    def cached(self):
        """(cache read, cache written, input) tokens over the agent requests of the run, as the gateway reported them."""
        calls = [call for call in self.manager.get(self.id).get("inference_calls") or []
                 if call.get("purpose") == "agent" and isinstance(call.get("usage"), dict)]
        usage = [call["usage"] for call in calls]
        return (sum(item.get("cache_read_input_tokens") or 0 for item in usage),
                sum(item.get("cache_creation_input_tokens") or 0 for item in usage),
                sum(item.get("input_tokens") or 0 for item in usage), len(calls))  # input: the uncached part


@pytest.fixture
def running(simulator, tmp_path_factory, monkeypatch):
    """running(trim, ask=False) -> a Run in a fresh app, state folder and workspace; every app is stopped afterwards.
    compaction_ask is off unless a test asks for it: these runs open a page (a person is there), and a card before
    each compaction would stop the scripted steps, which expect the compactions to run at once."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("LOCAL_AGENT_CODING_DIR", "")
    monkeypatch.setenv("LOCAL_AGENT_PYTHON_VENV", "")
    monkeypatch.setenv("LOCAL_AGENT_MODEL", MODEL)
    from local_agent import extensions, instructions
    monkeypatch.setattr(extensions, "USER_HOME", home)
    monkeypatch.setattr(instructions, "user_home", lambda: home)
    started = []

    def begin(trim, ask=False):
        root = tmp_path_factory.mktemp("long-chat")
        gateway, gateway_url = simulator.start_gateway(speed=0.0, long_chat_scale=SCALE)
        state, workspace = root / "state", root / "project"
        state.mkdir(mode=0o700)
        workspace.mkdir()
        simulator.long_chat_files(workspace)
        context = simulator.patched_settings(gateway_url, state, workspace)
        context.__enter__()
        server, thread, url = simulator.start_app()
        started.append((gateway, context, server, thread))
        manager = server.config.app.state.manager
        manager.settings.values.update(context_window=WINDOW, compaction_handoffs=True, context_trim=trim, compaction_ask=ask)
        client = LocalAgentClient(url)
        session = client.create_session(workspace=workspace, permission_mode="plan", model=MODEL)
        client.set_permission_mode(session["id"], "bypassPermissions", confirm=True)
        return Run(simulator, client, manager, url, session["id"])

    yield begin
    for gateway, context, server, thread in started:
        server.should_exit = True
        thread.join(15)
        context.__exit__(None, None, None)
        gateway.shutdown()
        gateway.server_close()


def percent(text):
    found = re.search(r"(\d+(?:\.\d+)?)%", text or "")
    return float(found.group(1)) if found else None


READ_BOTH = """() => {
  const meter = document.querySelector('details.context-meter > summary > span')
  const card = document.querySelector('.context-usage-head strong')
  return meter && card ? [meter.textContent, card.textContent] : null
}"""


def meter_and_card(page, run):
    """(collapsed meter %, usage card %, the server's percent_used): both read in one turn of the page's script,
    once the page shows the very reading the server holds (it follows the chat's stream, a little behind, and an
    earlier reading can have the same percentage: the token count is part of what it waits for)."""
    info = run.manager.get(run.id)["context_info"]
    wanted = info["percent_used"]
    label = f"{info['used_tokens']:,} tokens · {wanted}%" if not info["is_estimate"] else f"~{wanted}%"
    page.wait_for_function(f"""() => {{
      const [meter, card] = ({READ_BOTH})() || []
      return !!card && card.startsWith('{wanted}% used') && meter.includes({label!r}) }}""", timeout=30_000)
    meter, card = page.evaluate(READ_BOTH)
    return percent(meter), percent(card), wanted


def test_the_first_compaction_comes_30_percent_later_with_the_trim_and_the_chat_keeps_its_state(
        running, browser, record_property, capsys):
    off = running(False)
    for number in range(1, 41):
        off.step(number)
    first_off, cached_off = off.first_compaction(), off.cached()
    assert first_off is not None, "The baseline run never compacted in 40 steps."

    on = running(True)
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    try:
        page.goto(f"{on.url}/?session={on.id}")
        expect(page.get_by_role("textbox", name="Message")).to_be_editable(timeout=15_000)
        readings = {}
        for number in range(1, 41):
            on.step(number)
            if number in CHECK_STEPS:
                readings[number] = meter_and_card(page, on)
        marked = [event for event in on.manager.get(on.id)["events"] if "context_trim" in event]
        cached_on = on.cached()  # After the 40 steps, as the run without the trim was counted (not its questions).
        answers = on.score()
    finally:
        context.close()
    first_on = on.first_compaction()
    shown = "no compaction in 40 steps" if first_on is None else f"step {first_on}"
    summary = (f"first compaction: trim off at step {first_off} (all compactions during steps {off.compaction_steps}), "
               f"trim on at {shown} (during steps {on.compaction_steps}); results trimmed: {len(marked)}; "
               f"cached tokens over the 40 steps (read, written, input, requests): off {cached_off}, on {cached_on}")
    record_property("long_chat", summary)
    with capsys.disabled():
        print(f"\n[context trim] {summary}")

    assert errors == []
    assert marked, "The trimmed run marked no tool result."
    # The meter and the card always read the one percentage of the server.
    for number, (meter, card, server) in readings.items():
        assert meter == card == server, f"after step {number}: meter {meter}%, card {card}%, server {server}%"
    # SC-011: at least 30% later (a run that never compacts is later still).
    later = first_on if first_on is not None else 41
    assert later >= FIRST_GAIN * first_off, summary
    # The cost: a pass breaks the provider's prompt cache, so it runs rarely (context.plan_trim). Before the pass waited
    # for its turn the writes rose from about 84k to 308k tokens (3.7 times); now they stay under twice the baseline,
    # and the cache is read as much as before.
    assert cached_on[1] <= 2.0 * cached_off[1], f"cache writes {cached_off[1]:,} off, {cached_on[1]:,} on"
    assert cached_on[0] >= 0.9 * cached_off[0], f"cache reads {cached_off[0]:,} off, {cached_on[0]:,} on"
    # FR-030 and the retention scenario: what the trim and compaction left still answers every question.
    wrong = {topic: pair for topic, pair in answers.items() if pair[0] != pair[1]}
    assert not wrong and len(answers) == 10, f"{wrong} {summary}"


def test_shorten_on_the_usage_card_trims_one_result_of_the_real_chat(running, browser):
    """The page finds its chat by itself (api.activeSessionId), asks what the trim costs and sends it after Confirm."""
    run = running(False)  # No automatic pass: the one trim is the user's.
    for number in range(1, 9):
        run.step(number)
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    try:
        page.goto(f"{run.url}/?session={run.id}")
        expect(page.get_by_role("textbox", name="Message")).to_be_editable(timeout=15_000)
        page.locator("details.context-meter > summary").click()
        section = page.get_by_role("region", name="Largest items")
        row = section.locator("li").filter(has=page.get_by_role("button", name="Shorten")).first
        expect(row).to_be_visible(timeout=15_000)
        row.get_by_role("button", name="Shorten").click()
        expect(section.get_by_text(re.compile(r"Frees about [\d.]+k? tokens\. At least about [\d.]+k? cached tokens are re-read"))).to_be_visible()
        section.get_by_role("button", name="Confirm").click()
        expect(section.get_by_text(re.compile(r"1 result shortened, about [\d.]+k? tokens freed"))).to_be_visible()
        marked = [event for event in run.manager.get(run.id)["events"] if "context_trim" in event]
        assert len(marked) == 1 and marked[0]["context_trim"]["by"] == "user" and marked[0]["context_trim"]["mode"] == "shortened"
    finally:
        context.close()


def test_the_card_comes_before_a_compaction_and_continue_defers_it_until_the_request_does_not_fit(running, browser):
    """compaction_ask on, no trim. The scenario's own compactions come in the middle of a turn (a step's tool results
    cross the mark), which never asks. So after a few steps a long message typed in the page takes the chat just over
    the threshold at the start of its turn (and under the input budget): the card shows. Continue runs that turn with
    no compaction, later steps ask nothing, and a later step compacts when the request no longer fits, with a notice
    that says the deferral ended."""
    from local_agent.context import estimate_tokens
    run = running(False, ask=True)
    for number in range(1, 9):
        run.step(number)
    info = run.manager.get(run.id)["context_info"]
    threshold, budget = info["auto_compact_threshold"], info["input_budget"]
    wanted = threshold - info["next_estimate"]["tokens"] + (budget - threshold) // 3  # Over the mark, well inside the budget.
    filler = "alpha beta gamma delta " * 4 * wanted
    while estimate_tokens([{"role": "user", "content": filler}], []) > wanted:
        filler = filler[:-400]
    assert run.compactions() == 0 and 0 < len(filler) < 50_000, (run.compactions(), len(filler))
    message = f"step 9. {filler}"
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    card = page.get_by_role("region", name="Compact before this message?")
    try:
        page.goto(f"{run.url}/?session={run.id}")
        expect(page.get_by_role("textbox", name="Message")).to_be_editable(timeout=15_000)
        page.get_by_role("textbox", name="Message").fill(message)
        page.get_by_role("button", name="Send message").click()
        # Your message is in the chat, nothing is compacted and the turn waits.
        expect(card).to_be_visible(timeout=15_000)
        expect(card).to_contain_text(f"automatic compaction starts at {info['compact_at_percent']}%")  # The meter's number.
        expect(page.get_by_text("step 9. alpha beta", exact=False).first).to_be_visible()
        expect(page).to_have_title(re.compile(r"^\(1\) Local"))  # The tab counts it, like any waiting card.
        assert run.compactions() == 0
        card.get_by_role("button", name="Continue without compacting").click()
        expect(card).to_have_count(0)
        record = run.client.wait_result(run.id)
        assert record["terminal_reason"] == "completed"
        run.step_run = 9
        assert run.compactions() == 0, "Continue must run the turn without compacting."
        # Later steps ask nothing; the first compaction comes once the request no longer fits.
        number = 9
        while run.compactions() == 0:
            number += 1
            assert number <= 40, "The request never outgrew the input budget in 40 steps."
            run.step(number)
            assert not card.is_visible()
        events = run.manager.get(run.id)["events"]
        notice = next(event for event in events if event["type"] == "notice" and event.get("compaction"))
        assert ("You chose to continue without compacting. That deferral ended here because the request no longer fits."
                in notice["text"])
        assert len([event for event in events if "compaction_choice" in event]) == 1
        assert errors == []
    finally:
        context.close()
