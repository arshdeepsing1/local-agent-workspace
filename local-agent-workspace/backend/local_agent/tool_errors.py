"""Model-facing tool errors: a machine-readable category and a recovery hint.

The conversation card keeps the plain message; the model receives
{"error_kind", "message", "hint"} as JSON so it can tell a mismatching edit from
a missing file or an exclusion it must not work around.
"""
import json


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
}

# Checked in order; the first rule whose phrases occur in the message wins.
VALUE_ERROR_RULES = (
    ("excluded", ("Credential files are excluded", "directory is excluded")),
    ("access_required", ("needs access approval",)),
    # Before stale_file: 'Project instructions changed before execution' is about guidance, not a file.
    ("guidance_changed", ("instructions changed",)),
    ("stale_file", ("changed while waiting", "changed before execution", "changed since")),
    ("edit_mismatch", ("old_text",)),
    ("not_found", ("was not found", "not found in this conversation")),
    ("unavailable_tool", ("not available",)),
    ("limit", ("limit reached", "exceeds", "too large", "more than")),
)
INVALID_ARGUMENT_PHRASES = ("must be an integer", "must be a boolean", "must be booleans", "must be true or false", "Give either")


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
        if message.startswith("Invalid tool arguments") or any(phrase in message for phrase in INVALID_ARGUMENT_PHRASES):
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
