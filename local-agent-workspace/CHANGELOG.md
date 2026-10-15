# Changelog

Notable changes to Local Agent Workspace, newest first. Dates are UTC.

## Unreleased

Follow-up of 2026-10-15:

- Custom agents: Markdown files in a project's `.agents/agents` (or `.claude/agents`) define
  subagents with a description, tools, profile, model, effort, skills and default steps. The
  model chooses them from a short agent index in the system message, and `delegate_task`
  checks the name. Agent tools → Subagents lists every agent type and any agent file that
  cannot be used, with the reason.
- Each subagent type can have its own endpoint and reasoning effort (**Settings →
  Subagents**); explore now runs at Fast by default. Cards, Agent tools and the subagents index
  show the effort and model a subagent started with, and a subagent on a smaller endpoint keeps
  its requests within that endpoint's budgets.
- A custom agent never widens what its chat allows: its tools stay within the subagent tool
  ceiling, turned-off tools, rules and mode, and Manual mode's approval card names the agent,
  its tools, model and effort before it starts.
- Spending limit per conversation: a soft cap on the estimated DBU of a chat and its
  subagents, checked before each agent model request. **Settings → Agent** sets the default
  (0 = none), **Agent tools → Usage** shows the spend against the limit and sets a chat's own.
  One notice at 80%; at the limit the turn ends before the next request, with **Continue**
  and a "Reached the spending limit" notification.
- Hooks can answer in JSON: a before-tool hook can deny a call, ask for an approval card (also
  in Bypass), replace its arguments (checked again against the schema, rules and card) or add
  context for the agent; after-tool and failure hooks can add context.
- New hook events: `user_prompt` (block, edit or extend your message), `session_start`,
  `agent_stop` (keep the agent working, at most 3 times), `subagent_start` and `subagent_stop`
  (one more round, or a replacement report). Up to 16 hooks.
- Hook cards say what a hook decided, with a before/after view of changed arguments; messages
  show a hook's edit or block; Agent tools → Extensions lists the events and their JSON output.

Requests of 2026-10-14 (testing on a new machine):

- Reasoning effort per conversation: **Default**, **Fast**, **Medium**, **High**, **Extra
  high** or **Max**, chosen beside the model picker and applied from the agent's next request,
  also while a response runs; subagents use their chat's effort. Each endpoint gets the fields
  it takes: Claude Opus 5.5 and 4.8 `thinking` (adaptive) and `output_config.effort`, GPT OSS
  `reasoning_effort` (Extra high and Max are sent as high). **Settings → Reasoning** sets the
  default for new chats, the request format (Automatic, Anthropic fields, OpenAI field, none),
  **Show thinking summaries**, and previews the exact JSON added to agent requests. If
  Databricks rejects a reasoning field, the request is sent once more without it and a notice
  says how to change the format. Titles, compaction and side questions send no reasoning
  fields.
- Replies show the model's thinking in a collapsible **Thinking** block ("Thinking… 12 s" while
  it thinks, "Thought for 12 s" once the answer starts). Claude Opus 5.5 returns thinking text
  only when asked, so with summaries on its requests ask for it. Request details and the usage
  table name the effort.
- Settings: the "Use … defaults" button stays as a confirmed state instead of disappearing,
  the endpoint is chosen from a list that keeps every endpoint choosable (plus **Other
  endpoint…**), and the dialog has a section list on wide screens, shorter help with **More**,
  and one type scale.
- Subagents: the agent hands broad exploration, wide searches, large reads and long command
  output to subagents on its own, so only their short reports enter the chat. It chooses
  **explore** (read-only), **editor** (file edits) or **general**, with up to 24 steps. Several
  subagents run at once (three at a time; others wait for a free slot); their approval cards
  come one by one and Stop stops them all. Each has its own card with type, live status,
  elapsed time, **Open subagent** and a collapsible **Report**. Every subagent keeps its own
  conversation file, and the parent's file lists its subagents (task, type, status, times,
  file).
- Agent tools: sections are grouped in a side navigation (**This chat**: Overview, Tasks,
  Subagents, Tools, Usage, Export; **Project**: Checkpoints, Verify, Worktrees, Extensions)
  with a new **Overview**; **Tools** has groups, a filter and per-group switches; the dialog
  reopens on the last section. **Recovery** is now **Checkpoints** and **Conversation** is
  now **Export**.
