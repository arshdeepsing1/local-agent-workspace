"""find_files: glob patterns at any depth, paging, sorting and the file universe's limits."""
import json
import os

import pytest

from local_agent import search
from local_agent.permissions import READ_TOOLS
from local_agent.tool_profiles import READ_TOOLS as PROFILE_READ_TOOLS
from local_agent.tools import TOOL_DEFINITIONS, QUERY_OUTPUT_LIMIT, WorkspaceTools


@pytest.mark.parametrize("pattern, path, expected", [
    ("src/**/*.test.ts", "src/x.test.ts", True),
    ("src/**/*.test.ts", "src/a/b/c.test.ts", True),
    ("src/**/*.test.ts", "lib/x.test.ts", False),
    ("src/**/*.test.ts", "src/x.ts", False),
    ("*.py", "a.py", True),
    ("*.py", "d/e/f.py", True),
    ("*.py", "a.pyc", False),
    ("a?c", "abc", True),
    ("a?c", "a/c", False),
    ("README.MD", "README.md", True),
    ("README.MD", "docs/readme.md", True),
    ("**/*.{ts,tsx}", "a/b.tsx", True),
    ("**/*.{ts,tsx}", "c.ts", True),
    ("**/*.{ts,tsx}", "c.js", False),
    ("*.{ts,tsx}", "deep/er/c.tsx", True),
    ("src/**.py", "src/a.py", True),
    ("src/**.py", "src/a/b.py", False),
    ("src/**", "src/a/b/c.txt", True),
    ("src/**", "other/a.txt", False),
    ("docs/*.md", "docs/a.md", True),
    ("docs/*.md", "docs/sub/a.md", False),
    ("[abc].txt", "b.txt", True),
    ("[!abc].txt", "d.txt", True),
    ("[!abc].txt", "a.txt", False),
    ("[a-c]x", "bx", True),
    ("file{1", "file{1", True),
    ("a+b(c).txt", "a+b(c).txt", True),
    ("**", "any/where/at/all.txt", True),
    ("./src/**/*.ts", "src/a/b.ts", True),
    ("./*.py", "a.py", True),
    ("./*.py", "d/a.py", False),
    ("././docs/*.md", "docs/a.md", True),
])
def test_glob_table(pattern, path, expected):
    assert (search.compile_glob(pattern).fullmatch(path) is not None) is expected


@pytest.mark.parametrize("pattern, message", [("", "1 to 500"), ("x" * 501, "1 to 500"), ("a\x00b", "1 to 500"),
                                              ("{a,{b,c}}", "Nested"), ("./", "1 to 500"), ("{" + ",".join("abcdefghijklmnopqrstu") + "}", "20")])
def test_invalid_patterns(pattern, message):
    with pytest.raises(ValueError, match=message):
        search.compile_glob(pattern)


def tree(root, names):
    for name in names:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(name)


def test_find_files_lists_matches_at_any_depth_without_secrets_or_generated_folders(tmp_path):
    tree(tmp_path, ["a.py", "pkg/b.py", "pkg/deep/c.py", "pkg/c.txt", ".env", ".env.local", "credentials",
                    "node_modules/x/d.py", ".venv/lib/e.py", "dist/f.py", "__pycache__/g.py", ".secrets/id_rsa"])
    tools = WorkspaceTools(str(tmp_path))
    assert tools.find_files("*.py") == {"files": ["a.py", "pkg/b.py", "pkg/deep/c.py"], "total": 3,
                                        "next_offset": None, "truncated": False}
    everything = tools.find_files("**", max_results=500)["files"]
    assert everything == ["a.py", "pkg/b.py", "pkg/c.txt", "pkg/deep/c.py"]
    assert tools.find_files("*", path="pkg/deep")["files"] == ["pkg/deep/c.py"]
    assert tools.find_files("C.PY", path="pkg/deep/c.py")["files"] == ["pkg/deep/c.py"]


