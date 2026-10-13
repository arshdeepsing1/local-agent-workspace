"""outline_file, the read_file hint and the larger read pages (u-large-inputs item 3)."""
import ast
import json
import time

import httpx
import pytest

from local_agent import outline
from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.outline import outline_file, outline_format
from local_agent.store import Store
from local_agent.tool_errors import classify
from local_agent.tools import WorkspaceTools, default_read_lines, read_page_bytes


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root, WorkspaceTools(str(root))


def write(root, name, text, mode="w"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_bytes(text.encode("utf-8"))
    return name


def entries(tools, name, **options):
    return outline_file(tools, name, **options)["entries"]


def error_kind(call):
    with pytest.raises((ValueError, OSError)) as caught:
        call()
    return classify(caught.value, str(caught.value))[0], str(caught.value)


# --- Markdown -----------------------------------------------------------------------------------

def test_markdown_headings_with_section_ends(workspace):
    root, tools = workspace
    text = ("---\ntitle: notes\n---\n# Title #\nIntro\n## Part one\ntext\n### Detail\nmore\n\n---\n"
            "Second part\n-----------\nbody\nTop again\n=========\nend\n")
    write(root, "doc.md", text)
    assert entries(tools, "doc.md") == ["4-14 Title", "6-11   Part one", "8-11     Detail", "12-14   Second part",
                                        "15-17 Top again"]
    assert entries(tools, "doc.md", max_level=2) == ["4-14 Title", "6-11   Part one", "12-14   Second part",
                                                     "15-17 Top again"]
    result = outline_file(tools, "doc.md", max_level=1)
    assert result["max_level"] == 1 and result["total_entries"] == 2 and result["format"] == "markdown"


def test_fenced_blocks_are_not_headings(workspace):
    root, tools = workspace
    write(root, "f.md", "# A\n````\n# not\n```\n# still not\n````\n~~~\n## no\n~~~\n## B\n")
    assert entries(tools, "f.md") == ["1-10 A", "10-10   B"]


def test_line_numbers_match_read_file_with_crlf_and_bare_cr(workspace):
    root, tools = workspace
    write(root, "w.md", "# One\r\nx\ry\r\n## Two\r\n")
    assert entries(tools, "w.md") == ["1-3 One", "3-3   Two"]
    page = tools.read_file_range("w.md")
    assert page["content"].split("\n")[2] == "3: ## Two"


def test_a_very_long_heading_line_is_not_an_entry(workspace):
    root, tools = workspace
    write(root, "long.md", "#" + " x" * 2500 + "\n# Short\n")
    assert entries(tools, "long.md") == ["2-2 Short"]


def test_a_large_handoff_pages_every_entry_exactly_once(workspace):
    root, tools = workspace
    sections = []
    for number in range(1, 1201):
        sections.append(f"## Section {number} " + "word " * 20 + "\n" + ("Body line with details.\n" * 12))
    write(root, "handoff.md", "# Handoff\n\n" + "".join(sections))
    assert (root / "handoff.md").stat().st_size > 400_000
    seen, offset, pages = [], 0, 0
    while offset is not None:
        result = outline_file(tools, "handoff.md", offset=offset, page_bytes=8_000)
        assert len(json.dumps(result, indent=2).encode()) <= 8_000
        seen += result["entries"]
        offset, pages = result["next_offset"], pages + 1
    assert pages > 1 and len(seen) == 1201 == len(set(seen))
    assert seen[0].startswith("1-") and seen[-1].startswith(seen[-1].split("-")[0])


# --- Python ----------------------------------------------------------------------------------

PYTHON = '''import typing

@decorator
@other(1)
class Parser:
    """Doc."""

    def parse(self):
        return 1

    async def fetch(self):
        pass

if typing.TYPE_CHECKING:
    def typed():
        pass

try:
    import fast
except ImportError:
    def fallback():
        pass
finally:
    def cleanup():
        pass

match value:
    case 1:
        def matched():
            pass
'''


def test_python_classes_and_functions_from_ast(workspace):
    root, tools = workspace
    write(root, "p.py", PYTHON)
    result = outline_file(tools, "p.py")
    assert result["entries"] == ["3-12 class Parser:", "8-9   def parse(self):", "11-12   async def fetch(self):",
                                 "15-16 def typed():", "21-22 def fallback():", "24-25 def cleanup():",
                                 "29-30 def matched():"]
    assert "note" not in result


def test_end_lines_equal_ast_end_lineno(workspace):
    root, tools = workspace
    write(root, "p.py", PYTHON)
    tree = ast.parse(PYTHON)
    parser = tree.body[1]
    assert entries(tools, "p.py")[0] == f"3-{parser.end_lineno} class Parser:"


def test_python_syntax_errors_fall_back_to_a_line_scan(workspace):
    root, tools = workspace
    write(root, "bad.py", "class A:\n    def f(self):\n        return (\n")
    result = outline_file(tools, "bad.py")
    assert result["entries"] == ["1 class A:", "2   def f(self):"]
    assert result["note"].startswith("Python could not parse this file (line ")
    assert result["note"].endswith("; this outline comes from a line scan.")


def test_a_byte_order_mark_does_not_hide_line_one(workspace):
    root, tools = workspace
    write(root, "bom.py", b"\xef\xbb\xbfdef first():\n    pass\n\nclass A:\n    def f(self):\n        pass\n")
    result = outline_file(tools, "bom.py")
    assert result["entries"] == ["1-2 def first():", "4-6 class A:", "5-6   def f(self):"]
    assert "note" not in result
    write(root, "bom.md", b"\xef\xbb\xbf# Title\ntext\n## Part\n")
    assert entries(tools, "bom.md") == ["1-3 Title", "3-3   Part"]


def test_a_bare_carriage_return_uses_the_line_scan_like_read_file(workspace):
    root, tools = workspace
    write(root, "cr.py", "x = 1\rdef f():\n    pass\n")
    result = outline_file(tools, "cr.py")
    assert result["entries"] == ["1 def f():"] and outline.BARE_CR_NOTE in result["note"]
    assert tools.read_file_range("cr.py")["content"].startswith("1: x = 1\rdef f():")
    write(root, "cr2.py", "x = 1\n\rdef g():\n    pass\n")
    result = outline_file(tools, "cr2.py")
    assert result["entries"] == ["2 def g():"] and outline.BARE_CR_NOTE in result["note"]
    assert tools.read_file_range("cr2.py")["content"].split("\n")[1] == "2: \rdef g():"


def test_large_python_files_and_recursion_errors_use_the_line_scan(workspace, monkeypatch):
    root, tools = workspace
    write(root, "p.py", PYTHON)
    monkeypatch.setattr(outline, "PYTHON_AST_LIMIT", 10)
    result = outline_file(tools, "p.py")
    assert result["note"] == outline.LARGE_PYTHON_NOTE and "1 @decorator" not in result["entries"]
    assert "5 class Parser:" in result["entries"] and "8   def parse(self):" in result["entries"]
    monkeypatch.setattr(outline, "PYTHON_AST_LIMIT", 1_000_000)

    def deep(text):
        raise RecursionError("too deep")
    monkeypatch.setattr(outline.ast, "parse", deep)
    result = outline_file(tools, "p.py")
    assert result["note"] == "Python could not parse this file; this outline comes from a line scan."
    assert "5 class Parser:" in result["entries"]


# --- Line-scan families -------------------------------------------------------------------------

FAMILY_SAMPLES = {
    "app.ts": ("export default class App {\n  constructor(a) {\n    if (a) {\n    }\n  }\n  static async load(x: string): "
               "Promise<void> {\n  }\n}\nexport interface Props {}\nconst go = async (a) => a\nfunction plain() {}\n",
               ["1 export default class App {", "2   constructor(a) {", "6   static async load(x: string): "
                "Promise<void> {", "9 export interface Props {}", "10 const go = async (a) => a",
                "11 function plain() {}"]),
    "main.go": ("package main\n\ntype Server struct {\n}\n\nfunc (s *Server) Run() {\n}\nfunc main() {\n}\n",
                ["3 type Server struct {", "6 func (s *Server) Run() {", "8 func main() {"]),
    "lib.rs": ("pub struct A;\nimpl A {\n    pub(crate) async fn go(&self) {}\n}\n",
               ["1 pub struct A;", "2 impl A {", "3   pub(crate) async fn go(&self) {}"]),
    "Main.java": ("public class Main {\n    public static void main(String[] args) {\n    }\n    private int x;\n}\n",
                  ["1 public class Main {", "2   public static void main(String[] args) {"]),
    # Column-0 control lines (macro bodies, K&R style) reach the keyword filter: not entries.
    "x.c": ("#include <stdio.h>\nstruct point {\n};\nint main(void)\n{\n    if (x) {}\n}\nint declared(void);\n"
            "if (ready)\nwhile (x)\nreturn (value)\nswitch(mode)\n",
            ["2 struct point {", "4 int main(void)"]),
    "a.rb": "module M\n  class C\n    def run\n    end\n  end\nend\n",
    "a.php": "<?php\nfinal class Repo {\n    public static function find() {}\n}\n",
    "run.sh": "#!/bin/sh\nsetup() {\n}\nfunction deploy {\n}\n",
    "schema.sql": "CREATE TABLE users (id int);\ncreate or replace view v as select 1;\n",
    "conf.toml": "[tool.ruff]\nline = 1\n[[servers]]\nname = 'a'\n",
    "ci.yml": "name: ci\non:\n  push:\njobs:\n  test:\n",
    "Makefile": ".PHONY: test\ntest: build\n\tpytest\nbuild:\nVAR := 1\n",
}
FAMILY_EXPECTED = {
    "a.rb": ["1 module M", "2   class C", "3     def run"],
    "a.php": ["2 final class Repo {", "3   public static function find() {}"],
    "run.sh": ["2 setup() {", "4 function deploy {"],
    "schema.sql": ["1 CREATE TABLE users (id int);", "2 create or replace view v as select 1;"],
    "conf.toml": ["1 [tool.ruff]", "3 [[servers]]"],
    "ci.yml": ["1 name: ci", "2 on:", "4 jobs:"],
    "Makefile": ["2 test: build", "4 build:"],
}


@pytest.mark.parametrize("name", sorted(FAMILY_SAMPLES))
def test_each_family_finds_its_declarations(workspace, name):
    root, tools = workspace
    sample = FAMILY_SAMPLES[name]
    text, expected = sample if isinstance(sample, tuple) else (sample, FAMILY_EXPECTED[name])
    write(root, name, text)
    assert entries(tools, name) == expected


def test_formats_by_name():
    assert [outline_format(name) for name in ("a.MD", "b.pyi", "c.mts", "GNUmakefile", "x.mk", "n.ipynb", "data.bin",
                                              "notes.txt")] == [
        "markdown", "python", "javascript", "make", "make", "notebook", "text", "markdown"]


def test_unsupported_types_and_long_entries(workspace):
    root, tools = workspace
    write(root, "data.csv", "a,b\n1,2\n")
    assert outline_file(tools, "data.csv")["note"] == outline.NO_RULES_NOTE
    write(root, "empty.md", "plain text\n")
    assert outline_file(tools, "empty.md")["note"] == outline.NO_ENTRIES_NOTE
    write(root, "wide.md", "# " + "w" * 300 + "\n")
    entry, = entries(tools, "wide.md")
    assert entry == "1-1 " + "w" * 159 + "…"


def test_pathological_files_finish_or_hit_the_deadline_in_time(workspace):
    root, tools = workspace
    write(root, "big.c", ("int x" + ":b" * 498 + "\n") * 7980)
    write(root, "bad.java", (("public " * 6) + "a" * 300 + " b c d e f\n") * 15000)
    write(root, "bad.ts", ("  get " + "a" * 100 + "(" + "x" * 280 + "\n") * 15000)
    for name in ("big.c", "bad.java", "bad.ts"):
        started = time.monotonic()
        try:
            outline_file(tools, name)
        except ValueError as error:
            assert "took longer than" in str(error)
        assert time.monotonic() - started < 4


def test_the_deadline_gives_a_limit_error(workspace, monkeypatch):
    root, tools = workspace
    write(root, "many.md", "# heading\n" * 1000)
    monkeypatch.setattr(outline.tool_rules, "QUERY_TIMEOUT", -1)
    kind, message = error_kind(lambda: outline_file(tools, "many.md"))
    assert kind == "limit" and message == outline.TIMEOUT_MESSAGE


def test_errors_have_their_kinds(workspace, monkeypatch):
    root, tools = workspace
    (root / "folder").mkdir()
    write(root, "n.ipynb", "{}")
    write(root, "bin.md", b"# a\x00b\n")
    write(root, "doc.md", "# A\n# B\n")
    kind, message = error_kind(lambda: outline_file(tools, "folder"))
    assert kind == "wrong_type" and "project_overview" in message
    kind, message = error_kind(lambda: outline_file(tools, "n.ipynb"))
    assert kind == "wrong_type" and "notebook_read" in message
    assert error_kind(lambda: outline_file(tools, "bin.md"))[0] == "wrong_type"
    assert error_kind(lambda: outline_file(tools, "doc.md", offset=3)) == (
        "invalid_arguments", "offset is beyond the 2 outline entries.")
    assert error_kind(lambda: outline_file(tools, "doc.md", max_level=0))[0] == "invalid_arguments"
    assert error_kind(lambda: outline_file(tools, "missing.md"))[0] == "not_found"
    monkeypatch.setattr(outline, "OUTLINE_SCAN_LIMIT", 3)
    kind, message = error_kind(lambda: outline_file(tools, "doc.md"))
    assert kind == "limit" and "8 MB outline limit" in message


def test_invalid_utf8_is_replaced_with_a_note(workspace):
    root, tools = workspace
    write(root, "bad.md", b"# Caf\xe9\n")
    result = outline_file(tools, "bad.md")
    assert result["entries"] == ["1-1 Caf\ufffd"] and "U+FFFD" in result["note"]


# --- read_file: hint, pages and default lines -----------------------------------------------------

def test_read_page_bytes_and_default_lines():
    assert read_page_bytes(131_072) == 32_000 and read_page_bytes(262_143) == 32_000
    assert read_page_bytes(262_144) == 32_768 and read_page_bytes(1_048_576) == 128_000
    assert read_page_bytes(16_384) == 8_000
    assert default_read_lines(32_000) == 400 and default_read_lines(32_768) == 512
    assert default_read_lines(128_000) == 2_000


async def test_a_large_page_without_max_lines_reads_two_thousand_lines(workspace):
    root, tools = workspace
    write(root, "lines.txt", "".join(f"{'x' * 44} {number:04d}\n" for number in range(2000)))
    result = await tools.execute("read_file", {"path": "lines.txt"}, page_bytes=128_000)
    assert result["end_line"] == 2000 and result["next_line"] is None
    small = await tools.execute("read_file", {"path": "lines.txt"}, page_bytes=32_000)
    assert small["end_line"] == 400


def test_the_hint_is_on_the_first_page_of_a_large_structured_file_only(workspace):
    root, tools = workspace
    write(root, "big.md", "# Title\n" + "text line\n" * 10_000)
    write(root, "big.log", "text line\n" * 10_000)
    write(root, "small.md", "# Title\n" + "text\n" * 1000)
    first = tools.read_file_range("big.md", outline_hint=True, page_bytes=32_000)
    assert first["file_bytes"] == (root / "big.md").stat().st_size
    assert first["hint"] == ("This file is 100 KB. outline_file lists its sections with line numbers; read the ones you "
                             "need with start_line and max_lines.")
    assert len(json.dumps(first, indent=2).encode()) <= 32_000
    assert "hint" not in tools.read_file_range("big.md", start_line=2, outline_hint=True, page_bytes=32_000)
    assert "hint" not in tools.read_file_range("big.md", tail_lines=5, outline_hint=True, page_bytes=32_000)
    assert "hint" not in tools.read_file_range("big.log", outline_hint=True, page_bytes=32_000)
    assert "hint" not in tools.read_file_range("small.md", outline_hint=True, page_bytes=32_000)
    assert "hint" not in tools.read_file_range("big.md", page_bytes=32_000)


# --- Through the agent: exclusions, folder access, the overwrite guard and the hint ------------

@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Outline", title_generated=True, permission_mode="manual")
    yield manager, session, project, tmp_path
    store.db.close()


def call_stream(calls):
    parts = [{"index": index, "id": f"c{index}", "function": {"name": name, "arguments": json.dumps(arguments)}}
             for index, (name, arguments) in enumerate(calls)]
    chunk = {"choices": [{"delta": {"tool_calls": parts}, "finish_reason": "tool_calls"}]}
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


def done_stream():
    chunk = {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}]}
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


