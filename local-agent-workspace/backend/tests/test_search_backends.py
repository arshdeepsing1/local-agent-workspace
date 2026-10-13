"""The file universe (Git-aware listing) and ripgrep as a candidate filter for search_files.

With and without rg, a completed search returns the same result."""
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import regex

from local_agent import search
from local_agent.tools import WorkspaceTools

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
BACKEND = Path(__file__).resolve().parents[1]


def git(folder, *arguments):
    subprocess.run(["git", "-c", "init.defaultBranch=main", *arguments], cwd=folder, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env={"PATH": os.environ["PATH"], "HOME": str(folder), "GIT_CONFIG_NOSYSTEM": "1"})


def make_tree(root):
    """A Git repository with every kind of file the search has to treat alike."""
    root.mkdir()
    files = {
        "a.txt": "needle here\nNEEDLE there\nStraße and strasse\nplain i k s\n",
        "nested/deep/b.py": "def f():\r\n    return 'Needle'\r\n\r\n# a.b(c)[d]{e}^$|#&-~ marker\r\n",
        "unicode.txt": "Kelvin K sign\nsharp ß\ndotted İ capital\ndotless ı small\nlong ſ\n",
        "punct.txt": "x a.b(c)[d]{e}^$|#&-~ y\nA.B(C)[D]{E}^$|#&-~\n",
        "separators.txt": "one needle\x0bK\x0cS\x1cI\x85end\rbare cr needle\n",
        "nested/ignored.txt": "needle that git ignores\n",
        "build/out.txt": "needle in a build folder\n",
        "node_modules/pkg/index.js": "needle in a dependency\n",
        ".hidden/conf.txt": "hidden needle\n",
        ".env": "needle=secret\n",
    }
    for index in range(12):
        files[f"pages/{index:02d}.txt"] = "".join(f"needle {index} {line} i K s\n" for line in range(3))
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, newline="")
    (root / "binary.bin").write_bytes(b"needle\x00binary")
    (root / "latin.txt").write_bytes(b"needle caf\xe9\n")
    (root / "large.txt").write_bytes(b"needle\n" + b"x" * 2_000_001)
    (root / "link.txt").symlink_to("a.txt")
    (root / ".gitignore").write_text("nested/ignored.txt\nbuild/\n")
    git(root, "init", "-q")
    git(root, "add", "a.txt", ".gitignore")
    return root


def run_both(monkeypatch, rg, call):
    """call() without rg, then with rg; also returns rg's exit codes."""
    monkeypatch.setattr(search, "ripgrep", lambda: None)
    without = call()
    codes = []
    real = search.bounded_run

    def recording(argv, *args, **kwargs):
        result = real(argv, *args, **kwargs)
        if argv[0] == rg:
            codes.append(result[0])
        return result
    monkeypatch.setattr(search, "bounded_run", recording)
    monkeypatch.setattr(search, "ripgrep", lambda: rg)
    try:
        with_rg = call()
    finally:
        monkeypatch.setattr(search, "bounded_run", real)
    return without, with_rg, codes


def outcome(call):
    try:
        return call()
    except ValueError as error:
        return f"error: {error}"


QUERIES = ["i", "K", "S", "s", "strasse", "STRAßE", "needle", "Needle", "a.b(c)[d]{e}^$|#&-~", "ß", "k sign", "x"]


@needs_git
@pytest.mark.parametrize("case_sensitive", [False, True])
@pytest.mark.parametrize("output_mode", ["content", "files_with_matches", "count"])
def test_results_are_the_same_with_and_without_ripgrep(tmp_path, monkeypatch, real_ripgrep, case_sensitive, output_mode):
    root = make_tree(tmp_path / "repo")
    tools = WorkspaceTools(str(root))
    trusted = []
    for query in QUERIES:
        for offset in (0, 2, 5):
            def call():
                return outcome(lambda: tools.search_files(query, case_sensitive=case_sensitive, output_mode=output_mode,
                                                          offset=offset, max_results=4, context_lines=1))
            without, with_rg, codes = run_both(monkeypatch, real_ripgrep, call)
            assert with_rg == without, (query, offset)
            trusted += [code for code in codes if code in (0, 1)]
    assert trusted  # rg really ran and was trusted, so the comparison means something.
    sample = tools.search_files("needle", output_mode="files_with_matches", max_results=100)
    assert "nested/ignored.txt" not in sample["files"] and "build/out.txt" not in sample["files"]
    assert "link.txt" in sample["files"] and ".hidden/conf.txt" in sample["files"]
    assert sample["skipped_files"] == 1  # large.txt; binary and Latin-1 files are not counted.


