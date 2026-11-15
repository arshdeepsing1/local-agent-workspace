# Local Agent Workspace progress

**Overall**: 93% (219 of 235 items) · **Last updated**: 2026-11-15

| Section | Share of app | Implemented | Progress |
|---|---|---|---|
| 🛠️ Tools and permissions | 21% | 92% | █████████░ |
| 🤖 Subagents and workflows | 16% | 89% | █████████░ |
| 💬 Chat and conversations | 13% | 100% | ██████████ |
| 🧠 Context and memory | 12% | 100% | ██████████ |
| 🔌 Extensions (MCP, skills, hooks) | 8% | 79% | ████████░░ |
| 🎨 Interface | 7% | 100% | ██████████ |
| ⏰ Scheduled and headless runs | 7% | 88% | █████████░ |
| 🌐 Web and browsing | 6% | 100% | ██████████ |
| 📊 Usage and limits | 6% | 93% | █████████░ |
| 🧪 Quality and testing | 4% | 90% | █████████░ |

An item is one capability. "Implemented" is the done items over the section's items. "Share of app" is the section's items over all items, so a larger share means the app does more in that area. Run `python scripts/check_progress.py --write` to refresh the numbers. CI fails when they are stale.

## 💬 Chat and conversations

**Implemented**: 100% (30 of 30) · **Share of app**: 13%

### Done

- [x] Streamed Markdown chat replies
- [x] Multiple persistent conversations
- [x] Highlighted code blocks with a Copy code button
- [x] Paste, drop or attach PDF, Word, Excel and PowerPoint files
- [x] Paste, drop or attach text files
- [x] Image attachments sent to endpoints that take image input
- [x] Attached document text given to the agent as data
- [x] Unsent message drafts kept in the browser across reloads
- [x] Continue button after a turn stopped at a step, output or spending limit
- [x] Continue button after Stop or a server restart
- [x] Stop an agent response, including its foreground command or pending approval
- [x] Resume conversations after refreshing or restarting the app
- [x] Periodic partial-reply checkpoints
- [x] Safe JSON conversation export and import
- [x] Forks of completed conversations with fresh permissions
- [x] Skipped and reported conversation files that cannot be read
- [x] Optional fallback endpoint for a request the main endpoint failed before replying (no connection, HTTP 502, 503 or 504, or a rate limit once retries are used)
- [x] Databricks model selection
- [x] Reasoning effort per conversation from Default to Max
- [x] Collapsible Thinking block with the endpoint's summary and thinking time
- [x] Conversation status in the sidebar (needs approval, working, error, unread)
- [x] Running filter in the sidebar
- [x] Count of chats waiting for approval in the tab title
- [x] Opt-in browser notifications
- [x] Optional desktop notification from the server when no page is open
- [x] Markdown transcript download or copy from the app
- [x] Questions from the agent with options to choose from
- [x] Full conversation history kept on disk
- [x] Web images in replies withheld with a text placeholder
- [x] Chats that always save when model text is malformed

## 🛠️ Tools and permissions

**Implemented**: 92% (45 of 49) · **Share of app**: 21%

### Done

- [x] Numbered line-range file reads
- [x] Paginated regex and literal search
- [x] File creation and exact edits
- [x] Shell commands with live output
- [x] Per-command time and output limits
- [x] Managed background jobs with explicit Stop
- [x] Background job output retrieval and status history
- [x] Background job cleanup on normal server shutdown
- [x] Approval cards with proposed edits and command text
- [x] Editing a proposed edit or command before approving
- [x] Declined actions do not run
- [x] Decline with a note the agent reads
- [x] Allow an action for the rest of a chat, the project or all projects
- [x] Allow, Always ask and deny rules for a chat, a project or all projects
- [x] Protected files that always ask
- [x] Manual permission mode
- [x] Auto permission mode
- [x] Accept edits permission mode
- [x] Don't ask permission mode
- [x] Plan permission mode
- [x] Bypass permissions mode
- [x] Permission mode switch below the composer
- [x] Additional local folders granted through an approval card
- [x] Plan review in Plan mode that approves the plan and switches the mode
- [x] Overwrite only after reading the existing file
- [x] Notice when a file the agent read or wrote changed since
- [x] File checkpoints
- [x] Reviewed and reversible checkpoint restore
- [x] Managed Git worktrees
- [x] Files changed bar with Keep and Undo
- [x] Inline red and green diff with Keep and Undo for each change
- [x] Dots on changed files in the file explorer
- [x] Git snapshot sent with the message that starts each reply
- [x] Git status and diff view
- [x] Stage, Unstage and Commit from the app
- [x] Branch pull request status
- [x] Non-interactive command runner
- [x] Workspace file explorer
- [x] Granted and added folders shown in the file explorer
- [x] Text editor with stale-file detection
- [x] Hidden and direction-changing characters shown as escapes in approval cards
- [x] Folder rules that match any letter case of the same folder
- [x] Credential folders never searched or read without a card
- [x] Right-to-left text kept in typing order in approval cards
- [x] Checked Git commands that leave out credential folders

