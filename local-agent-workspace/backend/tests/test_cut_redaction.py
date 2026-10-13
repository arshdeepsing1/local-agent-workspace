"""A cut never splits the configured token (integration review B, findings 11 and 12): text a tool
or the result allowance shortens is redacted before the cut, since redaction is an exact match."""
import pytest

from local_agent.agents import AgentManager, redact_value
from local_agent.config import Settings
from local_agent.result_budget import TOOL_RESULT_HARD_LIMIT, ResultAllowance, bound_result
from local_agent.store import Store

TOKEN = "dapi" + "0123456789abcdef" * 2  # 36 characters, like a configured Databricks token.


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", compaction_handoffs=False)
    settings.credentials = lambda: ("https://gateway.example", TOKEN)
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Cuts", title_generated=True, permission_mode="bypassPermissions")
    yield manager, session, project
    store.db.close()


def piece(text):
    """The longest prefix or suffix of the token (8 characters or more) in text, or 0."""
    return next((n for n in range(len(TOKEN), 7, -1) if TOKEN[:n] in text or TOKEN[-n:] in text), 0)


def card(session, name):
    return [event for event in session["events"] if event["type"] == "tool" and event["name"] == name][-1]


async def test_a_shortened_write_result_never_carries_part_of_the_token(runtime):
    manager, session, project = runtime
    tools = manager.session_tools(session)
    leaks = []
    for pad in range(850, 1050, 3):  # The token crosses shorten_result's 1,000-character head cut.
        name = f"settings{pad}.py"
        (project / name).write_text("HOST = 'x'\n" + "#" * pad + "\n" + f'TOKEN = "{TOKEN}"\n'
                                    + "".join(f"V{number} = {number}\n" for number in range(200)))
        await manager.execute_tool(session, tools, "read_file", {"path": name}, f"r{pad}")
        output = await manager.execute_tool(session, tools, "write_file", {
            "path": name, "content": "HOST = 'y'\nTOKEN = os.environ['T']\n"}, f"w{pad}", allowance=ResultAllowance(10))
        event = card(session, "write_file")
        assert event.get("result_shortened") is True
        leaks += [(pad, n) for n in (piece(output), piece(event["output"]), piece(event.get("model_output", ""))) if n]
    assert leaks == []


def test_a_token_at_the_hard_cap_is_redacted_before_the_cut():
    result = {"content": "a" * 85_000 + TOKEN + "b" * 200_000}
    capped = bound_result(redact_value(result, [TOKEN]), TOOL_RESULT_HARD_LIMIT)
    assert piece(str(capped)) == 0
    assert redact_value({"a": [TOKEN, 1, None], "b": {"c": "x" + TOKEN}}, ["", TOKEN]) == {
        "a": ["[REDACTED]", 1, None], "b": {"c": "x[REDACTED]"}}


@pytest.mark.parametrize("name, content", [
    # The token starts at column 140 of the entry, across outline_file's 160-character cut.
    ("client.py", 'def connect(host="' + "h" * 101 + f'", token="{TOKEN}"):\n    pass\n'),
    ("conf.yaml", 'jdbc_url: "jdbc:databricks://adb-' + "1" * 48
     + f'.azuredatabricks.net:443/default;AuthMech=3;UID=token;PWD={TOKEN}"\n'),
])
async def test_an_outline_entry_cut_hides_the_token(runtime, name, content):
    manager, session, project = runtime
    (project / name).write_text(content)
    output = await manager.execute_tool(session, manager.session_tools(session), "outline_file", {"path": name}, "o1")
    assert "[REDACTED]" in output and piece(output) == 0 and piece(card(session, "outline_file")["output"]) == 0


async def test_a_search_excerpt_cut_hides_the_token(runtime):
    manager, session, project = runtime
    (project / "s.py").write_text('URL = "' + "u" * 264 + f'" + "{TOKEN}"\n')  # Across the 300-byte excerpt cut.
    output = await manager.execute_tool(session, manager.session_tools(session), "search_files",
                                        {"query": "URL", "path": ".", "context_lines": 1}, "s1")
    assert "[REDACTED]" in output and piece(output) == 0 and piece(card(session, "search_files")["output"]) == 0
