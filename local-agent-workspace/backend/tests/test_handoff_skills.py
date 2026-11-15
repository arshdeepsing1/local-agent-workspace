import re
from pathlib import Path

import pytest

from local_agent.extensions import SKILL_INDEX_DESCRIPTION, SKILL_LIMIT

SKILLS = Path(__file__).resolve().parents[2] / "skills"
IDS = ["handoff", "resume", "migrate-handoffs"]
FORBIDDEN = [r"/users/", r"/home/", r"[\w.+-]+@[\w-]+\.[\w.]+"]
LOG_SECTIONS = [
    "Header", "WORK DONE", "DECISIONS", "ISSUES -> ROOT CAUSE -> FIX", "CODE AND FILE CHANGES",
    "SUPERSEDED FACTS", "COMMANDS RAN", "USEFUL COMMANDS", "KEY FACTS AND GOTCHAS",
    "FILES & RESOURCES USED", "OPEN ITEMS", "NEXT ACTIONS",
]
BRIEF_SECTIONS = [
    "Header", "WHAT AND WHY", "ARCHITECTURE", "WHERE THINGS LIVE", "CURRENT LIVE STATE",
    "HOW TO WORK ON IT", "CONVENTIONS AND USER PREFERENCES", "GOTCHAS", "OPEN ITEMS",
    "SESSION INDEX", "SUPERSEDED FACTS",
]
HANDOFF_STRINGS = [
    "<project>-00-context.md", "HANDOFF/<project>-handoff-vNN-YYYY-MM-DD-<topic>.md",
    "32 KB", "48 KB", "100 KB", "<!-- activity-log -->", "insert_activity_log", "<redacted>",
    "<!-- handoff continues -->", "None this session", "[verified", "[unverified", "handoffs/auto/",
    "half the file text the system prompt says one response can hold",
    "Please read any referenced files listed above and confirm you are oriented before we begin.",
    "-activity.md", "/migrate-handoffs", "Not a directory", "take the next version",
]
RESUME_STRINGS = ["<project>-00-context.md", "HANDOFF/", "highest", "[unverified]", "Start no work", "-activity.md"]
MIGRATE_STRINGS = ["HANDOFF/", "<project>-00-context.md", "SESSION INDEX", "oldest first", "zero-padded", "move_path"]


def read_skill(skill_id: str) -> str:
    return (SKILLS / skill_id / "SKILL.md").read_text(encoding="utf-8")


def assert_in_order(text: str, names: list[str], start: int = 0) -> int:
    pos = start
    for name in names:
        found = text.find(name, pos)
        assert found != -1, f"{name!r} missing or out of order after offset {pos}"
        pos = found + len(name)
    return pos


@pytest.mark.parametrize("skill_id", IDS)
def test_skill_file_basics(skill_id):
    path = SKILLS / skill_id / "SKILL.md"
    assert path.is_file()
    # The app adds a short wrapper around an active skill.
    limit = SKILL_LIMIT - 400
    assert path.stat().st_size <= limit
    text = read_skill(skill_id)
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "frontmatter missing"
    front = match.group(1)
    assert re.search(rf"^name:\s*{re.escape(skill_id)}\s*$", front, re.MULTILINE)
    desc = re.search(r"^description:\s*(.+)$", front, re.MULTILINE)
    assert desc
    value = desc.group(1).strip().strip("\"'")
    assert value.startswith("Use when")
    assert len(value) <= SKILL_INDEX_DESCRIPTION
    lowered = text.lower()
    for pattern in FORBIDDEN:
        assert not re.search(pattern, lowered), pattern


def test_handoff_file_markers_and_sections_in_order():
    text = read_skill("handoff")
    file1 = text.find("FILE 1")
    file2 = text.find("FILE 2")
    assert -1 < file1 < file2
    end_log = assert_in_order(text[:file2], LOG_SECTIONS, file1 + len("FILE 1"))
    assert end_log <= file2
    assert_in_order(text, BRIEF_SECTIONS, file2 + len("FILE 2"))


@pytest.mark.parametrize("needle", HANDOFF_STRINGS)
def test_handoff_contains_contract_string(needle):
    assert needle in read_skill("handoff")


@pytest.mark.parametrize("needle", RESUME_STRINGS)
def test_resume_contains_contract_string(needle):
    assert needle in read_skill("resume")


@pytest.mark.parametrize("needle", MIGRATE_STRINGS)
def test_migrate_handoffs_contains_contract_string(needle):
    assert needle in read_skill("migrate-handoffs")


def test_read_handoff_skill_is_gone():
    assert not (SKILLS / "read-handoff").exists()
    root = SKILLS.parent
    files = [p for p in SKILLS.rglob("*") if p.is_file()]
    files += [root / "README.md"]
    files += list((root / "backend" / "local_agent").rglob("*.py"))
    files += list((root / "frontend" / "static" / "js").rglob("*.js"))
    leftovers = [
        str(p) for p in files
        if p.is_file() and "read-handoff" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leftovers == []