ORIGINAL_CLIENT = httpx.AsyncClient


def script(monkeypatch, calls):
    requests = []
    client = ORIGINAL_CLIENT

    async def gateway(request):
        requests.append(json.loads(request.content))
        return call_stream(calls) if len(requests) == 1 else done_stream()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(gateway)))
    return requests


def tool_events(session, name):
    return [event for event in session["events"] if event["type"] == "tool" and event["name"] == name]


async def test_outline_file_keeps_the_file_tool_exclusions(agent, monkeypatch):
    manager, session, project, tmp_path = agent
    (project / ".env").write_text("# SECRET=1\n")
    state_file = manager.settings.state_dir / "notes.md"
    state_file.write_text("# Private\n")
    asked = []

    async def decline(session, event):
        asked.append((event["name"], event["input"]))
        return False
    manager.approve = decline
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "o.md").write_text("# Outside\n")
    script(monkeypatch, [("outline_file", {"path": ".env"}), ("outline_file", {"path": str(state_file)}),
                         ("outline_file", {"path": str(outside / "o.md")})])
    await manager.run(session, "Outline them")
    env, state, external = tool_events(session, "outline_file")
    assert env["state"] == "error" and json.loads(env["model_output"])["error_kind"] == "excluded"
    assert state["state"] == "error" and json.loads(state["model_output"])["error_kind"] == "excluded"
    assert external["state"] == "rejected" and asked == [("access_directory", {"path": str(outside)})]
    assert "SECRET" not in json.dumps(session["wire"]) and "Private" not in json.dumps(session["wire"])


