---
name: migrate-handoffs
description: Use when the user wants to move old one-file handoff logs into the HANDOFF/ layout and build a project brief. Runs once per project.
---

# Migrate old handoffs

Move old handoff logs into the app layout, then write one brief from them. Never change a log's content. Never overwrite a file.

## Layout
- Brief: `<folder>/<project>-00-context.md`
- Logs: `<folder>/HANDOFF/<project>-handoff-vNN-YYYY-MM-DD-<topic>.md`
- `<folder>` is `handoffs/` in the workspace, or the folder the user names.
- `<project>` is the name the user gives, else the Git repository name, else the workspace folder name.
- `<topic>` is 3 to 6 words in kebab-case. `vNN` is zero-padded.

## Steps
1. Find `<project>` and the folder. If `HANDOFF/` already holds logs of this project or a brief exists, stop and ask the user before you renumber or replace anything. List the old handoff files with `list_files`. Skip `<folder>/auto/` and anything already in `HANDOFF/`. Leave other files (reports, plans, data, folders) in place and list them in your reply.
2. Order the files oldest first. Use the date in the file name, then a version number in the name, then the date in the file's first lines.
3. Show the user a table of old name -> new name. Move nothing before that.
4. Move each file to its new name in that order. Use the `move_path` tool. For a file Git tracks, use `git mv` through `run_command` so history is kept. For a file over the file write limit, use `mv` through `run_command`. An old `-activity.md` or `-part2` file keeps its log's version and moves with it.
5. Read every log oldest first, each once and completely. Follow `next_line`. Later logs override earlier ones.
6. Write `<folder>/<project>-00-context.md` in parts, so no response is cut off. Create it with `write_file`, ending with the line `<!-- handoff continues -->`. Append each part with `edit_file`, replacing that marker. Remove the marker in the final edit. Aim for 32 KB. Never pass 48 KB.
7. Reply with the rename table, the brief's path and size, the next version number, and each file you could not place. Say that `/handoff` ends a session and `/resume` starts one from now on.

## Brief sections, in this order
1. Header: project name, `Last updated: <date time tz> by vNN`, a one-paragraph status.
2. WHAT AND WHY
3. ARCHITECTURE
4. WHERE THINGS LIVE
5. CURRENT LIVE STATE
6. HOW TO WORK ON IT
7. CONVENTIONS AND USER PREFERENCES
8. GOTCHAS
9. OPEN ITEMS
10. SESSION INDEX: a table, newest first, with `vNN | date | file name | one-line summary`. List every log.
11. SUPERSEDED FACTS: what older logs state that is no longer true, and the truth now.

Describe the project as it is now. End the file with this line:
`Please read any referenced files listed above and confirm you are oriented before we begin.`

## Rules
- Never write a secret value. Write `<redacted>` and name where it lives. If an old log holds a plaintext secret, tell the user and do not copy it.
- State facts only. Mark guesses `[assumption]`.
- Mark state facts taken from old logs `[unverified]`.
