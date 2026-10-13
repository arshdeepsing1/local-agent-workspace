import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from local_agent.store import Store
from local_agent.transcript import transcript
from local_agent.usage_export import session_from_jsonl


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "conversation_transcript.py"


def recorded(title="Nightly export", created=1790000000.0):
    command = {"job_id": "j1", "state": "completed", "exit_code": 0, "output": "pod/etl-1 Running\n" * 100}
    page = {"path": "notes/v7.md", "start_line": 109, "end_line": 243, "content": "109: text", "next_line": 244}
    return {
        "title": title, "model": "databricks-test", "workspace": "/project", "created": created,
        "updated": created + 3600,
        "events": [
            {"id": "u1", "type": "user", "created": created, "text": "Why does the job fail?\n\n## Pasted notes\nold"},
            {"id": "a1", "type": "assistant", "text": "", "request_info": {"status": "completed"}},
            {"id": "t1", "type": "tool", "name": "read_file", "input": {"path": "notes/v7.md", "start_line": 109},
             "state": "completed", "output": json.dumps(page)},
            {"id": "t2", "type": "tool", "name": "run_command", "state": "completed", "output": json.dumps(command),
             "input": {"command": "kubectl get pods --token=abcdefghijklmnop1234"}},
            {"id": "t3", "type": "tool", "name": "write_file", "input": {"path": "fix.py", "content": "print(1)"},
             "state": "rejected", "output": "User declined this action. Do not retry it without a new instruction."},
            {"id": "a2", "type": "assistant", "text": "## Cause\nThe pod IP changed.\n```bash\n# keep this comment\n```"},
            {"id": "n1", "type": "notice", "text": "Context compacted."},
            {"id": "u2", "type": "user", "created": created + 60, "text": "Review it", "origin": {"kind": "delegated"}},
            {"id": "e1", "type": "error", "text": "Databricks returned HTTP 429"},
        ],
    }


def test_transcript_keeps_messages_in_order_and_shortens_tool_results():
    text = transcript({"id": "c0ffee00-1111", **recorded()}, excerpt_chars=100)
    assert text.startswith("# Transcript: Nightly export\n")
    assert "`c0ffee00-1111`, model `databricks-test`, 2 user messages" in text
    order = ["## Turn 1 ·", "**User:**", "Why does the job fail?", "#### Pasted notes",
             "- Read `notes/v7.md` lines 109–243", "- Command (completed, exit 0):",
             "- Wrote `fix.py` (rejected): User declined this action.", "**Assistant:**", "#### Cause",
             "# keep this comment", "> **Notice:** Context compacted.", "## Turn 2 ·",
             "**User (delegated by the parent conversation):**", "> **Error:** Databricks returned HTTP 429"]
    positions = [text.index(item) for item in order]
    assert positions == sorted(positions)
    assert "\n# keep this comment\n" in text and "### Cause" not in text.replace("#### Cause", "")
    # Command output is shortened; common secret shapes are redacted.
    assert "pod/etl-1 Running" in text and text.count("pod/etl-1 Running") < 10
    assert "more characters]" in text
    assert "--token=[REDACTED]" in text and "abcdefghijklmnop1234" not in text
    assert "print(1)" not in text  # File contents are not repeated.
    assert transcript({"events": []}).startswith("# Transcript: Untitled conversation\n")


def test_configured_credentials_are_redacted_too():
    session = {"events": [{"type": "user", "text": "My token is private-value-123"}]}
    assert "private-value-123" not in transcript(session, redact=lambda text: text.replace("private-value-123", "[REDACTED]"))


