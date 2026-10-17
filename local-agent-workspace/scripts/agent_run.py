#!/usr/bin/env python3
"""Run one prompt in the running app from a terminal or a script, and print the result.

Usage (from the local-agent-workspace folder; needs only Python 3 and the app running, `python3 run.py`):
    python3 scripts/agent_run.py "Summarize the README"
    python3 scripts/agent_run.py --mode manual --approve ask "Fix the failing test"
    echo "List the TODOs" | python3 scripts/agent_run.py --output json -

The prompt runs in a new chat of the app (listed in its sidebar like any other) with Plan mode by default,
which changes nothing. --output text prints the final reply (and a one-line summary to stderr), json one
result object, stream-json every message of the chat as one JSON line and then the result. Cards are
declined by default (--approve deny: nothing waits for the approval timeout, and questions are skipped);
--approve ask asks you in the terminal (a TTY only); --mode bypassPermissions also needs --confirm-bypass.
Exit codes: 0 completed, 1 error, 2 a limit was reached (steps, spending or the turn's tokens or time),
3 stopped or interrupted (Ctrl+C stops the turn).

The script talks to the app at --url (default http://127.0.0.1:8765) and never starts a server itself,
so it cannot run a second app on the same state folder. Every model request is a paid call to your
Databricks workspace.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from local_agent.client import DEFAULT_URL, Allow, Deny, LocalAgentClient, LocalAgentError  # noqa: E402

MODES = ("plan", "manual", "acceptEdits", "auto", "dontAsk", "bypassPermissions")
EFFORTS = ("default", "low", "medium", "high", "xhigh", "max")


def ask_tool(name, arguments, context):
    """--approve ask: one approval in the terminal."""
    who = " (subagent)" if context.get("subagent") else ""
    if name == "submit_plan":
        print(f"\nPlan{who}: {arguments.get('title') or ''}\n{arguments.get('plan') or ''}", file=sys.stderr)
        answer = input("Approve it? [m = yes, in Manual / a = yes, in Accept edits / N = keep planning] ").strip().lower()
        return Allow("acceptEdits") if answer == "a" else Allow("manual") if answer in ("m", "y", "yes") else Deny()
    preview = context.get("preview") or json.dumps(arguments, ensure_ascii=False)[:2000]
    print(f"\nApproval{who}: {name}\n{preview}", file=sys.stderr)
    if input("Allow it? [y/N] ").strip().lower() in ("y", "yes"):
        return Allow()
    note = input("Note for the agent (optional): ").strip()
    return Deny(note)


def ask_questions(questions, context):
    """--approve ask: ask_user questions in the terminal; an empty answer skips them all."""
    answers = []
    for question in questions:
        options = question.get("options") or []
        print(f"\n{question.get('question')}", file=sys.stderr)
        for number, option in enumerate(options, start=1):
            print(f"  {number}. {option.get('label')}" + (f" - {option['description']}" if option.get("description") else ""),
                  file=sys.stderr)
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
    parser.add_argument("--mode", choices=MODES, default="plan", help="permission mode of a new chat (default plan)")
    parser.add_argument("--confirm-bypass", action="store_true",
                        help="needed with --mode bypassPermissions: actions then run without approval cards")
    parser.add_argument("--model", help="endpoint (default: the Settings model)")
    parser.add_argument("--effort", choices=EFFORTS, help="reasoning effort (default: the Settings default)")
    parser.add_argument("--max-steps", type=int, help="agent steps per message for this chat (1-64)")
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
    if args.approve == "ask" and not (sys.stdin.isatty() and sys.stderr.isatty()):
        parser.error("--approve ask needs a terminal (and a prompt given as an argument)")
    interactive = args.approve == "ask"
    client = LocalAgentClient(args.url)
    result, session_id = None, args.session
    try:
        if session_id is None:
            session_id = client.create_session(args.workspace, args.mode, args.model, args.effort, args.max_steps,
                                               args.max_dbu, args.max_tokens, args.max_minutes)["id"]
        elif any(value is not None for value in (args.max_dbu, args.max_tokens, args.max_minutes)):
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
                if args.output == "stream-json":
                    print(json.dumps(message, ensure_ascii=False), flush=True)
        except KeyboardInterrupt:
            print("\nStopping the turn…", file=sys.stderr)
            messages.close()  # Its streams end now.
            client.interrupt(session_id)
            result = client.wait_result(session_id)  # The stopped turn's result.
            if args.output == "stream-json":
                print(json.dumps(result, ensure_ascii=False), flush=True)
    except LocalAgentError as exc:
        print(f"agent_run: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 3
    if result is None:
        print("agent_run: the turn ended without a result.", file=sys.stderr)
        return 1
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.output == "text":
        print(result["result"])
        print(summary_line(result), file=sys.stderr)
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
