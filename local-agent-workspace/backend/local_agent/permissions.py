"""Permission policy for the Databricks agent's tools: modes, saved rules and protected paths.

tool_decision is each mode's baseline without rules. evaluate is the one decision
every tool call and every waiting approval card goes through; its order is fixed:
turned-off tools, deny rules, Plan's denial, ask rules and uncertain commands,
protected paths, the mode baseline, allow rules (never in Plan), Don't ask (and Bypass
for a memory note), ask.
"""
import logging
from dataclasses import dataclass
from typing import Literal

from .tool_profiles import COVERED_BY
from .rules import (EDIT_KINDS_RULE, behavior_of, has_matcher, parse_command, read_only_argv, rule_label, rule_matches,
                    uncertain_match)

log = logging.getLogger("local_agent.permissions")

PermissionMode = Literal["manual", "auto", "acceptEdits", "plan", "bypassPermissions", "dontAsk"]
MODE_LABELS = {"manual": "Manual", "auto": "Auto", "acceptEdits": "Accept edits", "plan": "Plan",
               "bypassPermissions": "Bypass permissions", "dontAsk": "Don't ask"}

TOOL_KINDS = {
    **dict.fromkeys(("list_files", "find_files", "read_file", "search_files", "list_jobs", "get_job_output", "wait_job",
                     "notebook_read", "outline_file", "project_overview"), "read"),
    **dict.fromkeys(("list_skills", "use_skill", "list_tasks", "get_task", "create_task", "update_task"), "task"),
    "ask_user": "question",
    "submit_plan": "plan",
    **dict.fromkeys(("write_file", "edit_file", "insert_activity_log", "move_path", "delete_path", "notebook_edit"),
                    "edit"),
    "run_command": "command",
    "stop_job": "job_stop",
    "delegate_task": "delegate",
    "remember": "memory",
}
# Every path-valued argument of each edit tool: protected-path checks and path rules cover all of them.
EDIT_TOOL_PATHS = {"write_file": ("path",), "edit_file": ("path",), "insert_activity_log": ("path",),
                   "move_path": ("path", "destination"), "delete_path": ("path",), "notebook_edit": ("path",)}
# The other string arguments of edit tools (content and options, never paths).
EDIT_CONTENT_KEYS = frozenset({"content", "old_text", "new_text", "source", "cell_id", "edit_mode", "cell_type"})
NEVER_CARD_KINDS = ("read", "task", "question", "plan")
READ_TOOLS = {name for name, kind in TOOL_KINDS.items() if kind in NEVER_CARD_KINDS}
EDIT_TOOLS = {name for name, kind in TOOL_KINDS.items() if kind == "edit"}
COMMAND_TOOLS = {"run_command"}

PLAN_DENIED = ("Plan mode is read-only. Only read-only commands from a fixed list run in Plan mode (pwd, ls, git "
               "status/diff/log/show/branch). Describe the plan, or call submit_plan when it is ready.")
PLAN_DENIED_SUBAGENT = ("Plan mode is read-only. Only read-only commands from a fixed list run in Plan mode (pwd, ls, "
                        "git status/diff/log/show/branch). Return the plan as your final reply.")
DONT_ASK_DENIED = ("Not run: Don't ask mode blocks actions that no rule allows, without showing an approval card. "
                   "Continue without it and tell the user which action needs a rule or another mode.")
BYPASS_MEMORY_DENIED = ("Not saved: Bypass permissions mode shows no approval cards, and project memory is saved without "
                        "one only when a rule allows it. Continue without it and tell the user what you would remember.")
SCOPE_NAMES = {"chat": "this chat", "project": "this project", "user": "all projects"}


def tool_kind(name):
    if not isinstance(name, str):
        return None
    if name in TOOL_KINDS:
        return TOOL_KINDS[name]
    if name.startswith("mcp__"):
        return "mcp"
    if name.startswith("hook_"):
        return "hook"
    return None


def command_argv(name, arguments):
    if name != "run_command" or not isinstance(arguments, dict):
        return None
    return parse_command(arguments.get("command"))[0]


def foreground(arguments):
    return not (isinstance(arguments, dict) and arguments.get("background") is True)


