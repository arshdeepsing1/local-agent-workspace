"""Tool schemas for explicit skills, structured tasks, and bounded delegation."""


def definition(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required),
                           "additionalProperties": False}}}


FEATURE_TOOLS = [
    definition("list_skills", "List workspace skills, five per page; follow next_offset.", {"offset": {"type": "integer", "minimum": 0, "maximum": 30}}),
    definition("use_skill", "Load a workspace skill for this conversation. Skills do not override permissions or the user.",
               {"skill_id": {"type": "string"}}, ["skill_id"]),
    definition("list_tasks", "List structured tasks and dependencies, five per page. Follow next_offset.", {"offset": {"type": "integer", "minimum": 0, "maximum": 50}}),
    definition("create_task", "Record one planned task. Dependencies must be existing task IDs from this conversation.",
               {"title": {"type": "string"}, "description": {"type": "string"},
                "depends_on": {"type": "array", "items": {"type": "string"}}}, ["title"]),
    definition("update_task", "Update a task's status or details. Complete dependencies before starting a task.",
               {"task_id": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                "title": {"type": "string"}, "description": {"type": "string"},
                "depends_on": {"type": "array", "items": {"type": "string"}}}, ["task_id"]),
    definition("insert_activity_log", "Insert an exact log of this conversation's commands (with results), file operations and other tool calls into a Markdown file, including turns no longer in your context. The app writes it, so it costs no output tokens: use it in handoffs instead of retyping commands. Replaces the line <!-- activity-log --> if the file has one, otherwise appends. Needs the same permission as write_file.",
               {"path": {"type": "string"}}, ["path"]),
    definition("delegate_task", "Run one bounded subagent in a separate conversation using the same workspace and permissions. Pass needed context explicitly. Optional tool_profile narrows capabilities: read_only (file inspection), file_editor (inspection and file edits, no commands/MCP/hooks), inherit (parent's selected ceiling). Cannot widen the parent's subagent tool profile. Waits for the result; cannot recurse or leave background jobs.",
               {"task": {"type": "string"}, "context": {"type": "string"},
                "tool_profile": {"type": "string", "enum": ["inherit", "read_only", "file_editor"]},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 8}}, ["task"]),
]