@needs_git
def test_case_insensitive_literals_use_simple_folding_in_both_modes(tmp_path, monkeypatch, real_ripgrep):
    root = make_tree(tmp_path / "repo")
    tools = WorkspaceTools(str(root))
    for query, expected in (("k sign", [("unicode.txt", 1)]), ("LONG S", [("unicode.txt", 5)]),
                            ("SHARP SS", []), ("dotted i", [("unicode.txt", 3)]), ("DOTLESS I", [("unicode.txt", 4)])):
        without, with_rg, _ = run_both(monkeypatch, real_ripgrep, lambda: tools.search_files(query))
        assert with_rg == without
        assert [(match["path"], match["line"]) for match in without["matches"]] == expected


def test_lines_split_only_at_lf_in_search_and_read(tmp_path):
    (tmp_path / "f.txt").write_bytes("one two\x0bthree\x0c\x1c\x85four\rfive\n".encode() + b"six needle\r\n")
    tools = WorkspaceTools(str(tmp_path))
    match = tools.search_files("needle")["matches"][0]
    assert match["line"] == 2 and match["text"] == "six needle"
    assert tools.read_file_range("f.txt")["content"].split("\n")[1] == "2: six needle"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read files without read permission")
def test_unreadable_files_make_ripgrep_fall_back_with_the_same_result(tmp_path, monkeypatch, real_ripgrep):
    (tmp_path / "a.txt").write_text("needle\n")
    secret = tmp_path / "locked.txt"
    secret.write_text("needle\n")
    secret.chmod(0)
    tools = WorkspaceTools(str(tmp_path))
    try:
        without, with_rg, codes = run_both(monkeypatch, real_ripgrep, lambda: tools.search_files("needle"))
    finally:
        secret.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert codes == [2]
    assert with_rg == without and without["skipped_files"] == 1


def test_ripgrep_pattern_matches_every_simple_case_variant(tmp_path, real_ripgrep):
    classes = search.fold_classes()
    for code in range(32, 127):
        character = chr(code)
        query = f"<{character}>"
        variants = classes.get(character, character)
        lines = [f"<{variant}>" for variant in variants]
        python = regex.compile(regex.escape(query), regex.IGNORECASE | regex.VERSION0)
        assert all(python.search(line) for line in lines)
        target = tmp_path / f"{code}.txt"
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        found = subprocess.run([real_ripgrep, "--no-config", "--count", "--encoding", "none", "-e",
                                search.rg_pattern(query), "--", str(target)], capture_output=True, text=True)
        assert found.returncode == 0, (character, found.stderr)
        assert int(found.stdout.strip()) == len(lines), character


def test_case_fold_classes_contain_everything_python_equates_and_nothing_astral():
    classes = search.fold_classes()
    everything = "".join(chr(code) for code in range(0x110000) if not 0xD800 <= code <= 0xDFFF)
    for letter in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
        matcher = regex.compile(letter, regex.IGNORECASE | regex.VERSION0)
        equal = set(matcher.findall(everything))
        assert equal == set(classes[letter])
        assert all(ord(character) < 0x10000 for character in equal)
    assert set(classes["k"]) == set(classes["K"]) == {"k", "K", "\u212a"}
    assert set(classes["s"]) == set(classes["S"]) == {"s", "S", "\u017f"}
    assert {"i", "I"} <= set(classes["i"]) and {"i", "I"} <= set(classes["I"])
    pattern = search.rg_pattern("a.b-1")
    assert pattern == f"[{classes['a']}]\\.[{classes['b']}]\\-1"


def fake_rg(tmp_path, body):
    script = tmp_path / "bin" / "rg"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!{sys.executable}\nimport os, sys, time\n{body}\n")
    script.chmod(0o755)
    return str(script)


def small_tree(root):
    root.mkdir()
    (root / "a.txt").write_text("needle one\n")
    (root / "b.txt").write_text("other\n")
    (root / "c.txt").write_text("NEEDLE two\n")
    (root / "linked.txt").symlink_to("c.txt")
    return WorkspaceTools(str(root))


