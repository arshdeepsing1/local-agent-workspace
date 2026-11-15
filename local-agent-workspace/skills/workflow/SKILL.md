---
name: workflow
description: Use when the user types /workflow with a task that splits into independent parts for subagents to run in parallel.
disable-model-invocation: true
---

# Run a task as a workflow

The user's task follows the command. You are the orchestrator. Subagents do the parts, and you combine their reports.

## Split the task
1. Split the task into a small number of independent parts, as few as the task needs. A part must not need the result of another part.
2. Never give two parts the same files to edit. If parts overlap, merge them or run them in a later batch.
3. If the task does not split, say so. Then do it yourself or ask the user one question.
4. The app refuses a call over the per-turn subagent limit set in Settings. You are not told the number. If a call is refused, do not retry it. Continue with the subagents you have.

## Write each part
A subagent sees nothing of this chat, so each call must stand alone.
- `task`: the goal of this part in one or two sentences.
- `context`: everything the subagent needs. Give the paths, the constraints, the user's standing instructions, what "done" means and what to report.
- `agent_type`: `explore` for a part that only reads, `editor` for a part that edits files without commands, `general` for the rest.
- `tool_profile`: leave it out unless you must narrow tools. The values are `read_only`, `file_editor` and `inherit`.
- `max_steps`: 1 to 24. Give a small part 6 to 10 and a large part more.
- For a part that edits, name the files it owns in `context` and tell it to touch no others.

## Start the batch
Send all `delegate_task` calls in one response, next to each other. Put no other tool call between them. Then they run as one parallel batch.

Do not set `background` unless the user asked for it. Wait for all the reports.

## Combine the reports
Write one answer. For each part, give its name, what its subagent found or changed, what failed or was stopped, and what is left. Never invent a result for a subagent that failed. Say that it failed and why.

If the user asked for changes, run the project's checks once after the batch. Use the `verify` skill for that.