### Pending

- [ ] Full PTY terminal
- [ ] OS execution sandbox
- [ ] OS computer control
- [ ] A rewind re-announces a finished background job named only in a removed turn

## 🧠 Context and memory

**Implemented**: 100% (27 of 27) · **Share of app**: 12%

### Done

- [x] Configurable context budgeting
- [x] Automatic summaries of older turns
- [x] Summaries of one long request's earlier tool results
- [x] Visible context meter
- [x] Script that turns saved history into Markdown transcripts
- [x] Automatic loading of workspace AGENTS.md guidance
- [x] Automatic loading of workspace CLAUDE.md guidance
- [x] Local variants of guidance files
- [x] Guidance file imports with @path
- [x] Path-scoped project rules in .agents/rules
- [x] Personal guidance file in the home folder
- [x] Project memory notes saved with the remember tool after approval
- [x] Project memory shared with every later chat in the project
- [x] Memory review in Agent tools
- [x] Output styles Default, Concise and Explanatory
- [x] Custom output style files
- [x] Compact command for a manual summary
- [x] Hooks that run around compaction
- [x] Progress tab with context, tasks and running work
- [x] Two-file handoff memory: /handoff writes a session log and rewrites a project brief
- [x] /resume reads the brief and the newest log only
- [x] /migrate-handoffs moves old one-file handoffs into the new layout
- [x] One context percentage shared by the meter and the context card
- [x] A choice card before an automatic compaction
- [x] Automatic shortening of old tool results before compaction
- [x] Largest-items list in the context card with Shorten and Drop
- [x] Ten-question state check after one, two and three compactions

## 🤖 Subagents and workflows

**Implemented**: 89% (33 of 37) · **Share of app**: 16%

### Done

- [x] Persistent tasks
- [x] Task dependencies
- [x] Live task checklist above the message box
- [x] Subagents the model starts on its own
- [x] Explore subagent type
- [x] General subagent type
- [x] Editor subagent type
- [x] Custom agents defined in .agents/agents files
- [x] Several subagents running in parallel
- [x] Each subagent in its own conversation file linked from its parent
- [x] Model per agent type
- [x] Effort per agent type
- [x] Background subagent that works while the chat goes on
- [x] Background subagent report delivered to the chat when it ends
- [x] Folding of consecutive read-only steps into one line
- [x] Run card shared by subagents started together
- [x] Live step and current action for each subagent
- [x] Cards that need you never folded away
- [x] Full list of actions under Activity
- [x] Code review skill with findings shown as a card
- [x] Security review skill with findings shown as a card
- [x] Verify and debug skill for a change
- [x] Removed folder grants and chat rules reach running subagents
- [x] Manual workflow start with /workflow
- [x] One approval card for a whole batch of subagents
- [x] Settings for subagents running at once and per turn
- [x] Fork copies a chat's subagents with it
- [x] One transcript file for a chat with its subagents
- [x] Workflow status and design note
- [x] Feasibility study of a Claude Code style agent harness
- [x] Export includes subagent chats by default
- [x] Delete dialog that counts running subagent chats
- [x] Owner's acceptance of the workflow design

### Pending

- [ ] Stop a queued background delegation from the panel
- [ ] Top-bar badge for running background work
- [ ] Grouping of MCP tool cards
- [ ] Scripted workflow engine that runs agents from a file

## 🌐 Web and browsing

**Implemented**: 100% (15 of 15) · **Share of app**: 6%

### Done

