import asyncio
import json
import time
from pathlib import Path

import pytest

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
                                       {"max_lines": 0}, {"max_lines": 1001}, {"max_lines": False}])
def test_range_parameter_validation(tmp_path, arguments):
    with pytest.raises(ValueError):
        WorkspaceTools(str(tmp_path)).read_file_range("missing", **arguments)


@pytest.mark.parametrize("raw", [b"hello\x00binary", b"\xffinvalid"])
def test_range_rejects_binary_and_invalid_utf8(tmp_path, raw):
    (tmp_path / "bad").write_bytes(raw)
    with pytest.raises((ValueError, UnicodeError)):
        WorkspaceTools(str(tmp_path)).read_file_range("bad")


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
    assert result["skipped_files"] == 5


def test_search_literal_regex_case_and_unicode(tmp_path):
    (tmp_path / "sample.txt").write_text("a.b\naXb\nMIXED\nmixed\nStraße\n")
    tools = WorkspaceTools(str(tmp_path))
    assert [m["line"] for m in tools.search_files("a.b")["matches"]] == [1]
    assert [m["line"] for m in tools.search_files("a.b", regex=True)["matches"]] == [1, 2]
    assert [m["line"] for m in tools.search_files("mixed")["matches"]] == [3, 4]
    assert [m["line"] for m in tools.search_files("mixed", case_sensitive=True)["matches"]] == [4]
    assert [m["line"] for m in tools.search_files("STRASSE")["matches"]] == [5]
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
    assert result["skipped_files"] == 4


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


@pytest.mark.parametrize("constant, value, message", [("SEARCH_FILE_SCAN_LIMIT", 1, "file scan"),
                                                       ("SEARCH_DIR_SCAN_LIMIT", 0, "directory scan"),
                                                       ("SEARCH_TOTAL_LIMIT", 1, "byte scan"),
                                                       ("QUERY_TIMEOUT", 0, "time limit")])
def test_search_scan_limits_are_explicit_errors(tmp_path, monkeypatch, constant, value, message):
    (tmp_path / "a.txt").write_text("normal text")
    (tmp_path / "b.txt").write_text("more text")
    monkeypatch.setattr(tools_module, constant, value)
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
