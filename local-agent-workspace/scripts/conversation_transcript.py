#!/usr/bin/env python3
"""Turn saved conversations into readable Markdown transcripts.

Usage (from the local-agent-workspace folder; needs only Python 3):
    python3 scripts/conversation_transcript.py <state dir>/conversations/<id>.jsonl [more.jsonl ...]
        [--out-dir <folder> | --out <file.md>] [--excerpt 600]

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
Common secret shapes are replaced with [REDACTED]; review a transcript before
sharing it. Copy a conversation file first if the app is running.
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from local_agent.transcript import EXCERPT_CHARS, transcript  # noqa: E402
from local_agent.usage_export import session_from_jsonl  # noqa: E402


def file_name(session, folder):
    try:
        day = datetime.fromtimestamp(float(session.get("created"))).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        day = "undated"
    slug = re.sub(r"[^a-z0-9]+", "-", str(session.get("title") or "").lower()).strip("-")[:60].strip("-") or "chat"
    stem = f"{day}-{slug}-{str(session.get('id', ''))[:8]}".rstrip("-")
    for suffix in ("", *(f"-{number}" for number in range(2, 1000))):
        if not (folder / f"{stem}{suffix}.md").exists():
            return folder / f"{stem}{suffix}.md"
    raise SystemExit(f"Too many transcripts named {stem} in {folder}.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("files", nargs="+", type=Path, help="conversation .jsonl files")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--out-dir", type=Path, help="write one transcript per conversation into this folder")
    target.add_argument("--out", type=Path, help="write all transcripts, oldest first, into this file")
    parser.add_argument("--excerpt", type=int, default=EXCERPT_CHARS,
                        help=f"characters of command output to keep (default {EXCERPT_CHARS})")
    arguments = parser.parse_args(argv)
    if arguments.excerpt < 0:
        parser.error("--excerpt must be 0 or more.")
    sessions = sorted((session_from_jsonl(path) for path in arguments.files),
                      key=lambda session: session.get("created") or 0)
    if arguments.out_dir:
        arguments.out_dir.mkdir(parents=True, exist_ok=True)
        for session in sessions:
            text = transcript(session, excerpt_chars=arguments.excerpt)
            path = file_name(session, arguments.out_dir)
            path.write_text(text, encoding="utf-8")
            print(f"Wrote {path} ({len(text.encode('utf-8')):,} bytes)")
        return 0
    text = "\n".join(transcript(session, excerpt_chars=arguments.excerpt) for session in sessions)
    if arguments.out:
        arguments.out.write_text(text, encoding="utf-8")
        print(f"Wrote {arguments.out} ({len(text.encode('utf-8')):,} bytes)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