def tool_decision(mode: str, name: str, arguments: dict) -> str:
    """The mode's baseline decision, without rules: 'allow', 'ask' or 'deny'."""
    kind = tool_kind(name)
    if kind in NEVER_CARD_KINDS:
        return "allow"  # P3 rejects submit_plan outside Plan.
    read_only = kind == "command" and read_only_argv(command_argv(name, arguments)) and foreground(arguments)
    if mode == "plan":
        if read_only:
            return "allow"
        return "ask" if kind == "delegate" else "deny"
    if kind == "memory":
        return "ask"  # Every mode asks; Bypass and Don't ask save a note only when a rule allows it (evaluate).
    if mode == "bypassPermissions":
        return "allow"
    if kind == "edit" and mode in ("acceptEdits", "auto"):
        return "allow"
    if read_only and mode == "auto":
        return "allow"
    if kind == "job_stop":
        return "allow" if mode == "auto" else "ask"
    if kind == "delegate":
        return "allow" if mode in ("acceptEdits", "auto") else "ask"
    return "ask"


@dataclass(frozen=True)
class Verdict:
    """decision is 'allow', 'ask' or 'deny'. argv is set only for a command that runs without a shell:
    one allowed by an argv or prefix rule, or one on the read-only list (then read_only is True)."""
    decision: str
    source: str
    message: str = ""
    rule: dict | None = None
    scope: str | None = None
    argv: tuple | None = None
    read_only: bool = False
    protected: str | None = None
    uncertain: bool = False


def _scoped(chat_rules, project_rules, user_rules):
    return (("chat", chat_rules or []), ("project", project_rules or []), ("user", user_rules or []))


def evaluate(mode, name, arguments, *, chat_rules=(), project_rules=(), user_rules=(), resolve_path=None,
             protected=None, disabled=(), subagent=False, credential_file=None):
    """The decision for one tool call. See the module docstring for the fixed order."""
    mode = mode if mode in MODE_LABELS else "manual"
    kind = tool_kind(name)
    arguments = arguments if isinstance(arguments, dict) else {}
    scoped = _scoped(chat_rules, project_rules, user_rules)
    argv = command_argv(name, arguments)
    read_only = bool(argv) and read_only_argv(argv) and foreground(arguments)

    def matches(rule):
        try:
            return rule_matches(rule, name, arguments, resolve_path=resolve_path, credential_file=credential_file)
        except (ValueError, OSError, TypeError, KeyError, AttributeError):
            return behavior_of(rule) != "allow"  # A rule that cannot be checked never allows.

    covers = COVERED_BY.get(name, ())
    # (0) Tools the user turned off in this chat, directly or through a tool that covers this one.
    if name in set(disabled or ()):
        return Verdict("deny", "disabled", f"The user turned off {name} in this chat. Use other tools.")
    for cover in covers:
        if cover in set(disabled or ()):
            return Verdict("deny", "disabled", f"The user turned off {cover} in this chat, which also covers {name}. "
                           "Use other tools.")
    # (1) Deny rules win in every mode, Bypass included; a deny rule for a covering tool covers this one too.
    for scope, rules in scoped:
        for rule in rules:
            if behavior_of(rule) == "deny" and matches(rule):
                return Verdict("deny", "rule", f"Blocked by a deny rule for {SCOPE_NAMES[scope]}: {rule_label(rule)}. "
                               "Do not retry it or work around it.", rule=rule, scope=scope)
    for cover in covers:
        for scope, rules in scoped:
            for rule in rules:
                if behavior_of(rule) != "deny":
                    continue
                try:
                    hit = rule_matches(rule, cover, arguments, resolve_path=resolve_path, credential_file=credential_file)
                except (ValueError, OSError, TypeError, KeyError, AttributeError):
                    hit = True  # A deny rule that cannot be checked counts.
                if hit:
                    return Verdict("deny", "rule", f"Blocked by a deny rule for {SCOPE_NAMES[scope]}: "
                                   f"{rule_label(rule)}, which also covers {name}. Do not retry it or work around it.",
                                   rule=rule, scope=scope)
    baseline = tool_decision(mode, name, arguments)
    # (2) Plan mode: nothing outside its read-only set, whatever a rule says.
    if mode == "plan" and baseline == "deny":
        return Verdict("deny", "plan", PLAN_DENIED_SUBAGENT if subagent else PLAN_DENIED)
    reason = None
    if kind == "edit" and protected is not None:
        reason = next(filter(None, (protected(arguments[key]) for key in EDIT_TOOL_PATHS.get(name, ())
                                    if isinstance(arguments.get(key), str))), None)
    # (3) Ask rules, and commands a deny or ask rule might cover but that cannot be checked exactly.
    if kind not in NEVER_CARD_KINDS:
        asked, uncertain = None, False
        for scope, rules in scoped:
            for rule in rules:
                if behavior_of(rule) == "ask" and matches(rule):
                    asked = asked or (rule, scope)
        if asked is None and name == "run_command":
            text = arguments.get("command")
            uncertain = any(uncertain_match(rule, text, argv) for _, rules in scoped for rule in rules)
        if asked is not None or uncertain:
            if mode == "dontAsk":
                return Verdict("deny", "dont_ask", DONT_ASK_DENIED)
            # A protected file keeps its marker on an ask-rule card, so the card is never remembered.
            return Verdict("ask", "rule" if asked else "uncertain", rule=asked and asked[0],
                           scope=asked and asked[1], uncertain=uncertain, protected=reason)
    # (4) Protected paths: always a card, except in Bypass; no allow rule skips this.
    if reason:
        if mode == "bypassPermissions":
            return Verdict("allow", "mode", protected=reason)
        if mode == "dontAsk":
            return Verdict("deny", "dont_ask", f"Not run: this edit changes a protected file, {reason}, which "
                           "always needs an approval card, and Don't ask mode does not show cards. Continue "
                           "without it and tell the user which file needs their approval.", protected=reason)
        return Verdict("ask", "protected", protected=reason)
    # (5) The mode's own allowance; read-only list commands run hardened without a shell.
    if baseline == "allow":
        source = "read_only_command" if read_only and mode in ("auto", "plan") else "mode"
        return Verdict("allow", source, argv=tuple(argv) if read_only else None, read_only=read_only)
    # (6) Allow rules: chat (exact and prefix rules before legacy text), then project, then user. Never in Plan.
    if mode != "plan":
        for scope, rules in scoped:
            ordered = sorted(rules, key=lambda rule: "command" in rule) if scope == "chat" else rules
            for rule in ordered:
                if behavior_of(rule) == "allow" and matches(rule):
                    shell_free = name == "run_command" and argv is not None and "command" not in rule
                    return Verdict("allow", "rule", rule=rule, scope=scope,
                                   argv=tuple(argv) if shell_free else None, read_only=shell_free and read_only)
    # (7) Don't ask: never a card. Bypass shows none either, so a memory note no rule allows is not saved.
    if mode == "dontAsk":
        return Verdict("deny", "dont_ask", DONT_ASK_DENIED)
    if mode == "bypassPermissions" and kind == "memory":
        return Verdict("deny", "bypass_memory", BYPASS_MEMORY_DENIED)
    # (8) Everything else asks.
    return Verdict("ask", "mode")


