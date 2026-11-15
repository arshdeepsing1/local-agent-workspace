"""T021: PROGRESS.md and scripts/check_progress.py: the arithmetic, the table, check mode, --write, --json."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_progress.py"
DATE = "2026-11-14"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def sample(sections):
    """Markdown for (title, done_items, pending_items) sections, with placeholder (wrong) numbers."""
    lines = ["# Local Agent Workspace progress", "",
             f"**Overall**: 0% (0 of 0 items) · **Last updated**: {DATE}", "",
             "| Section | Share of app | Implemented | Progress |", "|---|---|---|---|", ""]
    for title, done, pending in sections:
        lines += [f"## {title}", "", "**Implemented**: 0% (0 of 0) · **Share of app**: 0%", ""]
        if done:
            lines += ["### Done", ""] + [f"- [x] {text}" for text in done] + [""]
        if pending:
            lines += ["### Pending", ""] + [f"- [ ] {text}" for text in pending] + [""]
    return "\n".join(lines)


def sized(counts, done_counts=None):
    """Sections 💬 S0, 💬 S1, ... with the given item totals and done counts (default none done)."""
    done_counts = done_counts or [0] * len(counts)
    return [(f"💬 S{i}", [f"s{i} done {n}" for n in range(d)], [f"s{i} todo {n}" for n in range(t - d)])
            for i, (t, d) in enumerate(zip(counts, done_counts))]


def make(tmp_path, text):
    path = tmp_path / "PROGRESS.md"
    path.write_text(text, encoding="utf-8")
    return path


def written(tmp_path, sections):
    path = make(tmp_path, sample(sections))
    assert run("--write", "--date", DATE, str(path)).returncode == 0
    return path


def data(path):
    result = run("--json", str(path))
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def table_rows(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = [line for line in lines if line.startswith("| ") and ord(line[2]) > 127]
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows]


def test_percent_and_overall_round_half_up(tmp_path):
    path = written(tmp_path, [("💬 A", ["a1"], ["a2", "a3"]), ("🌐 B", ["b1", "b2"], ["b3"]),
                              ("📁 C", ["c1"], [f"c{i}" for i in range(2, 9)])])
    report = data(path)
    assert [s["percent"] for s in report["sections"]] == [33, 67, 13]
    assert report["overall"] == {"done": 4, "total": 14, "percent": 29}
    text = path.read_text(encoding="utf-8")
    assert "**Overall**: 29% (4 of 14 items)" in text
    assert "**Implemented**: 13% (1 of 8)" in text
    assert "**Implemented**: 67% (2 of 3)" in text
    assert [row[2] for row in table_rows(path)].count("33%") == 1


@pytest.mark.parametrize("counts, shares", [([1, 1, 1], [34, 33, 33]), ([5, 3, 1], [56, 33, 11])])
def test_shares_use_the_largest_remainder_and_total_100(tmp_path, counts, shares):
    sections = data(written(tmp_path, sized(counts)))["sections"]
    assert [s["share"] for s in sections] == shares
    assert sum(s["share"] for s in sections) == 100


@pytest.mark.parametrize("done, total, filled", [(0, 5, 0), (5, 5, 10), (1, 3, 3), (9, 20, 5), (17, 18, 9)])
def test_the_bar_rounds_the_percent_half_up(tmp_path, done, total, filled):
    path = written(tmp_path, sized([total], [done]))
    assert table_rows(path)[0][3] == "█" * filled + "░" * (10 - filled)


def test_the_table_orders_sections_by_item_count_with_ties_in_file_order(tmp_path):
    path = written(tmp_path, sized([2, 2, 5]))
    assert [row[0] for row in table_rows(path)] == ["💬 S2", "💬 S0", "💬 S1"]
    assert [row[1] for row in table_rows(path)] == ["56%", "22%", "22%"]


def test_the_table_ranks_by_items_even_when_the_rounded_shares_tie(tmp_path):
    path = written(tmp_path, sized([15, 17, 16, 2000]))
    assert [row[1] for row in table_rows(path)] == ["97%", "1%", "1%", "1%"]
    assert [row[0] for row in table_rows(path)] == ["💬 S3", "💬 S1", "💬 S2", "💬 S0"]


def flip_one(path):
    """Mark the first pending item of section A done and move it under Done. Returns its status line number."""
    lines = path.read_text(encoding="utf-8").splitlines()
    status = next(i for i, line in enumerate(lines) if line.startswith("**Implemented**"))
    pending = next(i for i, line in enumerate(lines) if line.startswith("- [ ]"))
    item = lines.pop(pending).replace("- [ ]", "- [x]")
    lines.insert(lines.index("### Done") + 1, item)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return status + 1


def test_a_stale_number_fails_the_check_and_names_the_line(tmp_path):
    path = written(tmp_path, [("💬 A", ["a1"], ["a2", "a3"]), ("🌐 B", ["b1"], ["b2"])])
    assert run(str(path)).returncode == 0
    status_line = flip_one(path)
    result = run(str(path))
    assert result.returncode == 1
    out = result.stdout.splitlines()
    assert out and all(line.startswith("line ") for line in out)
    assert any(line.startswith(f"line {status_line}: ") for line in out)


def test_write_fixes_numbers_and_changes_no_item_or_heading(tmp_path):
    path = written(tmp_path, [("💬 A", ["a1"], ["a2", "a3"]), ("🌐 B", ["b1"], ["b2"])])
    flip_one(path)

    def keep(kind):
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith(kind)]

    items, headings = keep("- ["), keep("#")
    assert run("--write", "--date", DATE, str(path)).returncode == 0
    assert run(str(path)).returncode == 0
    assert keep("- [") == items
    assert keep("#") == headings


STRUCTURAL = {
    "under Pending": sample([("💬 A", ["a1"], ["a2"])]).replace("- [ ] a2", "- [x] a2"),
    "under Done": sample([("💬 A", ["a1"], ["a2"])]).replace("- [x] a1", "- [ ] a1"),
    "no items": sample([("💬 A", ["a1"], []), ("🌐 B", [], [])]),
    "repeated item": sample([("💬 A", ["Same item"], []), ("🌐 B", ["same ITEM"], [])]),
    "emoji": sample([("Chat", ["a1"], [])]),
    "malformed item": sample([("💬 A", ["a1"], ["a2"])]).replace("- [ ] a2", "- [] a2"),
    "repeated section": sample([("💬 A", ["a1"], []), ("💬 A", ["b1"], [])]),
    "outside Done and Pending": sample([("💬 A", ["a1"], ["a2"])]).replace("### Pending", "### Later"),
    "extra status line": sample([("💬 A", ["a1"], ["a2"])]).replace(
        "· **Share of app**: 0%\n", "· **Share of app**: 0%\n**Implemented**: 0% (0 of 0) · **Share of app**: 0%\n", 1),
}


@pytest.mark.parametrize("message", list(STRUCTURAL))
def test_structural_problems_fail_the_check_and_block_write(tmp_path, message):
    path = make(tmp_path, STRUCTURAL[message])
    result = run(str(path))
    assert result.returncode == 1
    assert message in result.stdout
    before = path.read_bytes()
    assert run("--write", "--date", DATE, str(path)).returncode == 1
    assert path.read_bytes() == before


def test_write_sets_the_date_and_a_non_date_fails_the_check(tmp_path):
    path = make(tmp_path, sample(sized([2], [1])))
    assert run("--write", "--date", "2026-01-02", str(path)).returncode == 0
    text = path.read_text(encoding="utf-8")
    assert "**Last updated**: 2026-01-02" in text
    assert run(str(path)).returncode == 0
    path.write_text(re.sub(r"(\*\*Last updated\*\*: ).*", r"\1yesterday", text), encoding="utf-8")
    assert run(str(path)).returncode == 1


def test_a_missing_file_exits_2(tmp_path):
    assert run(str(tmp_path / "nope.md")).returncode == 2


BASE = [("💬 A", ["a1"], ["a2", "a3"]), ("🌐 B", ["b1"], ["b2"])]
STATUS_A = "**Implemented**: 33% (1 of 3) · **Share of app**: 60%"


def drop(prefix):
    return lambda t: "\n".join(line for line in t.split("\n") if not line.startswith(prefix))


def swap_rows(text):
    lines = text.split("\n")
    a, b = (i for i, line in enumerate(lines) if line.startswith("| ") and ord(line[2]) > 127)
    lines[a], lines[b] = lines[b], lines[a]
    return "\n".join(lines)


def after(marker, new):
    return lambda t: t.replace(marker, marker + "\n" + new, 1)


ROW_A = "| 💬 A | 60% | 33% | ███░░░░░░░ |"
MISMATCHES = {
    "stale overall": lambda t: re.sub(r"(\*\*Overall\*\*: )\d+%", r"\g<1>99%", t),
    "stale share": lambda t: t.replace(ROW_A, ROW_A.replace("60%", "59%")),
    "stale percent": lambda t: t.replace(ROW_A, ROW_A.replace("33%", "34%")),
    "stale bar": lambda t: t.replace("███░░░░░░░", "████░░░░░░"),
    "wrong order": swap_rows,
    "missing row": drop("| 🌐 B"),
    "unknown row": after(ROW_A, "| 💬 Z | 0% | 0% | ░░░░░░░░░░ |"),
    "duplicate row": after(ROW_A, ROW_A),
    "no overall": drop("**Overall**"),
    "no table": drop("|"),
    "stale status": lambda t: t.replace(STATUS_A, STATUS_A.replace("1 of 3", "2 of 3")),
    "second status": after(STATUS_A, STATUS_A),
    "star bullet": lambda t: t.replace("- [ ] a2", "* [ ] a2"),
    "empty box": lambda t: t.replace("- [ ] a2", "- [] a2"),
    "no space": lambda t: t.replace("- [x] a1", "- [x]a1"),
    "two spaces": lambda t: t.replace("- [ ] a2", "-  [ ] a2"),
    "indented": lambda t: t.replace("- [ ] a2", "  - [ ] a2"),
    "under the heading": after(STATUS_A, "- [ ] stray"),
    "other heading": lambda t: t.replace("### Pending", "### Later", 1),
    "repeated section": lambda t: t + "\n## 💬 A\n\n### Pending\n\n- [ ] z1\n",
}


@pytest.mark.parametrize("name", list(MISMATCHES))
def test_each_hand_made_mismatch_fails_the_check(tmp_path, name):
    path = written(tmp_path, BASE)
    good = path.read_text(encoding="utf-8")
    assert run(str(path)).returncode == 0
    bad = MISMATCHES[name](good)
    assert bad != good
    path.write_text(bad, encoding="utf-8")
    result = run(str(path))
    assert result.returncode == 1, result.stdout
    assert result.stdout.startswith("line ")


def test_json_on_a_structural_problem_exits_1(tmp_path):
    path = make(tmp_path, STRUCTURAL["under Pending"])
    result = run("--json", str(path))
    assert result.returncode == 1
    assert result.stdout == ""


def test_a_second_write_changes_nothing_even_with_another_date(tmp_path):
    text = sample(BASE).replace("\n## 💬 A\n", "\nA paragraph after the table.\n\n## 💬 A\n", 1)
    text = text.replace("· **Share of app**: 0%\n", "· **Share of app**: 0%\n\nA paragraph inside a section.\n", 1)
    path = make(tmp_path, text)
    assert run("--write", "--date", DATE, str(path)).returncode == 0
    first = path.read_bytes()
    assert b"A paragraph after the table." in first and b"A paragraph inside a section." in first
    result = run("--write", "--date", "2027-01-01", str(path))
    assert result.returncode == 0
    assert "already up to date" in result.stdout
    assert path.read_bytes() == first
    assert DATE in first.decode("utf-8")


def test_trailing_blank_lines_count_as_up_to_date(tmp_path):
    path = written(tmp_path, BASE)
    path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    before = path.read_bytes()
    assert "already up to date" in run("--write", "--date", "2027-01-01", str(path)).stdout
    assert path.read_bytes() == before


def test_a_bad_date_or_write_with_json_is_a_usage_error(tmp_path):
    path = make(tmp_path, sample(BASE))
    before = path.read_bytes()
    assert run("--write", "--date", "tomorrow", str(path)).returncode == 2
    assert run("--write", "--date", "2026-13-45", str(path)).returncode == 2
    assert run("--write", "--json", str(path)).returncode == 2
    assert path.read_bytes() == before


def test_fenced_code_is_ignored(tmp_path):
    text = sample(BASE).replace("### Done\n", "```\n## 💬 Fake\n- [ ] hidden\n**Implemented**: 1% (1 of 1)\n```\n\n### Done\n", 1)
    path = make(tmp_path, text)
    assert run("--write", "--date", DATE, str(path)).returncode == 0
    report = data(path)
    assert [s["title"] for s in report["sections"]] == ["💬 A", "🌐 B"]
    assert report["overall"]["total"] == 5


def test_a_line_separator_inside_an_item_does_not_split_it(tmp_path):
    path = make(tmp_path, sample([("💬 A", ["one\u2028two"], ["p"])]))
    assert run("--write", "--date", DATE, str(path)).returncode == 0
    assert data(path)["overall"]["total"] == 2


def test_an_unreadable_file_names_the_path_and_exits_2(tmp_path):
    result = run(str(tmp_path))
    assert result.returncode == 2 and result.stdout.startswith(f"{tmp_path}: ")
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe\x00 not utf-8 \xc3\x28")
    result = run(str(bad))
    assert result.returncode == 2 and result.stdout.startswith(f"{bad}: ")
    missing = tmp_path / "nope.md"
    assert run(str(missing)).stdout.strip() == f"{missing}: not found"


NEEDLES = ["two-file handoff memory", "critical bug review", "progress tracker", "one context percentage",
           "browser tool", "one new light style", "manual workflow start", "agent harness"]


def test_the_real_progress_file_is_consistent():
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(run("--json").stdout)
    assert len(report["sections"]) >= 8
    assert report["overall"]["total"] >= 60
    lines = (SCRIPT.parents[1] / "PROGRESS.md").read_text(encoding="utf-8").split("\n")
    items = [line.lower() for line in lines if line.startswith("- [")]
    assert len(items) == len(set(items))
    for needle in NEEDLES:
        assert any(needle in item for item in items), needle