async def test_an_outline_is_not_a_read_for_the_overwrite_guard(agent, monkeypatch):
    manager, session, project, _ = agent
    session["permission_mode"] = "bypassPermissions"
    (project / "doc.md").write_text("# Doc\nkeep me\n")
    script(monkeypatch, [("outline_file", {"path": "doc.md"}), ("write_file", {"path": "doc.md", "content": "new"})])
    await manager.run(session, "Replace it")
    outlined, = tool_events(session, "outline_file")
    written, = tool_events(session, "write_file")
    assert outlined["state"] == "completed" and json.loads(outlined["output"])["entries"] == ["1-2 Doc"]
    assert written["state"] == "rejected" and (project / "doc.md").read_text() == "# Doc\nkeep me\n"


async def test_read_file_points_at_outline_file_unless_it_is_turned_off(agent, monkeypatch):
    manager, session, project, _ = agent
    (project / "big.md").write_text("# Title\n" + "text line\n" * 10_000)
    script(monkeypatch, [("read_file", {"path": "big.md"})])
    await manager.run(session, "Read it")
    first, = tool_events(session, "read_file")
    assert "outline_file" in json.loads(first["output"])["hint"]
    session["disabled_tools"] = ["outline_file"]
    requests = script(monkeypatch, [("read_file", {"path": "big.md"})])
    await manager.run(session, "Read it again")
    second = tool_events(session, "read_file")[-1]
    assert "hint" not in json.loads(second["output"])
    assert "outline_file" not in [tool["function"]["name"] for tool in requests[0]["tools"]]


