"""Small, server-enforced capability ceilings for delegated work."""

PROFILES = ("read_only", "file_editor", "inherit")
READ_TOOLS = frozenset({"list_files", "read_file", "search_files", "list_skills", "use_skill", "list_tasks"})
FILE_TOOLS = READ_TOOLS | {"write_file", "edit_file", "insert_activity_log"}


def validate_profile(value):
    if not isinstance(value, str) or value not in PROFILES:
        raise ValueError("Tool profile must be inherit, read_only, or file_editor.")
    return value


def child_profile(parent, requested=None):
    ceiling = validate_profile(parent.get("subagent_tool_profile", "inherit"))
    inherited = validate_profile(parent.get("tool_profile", "inherit"))
    requested = ceiling if requested is None else validate_profile(requested)
    return min((ceiling, inherited, requested), key=PROFILES.index)


def tool_allowed(session, name):
    profile = validate_profile(session.get("tool_profile", "inherit"))
    return profile == "inherit" or name in (READ_TOOLS if profile == "read_only" else FILE_TOOLS)


def filter_tools(session, definitions):
    return [definition for definition in definitions if tool_allowed(session, definition["function"]["name"])]