- Workspace panel: drag its left edge (or use the arrow keys on it) to resize it; the width is
  kept, Enter or a double-click restores it, and **Expand** makes it as wide as the chat allows.
  A worktree chat's panel names its project and branch. A file the agent edits again after
  **Keep** opens its review again, and an open file without unsaved edits follows the agent's
  later edits. Files a reply, a tool card or Git names are opened by their project path; a file
  named from the project's folder name, the repository root or a worktree's source project is
  found, and a missing file is reported with the folder searched.
- Sidebar: a **Tags** menu lists every tag with its number of chats and filters by any checked
  tag; `#word` in the search box filters by tag.
- Background jobs: `wait_job` can return when a job's output goes quiet (`idle_seconds`), and
  without `timeout_seconds` waits about what earlier runs of the same command say is left
  instead of 60 seconds; results suggest the next wait, and the agent is asked to size waits
  to the job.
- `scripts/simulate_databricks.py` runs the app against a simulated Databricks workspace (no
  network, no billed requests) to try effort levels, the Thinking block, subagents, jobs and
  change review; it checks reasoning fields the way the endpoints document them.

UI layout and behaviour (from a survey of the app at six window sizes, desktop to phone):

- Layout: below 1,024 px the Workspace panel opens over the chat instead of squeezing it (focus
  moves into it; Escape, **Close workspace** or a click outside closes it), and the sidebar no
  longer changes width. In a narrow chat column **Agent tools** and **Workspace** show only
  their icons, so the title stays readable (hover it for the full title and session ID). Sidebar
  rows have one **More actions** menu (**Rename**, **Pin**, **Archive**, **Edit tags…**,
  **Delete…**), titles use the whole row, the session ID shows only for repeated titles, and the
  menu opens upward near the bottom of the list. **Last model input** details open as a popover
  above the meter, and the bars above the message box no longer squeeze the conversation. The
  Agent tools dialog keeps its size and tabs in place; Settings is grouped, named and keeps
  **Save** visible; the permission menu fits the window; long `@` chips stay on one line; the
  welcome text sits above the message box.
- Behaviour: a command that exits non-zero, times out or is interrupted shows a warning icon and
  `exit N`, `timed out` or `interrupted` instead of a success tick, and expanded cards show the
  output as text with a **State** line and the **Raw result**. Error notices give a short reason
  with **Details**. A stop is marked by one divider that says what was interrupted. While the
  local server is unreachable one banner says so on every screen and the sidebar shows **Local
  server offline**; Databricks network errors read as plain sentences with **Check again**. On
  approval cards the allow-in-this-chat button and **Always allow…** form one split button. Plan
  cards scale their headings, drop the inner scroll and keep **Approve** reachable. **Local** is
  a label, not a button.
- Readability: muted text, links and the accent colour meet WCAG AA contrast (4.5:1 for text,
  3:1 for icons and controls) on every surface, also when hovered; every focusable element shows
  the same 2 px focus ring, including the message box and the Terminal fields; no text is
  smaller than 11 px (12 px for phone banners); on touch screens controls are at least 24 px;
  Markdown tables keep code cells whole and scroll sideways; the model picker's label sits next
  to its arrow; a long error no longer pushes the message box off screen.
- From a second survey: rate-limit wait notices come before the reply they delay and stop
  asking for Stop once the wait is over; in the tablet drawer the sidebar stays usable with the
  mouse as well as the keyboard; `wait_job` cards say whether the wait matched, timed out or the
  job ended; a failed command's card says it ran rather than completed; long file names and
  Terminal commands show in full on hover; in a narrower sidebar status badges become coloured
  dots so titles stay readable.

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
- Large files and limits, continued: `search_files` searches one named file up to 8 MB (a
  folder search still skips files over 2 MB); a compaction under a low output-tokens-per-minute
  limit is sized to fit it, and when nothing fits the error names the context budget that would;
  a message over 50,000 characters is not sent and suggests saving it as a file and attaching it
  with `@`; a stream that sends nothing for 300 seconds says so.
- Safety and speed: the configured token and secret shapes are redacted before any tool result,
  outline entry or search excerpt is shortened; hooks for `read_file`, `list_files` or
  `find_files` also run for `outline_file` and `project_overview`; long conversations save
  faster, and outlining a large Python file or starting a compaction pauses the app much less.

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