- [x] Public web pages read as text with web_fetch
- [x] Approval for each new domain
- [x] Blocked fetches of this computer, private networks and cloud metadata addresses
- [x] Web search with web_search
- [x] Brave search provider
- [x] Tavily search provider
- [x] Self-hosted SearXNG search provider
- [x] Search provider chosen in settings
- [x] Browser tool: open and read a page in a real browser
- [x] Browser tool: click, type, select and scroll
- [x] Browser tool: tabs, back and forward
- [x] Screenshots shown in the chat and sent to vision models
- [x] Console errors and failed network requests of a page
- [x] Per-site approval for browsing, with localhost allowed after approval
- [x] Credential fields and bot checks handed to the user

## 🎨 Interface

**Implemented**: 100% (17 of 17) · **Share of app**: 7%

### Done

- [x] Responsive desktop and mobile layout
- [x] Bundled fonts
- [x] No credential storage in the browser
- [x] Built-in slash commands such as /clear, /model, /mode, /rename and /copy
- [x] Custom command files with $ARGUMENTS
- [x] Skills sent with one message
- [x] One slash menu for commands and skills
- [x] Settings page with defaults
- [x] Agent tools panel
- [x] Init command that drafts an AGENTS.md
- [x] Commit skill
- [x] Pull request skill
- [x] One new light style in a single token block
- [x] Spacing, radius and type-size tokens
- [x] One status row above the composer
- [x] One shared frame for all dialogs
- [x] Before and after screenshot script for UI review

## 📊 Usage and limits

**Implemented**: 93% (14 of 15) · **Share of app**: 6%

### Done

- [x] Token limit per turn
- [x] Time limit per turn
- [x] Spending limit per conversation
- [x] Settings defaults for limits
- [x] Limits set on a single chat
- [x] Usage across all chats by day
- [x] Usage by endpoint
- [x] Usage by purpose
- [x] Usage by project
- [x] Provider-reported usage figures
- [x] Prompt caching on Claude endpoints
- [x] Cache-read billing rate for repeated input
- [x] Usage report script
- [x] Reasoning effort fields sent in the form each endpoint takes

### Pending

- [ ] Paid endpoint check that confirms cached tokens are counted inside prompt tokens

## 🔌 Extensions (MCP, skills, hooks)

**Implemented**: 79% (15 of 19) · **Share of app**: 8%

### Done

- [x] Explicit MCP servers
- [x] MCP servers over stdio
- [x] MCP servers over Streamable HTTP
- [x] MCP secrets referenced by name
- [x] Form and JSON editors for MCP configuration
- [x] Test saved configuration for MCP servers
- [x] Workspace skills
- [x] Personal skills
- [x] Hooks around tool calls
- [x] Hooks on your messages
- [x] Hooks on the app's notifications
- [x] Hooks at the end of a turn
- [x] Hooks that answer in JSON
- [x] Approval before hooks run
- [x] Approval before MCP servers start

### Pending

- [ ] MCP prompt workflows
- [ ] MCP servers that sign in with OAuth
- [ ] MCP servers over SSE
- [ ] MCP resource browsing

## ⏰ Scheduled and headless runs

**Implemented**: 88% (14 of 16) · **Share of app**: 7%

### Done

- [x] Scheduled prompt sent to a new chat of a project
- [x] Schedules by interval such as every 2 hours
- [x] Schedules by cron line
- [x] Fixed permission mode for a scheduled run, never Bypass
- [x] Approval limit per scheduled run
- [x] Token, time and spending limits per scheduled run
- [x] Manual Run button for each scheduled run
- [x] Option to run a schedule automatically
- [x] Missed runs caught up once
- [x] Schedule paused when a token is rejected
- [x] Script that runs one prompt and prints the reply
- [x] JSON result and exit code from the script
- [x] Python client that streams a chat's messages
- [x] Client that answers a chat's cards

### Pending

- [ ] Scheduled runs while the app is closed
- [ ] Cloud sessions

## 🧪 Quality and testing

**Implemented**: 90% (9 of 10) · **Share of app**: 4%

### Done

- [x] Test model button
- [x] Check endpoint capabilities button
- [x] Cost estimate approved before endpoint checks
- [x] Offline Databricks simulator
- [x] Automated development checks
- [x] Personal data check script
- [x] Progress tracker file with a checker script that CI runs
- [x] Critical bug review with a failing test per confirmed finding
- [x] Feature reachability test driven by this tracker

### Pending

- [ ] Check that compares this tracker with the shipped feature list
