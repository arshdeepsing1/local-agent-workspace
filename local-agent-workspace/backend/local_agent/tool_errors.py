"""Model-facing tool errors: a machine-readable category and a recovery hint.

The conversation card keeps the plain message; the model receives
{"error_kind", "message", "hint"} as JSON so it can tell a mismatching edit from
a missing file or an exclusion it must not work around.
"""
import json
import re


HINTS = {
    "timeout": "Use a longer timeout_seconds, or background=true with wait_job.",
    "not_found": "Check the path with find_files or list_files.",
    "invalid_arguments": "Fix the named field and call the tool again.",
    "excluded": "Do not try to reach this path another way.",
    "guidance_changed": "Re-read the refreshed project instructions, then retry.",
    "stale_file": "Read the file again, then retry.",
    "edit_mismatch": ("Read the file again and copy old_text exactly, with enough context to match once, "
                      "or set replace_all=true."),
    "limit": "Narrow the request (path, glob, line range) or split it.",
    "command_refused": "Do not run this command another way; ask the user to run it if it is needed.",
}

# Checked in order; the first rule whose phrases occur in the message wins.
VALUE_ERROR_RULES = (
    # Read-only commands (command_exec): checked first, '/bin/ls is not available' is not a turned-off tool.
    ("command_refused", ("does not run automatically", "Not a read-only", "not allowed in a read-only command")),
    ("excluded", ("Credential files are excluded", "directory is excluded", "folder is excluded from file tools")),
    ("access_required", ("needs access approval",)),
    # Before stale_file: 'Project instructions changed before execution' is about guidance, not a file.
    ("guidance_changed", ("instructions changed",)),
    ("stale_file", ("changed while", "changed before execution", "changed since", "changed immediately after",
                    "now resolves to a different file", "checkpoint was already restored")),
    ("edit_mismatch", ("old_text",)),
    ("not_found", ("was not found", "not found in this conversation", "does not exist", "job not found",
                   "no longer available", "Command not found", "Git is not installed")),
    ("unavailable_tool", ("not available",)),
    ("wrong_type", ("Not a directory", "must be a regular file", "Only regular", "Binary files cannot",
                    "Not a Jupyter notebook", "destination is a folder")),
    ("limit", ("limit reached", "exceeds", "too large", "more than", "More than", "took longer than", "took too long",
               "over the file write limit", "larger than", "commands can run at once", "is too long")),
)
INVALID_ARGUMENT_PHRASES = ("must be an integer", "must be a boolean", "must be booleans", "must be true or false", "Give either",
                            "Use either", "cannot be empty", "Give cell_id or index", "needs source", "Put replace_all",
                            "Invalid until_regex", "Invalid pattern", "Invalid regular expression", "not supported in pattern",
                            "alternatives", "is beyond", "is out of range", "must identify",
                            "in the foreground only", "without a shell needs")
# A message that starts with an argument name, e.g. "output_mode must be content, files_with_matches or count."
ARGUMENT_MESSAGE = re.compile(r"[A-Za-z_]+ must (?:be|contain) ")


def _kind(exc, message):
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, FileNotFoundError):
        return "not_found"
    if isinstance(exc, (IsADirectoryError, NotADirectoryError)):
        return "wrong_type"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, UnicodeError):
        return "encoding"
    if isinstance(exc, (KeyError, TypeError)):
        return "invalid_arguments"
    if isinstance(exc, ValueError):
        if (message.startswith("Invalid tool arguments") or ARGUMENT_MESSAGE.match(message)
                or any(phrase in message for phrase in INVALID_ARGUMENT_PHRASES)):
            return "invalid_arguments"
        for kind, phrases in VALUE_ERROR_RULES:
            if any(phrase in message for phrase in phrases):
                return kind
    return "error"


def classify(exc, message):
    """(error_kind, hint or None) for a tool exception and its displayed message."""
    kind = getattr(exc, "error_kind", None)
    if isinstance(kind, str) and kind:
        hint = getattr(exc, "hint", None)
        return kind, hint if isinstance(hint, str) and hint else None
    message = message if isinstance(message, str) else str(exc)
    kind = _kind(exc, message)
    if kind == "not_found" and not isinstance(exc, FileNotFoundError):
        return kind, None  # A task, job or notebook cell: the path hint would mislead.
    return kind, HINTS.get(kind)


def tool_error_text(exc, message):
    """The JSON tool result the model receives for a failed tool call."""
    kind, hint = classify(exc, message)
    result = {"error_kind": kind, "message": message}
    if hint:
        result["hint"] = hint
    return json.dumps(result, ensure_ascii=False)