def test_an_extra_candidate_from_rg_does_not_change_results(tmp_path, monkeypatch):
    tools = small_tree(tmp_path / "work")
    expected = tools.search_files("needle")
    rg = fake_rg(tmp_path, "sys.stdout.write('./a.txt\\0./b.txt\\0./c.txt\\0'); sys.exit(0)")
    monkeypatch.setattr(search, "ripgrep", lambda: rg)
    assert tools.search_files("needle") == expected
    assert [match["path"] for match in expected["matches"]] == ["a.txt", "c.txt", "linked.txt"]


def test_symbolic_links_are_searched_although_rg_skips_them(tmp_path, monkeypatch):
    tools = small_tree(tmp_path / "work")
    rg = fake_rg(tmp_path, "sys.stdout.write('./c.txt\\0'); sys.exit(0)")
    monkeypatch.setattr(search, "ripgrep", lambda: rg)
    assert [match["path"] for match in tools.search_files("needle")["matches"]] == ["c.txt", "linked.txt"]


def test_rg_errors_make_python_read_every_file(tmp_path, monkeypatch):
    tools = small_tree(tmp_path / "work")
    expected = tools.search_files("needle")
    rg = fake_rg(tmp_path, "sys.exit(2)")
    monkeypatch.setattr(search, "ripgrep", lambda: rg)
    assert tools.search_files("needle") == expected
    rg = fake_rg(tmp_path, "sys.exit(1)")  # No candidates: only symbolic links are read.
    monkeypatch.setattr(search, "ripgrep", lambda: rg)
    assert [match["path"] for match in tools.search_files("needle")["matches"]] == ["linked.txt"]


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/stat")
    try:
        return status.read_text().rsplit(")", 1)[1].split()[0] != "Z"  # A zombie has ended.
    except (OSError, IndexError):
        return True


def test_a_hanging_rg_times_out_falls_back_and_its_group_is_killed(tmp_path, monkeypatch):
    tools = small_tree(tmp_path / "work")
    expected = tools.search_files("needle")
    marker = tmp_path / "child.pid"
    rg = fake_rg(tmp_path, "import subprocess\n"
                           f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                           f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
                           "time.sleep(30)")
    monkeypatch.setattr(search, "ripgrep", lambda: rg)
    monkeypatch.setattr(search, "RG_TIMEOUT", 0.5)
    started = time.monotonic()
    assert tools.search_files("needle") == expected
    assert time.monotonic() - started < 5
    pid = int(marker.read_text())
    deadline = time.monotonic() + 3
    while process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not process_alive(pid)