def denied_tools(chat_rules, project_rules, user_rules, names):
    """The names covered by an unconditional deny rule; they are not offered to the model."""
    denied = set()
    for _, rules in _scoped(chat_rules, project_rules, user_rules):
        for rule in rules:
            if behavior_of(rule) != "deny" or has_matcher(rule):
                continue
            tool = rule.get("tool")
            for name in names:
                if (name == tool or tool in COVERED_BY.get(name, ())
                        or (tool == EDIT_KINDS_RULE and tool_kind(name) == "edit")
                        or (isinstance(tool, str) and tool.startswith("mcp__") and tool.endswith("__*")
                            and name.startswith(tool[:-1]))):
                    denied.add(name)
    return denied


PLAN_PROMPT = ("Plan mode: inspect files and describe a concrete plan with the proposed steps and content, rather than "
               "merely refusing an implementation request. File changes and commands are blocked, except read-only "
               "commands from a fixed list (pwd, ls, git status/diff/log/show/branch).")


def mode_prompt(mode: str, subagent: bool = False) -> str:
    if mode == "plan":
        if subagent:
            return PLAN_PROMPT + " Use ask_user only when a decision blocks you. Return the finished plan as your final reply."
        return (PLAN_PROMPT + " Use ask_user for clarifying questions. When the plan is complete, call submit_plan with "
                "the full plan: the user can approve it and switch modes, and you then implement it in this same turn. "
                "Do not invent mode names.")
    return {
        "manual": "Manual mode: reads run directly; file changes and commands require an approval card.",
        "auto": ("Auto mode: file edits run directly, and a fixed list of read-only commands (pwd, ls, and read-only git "
                 "status/diff/log/show/branch commands) runs without an approval card; other commands and actions need "
                 "an approval card unless a rule the user saved allows them."),
        "acceptEdits": "Accept edits mode: file edits run directly; commands require an approval card.",
        "dontAsk": ("Don't ask mode: actions run only when the mode or a rule the user saved allows them; anything else "
                    "is blocked without an approval card. If an action is blocked, continue with what you can do and "
                    "tell the user what needs their approval."),
        "bypassPermissions": ("Bypass permissions mode: actions run without approval cards. Deny and ask rules the user "
                              "saved and file-tool credential exclusions still apply."),
    }.get(mode, "Manual mode: ask before changes and commands.")
