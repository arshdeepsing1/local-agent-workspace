#!/usr/bin/env python3
"""Run one prompt in the running app from a terminal or a script, and print the result.

Usage (from the local-agent-workspace folder; needs only Python 3 and the app running, `python3 run.py`):
    python3 scripts/agent_run.py "Summarize the README"
    python3 scripts/agent_run.py --mode manual --approve ask "Fix the failing test"
    echo "List the TODOs" | python3 scripts/agent_run.py --output json -

The prompt runs in a new chat of the app (listed in its sidebar like any other) with Plan mode by default,
which changes nothing; --session sends it to an existing idle chat instead (--mode, --model and --effort
then change that chat). --output text prints the final reply (and a one-line summary to stderr), json one
result object, stream-json every message of the chat as one JSON line and then the result. Cards are
declined by default (--approve deny: nothing waits for the approval timeout, and questions are skipped);
--approve ask asks you in the terminal (a TTY only; control characters in what the agent wrote are shown
as escapes). Bypass permissions, chosen with --mode or already set on the --session chat, needs
--confirm-bypass. Exit codes: 0 completed, 1 error, 2 a limit was reached (steps, spending or the turn's
tokens or time), 3 stopped or interrupted (Ctrl+C stops the turn).

The script talks to the app at --url (default http://127.0.0.1:8765) and never starts a server itself,
so it cannot run a second app on the same state folder. Every model request is a paid call to your
Databricks workspace.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from local_agent.client import DEFAULT_URL, Allow, Deny, LocalAgentClient, LocalAgentError  # noqa: E402

MODES = ("plan", "manual", "acceptEdits", "auto", "dontAsk", "bypassPermissions")
EFFORTS = ("default", "low", "medium", "high", "xhigh", "max")
# What a card shows comes from the model (and so from web pages, files or tool output it read): control characters
# other than newline and tab (terminal escape sequences included), DEL, C1 controls and bidirectional overrides are
# printed as visible escapes, so that text cannot erase or rewrite what the terminal shows before you answer.
CONTROLS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
ESCAPED_NOTE = "(It contains control characters, shown as \\x.. or \\u.... escapes.)"
FALLBACKS = {"skipped": "the questions were skipped", "keep_planning": "the plan went back to planning",
             "declined": "the action was declined"}


def shown(text):
    """(text with its control characters as visible escapes, whether there were any)."""
    text = str(text)
    escaped = CONTROLS.sub(lambda match: f"\\x{ord(match[0]):02x}" if ord(match[0]) < 0x100 else f"\\u{ord(match[0]):04x}", text)
    return escaped, escaped != text


def json_text(value, **options):
    """value as JSON for the terminal or a pipe: json.dumps escapes C0 controls but leaves C1 controls and
    bidirectional overrides raw, so they are written as \\u escapes too (the parsed JSON is the same)."""
    return CONTROLS.sub(lambda match: f"\\u{ord(match[0]):04x}", json.dumps(value, ensure_ascii=False, **options))


def show(text):
    """Print model-written text to the terminal (stderr) as shown(); True when something was escaped."""
    text, escaped = shown(text)
    print(text, file=sys.stderr)
    return escaped


def ask_tool(name, arguments, context):
    """--approve ask: one approval in the terminal."""
    who = " (subagent)" if context.get("subagent") else ""
    if name == "submit_plan":
        print(f"\nPlan{who}:", file=sys.stderr)
        if show(f"{arguments.get('title') or ''}\n{arguments.get('plan') or ''}"):
            print(ESCAPED_NOTE, file=sys.stderr)
        answer = input("Approve it? [m = yes, in Manual / a = yes, in Accept edits / N = keep planning] ").strip().lower()
        return Allow("acceptEdits") if answer == "a" else Allow("manual") if answer in ("m", "y", "yes") else Deny()
    preview = context.get("preview") or json.dumps(arguments, ensure_ascii=False)[:2000]
    print(f"\nApproval{who}: {shown(name)[0]}", file=sys.stderr)
    if show(preview):
        print(ESCAPED_NOTE, file=sys.stderr)
    if input("Allow it? [y/N] ").strip().lower() in ("y", "yes"):
        return Allow()
    note = input("Note for the agent (optional): ").strip()
    return Deny(note)


def ask_questions(questions, context):
    """--approve ask: ask_user questions in the terminal; an empty answer skips them all."""
    answers = []
    for question in questions:
        options = question.get("options") or []
        print("", file=sys.stderr)
        escaped = show(question.get("question"))
        for number, option in enumerate(options, start=1):
            escaped = show(f"  {number}. {option.get('label')}" + (f" - {option['description']}" if option.get("description") else "")) or escaped
        if escaped:
            print(ESCAPED_NOTE, file=sys.stderr)
        typed = input("Choose a number, or type your own answer (empty skips): ").strip()
        if not typed:
            return None
        if typed.isdigit() and 1 <= int(typed) <= len(options):
            answers.append({"selected": [options[int(typed) - 1]["label"]], "other": None})
        else:
            answers.append({"selected": [], "other": typed})
    return answers


def summary_line(result):
    usage = result.get("usage") or {}
    tokens = result.get("tokens")
    parts = [f"{result.get('terminal_reason')}", f"{result.get('steps', 0)} steps", f"{result.get('tool_calls', 0)} tools",
             f"{result.get('duration_s', 0)} s", f"{tokens:,} tokens" if isinstance(tokens, int) else "tokens unreported"]
    if isinstance(result.get("estimated_dbu"), (int, float)):
        parts.append(f"~{result['estimated_dbu']:.3f} DBU")
    if usage.get("calls") is not None:
        parts.append(f"{usage['calls']} model calls")
    return " · ".join(parts) + f" · chat {result.get('session_id')}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("prompt", help="the message to send ('-' reads it from standard input)")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"the running app (default {DEFAULT_URL})")
    parser.add_argument("--workspace", help="the project folder (default: the Settings workspace)")
    parser.add_argument("--session", help="send to this existing, idle chat instead of a new one")
    parser.add_argument("--mode", choices=MODES,
                        help="permission mode (default plan for a new chat; with --session: changes the chat's)")
    parser.add_argument("--confirm-bypass", action="store_true",
                        help="needed for Bypass permissions (--mode or the --session chat's): actions then run "
                             "without approval cards")
    parser.add_argument("--model", help="endpoint (default: the Settings model, or the --session chat's)")
    parser.add_argument("--effort", choices=EFFORTS, help="reasoning effort (default: the Settings default, or the --session chat's)")
    parser.add_argument("--max-steps", type=int, help="agent steps per message for a new chat (1-64)")
    parser.add_argument("--max-dbu", type=float, help="spending limit of this chat in estimated DBU")
    parser.add_argument("--max-tokens", type=int, help="token limit of the turn (every processed token)")
    parser.add_argument("--max-minutes", type=int, help="time limit of the turn in minutes")
    parser.add_argument("--output", choices=("text", "json", "stream-json"), default="text")
    parser.add_argument("--approve", choices=("deny", "ask"), default="deny",
                        help="how cards are answered: deny (default) or ask in the terminal")
    args = parser.parse_args(argv)
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    if not prompt.strip():
        parser.error("the prompt is empty")
    if args.mode == "bypassPermissions" and not args.confirm_bypass:
        parser.error("--mode bypassPermissions runs actions without approval cards: add --confirm-bypass")
    if args.session is not None and (args.workspace is not None or args.max_steps is not None):
        parser.error("--workspace and --max-steps apply to a new chat only: leave them out with --session")
    if args.approve == "ask" and not (sys.stdin.isatty() and sys.stderr.isatty()):
        parser.error("--approve ask needs a terminal (and a prompt given as an argument)")
    interactive = args.approve == "ask"
    client = LocalAgentClient(args.url)
    result, session_id, previous = None, args.session, None
    try:
        if session_id is None:
            session_id = client.create_session(args.workspace, args.mode or "plan", args.model, args.effort,
                                               args.max_steps, args.max_dbu, args.max_tokens, args.max_minutes)["id"]
        else:
            chat = client.info(session_id)
            if chat.get("status", "idle") != "idle":
                raise LocalAgentError("This chat is busy: wait for its turn to end, or use another chat.")
            if args.mode is None and chat.get("permission_mode") == "bypassPermissions" and not args.confirm_bypass:
                print("agent_run: this chat is in Bypass permissions, which runs actions without approval cards: add "
                      "--confirm-bypass, or choose another --mode.", file=sys.stderr)
                return 1
            # The started time of its last turn: Ctrl+C before this prompt started a turn gives no result.
            previous = chat["last_turn_result"].get("started") if isinstance(chat.get("last_turn_result"), dict) else None
            if args.mode is not None:
                client.set_permission_mode(session_id, args.mode, confirm=args.mode == "bypassPermissions")
            if args.model is not None:
                client.set_model(session_id, args.model)
            if args.effort is not None:
                client.set_effort(session_id, args.effort)
            if args.max_dbu is not None:
                client.set_spending_limit(session_id, args.max_dbu)
            if args.max_tokens is not None or args.max_minutes is not None:
                client.set_turn_limits(session_id, args.max_tokens, args.max_minutes)
        messages = client.query(prompt.strip(), session_id=session_id, can_use_tool=ask_tool if interactive else None,
                                on_question=ask_questions if interactive else None)
        try:
            for message in messages:
                if message["type"] == "result":
                    result = message
                elif message["type"] == "answer_refused":
                    print(f"agent_run: the app refused that answer ({shown(message['error'])[0]}): "
                          f"{FALLBACKS.get(message['answered'], 'it was declined')}.", file=sys.stderr)
                if args.output == "stream-json":
                    print(json_text(message), flush=True)
        except KeyboardInterrupt:
            print("\nStopping the turn…", file=sys.stderr)
            messages.close()  # Its streams end now.
            client.interrupt(session_id)
            result = client.wait_result(session_id, after=previous)  # The stopped turn's result.
            if result is None:
                print("agent_run: stopped before the prompt started a turn.", file=sys.stderr)
                return 3
            if args.output == "stream-json":
                print(json_text(result), flush=True)
    except LocalAgentError as exc:
        print(f"agent_run: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 3
    if result is None:
        print("agent_run: the turn ended without a result.", file=sys.stderr)
        return 1
    if args.output == "json":
        print(json_text(result, indent=2))
    elif args.output == "text":
        print(shown(result["result"])[0] if sys.stdout.isatty() else result["result"])
        print(summary_line(result), file=sys.stderr)
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