async def test_a_deny_rule_for_outline_file_removes_the_read_file_hint(agent, monkeypatch):
    manager, session, project, _ = agent
    (project / "big.md").write_text("# Title\n" + "text line\n" * 10_000)
    manager.rules.add("project", session["workspace"], {"behavior": "deny", "tool": "outline_file"})
    script(monkeypatch, [("read_file", {"path": "big.md"})])
    await manager.run(session, "Read it")
    read, = tool_events(session, "read_file")
    result = json.loads(read["output"])
    assert read["state"] == "completed" and result["start_line"] == 1 and "hint" not in result


async def test_the_system_message_guidance_follows_the_offered_tools_and_the_input_budget(agent, monkeypatch):
    manager, session, project, _ = agent
    manager.settings.values.update(context_window=65536, max_output_tokens=8192)
    requests = script(monkeypatch, [("list_files", {"path": "."})])
    await manager.run(session, "Hello")
    system = requests[0]["messages"][0]["content"]
    budget = 65536 - 8192 - 2048
    assert "call outline_file" in system and "call project_overview first" in system
    # test-endpoint starts at estimate scale 1.0: 55,296 / 1.25 for read_file's overhead.
    assert ("smaller than about 44,000 bytes (read_file's file_bytes; about a third of your "
            f"{budget:,}-token input budget once read)") in system
    # Turning off read_file also turns off outline_file (it covers it): the guidance leaves it out.
    session["disabled_tools"] = ["read_file"]
    requests = script(monkeypatch, [("list_files", {"path": "."})])
    await manager.run(session, "Hello again")
    system = requests[0]["messages"][0]["content"]
    assert "outline_file" not in system and "call project_overview first" in system