def test_sort_by_modification_time_newest_first(tmp_path):
    tree(tmp_path, ["old.txt", "new.txt", "same-b.txt", "same-a.txt"])
    for name, stamp in (("old.txt", 1_000_000_000), ("new.txt", 1_700_000_000), ("same-a.txt", 1_500_000_000),
                        ("same-b.txt", 1_500_000_000)):
        os.utime(tmp_path / name, (stamp, stamp))
    result = WorkspaceTools(str(tmp_path)).find_files("*.txt", sort="modified")
    assert result["files"] == [{"path": "new.txt", "modified": "2023-11-14T22:13:20Z"},
                               {"path": "same-a.txt", "modified": "2017-07-14T02:40:00Z"},
                               {"path": "same-b.txt", "modified": "2017-07-14T02:40:00Z"},
                               {"path": "old.txt", "modified": "2001-09-09T01:46:40Z"}]


def test_pages_follow_next_offset_past_300_files(tmp_path):
    folder = tmp_path / "many"
    folder.mkdir()
    for index in range(450):
        (folder / f"file-{index:03d}.log").touch()
    tools = WorkspaceTools(str(tmp_path))
    seen, offset, pages = [], 0, 0
    while offset is not None:
        page = tools.find_files("*.log", offset=offset, max_results=500)
        assert len(json.dumps(page, indent=2).encode()) <= QUERY_OUTPUT_LIMIT
        assert page["total"] == 450
        seen += page["files"]
        offset = page["next_offset"]
        pages += 1
    assert pages > 1 and seen == [f"many/file-{index:03d}.log" for index in range(450)]
    late = tools.find_files("*.log", offset=310, max_results=5)
    assert late["files"] == [f"many/file-{index:03d}.log" for index in range(310, 315)]
    assert late["next_offset"] == 315 and late["truncated"]
    assert tools.find_files("*.log", offset=450)["files"] == []
    with pytest.raises(ValueError, match="offset is beyond the 450 matching files."):
        tools.find_files("*.log", offset=451)


def test_listing_limits_are_explicit_errors(tmp_path, monkeypatch):
    tree(tmp_path, [f"{index}.txt" for index in range(6)])
    tools = WorkspaceTools(str(tmp_path))
    monkeypatch.setattr(search, "UNIVERSE_LIMIT", 5)
    with pytest.raises(ValueError, match="More than 5 files under this path; narrow the path."):
        tools.find_files("*.txt")
    with pytest.raises(ValueError, match="More than 5 files"):
        tools.search_files("txt")
    monkeypatch.setattr(search, "UNIVERSE_LIMIT", 200_000)
    monkeypatch.setattr(search, "ENUMERATION_TIMEOUT", 0)
    with pytest.raises(ValueError, match="Listing files took longer than"):
        tools.find_files("*.txt")


@pytest.mark.parametrize("arguments", [{"sort": "size"}, {"include_ignored": "yes"}, {"offset": -1},
                                       {"offset": 200_001}, {"max_results": 0}, {"max_results": 501}])
def test_argument_validation(tmp_path, arguments):
    with pytest.raises(ValueError):
        WorkspaceTools(str(tmp_path)).find_files("*", **arguments)


async def test_find_files_is_a_read_tool_run_through_execute(tmp_path):
    tree(tmp_path, ["src/a.ts", "src/b.tsx"])
    assert "find_files" in READ_TOOLS and "find_files" in PROFILE_READ_TOOLS
    names = [item["function"]["name"] for item in TOOL_DEFINITIONS]
    assert names[names.index("list_files") + 1] == "find_files"
    result = await WorkspaceTools(str(tmp_path)).execute("find_files", {"pattern": "src/*.{ts,tsx}", "sort": "path"})
    assert result["files"] == ["src/a.ts", "src/b.tsx"]


def test_patterns_match_paths_below_path_or_as_results_show_them(tmp_path):
    tree(tmp_path, ["src/app/a.test.ts", "src/b.ts", "lib/c.ts"])
    tools = WorkspaceTools(str(tmp_path))
    expected = ["src/app/a.test.ts", "src/b.ts"]
    assert tools.find_files("**/*.ts", path="src")["files"] == expected
    assert tools.find_files("src/**/*.ts", path="src")["files"] == expected  # As the results show the paths.
    assert tools.find_files("./src/**/*.ts")["files"] == expected
    assert tools.find_files("app/*.ts", path="src")["files"] == ["src/app/a.test.ts"]
    assert tools.find_files(str(tmp_path / "src" / "**" / "*.ts"), path="src")["files"] == expected
    assert tools.find_files("./*.ts", path="src")["files"] == ["src/b.ts"]
    assert tools.find_files("src/*.ts", path="lib")["files"] == []  # Never outside path.
