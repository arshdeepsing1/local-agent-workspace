# Changelog

Notable changes to Local Agent Workspace, newest first. Dates are UTC.

## Unreleased

Default model, code folder, Python environment, large inputs and compaction:

- The default model is Claude Opus 5.5 (`databricks-claude-opus-5-5`) with a 200,000-token
  context budget and 16,384 Max output tokens. Saved settings still at the old default (GPT OSS
  120B at 131,072 and 8,192) move to Opus 5.5 once; Settings says so, notes that Opus 5.5 costs
  more per token and how to switch back. Budgets left at a model's defaults follow a model
  change. Settings shows each endpoint's limits and warns about budgets above what it accepts or
  above its per-minute limits; requests use the endpoint's caps. Opus 5.5 usage rates are in the
  usage panel.
- `LOCAL_AGENT_CODING_DIR` (Settings → **Default folder for new code**): where the agent creates
  new standalone scripts, tools and projects when you do not say where. Every chat may read and
  write there without a Folder access card (edits still follow the permission mode); home,
  hidden, system and app folders are refused.
- `LOCAL_AGENT_PYTHON_VENV` (Settings → **Python virtual environment**): the agent's commands,
  Verify, hooks and the Terminal run with that virtual environment first on `PATH` (and without
  the app server's own environment), the system prompt tells the agent to use it, and edits
  inside it always ask outside Bypass.
- Large inputs within the Databricks limits: requests stay under the 4 MB request limit
  (compaction also runs on size, and an oversized request is refused before it is sent);
  rate-limit responses show which limit was hit and are waited out together by every chat on the
  endpoint; a Max output tokens setting above the endpoint's output-tokens-per-minute limit is
  refused with what to set instead. New read tools `outline_file` (sections of large Markdown,
  Python and other source files with line ranges) and `project_overview` (a bounded summary of a
  large codebase); read pages grow to 128 KB on large budgets; each response's tool results
  share an allowance so one step cannot overflow the context (a read that does not fit is
  withheld with a note, other results keep their start and end). Agent requests wait up to 300
  seconds for the stream, compaction and side questions up to 600. The system prompt carries the
  date only, so Databricks can reuse its cached prefix all day.
- Compaction keeps what matters: the summary has seven fixed sections (goal and status, open
  tasks and next steps, your requirements and constraints in your words, decisions, errors,
  files, commands), large tool output is shortened before summarizing, your earlier requests are
  kept word for word in an app record next to the summary, and files whose contents left the
  context must be read again before they are overwritten. **Settings → Auto-compact at**
  (50–100% of the input budget, 90 by default) sets when automatic compaction runs. Models that
  always think get extra output room for summaries.
- **Last model input** shows the input tokens Databricks reported for the last request and their
  share of the input budget, with the components (cached input included once) and how far the
  app's estimate was off; it says when it shows an estimate instead, and follows model and
  Settings changes. A request-size line shows the body size against the 4 MB limit.

Roadmap milestone M4 (conversation control and history):

- Edit, retry and rewind: **Edit message** replaces a message and everything after it (and can
  undo the agent's file edits from that message on), **Retry response** sends the latest
  message again, and **Rewind to here** offers **Conversation and code**, **Conversation only**
  or **Code only**, listing each file it would restore and refusing when a file changed since.
  The earlier version is kept as an archived `(before rewind)` copy, linked from a notice above
  the message box. **Fork from here** opens a new chat up to a reply, optionally in a new Git
  worktree; **New chat in worktree** on the welcome screen does the same for a new chat. All of
  them need an idle chat.
- **Stop** keeps the reply's partial text in the model history with a marker saying you stopped
  it, so the next message knows what was cut short; a server restart marks it the same way.
- Messages while the agent works: **Queue message** keeps up to 3 messages (50,000 characters in
  total). They reach the agent at the next step boundary as part of the same turn, or as the
  next turn after the final reply; after Stop or an error they wait until **Send queued
  messages**. Queued messages are saved with the chat.
- `@path` (and `@path:10-40`) attaches a redacted snapshot of a project file, read with the
  chat's own file access; `!command` runs one shell command as a user action and attaches its
  output to your next message; `/btw <question>` asks a side question about the conversation
  without adding to it (billed, listed as **Side question** in the usage panel).
- Change review: a subagent's edits also show in its parent's changes bar, marked
  **Subagent**. Files a foreground `run_command` (Verify included) created, changed or deleted
  in a Git repository of the project, a granted folder or a folder added to Files are listed
  with a **Command** mark and can be kept or undone. **Automatic Keep** (Settings, off by
  default) keeps a completed reply's changes. **Line comments** in a file's review are sent
  together as one message.
- Sidebar: **Pin**, **Archive** and **Edit tags…** from a row's **More actions**, a tag filter,
  an amber dot for chats with unreviewed changes, and **Search conversations** that also
  searches message text (redacted excerpts, bounded at 64 MiB or 3 seconds, no index).
- Long chats open with their latest 200 messages (extended to the whole latest turn) and
  **Load earlier messages** loads more. After a clean shutdown the app starts by reading only
  each conversation's header line.

Roadmap milestone M3 (permission rules, plan hand-off, questions):

- Permission rules: **Always allow…** on an approval card saves a rule for this chat, this
  project or all projects: an exact command, commands starting with a prefix followed only by
  listed options, all file edits or edits under one folder, an MCP tool or a hook. The
  Permissions menu lists rules by scope, adds **Deny** and **Always ask** rules (**Add a
  rule…**) and removes them. Deny beats ask, and ask beats allow, in every mode (Bypass
  included); rules never override credential exclusions, folder access or Plan. Project and
  all-project rules live in `permission_rules.json` in the state folder, never in a repository.
- Commands allowed by an exact or prefix rule run without a shell, and shell syntax never
  matches such a rule. Programs that run other programs or code (`sh`, `sudo`, `env`, `xargs`,
  `python -c`, `git -c`, `rg --pre` and similar) cannot be allowed by a rule, and a command a
  deny or ask rule might cover but cannot be checked exactly gets a card.
- Auto and Plan run a fixed list of read-only command forms (`pwd`, `ls`, `git status`,
  `git diff`, `git log --oneline` and a few more) without a card, with Git hardened against a
  repository's own programs (fsmonitor, hooks, filters, external diff and text conversion,
  pager, signing, lazy fetch). No test runners or package managers are on the list.
- Modes: a new **Don't ask** mode refuses instead of asking. The mode can change while a
  response runs or a card waits; waiting cards are checked again, a running subagent follows,
  and switching to Bypass asks for confirmation. Protected files (editor, CI and agent
  configuration such as `.github/workflows`, `.vscode`, `AGENTS.md`, `CLAUDE.md`, shell startup
  files) always ask outside Bypass. **Agent tools → Tools** turns tools off for one chat.
  `delegate_task` and `stop_job` ask in Manual mode.
- **Edit before approving**: change a waiting command, a `write_file` content or a single
  `edit_file` replacement; the app checks it again and shows the new preview.
- Plan hand-off: in Plan the agent calls `submit_plan`; its card offers **Approve** (then
  Accept edits or Manual) or **Keep planning** with feedback. Questions: `ask_user` asks 1–4
  questions with 2–4 options plus **Other**, also in Plan; subagents' questions reach you
  through the parent chat. Both cards use the approval wait.
- The state folder (settings, rules, conversations) cannot be reached through any file tool.
- Markdown transcripts include answers to questions and plan reviews.

Roadmap milestone M2 (agent quality):

- Search and reading: a new `find_files` tool (globs with `**` and `{a,b}`, sort by path or
  last change, paging). `search_files` honours `.gitignore` inside Git repositories by
  default (`include_ignored` turns it off), has output modes (matching lines, matching
  files, counts) and uses ripgrep when it is installed, with the same results without it.
  Case-insensitive literal search now uses simple case folding ("STRASSE" no longer finds
  "Straße"; regular expressions keep full folding), and results come in path order.
  `read_file` pages grow with the context window (8–32 KB), read up to 2,000 lines, can
  read the last lines of a file (`tail_lines`) and replace invalid UTF-8 with a note. Lines
  are split at LF only, so line numbers match editors.
- Commands: a failed or timed-out command shows the start and the end of its output;
  Settings → Command environment passes named variables to commands (credential and loader
  variables are refused); Stop and time limits send SIGTERM and wait 2 seconds before
  killing. A new `wait_job` tool waits for a background job to end or to print a line
  matching a pattern, and background jobs the agent started post a notice when they end.
- File edits: `edit_file` can replace every match (`replace_all`) and make several edits in
  one call (`edits`); edits keep a file's line endings (CRLF stays CRLF). Settings → File
  write limit (16–1,000 KB, 80 KB by default) governs new file content and the editor. New
  `move_path` and `delete_path` tools for single text files, with Undo, and
  `notebook_read`/`notebook_edit` for Jupyter notebooks. After an edit the app checks the
  file offline (Python syntax, JSON, TOML, notebooks, merge-conflict markers) and tells the
  agent only about problems the edit introduced. Diffs stay fast on large files.
- Agent loop: 502/503/504 responses and connection errors before a response starts are
  retried (up to 3 times, each recorded in Agent tools → Usage). After those retries, a
  rate limit is waited out (Retry-After up to 120 seconds, at most 2 extra waits; Stop
  cancels; Settings toggle, on by default). Invalid tool arguments or an incomplete reply
  get up to 2 hidden repair requests. Tool errors reach the agent as
  `{error_kind, message, hint}`. A per-project **Verify** command (Agent tools → Verify) runs
  after the agent changes files, through the normal approvals, for at most 3 rounds.
- Context: Settings → Custom instructions (up to 8,000 characters) apply to every chat. A
  short index of available skills (at most 2 KB) is part of the system prompt, and skills in
  `.claude/skills` are read (never written).
- Replies: `path:line` mentions become links that open the file at that line in Workspace →
  Files, which also offers **Open in VS Code**.
- Workspace → Changes shows Git's view as **Staged changes**, **Changes** and **Untracked**
  with counts and per-file diffs; generated folders are grouped and credential files never
  show a diff.
- Workspace → Files: **Add folder…** adds a folder to Files for the project (view only: the
  agent still needs Folder access), with change dots, and offers **Add <folder>** for changed
  files outside every folder. Every file the agent changed can be reviewed, kept or undone,
  also after leaving Bypass permissions or removing a folder grant.
- Safety: regular expressions whose counted repeats would expand too far are refused, and
  paths a Git index lists outside the searched folder are skipped.
- CI installs ripgrep on Ubuntu and macOS.

Roadmap milestone M1 (attention, approval quick wins, safety):

- Notifications: opt-in browser notifications when a chat needs approval, finishes, reaches the
  step limit or stops with an error; a "(n)" count in the tab title; optional desktop notifications
  from the server when no page is open (Settings). They show only a status and the chat title,
  with credentials redacted from a title made from the first message.
- Sidebar status: Needs approval, working, Error and unread markers, and a Running filter.
- Approvals: **Allow for this chat** (an exact command, edits, an MCP tool or a hook), **Decline
  with a note** that reaches the agent, and errors that name the busy chat with an Open button.
- Safety: Workspace → Changes runs Git without a shell, hooks, fsmonitor or filters; an unreadable
  conversation file is reported and hidden instead of stopping the app; a stalled browser tab is
  disconnected after 5 seconds; `write_file` refuses to overwrite a file the agent has not read
  (or that changed since), and the agent is told which files changed under it.
- Storage: "Allow for this chat" rules and the files the agent read are kept in
  `conversations/<id>.state.json`, so earlier versions can still open chats saved by this one.
- Conversation: a Continue button after a step limit, output limit, Stop or restart; a live task
  checklist and a `get_task` tool; message drafts saved across reloads; highlighted code blocks
  with Copy code; Markdown export and Copy conversation in Agent tools → Conversation.
- Stopping commands: on macOS, Workspace → Changes and command jobs no longer fail with "Operation
  not permitted" when the command has just exited. A command that does not end when stopped (for
  example one running as another user through sudo) no longer makes Stop or a time limit hang:
  after 5 seconds the job ends with a note that the command may still be running.
