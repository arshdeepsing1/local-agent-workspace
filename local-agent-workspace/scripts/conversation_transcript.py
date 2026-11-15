#!/usr/bin/env python3
"""Turn saved conversations into readable Markdown transcripts.

Usage (from the local-agent-workspace folder; needs only Python 3):
    python3 scripts/conversation_transcript.py <state dir>/conversations/<id>.jsonl [more.jsonl ...]
        [--out-dir <folder> | --out <file.md>] [--excerpt 600] [--with-subagents]

A conversation file repeats every reply in its model history and keeps whole
tool results, and some of its lines are too long for the agent's read_file.
A transcript keeps the messages, replies, notices and errors in order and lists
each tool call with its main input; command output is shortened to --excerpt
characters. With --out-dir each conversation gets its own
<date>-<title>-<id>.md file, never replacing an existing one; otherwise the
transcripts are joined, oldest first, into --out or printed.

To give the agent a project's history, write the transcripts into a folder of
its workspace and ask it to read them, for example:
    python3 scripts/conversation_transcript.py .local/conversations/*.jsonl --out-dir ~/project/history
With --with-subagents each conversation's subagent chats (the files in its folder whose header names it as parent)
are written inside its transcript, under the delegate_task card that started them. Without the option the output
is the same as before.
Common secret shapes are replaced with [REDACTED]; review a transcript before
sharing it. Copy a conversation file first if the app is running.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from local_agent.transcript import EXCERPT_CHARS, transcript, transcript_file_stem  # noqa: E402
from local_agent.usage_export import session_from_jsonl  # noqa: E402


def file_name(session, folder):
    stem = transcript_file_stem(session)
    for suffix in ("", *(f"-{number}" for number in range(2, 1000))):
        if not (folder / f"{stem}{suffix}.md").exists():
            return folder / f"{stem}{suffix}.md"
    raise SystemExit(f"Too many transcripts named {stem} in {folder}.")


def subagent_chats(path, session):
    """{ID: conversation} of the subagent chats of this conversation file: the files in its folder whose header
    names it as parent (a chat that is gone has no file, and its card says so)."""
    found = {}
    for other in sorted(path.parent.glob("*.jsonl")):
        if other == path:
            continue
        try:
            with open(other, encoding="utf-8") as source:
                header = json.loads(source.readline())
            data = header.get("data") if isinstance(header, dict) and header.get("record") == "session" else None
            if isinstance(data, dict) and data.get("parent_session_id") == session.get("id") and data.get("id"):
                found[data["id"]] = session_from_jsonl(other)
        except (OSError, ValueError):
            continue  # Unreadable: left out like a missing file.
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("files", nargs="+", type=Path, help="conversation .jsonl files")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--out-dir", type=Path, help="write one transcript per conversation into this folder")
    target.add_argument("--out", type=Path, help="write all transcripts, oldest first, into this file")
    parser.add_argument("--excerpt", type=int, default=EXCERPT_CHARS,
                        help=f"characters of command output to keep (default {EXCERPT_CHARS})")
    parser.add_argument("--with-subagents", action="store_true",
                        help="write each conversation's subagent chats inside it, under the card that started them")
    arguments = parser.parse_args(argv)
    if arguments.excerpt < 0:
        parser.error("--excerpt must be 0 or more.")
    loaded = sorted(((session_from_jsonl(path), path) for path in arguments.files),
                    key=lambda item: item[0].get("created") or 0)
    sessions = [(session, subagent_chats(path, session) if arguments.with_subagents else None) for session, path in loaded]
    if arguments.out_dir:
        arguments.out_dir.mkdir(parents=True, exist_ok=True)
        for session, children in sessions:
            text = transcript(session, excerpt_chars=arguments.excerpt, subagents=children)
            path = file_name(session, arguments.out_dir)
            path.write_text(text, encoding="utf-8")
            print(f"Wrote {path} ({len(text.encode('utf-8')):,} bytes)")
        return 0
    text = "\n".join(transcript(session, excerpt_chars=arguments.excerpt, subagents=children)
                     for session, children in sessions)
    if arguments.out:
        arguments.out.write_text(text, encoding="utf-8")
        print(f"Wrote {arguments.out} ({len(text.encode('utf-8')):,} bytes)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
