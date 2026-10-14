import asyncio
import json
import time
from pathlib import Path

import pytest

from local_agent import search
from local_agent import tools as tools_module
from local_agent.tools import WorkspaceTools


def size(result):
    return len(json.dumps(result, indent=2).encode("utf-8"))


async def test_large_external_listing_pages_without_losing_entries(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    external = tmp_path / ("long-backup-path-" * 8)
    external.mkdir()
    for index in range(110):
        (external / f"{index:03d}-session-日😀.jsonl").touch()
    (external / ".env").write_text("excluded")
    tools = WorkspaceTools(str(workspace), allowed_directories=[str(external)])
    expected = tools.list_files(str(external))
    assert size(expected) > tools_module.QUERY_OUTPUT_LIMIT
    entries, offset, pages = [], 0, 0
    while True:
        page = await tools.execute("list_files", {"path": str(external), "offset": offset})
        assert size(page) <= tools_module.QUERY_OUTPUT_LIMIT
        assert not page["listing_limit_reached"]
        entries.extend(page["entries"])
        pages += 1
        if page["next_offset"] is None:
            assert not page["truncated"]
            break
        assert page["truncated"] and page["next_offset"] > offset
        offset = page["next_offset"]
    assert pages > 1
    assert entries == expected
    assert all(entry["name"] != ".env" for entry in entries)


def test_listing_reports_existing_discovery_cap(tmp_path):
    for index in range(301):
        (tmp_path / f"{index:03d}.txt").touch()
    tools = WorkspaceTools(str(tmp_path))
    page = tools.list_files_page(offset=299)
    assert [entry["name"] for entry in page["entries"]] == ["299.txt"]
    assert page["listing_limit_reached"] and page["next_offset"] is None
    assert len(tools.list_files()) == 300  # Explorer retains its existing list shape.


def test_listing_final_page_metadata_fits_byte_limit(tmp_path, monkeypatch):
    (tmp_path / "file.txt").touch()
    tools = WorkspaceTools(str(tmp_path))
    final_size = size(tools.list_files_page())
    monkeypatch.setattr(tools_module, "QUERY_OUTPUT_LIMIT", final_size - 1)
    with pytest.raises(ValueError, match="file entry exceeds"):
        tools.list_files_page()


@pytest.mark.parametrize("offset", [-1, 301, True, "1"])
def test_listing_rejects_invalid_page_offsets(tmp_path, offset):
    with pytest.raises(ValueError, match="offset"):
        WorkspaceTools(str(tmp_path)).list_files_page(offset=offset)


@pytest.mark.parametrize("trailing_newline", [False, True])
def test_range_page_boundaries_and_eof(tmp_path, trailing_newline):
    (tmp_path / "lines.txt").write_text("one\ntwo\nthree" + ("\n" if trailing_newline else ""))
    tools = WorkspaceTools(str(tmp_path))
    first = tools.read_file_range("lines.txt", max_lines=2)
    assert first == {"path": "lines.txt", "start_line": 1, "end_line": 2,
                     "content": "1: one\n2: two", "next_line": 3, "truncated": True}
    last = tools.read_file_range("lines.txt", start_line=first["next_line"], max_lines=1)
    assert last == {"path": "lines.txt", "start_line": 3, "end_line": 3,
                    "content": "3: three", "next_line": None, "truncated": False}
    beyond = tools.read_file_range("lines.txt", start_line=10)
    assert beyond["content"] == "" and beyond["end_line"] == 9 and beyond["next_line"] is None


def test_empty_range(tmp_path):
    (tmp_path / "empty").touch()
    result = WorkspaceTools(str(tmp_path)).read_file_range("empty")
    assert result["content"] == "" and result["end_line"] == 0
    assert not result["truncated"]


def test_empty_range_metadata_also_respects_output_limit(tmp_path):
    (tmp_path / "empty").touch()
    with pytest.raises(ValueError, match="line offset"):
        WorkspaceTools(str(tmp_path)).read_file_range("empty", start_line=10 ** 4100)


def test_large_range_streams_without_changing_raw_editor_limit(tmp_path, monkeypatch):
    target = tmp_path / "large.txt"
    target.write_text("ordinary line\n" * 10_000)
    tools = WorkspaceTools(str(tmp_path))
    with pytest.raises(ValueError, match="80 KB"):
        tools.read_file("large.txt")

    def no_whole_text(*args, **kwargs):
        raise AssertionError("Ranged reads must not call Path.read_text")

    monkeypatch.setattr(Path, "read_text", no_whole_text)
    result = tools.read_file_range("large.txt", start_line=9000, max_lines=2)
    assert result["content"] == "9000: ordinary line\n9001: ordinary line"
    assert result["next_line"] == 9002


def test_range_unicode_output_budget_and_complete_line_pagination(tmp_path):
    lines = [f"{number} " + "😀日" * 20 for number in range(300)]
    (tmp_path / "unicode.txt").write_text("\n".join(lines))
    tools = WorkspaceTools(str(tmp_path))
    first = tools.read_file_range("unicode.txt", max_lines=1000)
    assert size(first) <= tools_module.QUERY_OUTPUT_LIMIT
    assert first["truncated"] and first["next_line"] == first["end_line"] + 1
    second = tools.read_file_range("unicode.txt", start_line=first["next_line"], max_lines=1)
    assert second["content"] == f"{first['next_line']}: {lines[first['next_line'] - 1]}"


@pytest.mark.parametrize("arguments", [{"start_line": 0}, {"start_line": True}, {"start_line": "1"},
                                       {"max_lines": 0}, {"max_lines": 2001}, {"max_lines": False},
                                       {"tail_lines": 0}, {"tail_lines": 2001}, {"tail_lines": True}])
def test_range_parameter_validation(tmp_path, arguments):
    with pytest.raises(ValueError):
        WorkspaceTools(str(tmp_path)).read_file_range("missing", **arguments)


def test_range_rejects_binary_and_replaces_invalid_utf8(tmp_path):
    (tmp_path / "bad").write_bytes(b"hello\x00binary")
    with pytest.raises(ValueError, match="Binary files"):
        WorkspaceTools(str(tmp_path)).read_file_range("bad")
    (tmp_path / "latin").write_bytes(b"caf\xe9\nfine\n\xffinvalid\r\n")
    result = WorkspaceTools(str(tmp_path)).read_file_range("latin")
    assert result["content"] == "1: caf\ufffd\n2: fine\n3: \ufffdinvalid"
    assert result["invalid_utf8_lines"] == [1, 3]
    assert result["encoding_note"] == "Some bytes are not valid UTF-8 and are shown as U+FFFD (\ufffd)."
    tail = WorkspaceTools(str(tmp_path)).read_file_range("latin", tail_lines=2)
    assert tail["content"] == "2: fine\n3: \ufffdinvalid" and tail["invalid_utf8_lines"] == [3]
    clean = WorkspaceTools(str(tmp_path)).read_file_range("latin", start_line=2, max_lines=1)
    assert "invalid_utf8_lines" not in clean and "encoding_note" not in clean


def test_range_rejects_nonregular_and_excessive_lines(tmp_path, monkeypatch):
    tools = WorkspaceTools(str(tmp_path))
    with pytest.raises(ValueError, match="regular"):
        tools.read_file_range(".")
    (tmp_path / "line").write_text("x" * 130_000)
    with pytest.raises(ValueError, match="too long"):
        tools.read_file_range("line")
    (tmp_path / "line").write_text("日" * 3000)
    with pytest.raises(ValueError, match="8 KB"):
        tools.read_file_range("line")
    (tmp_path / "line").write_text("abc\n" * 100)
    monkeypatch.setattr(tools_module, "READ_SCAN_LIMIT", 20)
    with pytest.raises(ValueError, match="scanning"):
        tools.read_file_range("line", start_line=20)


def test_query_guards_credentials_aliases_and_external_paths(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    credential = root / "gateway.conf"
    credential.write_text("private needle")
    (root / "alias.txt").hardlink_to(credential)
    external = tmp_path / "external.txt"
    external.write_text("external needle")
    (root / "outside.txt").symlink_to(external)
    (root / ".ENV").write_text("private needle")
    (root / "normal.txt").write_text("public needle")
    (root / ".env.production").symlink_to(root / "normal.txt")
    tools = WorkspaceTools(str(root), str(credential))
    for name in ("gateway.conf", "alias.txt", "outside.txt", ".ENV", ".env.production"):
        with pytest.raises(ValueError):
            tools.read_file_range(name)
    with pytest.raises(ValueError, match="approval"):
        tools.search_files("needle", path=str(tmp_path))
    result = tools.search_files("needle")
    assert [m["path"] for m in result["matches"]] == ["normal.txt"]
    assert result["skipped_files"] == 0  # Excluded and credential files are not part of the search at all.


def test_search_literal_regex_case_and_unicode(tmp_path):
    (tmp_path / "sample.txt").write_text("a.b\naXb\nMIXED\nmixed\nStraße\n")
    tools = WorkspaceTools(str(tmp_path))
    assert [m["line"] for m in tools.search_files("a.b")["matches"]] == [1]
    assert [m["line"] for m in tools.search_files("a.b", regex=True)["matches"]] == [1, 2]
    assert [m["line"] for m in tools.search_files("mixed")["matches"]] == [3, 4]
    assert [m["line"] for m in tools.search_files("mixed", case_sensitive=True)["matches"]] == [4]
    # Literal searches use simple case folding (one character for one), regular expressions full folding.
    assert [m["line"] for m in tools.search_files("STRASSE")["matches"]] == []
    assert [m["line"] for m in tools.search_files("STRAßE")["matches"]] == [5]
    assert [m["line"] for m in tools.search_files("STRASSE", regex=True)["matches"]] == [5]


def test_search_accepts_a_single_file_path_and_honors_glob(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("first\nneedle\nlast\n")
    tools = WorkspaceTools(str(tmp_path))
    result = tools.search_files("needle", path="sample.txt", context_lines=1)
    assert result["matches"] == [{"path": "sample.txt", "line": 2, "text": "needle",
                                   "before": [{"line": 1, "text": "first"}],
                                   "after": [{"line": 3, "text": "last"}], "text_truncated": False}]
    assert tools.search_files("needle", glob="*.py", path="sample.txt")["matches"] == []


def test_search_context_and_deterministic_pagination(tmp_path):
    (tmp_path / "b.txt").write_text("first\nneedle b\nlast\n")
    (tmp_path / "a.txt").write_text("first\nneedle a\nlast\n")
    tools = WorkspaceTools(str(tmp_path))
    first = tools.search_files("needle", "*.txt", ".", context_lines=1, max_results=1)
    assert first["matches"] == [{"path": "a.txt", "line": 2, "text": "needle a",
                                  "before": [{"line": 1, "text": "first"}],
                                  "after": [{"line": 3, "text": "last"}], "text_truncated": False}]
    assert first["next_offset"] == 1 and first["truncated"]
    assert first == tools.search_files("needle", "*.txt", ".", context_lines=1, max_results=1)
    second = tools.search_files("needle", context_lines=1, offset=first["next_offset"], max_results=1)
    assert [m["path"] for m in second["matches"]] == ["b.txt"]
    assert second["next_offset"] is None and not second["truncated"]
    assert tools.search_files("needle", offset=99)["matches"] == []


def test_search_large_files_and_explicit_skips(tmp_path):
    (tmp_path / "large.txt").write_text("filler\n" * 20_000 + "needle\n")
    (tmp_path / "oversize.txt").write_bytes(b"x" * 2_000_001)
    (tmp_path / "binary.txt").write_bytes(b"needle\x00")
    (tmp_path / "invalid.txt").write_bytes(b"needle\xff")
    (tmp_path / ".env").write_text("needle")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "secret").write_text("needle")
    result = WorkspaceTools(str(tmp_path)).search_files("needle")
    assert [(m["path"], m["line"]) for m in result["matches"]] == [("large.txt", 20_001)]
    assert result["skipped_files"] == 1  # Only the oversize file: binary and non-UTF-8 files are not counted.


def test_search_output_budget_and_excerpt_metadata(tmp_path):
    (tmp_path / "many.txt").write_text(("needle 😀" * 100 + "\n") * 150)
    tools = WorkspaceTools(str(tmp_path))
    first = tools.search_files("needle", context_lines=5, max_results=100)
    assert size(first) <= tools_module.QUERY_OUTPUT_LIMIT
    assert 0 < len(first["matches"]) < 100
    assert first["next_offset"] == len(first["matches"])
    assert first["truncated"] and all(m["text_truncated"] for m in first["matches"])
    last = tools.search_files("needle", offset=149)
    assert last["truncated"] and last["next_offset"] is None


@pytest.mark.parametrize("arguments", [{"context_lines": -1}, {"context_lines": 6}, {"offset": -1},
                                       {"offset": 10001}, {"max_results": 0}, {"max_results": 101},
                                       {"regex": "true"}, {"case_sensitive": 1}])
def test_search_parameter_validation(tmp_path, arguments):
    with pytest.raises(ValueError):
        WorkspaceTools(str(tmp_path)).search_files("needle", **arguments)


def test_search_invalid_or_excessive_patterns(tmp_path):
    tools = WorkspaceTools(str(tmp_path))
    with pytest.raises(ValueError, match="empty"):
        tools.search_files("")
    with pytest.raises(ValueError, match="Invalid regular expression"):
        tools.search_files("[", regex=True)
    with pytest.raises(ValueError, match="1,000"):
        tools.search_files("a" * 1001, regex=True)


@pytest.mark.asyncio
async def test_expensive_regex_has_deadline_without_blocking_event_loop(tmp_path):
    (tmp_path / "expensive.txt").write_text("a" * 10_000 + "!")
    tools = WorkspaceTools(str(tmp_path))
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.001)

    heartbeat = asyncio.create_task(ticker())
    started = time.monotonic()
    try:
        with pytest.raises(ValueError, match="Regular expression time limit"):
            await asyncio.wait_for(asyncio.to_thread(tools.search_files, "(a|aa)+$", regex=True), timeout=2)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
    assert time.monotonic() - started < 2
    assert ticks >= 2


def test_a_regex_whose_counted_repeats_expand_too_far_is_refused_before_compiling(tmp_path):
    (tmp_path / "a.txt").write_text("2026-10-13 aaaa\n")
    tools = WorkspaceTools(str(tmp_path))
    for pattern in ("(?:a{3000}){3000}", "(?:x{65535}){65535}", "(?:a{1000}|b{1000}){1000}", "(?:(?:a{100}){100}){100}"):
        started = time.monotonic()
        with pytest.raises(ValueError, match="Invalid regular expression: its counted repeats"):
            tools.search_files(pattern, regex=True)
        assert time.monotonic() - started < 1
    assert len(tools.search_files(r"\d{4}-\d{2}-\d{2} a{4}", regex=True)["matches"]) == 1


@pytest.mark.parametrize("module, constant, value, message", [(tools_module, "SEARCH_FILE_SCAN_LIMIT", 1, "file scan"),
                                                               (search, "WALK_DIR_LIMIT", 0, "Directory scan"),
                                                               (tools_module, "SEARCH_TOTAL_LIMIT", 1, "byte scan"),
                                                               (tools_module, "SEARCH_TIMEOUT", 0, "time limit")])
def test_search_scan_limits_are_explicit_errors(tmp_path, monkeypatch, module, constant, value, message):
    (tmp_path / "a.txt").write_text("normal text")
    (tmp_path / "b.txt").write_text("more text")
    monkeypatch.setattr(module, constant, value)
    with pytest.raises(ValueError, match=message):
        WorkspaceTools(str(tmp_path)).search_files("absent")


def test_binary_reads_count_toward_total_scan_budget(tmp_path, monkeypatch):
    (tmp_path / "binary").write_bytes(b"\x00" * 20)
    monkeypatch.setattr(tools_module, "SEARCH_TOTAL_LIMIT", 10)
    with pytest.raises(ValueError, match="byte scan"):
        WorkspaceTools(str(tmp_path)).search_files("absent")


def test_search_unreadable_walk_raises(tmp_path, monkeypatch):
    def denied(path, *, followlinks, onerror):
        onerror(PermissionError(13, "denied", str(path)))
        return iter(())

    monkeypatch.setattr(tools_module.os, "walk", denied)
    with pytest.raises(PermissionError):
        WorkspaceTools(str(tmp_path)).search_files("needle")


@pytest.mark.parametrize("separator", [" ", "\x0b", "\x0c", "\x1c", "\x85", "\r\n"])
def test_search_and_read_agree_on_line_numbers(tmp_path, separator):
    (tmp_path / "bare-cr").write_bytes(b"one\rtwo\nthree four\nfive TARGET\n")
    tools = WorkspaceTools(str(tmp_path))
    match = tools.search_files("TARGET", path="bare-cr")["matches"][0]
    assert match["line"] == 3
    assert tools.read_file_range("bare-cr")["content"].split("\n")[match["line"] - 1] == "3: five TARGET"

    text = f"alpha{separator}beta\ngamma\nTARGET{separator}x\ndelta\n"
    (tmp_path / "mixed").write_bytes(text.encode("utf-8"))
    match = tools.search_files("TARGET", path="mixed", context_lines=1)["matches"][0]
    page = dict(line.split(": ", 1) for line in tools.read_file_range("mixed")["content"].split("\n"))
    assert page[str(match["line"])] == match["text"]
    for neighbour in (*match["before"], *match["after"]):
        assert page[str(neighbour["line"])] == neighbour["text"]
    assert [item["line"] for item in match["before"] + match["after"]] == [match["line"] - 1, match["line"] + 1]


def test_split_lines():
    assert tools_module.split_lines("") == []
    assert tools_module.split_lines("a\n") == ["a"]
    assert tools_module.split_lines("a\r\n\r\nb") == ["a", "", "b"]
    assert tools_module.split_lines("a\r\r\n") == ["a\r"]
    assert tools_module.split_lines("a\rb c") == ["a\rb c"]


@pytest.mark.parametrize("trailing_newline", [False, True])
def test_tail_of_a_long_file_has_exact_line_numbers(tmp_path, trailing_newline):
    text = "".join(f"line {number}\n" for number in range(1, 100_001))
    (tmp_path / "log.txt").write_text(text if trailing_newline else text[:-1])
    tools = WorkspaceTools(str(tmp_path))
    result = tools.read_file_range("log.txt", tail_lines=3)
    assert result == {"path": "log.txt", "start_line": 99_998, "end_line": 100_000,
                      "content": "99998: line 99998\n99999: line 99999\n100000: line 100000",
                      "next_line": None, "truncated": False, "tail": True}
    small = tmp_path / "small.txt"
    small.write_text("a\r\nb\r\n")
    assert tools.read_file_range("small.txt", tail_lines=5)["content"] == "1: a\n2: b"
    (tmp_path / "empty.txt").touch()
    assert tools.read_file_range("empty.txt", tail_lines=5)["content"] == ""
    with pytest.raises(ValueError, match="either start_line or tail_lines"):
        tools.read_file_range("log.txt", start_line=2, tail_lines=3)


def test_tail_keeps_the_last_lines_that_fit_the_page(tmp_path):
    (tmp_path / "wide.txt").write_text("".join(f"{number} " + "x" * 200 + "\n" for number in range(1, 501)))
    result = WorkspaceTools(str(tmp_path)).read_file_range("wide.txt", tail_lines=200)
    assert result["truncated"] and result["end_line"] == 500
    assert size(result) <= tools_module.QUERY_OUTPUT_LIMIT
    assert result["content"].endswith("500: 500 " + "x" * 200)
    assert result["content"].startswith(f"{result['start_line']}: {result['start_line']} ")


def test_tail_works_on_files_too_large_to_scan_from_the_start(tmp_path, monkeypatch):
    target = tmp_path / "big.log"
    with target.open("w") as output:
        for number in range(1, 400_001):
            output.write(f"{number:08d} {'.' * 41}\n")
    assert target.stat().st_size >= 20_000_000
    tools = WorkspaceTools(str(tmp_path))
    result = tools.read_file_range("big.log", tail_lines=2)
    assert result["content"] == f"399999: 00399999 {'.' * 41}\n400000: 00400000 {'.' * 41}"
    with pytest.raises(ValueError, match="8 MB"):
        tools.read_file_range("big.log", start_line=390_000)
    monkeypatch.setattr(tools_module, "READ_SCAN_LIMIT", 100)
    with pytest.raises(ValueError, match="The last lines exceed 8 MB"):
        tools.read_file_range("big.log", tail_lines=10)
    monkeypatch.setattr(tools_module, "TAIL_FILE_LIMIT", 1000)
    with pytest.raises(ValueError, match="files up to 512 MB"):
        tools.read_file_range("big.log", tail_lines=1)


def test_larger_pages_for_larger_context_budgets(tmp_path):
    (tmp_path / "long.txt").write_text("".join(f"{number} " + "y" * 60 + "\n" for number in range(1, 1001)))
    tools = WorkspaceTools(str(tmp_path))
    default = tools.read_file_range("long.txt")
    large = tools.read_file_range("long.txt", page_bytes=32_000)
    assert size(default) <= 8_000 < size(large) <= 32_000
    assert large["end_line"] > default["end_line"] and large["truncated"]
    assert tools.read_file_range("long.txt", page_bytes=10 ** 9)["end_line"] == large["end_line"]  # Clamped.
    assert tools_module.read_page_bytes(131_072) == 32_000
    assert tools_module.read_page_bytes(64_000) == 16_000
    assert tools_module.read_page_bytes(8_192) == 8_000
    assert tools_module.read_page_bytes("not a number") == 8_000
    assert tools_module.read_page_bytes(None) == 8_000
    with pytest.raises(ValueError, match="32 KB"):
        (tmp_path / "line.txt").write_text("z" * 40_000)
        tools.read_file_range("line.txt", page_bytes=32_000)


async def test_agent_reads_use_the_context_budget(tmp_path, monkeypatch):
    import httpx
    from local_agent.agents import AgentManager
    from local_agent.config import Settings
    from local_agent.store import Store

    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "long.txt").write_text("".join(f"{number} " + "w" * 60 + "\n" for number in range(1, 1001)))
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", context_window=131_072)
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "reads.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    requests = []

    def reply(delta):
        chunk = {"choices": [{"delta": delta, "finish_reason": "tool_calls" if "tool_calls" in delta else "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    async def gateway(request):
        payload = json.loads(request.content)
        if "tools" not in payload:
            return reply({"content": "Title"})
        requests.append(payload)
        if len(requests) == 1:
            return reply({"tool_calls": [{"index": 0, "id": "r1", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": "long.txt"})}}]})
        return reply({"content": "Done."})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(gateway)))
    try:
        await manager.run(session, "Read long.txt")
    finally:
        store.db.close()
    result = next(message for message in requests[-1]["messages"] if message["role"] == "tool")["content"]
    assert 8_000 < len(result.encode()) <= 32_000
    assert json.loads(result)["start_line"] == 1


def test_activity_groups_name_the_large_input_tools():
    from local_agent.activity import FILE_GROUPS
    assert ("outline_file", "Outlined") in FILE_GROUPS and ("project_overview", "Surveyed") in FILE_GROUPS


def test_the_large_input_tools_run_through_workspace_tools(tmp_path):
    (tmp_path / "doc.md").write_text("# Title\n## Part\n")
    tools = WorkspaceTools(str(tmp_path))
    outlined = asyncio.run(tools.execute("outline_file", {"path": "doc.md", "max_level": 1}))
    assert outlined["entries"] == ["1-2 Title"] and outlined["max_level"] == 1
    surveyed = asyncio.run(tools.execute("project_overview", {}))
    assert surveyed["files"] == 1 and surveyed["key_files"] == []