def test_bounded_run_stops_output_past_its_limit(tmp_path):
    code, output, overflow = search.bounded_run(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 300000)"], tmp_path, None, 10, 1000)
    assert overflow and len(output) <= 1000
    code, output, overflow = search.bounded_run([sys.executable, "-c", "print('hi')"], tmp_path, None, 10, 1000)
    assert (code, output, overflow) == (0, b"hi\n", False)
    code, _, _ = search.bounded_run([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, None, 0.3, 1000)
    assert code is None


@needs_git
def test_gitignored_files_are_left_out_unless_asked(tmp_path):
    root = make_tree(tmp_path / "repo")
    tools = WorkspaceTools(str(root))
    assert "nested/ignored.txt" not in tools.find_files("*.txt")["files"]
    assert not [m for m in tools.search_files("git ignores")["matches"]]
    assert "nested/ignored.txt" in tools.find_files("*.txt", include_ignored=True)["files"]
    assert [m["path"] for m in tools.search_files("git ignores", include_ignored=True)["matches"]] == ["nested/ignored.txt"]
    # Named explicitly, an ignored file is searched.
    assert tools.search_files("git ignores", path="nested/ignored.txt")["matches"]
    # The fixed exclusions apply either way.
    for include in (False, True):
        files = tools.find_files("*", include_ignored=include, max_results=500)["files"]
        assert not [name for name in files if "node_modules" in name or name.endswith(".env")]


def test_folders_outside_git_are_walked_without_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("skipped.txt\n")
    (tmp_path / "skipped.txt").write_text("needle\n")
    tools = WorkspaceTools(str(tmp_path))
    result = tools.find_files("*.txt")
    assert result["files"] == ["skipped.txt"] and "note" not in result


@needs_git
def test_untracked_nested_repositories_are_listed(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "top.txt").write_text("needle\n")
    inner = root / "vendor" / "lib"
    inner.mkdir(parents=True)
    git(inner, "init", "-q")
    (inner / ".gitignore").write_text("inner.txt\n")  # Not applied: nested repositories are walked.
    (inner / "inner.txt").write_text("needle\n")
    tools = WorkspaceTools(str(root))
    assert tools.find_files("*.txt")["files"] == ["top.txt", "vendor/lib/inner.txt"]
    assert [m["path"] for m in tools.search_files("needle")["matches"]] == ["top.txt", "vendor/lib/inner.txt"]


@needs_git
def test_a_repository_above_the_permitted_folder_is_not_used(tmp_path):
    outer = tmp_path / "home"
    outer.mkdir()
    git(outer, "init", "-q")
    (outer / ".gitignore").write_text("*\n")  # A dotfiles repository that ignores everything.
    project = outer / "project"
    granted = outer / "granted"
    for folder in (project, granted):
        folder.mkdir()
        (folder / "visible.txt").write_text("needle\n")
    tools = WorkspaceTools(str(project), allowed_directories=[str(granted)])
    assert tools.find_files("*.txt")["files"] == ["visible.txt"]
    assert tools.find_files("*.txt", path=str(granted))["files"] == [str(granted / "visible.txt")]
    assert [m["path"] for m in tools.search_files("needle", path=str(granted))["matches"]] == [str(granted / "visible.txt")]


@needs_git
def test_a_failing_git_listing_falls_back_with_a_note(tmp_path, monkeypatch):
    root = make_tree(tmp_path / "repo")
    real = search.bounded_run

    def failing(argv, *args, **kwargs):
        if "ls-files" in argv:
            return 128, b"", False
        return real(argv, *args, **kwargs)
    monkeypatch.setattr(search, "bounded_run", failing)
    tools = WorkspaceTools(str(root))
    found = tools.find_files("*.txt", max_results=500)
    assert found["note"] == "Git could not list the files here, so .gitignore was not applied."
    assert "nested/ignored.txt" in found["files"]
    assert tools.search_files("needle")["note"] == found["note"]


@pytest.mark.parametrize("use_git", [False, True])
def test_the_credential_file_and_its_hard_links_are_never_listed(tmp_path, use_git):
    if use_git and not shutil.which("git"):
        pytest.skip("git is not installed")
    root = tmp_path / "work"
    root.mkdir()
    credential = root / "databricks.cfg"
    credential.write_text("token = needle\n")
    (root / "copy.cfg").hardlink_to(credential)
    (root / "public.cfg").write_text("needle\n")
    if use_git:
        git(root, "init", "-q")
        git(root, "add", ".")
    tools = WorkspaceTools(str(root), str(credential))
    assert tools.find_files("*.cfg")["files"] == ["public.cfg"]
    assert [m["path"] for m in tools.search_files("needle")["matches"]] == ["public.cfg"]
    assert tools.search_files("needle", output_mode="count")["counts"] == [{"path": "public.cfg", "count": 1}]


@pytest.mark.parametrize("modules", ["local_agent.search, local_agent.tools", "local_agent.tools, local_agent.search",
                                     "local_agent.git_tools, local_agent.search"])
def test_search_and_tools_import_in_either_order(modules):
    subprocess.run([sys.executable, "-c", f"import {modules}"], cwd=BACKEND, check=True)


def test_output_modes_page_by_files_and_count_matches(tmp_path):
    for index in range(5):
        (tmp_path / f"{index}.txt").write_text("needle\n" * (index + 1))
    tools = WorkspaceTools(str(tmp_path))
    files = tools.search_files("needle", output_mode="files_with_matches", max_results=2)
    assert files == {"files": ["0.txt", "1.txt"], "next_offset": 2, "truncated": True, "skipped_files": 0}
    counts = tools.search_files("needle", output_mode="count", offset=2, max_results=2)
    assert counts["counts"] == [{"path": "2.txt", "count": 3}, {"path": "3.txt", "count": 4}]
    assert counts["total_matches"] == 1 + 2 + 3 + 4 and counts["next_offset"] == 4
    last = tools.search_files("needle", output_mode="count", offset=4)
    assert last["counts"] == [{"path": "4.txt", "count": 5}] and last["total_matches"] == 15 and last["next_offset"] is None
    content = tools.search_files("needle", offset=14)
    assert [(m["path"], m["line"]) for m in content["matches"]] == [("4.txt", 5)]
    with pytest.raises(ValueError, match="output_mode"):
        tools.search_files("needle", output_mode="lines")


def test_scan_limits_count_files_that_are_never_read(tmp_path, monkeypatch):
    from local_agent import tools as tools_module
    (tmp_path / "a.txt").write_text("needle\n")
    (tmp_path / "b.txt").write_text("other\n" * 10)
    tools = WorkspaceTools(str(tmp_path))
    rg = fake_rg(tmp_path.parent / f"{tmp_path.name}-bin", "sys.stdout.write('./a.txt\\0'); sys.exit(0)")
    monkeypatch.setattr(search, "ripgrep", lambda: rg)
    monkeypatch.setattr(tools_module, "SEARCH_TOTAL_LIMIT", 20)
    with pytest.raises(ValueError, match="byte scan"):
        tools.search_files("needle")  # b.txt is not a candidate, but it counts.
    monkeypatch.setattr(tools_module, "SEARCH_TOTAL_LIMIT", 64_000_000)
    monkeypatch.setattr(tools_module, "SEARCH_FILE_SCAN_LIMIT", 1)
    with pytest.raises(ValueError, match="file scan"):
        tools.search_files("needle")


@needs_git
@pytest.mark.parametrize("target", ["outside", ".git"])
def test_a_git_listed_file_below_a_symlinked_folder_is_never_listed_or_read(tmp_path, monkeypatch, target):
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_text("needle inside\n")
    (root / "readme.txt").write_text("plain\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x")
    shutil.rmtree(root / "sub")
    if target == "outside":
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "a.txt").write_text("needle OUTSIDE-SECRET-VALUE\n")
        (root / "sub").symlink_to(outside)
    else:
        (root / ".git" / "a.txt").write_text("needle [remote] OUTSIDE-SECRET-VALUE\n")
        (root / "sub").symlink_to(".git")
    tools = WorkspaceTools(str(root))
    with pytest.raises(ValueError):
        tools.path("sub/a.txt")
    assert tools.find_files("*.txt")["files"] == ["readme.txt"]
    for regex_mode in (False, True):
        result = tools.search_files("needle", regex=regex_mode)
        assert result["matches"] == [] and result["skipped_files"] == 0
    rg = fake_rg(tmp_path, "sys.stdout.write('./sub/a.txt\\0'); sys.exit(0)")
    monkeypatch.setattr(search, "ripgrep", lambda: rg)
    assert tools.search_files("needle", case_sensitive=True)["matches"] == []
    assert "OUTSIDE-SECRET-VALUE" not in str(tools.search_files("needle", output_mode="count"))


