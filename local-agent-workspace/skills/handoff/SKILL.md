---
name: handoff
description: Use when the user asks for a handoff, memory file or session summary. Writes a session log and a project brief.
---

# Two-file handoff memory

The reader is a new LLM session with zero context. Write concrete details (names, paths, IDs, SHAs, versions, numbers, exact error text), not topic labels. Completeness beats brevity. An empty section says "None this session".

Write two files. `<folder>` is `handoffs/` in the workspace unless the user names another folder. `<project>` is a name the user states as the project (`/handoff project=<name>` or "for project <name>"), else the Git repository name, else the workspace folder name. Never take the first word of a free-text request as the project.
- Session log, append-only: `<folder>/HANDOFF/<project>-handoff-vNN-YYYY-MM-DD-<topic>.md`. `<topic>` is a 3-6 word kebab-case summary you choose. `vNN` is zero-padded (v01).
- Brief, created or fully rewritten: `<folder>/<project>-00-context.md`.

## Before writing
1. Take the date and time from the system prompt, never from file names.
2. List `<folder>/` and `<folder>/HANDOFF/` with `list_files`. "Not a directory." means that folder does not exist yet, so it has no logs: use v01. If dated or versioned handoff files sit directly in `<folder>/` and `HANDOFF/` has no log of this project, stop. Tell the user to run `/migrate-handoffs` first, or to confirm a fresh start at v01. Otherwise read the brief if it exists and the newest log.
   Next version = highest `vNN` among `<project>-handoff-v*.md` + 1. Ignore `-part2.md` and `-activity.md` companions. If `write_file` reports that the log exists, or the listing shows that version taken (two chats at once), list again and take the next version. Never read and replace another log.
3. For each Git repository touched, run in one command `git status --short`, `git branch --show-current` and `git log --oneline -5`.
4. If live state matters, re-check it read-only now. Record the command and its result. Label each state fact `[verified <time>]` or `[unverified - from earlier in session]`.
5. Follow extra instructions in the user's message. Ask only if the destination is unclear.
6. Resuming an interrupted handoff: read the end of the file and continue from the `<!-- handoff continues -->` marker.
7. If your context contains "Summary of earlier conversation", the log must still cover the chat from its start. The summary names the auto handoffs of this chat (`handoffs/auto/...-compaction-<n>.md`, whose first lines name the conversation ID). Oldest first, read each one's Contents list, then only its state sections: goal and status, open tasks, requirements, decisions, errors and fixes, files changed, work done, key facts. Skip its command and activity logs. Carry the facts in, list each auto handoff under FILES & RESOURCES USED, say the chat was compacted, and never invent details. If none exist, use the summary and say what is missing.
8. When the handoff must cover older notes, memory files or past conversations, list them first. Read each once and completely, oldest first. Read past conversations from Markdown transcripts, not raw `.jsonl` files.

Cost: batch tool calls, do not call `get_job_output` or `list_jobs` just to list commands, do not re-read files.

## FILE 1 - session log
Sections, in this order:
1. Header: title, date, time and timezone, previous log path, brief path, repository, branch, HEAD SHA, environment, and a 3-5 line status.
2. WORK DONE: what was accomplished, with concrete values (config keys, IDs, counts, tags, run IDs, PR numbers).
3. DECISIONS: one per line: decision, why, alternatives rejected, who decided.
4. ISSUES -> ROOT CAUSE -> FIX: exact error text, root cause, fix, verification, dead ends.
5. CODE AND FILE CHANGES: every file created, modified or deleted (full path), what and why, snippets for non-obvious changes, commit SHA or local.
6. SUPERSEDED FACTS: statements in earlier logs or the brief that are now false, as old fact -> new fact -> source log.
7. COMMANDS RAN: 3-10 bullets on the commands that mattered, each with a one-line result. Then a line containing only `<!-- activity-log -->`. Do not type the full command list. The app replaces that line with every command, its result and the files touched.
8. USEFUL COMMANDS: copy-paste commands to verify, rerun, deploy, debug and roll back, with secret placeholders.
9. KEY FACTS AND GOTCHAS: environment limits and "do not do X because Y" lessons.
10. FILES & RESOURCES USED: every file read, written or referenced (full paths), URLs, services, databases, tickets. No omissions.
11. OPEN ITEMS: one per line: `- [unresolved] - Needs: [action or decision] - Owner: [User / Assistant / Other: name]`.
12. NEXT ACTIONS: numbered first tasks with exact commands and paths. Do not run them now.

## FILE 2 - brief
Describe the project as it is NOW. Merge the old brief with this session and remove what this session made false (no "update:" notes). Sections, in this order:
1. Header: project name, "Last updated: <date time tz> by vNN", one-paragraph status.
2. WHAT AND WHY: what the project does, for whom, ticket IDs.
3. ARCHITECTURE: components, data flow, languages, frameworks.
4. WHERE THINGS LIVE: local paths, remote repositories, branches, registries, clusters, accounts, databases, secret NAMES only.
5. CURRENT LIVE STATE: per environment: version, SHA or tag, health, last deploy, with `[verified <date>]` labels.
6. HOW TO WORK ON IT: short runbook: build, test, deploy, rollback, access.
7. CONVENTIONS AND USER PREFERENCES: commit style, branch rules, approvals, things the user dislikes.
8. GOTCHAS: standing lessons from all sessions.
9. OPEN ITEMS: the full current list, closed ones removed.
10. SESSION INDEX: table, newest first: vNN | date | file name | one-line summary. List every log in HANDOFF/.
11. SUPERSEDED FACTS: facts older logs state that are no longer true, with the truth now.

## Rules
- Never write secret values. Write `<redacted>` and name the variable or file that holds it.
- Facts only. Mark guesses `[assumption]` and unknowns `[unknown]`.
- Use full absolute paths.
- Sizes: brief aim 32 KB, hard cap 48 KB. If the brief would pass 48 KB, move session detail into the log until it fits. Completeness wins over the aim, never over a cap. Log aim 15-50 KB. Split a log into `-part2.md` at 100 KB, or at the app's file write limit (Settings, 80 KB by default) if that is lower. Link the parts both ways and list the parts at the top of the first. Never overwrite or edit an existing log.
- If the brief names a progress tracker file, update its done and pending items for this session's work.
- End both files with this line exactly:
Please read any referenced files listed above and confirm you are oriented before we begin.

## Writing the files
1. Write FILE 1 first, in parts. Create it with `write_file`, ending with the line `<!-- handoff continues -->`. Append each next part with `edit_file`, replacing that marker with the new part and the marker. Size each part at about half the file text the system prompt says one response can hold. Redo a cut-off part at half size. The final edit removes the marker.
2. Call `insert_activity_log` with the log's path. If it reports `commands_omitted` above 0 or the file is too large, call it again with a new `<name>-activity.md` and link it from COMMANDS RAN.
3. Write FILE 2 the same way, in parts. Read the old brief again only if the app refuses the write.

Reply briefly with both paths, their approximate sizes, and anything you could not recover (for example compacted turns).