- Workspace → Files can browse folders granted to the chat, with change dots there too.
- The volatile parts of the system prompt (time, granted folders) now come last.
- Continuous integration: offline backend and UI tests, a lint check and a personal-data check on
  Ubuntu and macOS for every pull request and push to `main` (`.github/workflows/tests.yml`).
  `scripts/check_personal_data.py` runs the same personal-data check locally.
- `handoffs/` folders are ignored by Git, so automatic compaction handoffs written into a clone
  of this repository are not committed by mistake.

## 2026-10-06

- Approval wait: **Settings → Approval wait (minutes)**, 60 by default (1–1,440; 0 waits until
  answered). An unanswered card says "Not answered in time" and the agent is told nobody
  answered, not that you declined. Actions blocked without a card say "Not run:" and why.
- Review agent changes as in an editor's chat: a "files changed" bar above the message box with
  Keep and Undo, dots on changed files in Workspace → Files, and a red/green diff with Keep and
  Undo for each change. Undo is a normal file checkpoint and can be restored from Recovery.

## 2026-09-30

- One long request can outgrow the context budget: its earlier tool results are summarized and
  it continues. Read cards name the lines each page covered.
- Tools called without arguments run, and `/<skill-id>` anywhere in a message selects that skill.
- `scripts/conversation_transcript.py` turns saved conversations into Markdown transcripts.

## 2026-09-28

- `/` in the message box lists and inserts skills; the app's own skills are offered in every
  workspace.
- First public release: a no-build Preact frontend served by the Python backend (no Node.js),
  streamed chat with Databricks models, file and command tools with approvals and permission
  modes, background jobs, compaction, checkpoints, worktrees, MCP, skills, hooks, tasks and
  subagents.
