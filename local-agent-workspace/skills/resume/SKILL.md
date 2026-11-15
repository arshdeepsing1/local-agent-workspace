---
name: resume
description: Use when the user asks, at the start of a session, to resume a project from its handoff memory (the brief and the newest log).
---

# Resume a project from its handoff memory

The reader is a new session with no prior context. Rebuild the working state from the handoff files, then wait for the user.

## Find the files
1. Work out `<project>`. Use the name the user gives. Otherwise use the Git repository name. Otherwise use the workspace folder name.
2. Work out the memory folder. Use the folder the user names. Otherwise use `handoffs/` in the workspace.
3. Call `list_files` on `<folder>/` and on `<folder>/HANDOFF/` in one response.

The memory has two kinds of file:
- The brief: `<folder>/<project>-00-context.md`. It describes the project as it is now. The app rewrites it at every handoff. It is the single source of truth.
- Session logs: `<folder>/HANDOFF/<project>-handoff-vNN-YYYY-MM-DD-<topic>.md`. They are append-only. `NN` is zero-padded.

## Read
1. Read `<project>-00-context.md` fully. Follow `next_line` until the end.
2. Read the newest log fully. The newest log is the one with the highest `vNN` among `<project>-handoff-v*.md`. A `-part2.md` file belongs to its log, so read both. Ignore `-activity.md` files. They are command logs.
3. Batch these reads into one response. Never read a file twice.
4. Do not read older logs unless a task needs history. The SESSION INDEX in the brief says which log covers what.
5. Where an older log disagrees with the brief, the brief wins.
6. Do not read secrets files unless the user asks.

## Check what you read
- Re-verify anything marked `[unverified]` before you rely on it. Say what you would re-check. Run read-only checks only if the user asked you to continue work.
- If the brief is missing, say so and read the newest log only.
- If there are no logs, say so and read the brief only.
- If nothing exists (no brief, no logs, no old-layout files), say so and stop.
- If only old-layout handoffs exist (dated files directly in `handoffs/`), say so and suggest `/migrate-handoffs`.

## Reply
1. A summary of the project and its current state, in 5 lines.
2. The open items.
3. The next action you would take.
4. End with a question that asks which next action to take.

Start no work until the user says so.
