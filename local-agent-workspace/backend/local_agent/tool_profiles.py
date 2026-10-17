"""Small, server-enforced capability ceilings for delegated work."""

PROFILES = ("read_only", "file_editor", "inherit")
READ_TOOLS = frozenset({"list_files", "find_files", "read_file", "search_files", "list_skills", "use_skill", "list_tasks",
                        "get_task", "notebook_read", "ask_user", "outline_file", "project_overview", "get_tool_output",
                        "report_findings"})
FILE_TOOLS = READ_TOOLS | {"write_file", "edit_file", "insert_activity_log", "move_path", "delete_path", "notebook_edit"}


# Tools that show what another tool shows: turning off (or denying) a covering tool also
# covers them. outline_file shows file content (headings, signatures); project_overview
# lists file names and sizes.
COVERED_BY = {"outline_file": ("read_file",), "project_overview": ("list_files", "find_files")}

# Helpers the app offers once a chat needs them (AgentManager.feature_definitions): get_tool_output after a long
# result was kept, report_findings once a skill that names it was selected. An agent file's tools list does not
# take them away; its disallowed_tools does.
FOLLOW_UP_TOOLS = frozenset({"get_tool_output", "report_findings"})


def covering_tool(name, names):
    """The first tool in names that covers name (COVERED_BY), else None."""
    names = set(names or ())
    return next((cover for cover in COVERED_BY.get(name, ()) if cover in names), None)


def server_off(name, disabled):
    """The mcp__<server>__* entry in disabled (a chat's turned-off tools) that turns name's server off, else None."""
    if not isinstance(name, str) or not name.startswith("mcp__"):
        return None
    return next((item for item in disabled or () if isinstance(item, str) and item.startswith("mcp__")
                 and item.endswith("__*") and name.startswith(item[:-1])), None)


def off_servers(disabled):
    """The IDs of the MCP servers a chat's turned-off tools switch off (mcp__<id>__*)."""
    return [item[5:-3] for item in disabled or () if isinstance(item, str) and item.startswith("mcp__")
            and item.endswith("__*") and len(item) > 8]


def validate_profile(value):
    if not isinstance(value, str) or value not in PROFILES:
        raise ValueError("Tool profile must be inherit, read_only, or file_editor.")
    return value


def child_profile(parent, requested=None):
    ceiling = validate_profile(parent.get("subagent_tool_profile", "inherit"))
    inherited = validate_profile(parent.get("tool_profile", "inherit"))
    requested = ceiling if requested is None else validate_profile(requested)
    return min((ceiling, inherited, requested), key=PROFILES.index)


def agent_refusal(session, name):
    """Why a custom agent's subagent (custom_agents.py) may not use name, else None: its file's tools list
    does not include it (FOLLOW_UP_TOOLS aside), its disallowed_tools take it away, or its mcp_servers leave out
    the tool's server."""
    agent_tools = session.get("agent_tools")
    if isinstance(agent_tools, list) and name not in agent_tools and name not in FOLLOW_UP_TOOLS:
        return "tools"
    denied = session.get("agent_denied_tools")
    if isinstance(denied, list) and (name in denied or covering_tool(name, denied) or server_off(name, denied)):
        return "disallowed_tools"
    servers = session.get("agent_mcp_servers")
    if isinstance(servers, list) and isinstance(name, str) and name.startswith("mcp__") and not any(
            name.startswith(f"mcp__{server}__") for server in servers):
        return "mcp_servers"
    return None


def tool_allowed(session, name):
    disabled = session.get("disabled_tools") or []
    if name in disabled or covering_tool(name, disabled) or server_off(name, disabled):
        return False  # Turned off by the user in this chat: directly, through a covering tool or its MCP server.
    if name == "submit_report":
        return bool(session.get("is_subagent"))  # A subagent's final report, whatever its tools (feature_tools.py).
    if agent_refusal(session, name):
        return False
    profile = validate_profile(session.get("tool_profile", "inherit"))
    return profile == "inherit" or name in (READ_TOOLS if profile == "read_only" else FILE_TOOLS)


def filter_tools(session, definitions):
    return [definition for definition in definitions if tool_allowed(session, definition["function"]["name"])]
