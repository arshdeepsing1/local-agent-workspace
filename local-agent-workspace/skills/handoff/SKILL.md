---
name: handoff
description: Write a detailed cold-start handoff (often 20-60 KB) so a new session with zero context can resume the work, with an exact app-generated log of every command run. Use when the user asks for a handoff, memory file, or session summary.
---

# Detailed session handoff

The reader is a new LLM session with zero prior context. Write every section so that reader can reconstruct the working state: specific details, not topic labels. Completeness beats brevity in this document; conciseness applies to chat replies, not to the handoff. A long working session typically needs 20-60 KB. A short read-only session can be smaller, but never drop facts to save space. If a section has nothing to report, write "None this session" instead of removing it.

## Before writing
1. Date: use the current local date and time stated in the system prompt. Never infer the date from file names or older handoffs.
2. If the work touched a Git repository, run `git status --short` and `git log --oneline -5` there (one command) so file changes and commits are exact.
3. If earlier turns were compacted (your context contains "Summary of earlier conversation"), say so in the handoff and never invent details you can no longer see. The app's activity log (below) still lists every command and file from those turns.
4. Follow any extra instructions in the user's message. Do not stop to ask questions unless the destination is unclear.
5. Resuming an interrupted handoff: read the end of the file and continue from the `<!-- handoff continues -->` marker; do not start over.

Cost: every extra model request resends the whole conversation, which is what uses Databricks' per-minute input-token limit and most of the cost. Batch independent tool calls into one response, do not call `get_job_output` or `list_jobs` just to list commands (the activity log does that), and do not re-read files you already read.

## Required sections, in this order
1. **Header**: title, date and time with timezone, project, key paths, repository/branch/commit if known, and a paragraph on current status.
2. **WORK DONE**: what was accomplished, decisions with reasoning, outputs created, conclusions. Include names, numbers, file names, account/namespace/cluster names, configuration keys and values, versions, IDs, row counts, and exact error codes.
3. **Issues faced -> root cause -> fix**: one subsection per issue: symptom with the exact error text, how it was diagnosed, root cause, fix, and how it was verified. Include dead ends and why they failed.
4. **Code and file changes**: every file created, modified, or deleted (full path), what changed and why, with snippets for non-obvious changes.
5. **Commands run**: 3-10 bullets on the commands that mattered and what they showed, then a line containing only `<!-- activity-log -->`. Do not type the full command list yourself: the app replaces that line with every command, its result, and the files touched.
6. **Useful commands (runbook)**: copy-paste commands to verify, rerun, deploy, debug, and roll back, with placeholders for secrets.
7. **Key facts and gotchas**: environment details, constraints, and "do not do X because Y" lessons.
8. **FILES & RESOURCES USED**: every file read, written, or referenced (full paths), every URL visited, and every data source, service, or tool touched. No omissions.
9. **OPEN ITEMS**: one per line: `- [What is unresolved] — Needs: [specific action or decision] — Owner: [Me(User) / Assistant / Research]`. No vague entries; anything that cannot be written this specifically belongs in WORK DONE.
10. **NEXT ACTIONS**: numbered, specific first tasks in order, with exact commands and paths, so the next session can start without a clarifying question. Do not execute them now.

End the document with this line exactly:
Please read any referenced files listed above and confirm you are oriented before we begin.

## Writing the file
- Destination: `handoffs/YYYY-MM-DD-<short-topic>.md` in the workspace unless the user names another folder. Check the folder with `list_files` first; never overwrite an existing handoff (add `-v2`, `-v3`), and continue any existing version numbering such as `project-v12-...`.
- Write in parts so no single response gets too long: create the file with `write_file` holding the header and first sections, ending with the line `<!-- handoff continues -->`. Append each next part with `edit_file`, replacing that marker line with the new sections followed by the same marker. Size each part at about half the file text the system prompt says one response can hold (for example about 20 KB when it says about 40 KB): fewer, larger parts mean fewer full-conversation requests. If a part is cut off at the output limit, redo it at half that size. The final edit removes the marker.
- Last step: call `insert_activity_log` with the handoff's path. If it reports `commands_omitted` above 0 or says the file is too large, call it again with a new `<name>-activity.md` file and link that file from section 5.
- The app's file limit is 80 KB. If the handoff will exceed it, continue in `<name>-part2.md` and link the parts to each other.
- Never write secrets: replace tokens, passwords, keys, and connection strings with `<redacted>` and name the variable or file that holds them.

## Finish
Reply briefly with the path, the approximate size, and anything you could not recover (for example compacted turns).