def rename_index_entries(index, names):
    """Rewrite a version 2 Git index so its entries carry other names, as a hand-made index can.
    Git refuses such names when it writes the index, but lists them when it reads one."""
    import hashlib
    import struct
    data = index.read_bytes()
    signature, version, count = struct.unpack(">4sII", data[:12])
    assert (signature, version) == (b"DIRC", 2)
    entries, position = [], 12
    for _ in range(count):
        fixed = data[position:position + 62]
        length = struct.unpack(">H", fixed[60:62])[0] & 0xFFF
        name = data[position + 62:position + 62 + length].decode()
        position += (62 + length + 8) // 8 * 8
        entries.append((names.get(name, name).encode(), fixed))
    body = struct.pack(">4sII", b"DIRC", 2, len(entries))
    for name, fixed in sorted(entries):
        flags = struct.unpack(">H", fixed[60:62])[0] & ~0xFFF | len(name)
        entry = fixed[:60] + struct.pack(">H", flags) + name
        body += entry + b"\0" * (8 - len(entry) % 8)
    index.write_bytes(body + hashlib.sha1(body).digest())


@needs_git
def test_git_index_names_outside_the_folder_are_never_listed_or_read(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("needle OUTSIDE-SECRET-VALUE\n")
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        (root / name).write_text("needle inside\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    rename_index_entries(root / ".git" / "index", {"b.txt": str(outside / "private.txt"), "c.txt": "../outside/private.txt",
                                                   "d.txt": "a/../../outside/private.txt"})
    for name in ("b.txt", "c.txt", "d.txt"):
        (root / name).unlink()
    listed = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout
    assert str(outside / "private.txt") in listed  # Git lists the hand-made names.
    tools = WorkspaceTools(str(root))
    assert tools.find_files("*")["files"] == ["a.txt"]
    monkeypatch.setattr(search, "ripgrep", lambda: None)
    for query, regex_mode in (("needle", False), ("needle", True), (".", True)):
        result = tools.search_files(query, regex=regex_mode)
        assert [match["path"] for match in result["matches"]] == ["a.txt"]
        assert "OUTSIDE-SECRET-VALUE" not in str(result)