# --- The handoff size in the system message (integration review B, findings 2 and 7) -------------

def markdown_handoff(size):
    """A Markdown handoff of just under size bytes: headings, blank and short lines, quotes and
    backslashes, which read_file's line numbers and JSON escaping make costlier than plain text."""
    words = 'the app keeps state in sqlite and "quoted" paths like src/billing/db.py with \\ escapes — →'.split()
    lines, total, number = [], 0, 0
    while True:
        number += 1
        kind = number % 7
        line = ("" if kind == 0 else "## " + " ".join(words[number % 5:number % 5 + 3]) if kind == 1
                else "    " + " ".join(words[:number % 9 + 2]) if kind == 2
                else "- " + " ".join(words[(number + i) % len(words)] for i in range(4 + number % 21)))
        if total + len(line.encode()) + 1 > size:
            return "\n".join(lines) + "\n"
        lines.append(line)
        total += len(line.encode()) + 1


async def test_a_handoff_of_the_stated_size_costs_about_a_third_of_the_budget_once_read(tmp_path, monkeypatch):
    import re

    from local_agent.context import estimate_scale, prepare_context, scaled_estimate
    monkeypatch.delenv("LOCAL_AGENT_MODEL")  # The Opus 5.5 default (200,000 / 16,384, scale 1.3).
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(tmp_path / "handoff.sqlite3")
    try:
        manager = AgentManager(store, settings)
        session = store.create(settings.values)
        tools = manager.session_tools(session)
        definitions = manager.step_definitions(session, [])
        available = {item["function"]["name"] for item in definitions}
        system, _ = manager.context_inputs(session, tools, "manual", available=available)
        size = int(re.search(r"smaller than about ([\d,]+) bytes \(read_file's file_bytes", system["content"])
                   .group(1).replace(",", ""))
        assert size == 111_000
        (project / "handoff.md").write_text(markdown_handoff(size - 1), encoding="utf-8")
        window, output = manager.request_limits(session)
        budget = manager.input_budget(session)
        scale = estimate_scale([], session["model"])
        wire, state, compactions, start = [{"role": "user", "content": "Resume from handoff.md"}], {}, [], 1
        base = scaled_estimate([system, *wire], definitions, scale)

        async def summarize(previous, chunk, limit):
            return "## Goal and current status\nsummary"
        while start:
            result = tools.read_file_range("handoff.md", start, 2000, page_bytes=read_page_bytes(window))
            call = f"c{start}"
            wire += [{"role": "assistant", "content": None, "tool_calls": [{"id": call, "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": "handoff.md", "start_line": start})}}]},
                     {"role": "tool", "tool_call_id": call, "content": json.dumps(result, indent=2)}]
            _, state, info = await prepare_context(wire, state, system, definitions, window, summarize,
                                                   reply_reserve=output, scale=scale, auto_compact_percent=50)
            compactions += ["compaction" in info]
            start = result["next_line"]
        # Read as advised, it neither reaches the lowest Auto-compact (50%) nor costs much more than a third.
        assert not any(compactions)
        assert scaled_estimate([system, *wire], definitions, scale) - base <= 0.40 * budget
        # The text uses the model's starting scale, so calibration does not change the system message.
        session["inference_calls"] = [{"purpose": "agent", "model": session["model"], "status": "completed",
                                       "estimated_input_tokens": 100_000, "usage": {"input_tokens": 117_000}}]
        assert estimate_scale(session["inference_calls"], session["model"]) == 1.17
        again, _ = manager.context_inputs(session, tools, "manual", available=available)
        assert again["content"] == system["content"]
    finally:
        store.db.close()
