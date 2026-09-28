"""Permission policy for the Databricks agent's workspace tools."""
from typing import Literal

PermissionMode = Literal["manual", "auto", "acceptEdits", "plan", "bypassPermissions"]

READ_TOOLS = {"list_files", "read_file", "search_files", "list_jobs", "get_job_output", "stop_job",
              "list_skills", "use_skill", "list_tasks", "create_task", "update_task", "delegate_task"}
EDIT_TOOLS = {"write_file", "edit_file", "insert_activity_log"}
COMMAND_TOOLS = {"run_command"}
# Exact commands only; never classify arbitrary shell syntax as read-only.
# Resolve to OS binaries so PATH and shell startup files cannot replace them.
BASIC_COMMANDS = {"pwd": "/bin/pwd", "/bin/pwd": "/bin/pwd",
                  "ls": "/bin/ls", "/bin/ls": "/bin/ls",
                  "ls -la": "/bin/ls -la", "/bin/ls -la": "/bin/ls -la"}


def tool_decision(mode: str, name: str, arguments: dict) -> str:
    if name in READ_TOOLS:
        return "allow"
    if mode == "plan":
        return "deny"
    if mode == "bypassPermissions":
        return "allow"
    if mode in ("acceptEdits", "auto") and name in EDIT_TOOLS:
        return "allow"
    if mode == "auto" and name in COMMAND_TOOLS and arguments.get("command", "").strip() in BASIC_COMMANDS:
        return "allow"
    return "ask"


def mode_prompt(mode: str) -> str:
    return {
        "manual": "Manual mode: reads run directly; file changes and commands require an approval card.",
        "auto": "Auto mode: file edits and basic pwd/ls commands run directly; other commands need an approval card.",
        "acceptEdits": "Accept edits mode: file edits run directly; commands require an approval card.",
        "plan": "Plan mode: inspect files and provide a concrete plan with the proposed steps/content, rather than merely refusing an implementation request. File modifications and commands are blocked. For implementation, tell the user to select Manual or Accept edits in the permissions menu below the chat input. Do not invent mode names.",
        "bypassPermissions": "Bypass permissions mode: actions run without approval cards. File-tool credential exclusions still apply.",
    }.get(mode, "Manual mode: ask before changes and commands.")