@needs_git
@pytest.mark.parametrize("workspace", ["home", "granted"])
def test_a_dotfiles_repository_at_home_never_hides_files_in_home(tmp_path, monkeypatch, workspace):
    home = tmp_path / "home"
    home.mkdir()
    git(home, "init", "-q")
    (home / ".gitignore").write_text("*\n")  # Ignores everything.
    (home / "proj").mkdir()
    (home / "proj" / "app.py").write_text("needle = 1\n")
    monkeypatch.setenv("HOME", str(home))
    if workspace == "home":
        tools = WorkspaceTools(str(home))
        expected = "proj/app.py"
    else:
        (tmp_path / "work").mkdir()
        tools = WorkspaceTools(str(tmp_path / "work"), allowed_directories=[str(home)])
        expected = str(home / "proj" / "app.py")
    for path in (str(home / "proj"), str(home)):
        found = tools.find_files("*.py", path=path)
        assert found["files"] == [expected] and "note" not in found
        assert [m["path"] for m in tools.search_files("needle", path=path)["matches"]] == [expected]


@needs_git
def test_a_workspace_inside_a_larger_repository_says_gitignore_was_not_applied(tmp_path):
    mono = tmp_path / "mono"
    (mono / "app" / "src").mkdir(parents=True)
    (mono / "app" / "build").mkdir()
    (mono / ".gitignore").write_text("build/\n")
    (mono / "app" / "src" / "a.js").write_text("needle\n")
    (mono / "app" / "build" / "bundle.js").write_text("needle\n")
    git(mono, "init", "-q")
    assert WorkspaceTools(str(mono)).find_files("*.js") == {
        "files": ["app/src/a.js"], "total": 1, "next_offset": None, "truncated": False}
    tools = WorkspaceTools(str(mono / "app"))
    found = tools.find_files("*.js")
    assert found["files"] == ["build/bundle.js", "src/a.js"]
    assert found["note"] == search.OUTSIDE_NOTE
    assert "outside" in found["note"] and ".gitignore was not applied" in found["note"]
    assert tools.search_files("needle", output_mode="files_with_matches")["note"] == search.OUTSIDE_NOTE


def test_a_query_rg_cannot_take_falls_back_with_the_same_result(tmp_path, monkeypatch):
    tools = small_tree(tmp_path / "work")
    (tmp_path / "work" / "emoji.txt").write_text("half \U0001f600 here\n")
    for query in ("\ud83d", "needle \udcff"):
        expected = tools.search_files(query, case_sensitive=True)
        rg = fake_rg(tmp_path, "sys.exit(1)")
        monkeypatch.setattr(search, "ripgrep", lambda: rg)
        assert tools.search_files(query, case_sensitive=True) == expected
        monkeypatch.setattr(search, "ripgrep", lambda: None)


def test_rg_candidates_match_files_whatever_case_or_normal_form_rg_prints(tmp_path, monkeypatch):
    import unicodedata
    root = tmp_path / "work"
    root.mkdir()
    composed = unicodedata.normalize("NFC", "café.txt")
    (root / "Readme.md").write_text("needle in readme\n")
    (root / composed).write_text("needle in cafe\n")
    tools = WorkspaceTools(str(root))
    expected = tools.search_files("needle")
    assert [m["path"] for m in expected["matches"]] == ["Readme.md", composed]
    # As rg prints names on a case- and normalization-insensitive file system after a
    # rename: the spelling on disk, not Git's.
    decomposed = unicodedata.normalize("NFD", composed)
    rg = fake_rg(tmp_path, f"sys.stdout.buffer.write(b'./README.md\\0./' + {decomposed.encode()!r} + b'\\0'); sys.exit(0)")
    monkeypatch.setattr(search, "ripgrep", lambda: rg)
    assert tools.search_files("needle") == expected
    assert tools.search_files("needle", case_sensitive=True) == expected
