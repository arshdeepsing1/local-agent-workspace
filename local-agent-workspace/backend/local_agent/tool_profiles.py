"""Small, server-enforced capability ceilings for delegated work."""

PROFILES = ("read_only", "file_editor", "inherit")
READ_TOOLS = frozenset({"list_files", "find_files", "read_file", "search_files", "list_skills", "use_skill", "list_tasks",
                        "get_task", "notebook_read", "ask_user", "outline_file", "project_overview"})
FILE_TOOLS = READ_TOOLS | {"write_file", "edit_file", "insert_activity_log", "move_path", "delete_path", "notebook_edit"}


# Tools that show what another tool shows: turning off (or denying) a covering tool also
# covers them. outline_file shows file content (headings, signatures); project_overview
# lists file names and sizes.
COVERED_BY = {"outline_file": ("read_file",), "project_overview": ("list_files", "find_files")}


def covering_tool(name, names):
    """The first tool in names that covers name (COVERED_BY), else None."""
    names = set(names or ())
    return next((cover for cover in COVERED_BY.get(name, ()) if cover in names), None)


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
    disabled = session.get("disabled_tools") or []
    if name in disabled or covering_tool(name, disabled):
        return False  # Turned off by the user in this chat, directly or through a tool that covers it.
    profile = validate_profile(session.get("tool_profile", "inherit"))
    return profile == "inherit" or name in (READ_TOOLS if profile == "read_only" else FILE_TOOLS)


def filter_tools(session, definitions):
    return [definition for definition in definitions if tool_allowed(session, definition["function"]["name"])]