def test_script_writes_one_file_per_conversation_without_replacing_any(tmp_path):
    store = Store(tmp_path / "state" / "tests.sqlite3")
    try:
        sessions = []
        for title, created in (("Later chat", 1790086400.0), ("Nightly export", 1790000000.0)):
            session = store.create({"workspace": str(tmp_path), "model": "databricks-test"})
            session.update(recorded(title, created), id=session["id"])
            store.save(session)
            sessions.append(session)
    finally:
        store.db.close()
    sources = [tmp_path / "state" / "conversations" / f"{session['id']}.jsonl" for session in sessions]
    folder = tmp_path / "history"
    for _ in range(2):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, sources), "--out-dir", str(folder)],
                                capture_output=True, text=True, check=True)
        assert result.stdout.count("Wrote ") == 2
    names = sorted(path.name for path in folder.iterdir())
    later = f"{datetime.fromtimestamp(1790086400.0):%Y-%m-%d}-later-chat-{sessions[0]['id'][:8]}"
    assert len(names) == 4 and f"{later}.md" in names and f"{later}-2.md" in names
    first = next(path for path in folder.iterdir() if path.name.endswith(f"{sessions[1]['id'][:8]}.md"))
    assert first.read_text(encoding="utf-8") == transcript(session_from_jsonl(sources[1]))
    # One joined file lists the conversations oldest first.
    joined = tmp_path / "all.md"
    subprocess.run([sys.executable, str(SCRIPT), *map(str, sources), "--out", str(joined)], check=True,
                   capture_output=True)
    text = joined.read_text(encoding="utf-8")
    assert text.index("# Transcript: Nightly export") < text.index("# Transcript: Later chat")
    usage = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert usage.returncode == 2 and "usage:" in usage.stderr


def test_find_files_and_wait_job_lines():
    events = [{"id": "u", "type": "user", "created": 1790000000.0, "text": "Find the tests"},
              {"id": "f", "type": "tool", "name": "find_files", "state": "completed", "output": "{}",
               "input": {"pattern": "src/**/*.test.ts", "path": "web"}},
              {"id": "w", "type": "tool", "name": "wait_job", "state": "completed", "output": "{}",
               "input": {"job_id": "0123456789abcdef", "until_regex": "ready"}}]
    text = transcript({"id": "c1", "title": "Tests", "events": events})
    assert "- Found files matching `src/**/*.test.ts` in `web`\n" in text
    assert "- Waited for job `01234567`\n" in text


def test_questions_show_the_answers_and_plans_show_in_full_with_their_review():
    question = {"question": "Which database?", "header": "", "multi_select": False,
                "options": [{"label": "Postgres", "description": ""}, {"label": "SQLite", "description": ""}]}
    plan = "# Plan\n\n1. Add `notes.txt`.\n2. Check it.\n" + "More detail. " * 100
    asked = {"type": "tool", "name": "ask_user", "input": {"questions": [question]}}
    reviewed = {"type": "tool", "name": "submit_plan", "input": {"title": "Add notes", "plan": plan}}
    session = {"events": [
        {**asked, "state": "completed", "question_outcome": "answered", "output": "{}",
         "answers": [{"question": "Which database?", "selected": [], "other": "DuckDB please"}]},
        {**asked, "state": "completed", "question_outcome": "skipped", "skipped_reason": "dont_ask"},
        {**asked, "state": "completed", "question_outcome": "expired"},
        {**reviewed, "state": "completed", "plan_outcome": "keep_planning", "feedback": "Use fewer steps"},
        {**reviewed, "state": "completed", "plan_outcome": "approved", "plan_mode": "acceptEdits"},
        {**reviewed, "state": "completed", "plan_outcome": "superseded"},
        {**reviewed, "state": "pending"},
    ]}
    text = transcript(session)
    assert "- Asked the user (answered):\n  - Which database?\n    Answer: Other: DuckDB please\n" in text
    assert "- Asked the user (skipped in Don't ask mode):\n  - Which database?\n" in text
    assert "- Asked the user (not answered in time):" in text
    assert "- Plan \"Add notes\" (kept planning):\n  ```\n  # Plan\n" in text
    assert text.count("More detail.") == 400 and "more characters]" not in text
    assert "  Feedback: Use fewer steps\n" in text
    assert "- Plan \"Add notes\" (approved; mode: Accept edits):" in text
    assert "- Plan \"Add notes\" (not reviewed: the mode was changed from the menu):" in text
    assert "- Plan \"Add notes\" (pending):" in text
    assert '"questions"' not in text and '"plan"' not in text


def test_outline_and_overview_lines():
    events = [{"id": "o", "type": "tool", "name": "outline_file", "state": "completed", "output": "{}",
               "input": {"path": "docs/h.md", "max_level": 2}},
              {"id": "p", "type": "tool", "name": "project_overview", "state": "completed", "output": "{}",
               "input": {"path": "."}},
              {"id": "q", "type": "tool", "name": "outline_file", "state": "completed", "output": "{}",
               "input": {"path": "a.py", "max_level": 6}}]
    text = transcript({"id": "c1", "title": "Outline", "events": events})
    assert "- Outlined `docs/h.md` to level 2\n" in text and "- Project overview of `.`\n" in text
    assert "- Outlined `a.py`\n" in text
