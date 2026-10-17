// Shapes of the main API objects, for reference while editing (JSDoc only).
//
// Settings: { workspace, model, env_file, context_window, max_output_tokens,
//   max_agent_steps, compaction_handoffs?, auto_compact_percent? (50-100, default 90: automatic compaction runs
//   when the next request's estimate passes this share of the input budget), approval_timeout_minutes? (0 = no limit),
//   system_notifications? (server notifications while no page is open),
//   auto_keep_changes? (true: Keep every listed change when a chat's reply ends), host, configured,
//   background_subagent_wake? (true: an idle chat gets an app message, and so a paid turn, when one of its background
//   subagents finishes; default false; planning.DelegateManager.maybe_wake),
//   secret_file_patterns? (at most 32 file name patterns, such as '*.tfvars': a command naming such a file
//   always shows an approval card with a warning, like the built-in credential names; secret_paths.py),
//   spending_limit_dbu? (0-100000, default 0 = none: the spending limit of chats without their own; spending.py),
//   turn_token_limit?, turn_time_limit_minutes? (whole tokens 0-100,000,000 and minutes 0-1440, default 0 = none: the
//   token and time limits of each turn of chats without their own; turns.py),
//   coding_dir (default folder for new standalone code: the saved or .env value, '' = off),
//   python_venv (virtual environment the agent's commands, Verify, hooks and the Terminal use, '' = off),
//   coding_dir_status: { path, state: 'off'|'ready'|'missing'|'invalid', message, source: 'settings'|'env'|'none' }
//     (path: the checked absolute folder; the raw value only when invalid; null when off),
//   python_venv_status: { path, state: 'off'|'ready'|'invalid', message, python, version, pip (when ready: bool, or null if no site-packages folder is found), source },
//   default_model (the app's default endpoint),
//   default_limits: { context_window, max_output_tokens } (the saved model's defaults; budgets equal to them
//     follow a model change), model_migrated_from? (set while a legacy settings file at the old default
//   model is moved at load; cleared by the next save, which writes settings_version 2),
//   reasoning_effort: Effort (new chats' level, default 'default'), reasoning_format: 'auto'|'anthropic'|'anthropic_budget'|
//   'openai'|'none' (how agent requests ask for it; 'auto' = per endpoint; 'anthropic_budget' = thinking.budget_tokens for
//   older Claude endpoints), thinking_summaries: boolean (thinking.display 'summarized' for adaptive Claude endpoints;
//   default true), thinking_replay: boolean (Send earlier thinking back: a turn's signed thinking goes back to its own
//   endpoint during tool use; experimental, default false), prompt_caching: 'auto'|'messages'|'off' (cache markers on agent
//   requests to Databricks-hosted Claude endpoints; 'messages' = only the documented forms; default 'auto'),
//   fallback_model: string ('' = none: the endpoint an agent or subagent request goes to once when the model
//   endpoint failed before the reply started; one with built-in facts or one the user uses; chats on it have none),
//   slash_commands: boolean (default true: /name at the start of a message runs a built-in or custom command and
//   /skill-id selects a skill for that message; false sends such text as typed, and built-ins run only from the / menu),
//   endpoint_profiles: [{ model, label }] (endpoints with built-in facts),
//   user_agents_file: boolean (load ~/.agents/AGENTS.md; default true), user_claude_file: boolean (also load the
//   read-only ~/.claude/CLAUDE.md; default false), project_memory: boolean (offer remember and load the project's notes;
//   default true), output_style: string ('default' | 'concise' | 'explanatory' | a style file's id; default 'default'),
//   web_fetch: boolean (offer web_fetch; default true), web_search_provider: '' | 'brave' | 'tavily' | 'searxng' (default ''
//   = no web_search), web_search_url: string (the SearXNG address), web_search_key: string (the credential-file entry
//   that holds the API key; '' = BRAVE_API_KEY or TAVILY_API_KEY), web_search_status: { provider, ready, problem?,
//   key_name? } | null (never the key),
//   git_context: boolean (each turn's first message carries a short Git snapshot: branch, upstream, change counts,
//   recent commits; default true), schedules_enabled: boolean (the scheduler runs Agent tools → Schedules at their
//   times; default false), token_expires_on: '' | 'YYYY-MM-DD' (the Databricks token's expiry, typed by the user),
//   token_warning: string | null (from 7 days before that day, and after it; backend schedules.token_warning) }
// Schedules (backend schedules.py, schedules_api.py). GET /schedules?session_id= -> { enabled (Settings), running (this
//   app runs them), problem (another app holds the state folder's lock, or schedules.json cannot be read; or null),
//   project, schedules: Schedule[] (this project's), other_projects, token_warning, limits: { max_approval_minutes,
//   max_turn_tokens, max_turn_minutes, min_interval_minutes, max_name, max_prompt, max_schedules, max_running, modes } }.
//   POST /schedules { ScheduleFields } adds one for the chat's project, PUT /schedules/<id> edits, DELETE deletes,
//   POST /schedules/<id>/pause|resume|skip; each returns the listing. POST /schedules/<id>/run (Run now, or Run on a
//   waiting run) -> the listing and session_id (the run's new chat). POST /schedules/preview { spec, timezone,
//   schedule_id? (the edited one: an unchanged timing keeps its saved times) } -> { text, zone, next_runs: [epoch seconds] }. GET /schedules/ready -> { ready: [{ id, name, workspace, due, note }],
//   paused: [{ id, name, workspace }] (a rejected token) } over all projects.
// ScheduleFields: { name, prompt, spec ('every N minutes|hours|days' or a five-field cron line), timezone ('' = this
//   computer's), model ('' = Settings'), reasoning_effort ('' = Settings'), permission_mode (never 'bypassPermissions'),
//   approval_timeout_minutes (1-60, required), turn_token_limit, turn_time_limit_minutes (required), spending_limit_dbu
//   (0 = the Settings default), max_agent_steps (null = Settings'), run_policy: 'ask'|'auto', catch_up: 'once'|'skip' }.
// Schedule: ScheduleFields and { id, workspace, enabled, paused_reason?: 'paused'|'token_rejected'|'invalid', created,
//   anchor, next_run (epoch seconds or null), pending?: { due, created, trigger, note? } (waiting for Run), running,
//   zone, mode_label, history: [{ id, at, due, trigger: 'scheduled'|'catch_up'|'run_now'|'ready', outcome:
//   'started'|'not_run'|'skipped', note?, session_id?, started?, ended?, terminal_reason? ('deleted': its chat was
//   deleted), estimated_dbu?, tokens?, duration_s?, steps? }] (the last 20) }. A run's chat has tags ['scheduled'] and
//   schedule: { id, name, trigger, due? }.
// GET /output-styles?session_id= -> { styles: [{ id, name, description, source: 'built-in'|'project'|'user', path? }],
//   errors: [{ path, error }] } (backend output_styles.py; the chat's project, or the default one without a chat).
// Memory (GET /memory?session_id=; POST { text } adds, PATCH /memory/<id> { text } edits, DELETE /memory/<id> deletes
//   one, DELETE /memory clears; each returns the listing): { project (the repository folder whose notes these are),
//   notes: [{ id, date: 'YYYY-MM-DD', text }], bytes, max_bytes, max_notes, max_note_chars, enabled (Settings →
//   Project memory), problem (why the file could not be read, or null) }. Kept in the app's state folder.
// Effort: 'default' (no effort field: the endpoint's own) | 'low' (labelled Fast) | 'medium' | 'high' | 'xhigh' (Extra
//   high) | 'max'. Labels: types.js EFFORT_LABELS.
// POST /settings/reasoning { model, reasoning_effort?, reasoning_format?, thinking_summaries? } -> ReasoningPreview:
//   { model, label (profile label or null), verified (false: no profile; every level offered, fields unverified),
//     format (the one requests use: 'anthropic'|'openai'|'none'), format_setting, endpoint_format, default_effort,
//     default_label ('Medium', 'no thinking' or null), levels: [{ value: Effort, label ('Default (Medium)'), sent (the level
//     sent, or null), note? ('sent as High') }], effort, sent_effort?, effort_field? ('output_config.effort' |
//     'reasoning_effort'), fields (the exact top-level JSON added to agent requests), skipped?: 'rejected_earlier',
//     rejected_field?, notes: string[] }. Computed by the server's request builder (backend reasoning.py).
// GET /settings -> Settings (the folder states checked again). PUT /settings { ...editable fields } -> Settings.
// Endpoint checks (backend endpoint_probes.py, endpoint_checks_api.py): POST /endpoints/plan { model, probes? } ->
//   EndpointPlan { model, probes: [{ id, title, question, requests: [{ id, label, estimated_input_tokens, max_tokens,
//   estimated_dbu (null without price data), alternative_to? }] }], total_dbu (at most; null: unpriced), priced,
//   max_output_tokens, pricing_effective_at, plan_id } (no request is sent). POST /endpoints/run { model, probes?, plan_id,
//   approved_dbu } and POST /endpoints/test { model, plan_id, approved_dbu } (the smoke check alone) -> 202 EndpointRun
//   { id, model, kind: 'check'|'test', status: 'running'|'completed'|'stopped'|'failed', requests: [{ id, probe, label,
//   state: 'pending'|'running'|'done'|'skipped'|'not_sent'|'stopped', http_status, summary }], error, waiting,
//   reported_dbu, approved_dbu }; 400 without approved_dbu, 409 for another plan or while a run goes on (nothing sent).
//   POST /endpoints/run/stop -> { run }. GET /endpoints/<model>/checks -> { model, saved: { checked, plan_id, results:
//   { <probe>: { status: 'accepted'|'rejected'|'not_seen'|'error', summary, checked, ... } }, calls } | null, run, capabilities:
//   [{ id, capability, result, source ('probed <date>'|'documented'|'unknown'), status }] }. GET /endpoints/<model>/state
//   -> { model, ready, config_update, task, error } (Databricks' serving state; no model request).
// POST /settings/limits { model, context_window, max_output_tokens } -> LimitsCheck:
//   { profile: { label, context_max, output_max, itpm, otpm, recommended_context_window,
//       recommended_max_output_tokens, thinking_reserve, reasoning fields, prompt_caching, vision (image_url content
//       parts documented), structured_output (false: unconfirmed), model, output_concurrency_limit,
//       context_rate_limit } | null,
//     recommended: { context_window, max_output_tokens } | null,
//     defaults: { context_window, max_output_tokens } (this endpoint's defaults, with or without a profile),
//     effective: { context_window, max_output_tokens, clamped, input_budget (what requests use: the capped budgets
//       less the reply reserve and 2,048, and near a known endpoint window 5% less for estimate error) },
//     warnings: [{ code, level: 'error'|'warning'|'note', text }],
//     note }
// Bootstrap (GET /bootstrap): { token, settings: Settings, storage_problems: StorageProblem[] }
// StorageProblem: { file, reason } a conversation file in the state folder that could not
//   be read; it is hidden and left unchanged. reason never quotes the file's contents.
// Connection: { connected, models: string[], error: string | null,
//   error_kind?: 'credentials'|'http'|'network'|'invalid_response' (which step failed; absent on success),
//   http_status?: number (with error_kind 'http') }
// Session: { id, title, workspace, model, status, events: AgentEvent[], updated,
//   permission_mode: 'manual'|'auto'|'acceptEdits'|'dontAsk'|'plan'|'bypassPermissions', allowed_directories,
//   allow_rules: Rule[] (this chat's rules), disabled_tools?: string[] (turned-off tools; 'mcp__<server id>__*' turns a
//   whole MCP server off in the chat, so it never starts), context_info?, active_skills?,
//   mcp_status?: McpServerStatus[] (the last turn's MCP servers; pushed as { type: 'mcp_status', mcp_status }),
//   parent_session_id?, is_subagent?, delegation?, tool_profile?, subagent_tool_profile?,
//   agent_type?: 'explore'|'general'|'editor' or a custom agent's name (a subagent), subagents?: SubagentEntry[] (a parent's index of its
//   subagent chats; left out of GET /sessions list items),
//   terminal_reason?: how the last turn ended: 'completed'|'stopped'|'error'|'inference_error'|
//   'tool_error'|'step_limit'|'spending_limit'|'budget_limit' (the turn's token or time limit)|'approval_expired' (a
//   scheduled run's card waited its approval limit)|'interrupted'|
//   'hook_blocked' (a user_prompt hook blocked the message; also in the /api/sessions summaries),
//   turn_ended?: epoch seconds when the last turn ended; null while a turn runs (also in summaries),
//   turn_delegated?: true while the last turn is one a parent's delegate_task started (also in summaries),
//   reasoning_effort?: Effort (the chat's own level, saved in its header; absent on chats saved before it existed,
//   which use Settings.reasoning_effort; kept by forks and imports; also in summaries),
//   spending_limit_dbu?: number (the chat's own spending limit; 0 = none; absent: Settings.spending_limit_dbu;
//   kept by forks and imports), turn_token_limit?, turn_time_limit_minutes? (the chat's own turn limits; 0 = none;
//   absent: Settings'), max_agent_steps? (a scripted chat's own Agent steps), last_turn_result?: TurnResult (the
//   last turn's; GET /info and the snapshot, not the list) }
// POST /sessions { permission_mode, reasoning_effort?, model?, workspace?, max_agent_steps? } creates a chat (Settings'
//   effort and model when absent; /clear sends the replaced chat's; workspace: an existing absolute folder, never the
//   app's state folder, for scripts, default the Settings workspace).
// PUT /sessions/<id>/reasoning-effort { reasoning_effort } -> Session, also while busy: the agent's next model request
//   (and a running subagent's) uses it. Pushed as { type: 'reasoning_effort', reasoning_effort }.
// PUT /sessions/<id>/spending-limit { spending_limit_dbu: number 0..100000 | null (follow Settings) } -> Session, also
//   while busy (400 for a subagent). Pushed as { type: 'spending_limit', spending_limit_dbu, spending: Spending }.
// PUT /sessions/<id>/turn-limits { turn_token_limit?, turn_time_limit_minutes?: whole number | null (follow Settings) }
//   -> Session, also while busy (400 for a subagent or an empty body). Pushed as { type: 'turn_limits', turn_token_limit,
//   turn_time_limit_minutes } (null: the Settings default). TurnLimits (GET /sessions/<id>/metrics → turn_limits):
//   { turn_token_limit: number | null (none), token_source: 'chat'|'settings', turn_time_limit_minutes, time_source,
//   follows_parent } — a subagent follows its parent's.
// TurnResult (turns.turn_result; on the turn's last reply as turn_result, in the header as last_turn_result and in
//   the idle status that ends the turn): { terminal_reason, is_error, started, ended (epoch seconds), duration_s,
//   steps (agent replies), tool_calls, usage: { calls, input_tokens, output_tokens, cache_read_input_tokens,
//   cache_creation_input_tokens, reasoning_tokens } (the turn's own calls; null = no call reported it), tokens: number |
//   null (every processed token, its subagents' included; null: no call reported a count), estimated_dbu: number | null
//   (no priced call), unpriced_calls, subagents: { count, tokens (null: none of them reported a count), estimated_dbu },
//   changed_files: string[] (at most 20), changed_files_total? }.
// GET /usage?days=1..90 (30) -> { days, since, until, totals: { calls, input_tokens, cache_read_input_tokens,
//   cache_creation_input_tokens, output_tokens, estimated_dbu, unpriced_calls, chats }, by_day: [{ date, ... }] (every
//   local date, oldest first), by_model: [{ key, priced, ... }], by_purpose: [{ key, label, ... }], by_project:
//   [{ key, name, ... }] (at most 20, then one '' row), pricing: { effective_at, source_url, open_source_url, models },
//   pending_chats (older chats not summarized yet) }: every chat's usage from the summaries in their headers.
// Spending (GET /sessions/<id>/metrics → spending): { limit_dbu: number | null (none), source: 'chat'|'settings',
//   spent_dbu, own_dbu, subagents_dbu, unpriced_calls, percent: number | null } — a subagent reports its parent's.
//   GET /sessions list items omit events and allow_rules, and add unreviewed_changes (files with agent
//   changes not yet kept or undone, including rows rolled up from its subagents).
//   pinned?, archived? (hidden in the sidebar; wins over pinned), tags?: string[] (at most 10, 1-32 characters),
//   parent_deleted? (a subagent chat whose parent was deleted; its parent links are gone),
//   worktree?: { id, branch, source_workspace } (a chat in an app-managed worktree; grouped under source_workspace).
//   The first stream snapshot carries only the latest events: events_start (index of events[0]; 0 = complete)
//   and events_total. Later snapshots are complete and have no events_start.
// GET /sessions/<id>/info: the list item of one chat (header and status, no events); 404 if missing, 400 if unreadable.
// GET /sessions/<id>/events?before=<event id>&limit=1..500 (100) -> { events, start, total }: the page before that event.
// PUT /sessions/<id>/meta { pinned?, archived?, tags? } -> Session; pushed as { type: 'meta', pinned, archived, tags }.
// DELETE /sessions/<id>?children=keep|delete (keep) -> { ok, deleted: string[] }: delete also deletes its subagent chats.
// GET /session-search?q=2..200 chars&workspace?&include_subagents?&limit=1..50 (20) -> { results: [{ id, title, updated,
//   workspace, archived, matches: [{ event_id, role: 'user'|'assistant', snippet }] }], truncated }.
// Rule: { behavior: 'allow'|'ask'|'deny', tool, command?, argv?: string[], prefix?: string[], flags?: string[],
//   path?: string, hook?: string, domain?: string }. A rule without behavior (an M1 "Allow for this chat" rule) means allow.
//   tool: 'run_command' (command = M1 exact text, chat and allow only; argv = exact argv; prefix with flags =
//   the prefix followed only by those options, '--name=' allowing any value; no matcher = every command,
//   deny and ask only) | 'edits' (every edit tool; path = an absolute folder or file) | 'hook' (hook = id)
//   | 'delegate_task' | 'stop_job' | 'mcp__<server>__<tool>' | 'mcp__<server>__*' (deny and ask only)
//   | 'web_fetch' (domain = one host for allow, which needs it; deny and ask cover its subdomains too, or every fetch)
//   | 'web_search' | another tool name (deny only). Deny wins over ask, ask over allow. A deny rule for read_file also covers
//   outline_file, and one for list_files or find_files also covers project_overview.
// Rule draft (POST only): { tool: 'run_command', behavior, text, match: 'exact'|'prefix', options?: string }.
// RuleSets (GET /permission-rules?session_id=; POST /permission-rules { scope: 'chat'|'project'|'user',
//   session_id, rule: Rule | draft }; DELETE /permission-rules { scope, session_id, rule };
//   POST /permission-rules/reset): { chat: Rule[], project: Rule[], user: Rule[], project_path,
//   problem: string | null (the saved rules file could not be read; its rules are ignored until reset) }.
//   A 400 detail explains a rejected rule. M1 chat rules are also removed with DELETE /sessions/<id>/allow-rules (body: the rule).
// Approval answer (POST /sessions/<id>/approvals/<event id>): { allowed, remember?: true (approve only: an exact
//   rule for this chat), note?: string (decline only, at most 2,000 characters), scope?: 'chat'|'project'|'user'
//   (approve only, one of the card's rememberable), rule?: Rule (from the card's rule_options; without it the
//   server stores its default rule for the card) }
// Edit before approving: POST /sessions/<id>/approvals/<event id>/edit { input: { <one of editable>: string } }
//   -> { ok: true }; the card stays pending and its new input and preview arrive as an event update.
// Mode change: PUT /sessions/<id>/permissions { permission_mode, confirm?: true }; switching to
//   bypassPermissions needs confirm (400 'Confirm switching to Bypass permissions.'). POST /sessions
//   { permission_mode } needs no confirm. Allowed while the chat is busy; waiting cards are re-checked.
// Turned-off tools: PUT /sessions/<id>/disabled-tools { tools: string[] } -> Session;
//   GET /tools?session_id= -> { tools: [{ name, description }], disabled: string[] }. The list includes the read
//   tools outline_file and project_overview; turning off read_file also turns off outline_file, and turning off
//   list_files or find_files also turns off project_overview.
// The 'permissions' socket message: { type: 'permissions', permission_mode, allowed_directories, allow_rules,
//   disabled_tools?, project_rules?: Rule[], user_rules?: Rule[], rules_problem?: string | null }
// Status message: { type: 'status', status, terminal_reason?, turn_ended? (sent with 'idle'
//   when a turn ended; a manual compaction leaves turn_ended unchanged), turn_delegated? (true, with
//   turn_ended, when a parent's delegate_task started that turn), turn_result?: TurnResult (with turn_ended) }
// Compaction message: { type: 'compaction', progress: CompactionProgress }, sent before each summary or condensing
//   request of a compaction. CompactionProgress: { phase: 'handoff'|'summary'|'condense', request (from 1; a retry
//   keeps the number), started and request_started (server time, epoch seconds), trigger: 'manual'|'in_turn'|'auto'
//   |null }. The session (snapshot, GET /sessions/<id>) carries it as compaction_progress? while its status is
//   'compacting'; a status other than 'compacting' ends it.
// GET /sessions/<id>/context -> { status, context_info: ContextInfo | null, compaction_progress? }: the meter as
//   last prepared, without building a request.
// GET /sessions/<id>/stream.ndjson: the WebSocket's messages for scripts, one JSON per line (snapshot first,
//   keep_alive lines while quiet, stream_closed { reason } before the app ends it).
// /api/presence: a WebSocket (same protocols as the stream) each page holds open; no messages.
// AgentEvent: { id, type: 'user'|'assistant'|'tool'|'notice'|'error', text?,
//   reasoning_summary? (the reply's thinking text the endpoint returned, redacted, at most 12,000 characters),
//   reasoning_truncated?, reasoning_started? (epoch seconds when its thinking began), reasoning_seconds? (how long it
//   thought: until answer text or a tool call began, or the response ended; also set when the endpoint reported
//   reasoning tokens without text), reasoning_wait_seconds? (seconds from sending the request to its first thinking
//   item; Request details show it), child_session_id?, request_info?, turn_result?: TurnResult (a turn's last reply:
//   the footer under it; exports and forks leave it out),
//   name?, input?, output?, preview?, state?, delegation?, origin?,
//   (delegation: Delegation, on a delegate_task card or a notice with child_session_id),
//   approval?: 'approved'|'declined'|'expired'|'blocked', approval_expires? (epoch seconds, only with a limit),
//   decline_note? (the user's note sent with Decline), allowed_by_rule? (true: ran by a Rule, no card),
//   rule_scope?: 'chat'|'project'|'user' (with allowed_by_rule), read_only_command? (true: ran from the fixed
//   read-only command list), read_only_pipeline? (true: a command the app checked as read-only and ran as its own
//   script, shell_vet; with allowed_by_rule when a part ran because a rule allows it), command_cwd? (the resolved
//   folder of a run_command with cwd), secret_files? (the words of a command that look like credential files: its
//   card warns and cannot be remembered), approved_by_mode? (the mode whose switch approved a waiting card),
//   without_shell? (true: the command ran as an argv without a shell), job_id? and job_started? (a command
//   card's job and when it started, epoch seconds: a running card shows its time against input.timeout_seconds),
//   edited? (true: the user edited the action before approving it), blocked_reason? (with approval 'blocked': why it did not run),
//   protected? (the reason a file is protected), model_output? (what the agent received when it differs from
//   output), result_withheld?: { estimated_tokens, allowance_tokens } (a read whose result did not fit the
//   response's context allowance: the agent got a result_deferred message), result_shortened?: true (an action's
//   result was shortened for the agent),
//   and on pending cards only: rememberable?: ('chat'|'project'|'user')[], rule_options?: { argv: string[] | null,
//   prefixes: [{ prefix: string[], flags: string[] }], reason: string | null } (commands) | { folder } (edits) |
//   { domain } (web_fetch),
//   editable?: ('command'|'content'|'new_text')[], uncertain?: true (a command that could not be checked
//   exactly against deny and ask rules) }
//   ask_user events: { name: 'ask_user', input: { questions }, answers?: [{ question, selected, other }],
//   question_outcome?: 'answered'|'skipped'|'expired', skipped_reason?: 'dont_ask', approval_expires? };
//   answered with
//   POST /sessions/<id>/questions/<event id> { answers?: [{ selected, other? }], skip?: true }.
//   submit_plan events: { name: 'submit_plan', input: { plan, title? }, plan_outcome?: 'approved'|'keep_planning'|
//   'expired'|'superseded', plan_mode?, feedback?, approval_expires? }; answered with
//   POST /sessions/<id>/plans/<event id> { choice, feedback? }.
//   report_findings events (a review skill's result, no card): { name: 'report_findings', input: { summary,
//   findings: [{ path, line?, end_line?, severity: 'high'|'medium'|'low'|'info', title, detail?, suggestion? }] }
//   (redacted by the app) }; completed ones render as the Review findings card.
//   User events also carry in_wire? (true once the text reached the model history as its own message; false
//   when the turn failed before that; absent on older events), steered? (true: sent while the agent was
//   working), git_context? (true: the app's Git snapshot went with it) and attachments?: Attachment[]. The 'Stopped.' notice carries stop_phase?: 'streaming'|'tools'|'waiting'.
//   A rate-limit retry, automatic-continue or shared-wait notice carries details?: string ('HTTP 429 <code>: <message>
//   · Retry-After: <n s | none>', the 429 behind it; a shared wait shows the other chat's), shown under Details.
//   A compaction's notice carries compaction?: Compaction (one notice per compaction).
//   Hooks (lifecycle_hooks.py): a hook card (name 'hook_<event>', input { hook_id, tool?, call_id?, event?,
//   subagent_session_id? }) carries hook_result?: { json: true, decision?, reason?, context?, arguments?: { before,
//   after }, prompt?: { before, after }, report?, ignored?: string[] } when the hook printed one JSON object. A tool
//   card can carry hook_change?: { hooks: string[], original } (a before-tool hook replaced the arguments; input is
//   what ran), hook_ask?: { hook_id, reason } (a hook asked for its card) and hook_blocked?: { hook_id, reason }.
//   A user event can carry hook_blocked? (never sent), hook_edit?: { hooks, original } (text is the hook's
//   replacement) and hook_notes?: [{ event, hook_id, context }] (context sent to the model after the message).
//   MCP cards (name 'mcp__<server>__<tool>'): mcp_annotations?: { title?, read_only?, destructive?, idempotent?,
//   open_world? } (the server's hints, shown, never used for permissions) and mcp_images?: [{ id, mime_type, bytes }]
//   (GET /sessions/<id>/tool-images/<id> -> { mime_type, data } while the app runs; never sent to the model). A long
//   result's output JSON has result_id, next_offset, total_chars (and rest_not_kept) for get_tool_output.
//   Web cards (web_fetch.py): web_fetch carries web_domain? (the checked IDNA host the card and its rules name) and
//   web_search web_provider? (the provider's label); their output JSON has a note that the content is untrusted.
// McpServerStatus: { id, name, status: 'connected'|'failed'|'skipped'|'off'|'disabled', tool_count, error?,
//   stderr_tail? (redacted end of a stdio server's stderr), server_info?: { name, title?, version? },
//   retry_seconds? (a failed server is tried again after 60 s), capped?: string[] (tools left out by the
//   32-tool request limit) }. POST /extensions/test returns { tools, servers: [the same, with tools: string[],
//   instructions?, tool_annotations?] }. POST /extensions/import?session_id= { source: 'project'|'desktop'|'text',
//   text? } -> { source, path?, entries (disabled servers), needs: string[] (credential-file keys), present: string[]
//   (needed keys the credential file already has; names only), skipped: [{ name, reason }], ignored: [{ name, fields }],
//   notes: [{ name, text }] (values to put whole in the credential file) }: a preview, nothing is saved. GET /tools also returns
//   servers: [{ id, name, enabled }].
// Delegation: { status: 'queued'|'running'|'awaiting_approval'|...|'completed'|'failed'|'cancelled'|'interrupted',
//   completed_tools, last_tool?, terminal_reason?, agent_type?, started?, ended? (epoch seconds), background? (true:
//   delegate_task background), pending?: { event_id, name, kind: 'approval'|'question'|'plan', title, warnings?:
//   string[], review?: true (the title is not the whole action, or the card warns: no Allow on the parent's card),
//   expires? } (the child's waiting card while awaiting_approval: POST /sessions/<child>/approvals/<event_id>
//   { allowed } answers an approval) }. A finished delegate_task card's output is the JSON result { child_session_id, agent_type,
//   tool_profile, status, terminal_reason, steps_used, tool_calls, changed_files, changed_files_total?, transcript,
//   report?: { summary, findings?: [{ path?, line?, text }], files_changed?, next_steps?, findings_total?, ... } (its
//   submit_report), output (its final reply, '' with a report) }. A background one's output is { child_session_id
//   (null while queued), agent_type, status: 'running'|'queued', background: true, transcript, message }; its result
//   arrives later as the card's delegation_result, and the parent gets a notice { subagent_id, subagent_status }.
//   A tool card a running sibling subagent's change made ask has sibling_change: { subagent_id, agent_type, path }.
//   A user message the app sent after a background subagent finished has background_wake: true.
// SubagentEntry: { id, task (200 characters), agent_type, status, created, ended, transcript ('conversations/<id>.jsonl'
//   in the state folder, null once deleted), steps_used?, tool_calls?, deleted? }.
// GET /sessions/<id>/children -> list items of its subagent chats, each with status, transcript and subagent?: SubagentEntry.
// Attachment (metadata only, never content): { kind: 'file', path, start_line, end_line, truncated? }
//   | { kind: 'shell', command (at most 2,000 characters), exit_code }
//   | { kind: 'document', name, upload, format, chars (sent), truncated, units?: 'page'|'sheet'|'slide', count?, as_path? }
//   | { kind: 'image', upload, name, media_type, width, height, bytes }
// RewindPreview (GET /sessions/<id>/rewind-preview?before_event_id=<user event id>; read-only, also while busy):
//   { before_event_id, conversation: { possible, reason: string | null, events_removed, messages_removed,
//   summary_dropped }, code: { possible, reason: string | null, files: [{ path, status: 'restore'|'unchanged'|
//   'drifted'|'interleaved'|'unavailable', error? }] } }
// Rewind (POST /sessions/<id>/rewind { before_event_id, conversation?: boolean, code?: boolean, resend_text?,
//   resend_verbatim? (a message sent as plain text is resent as plain text), resend_uploads? ([{ id, as_path? }]: its
//   attached files, sent again) };
//   needs an idle chat, and an idle workspace for code): { session: Session, backup_session_id: string | null,
//   files: [{ path, status }], resent: boolean, resend_error? }. 409 detail { message, files } when a file
//   changed; 500 detail { message } when files were rewound but the conversation could not be saved.
// Fork from a point (POST /sessions/<id>/fork { before_event_id?, title?, worktree_branch? }) -> Session (201).
//   Needs an idle chat (409 naming it otherwise); without a body, only a completed idle chat forks whole.
//   A rewind backup Session has archived: true and provenance { kind: 'rewind_backup', source_session_id,
//   copied_at }; worktree chats, and their backups, have worktree: { id, branch, source_workspace (the root project) }.
// ApiError: { message, status, detail? }. A 409 that a busy chat causes has
//   detail { message, session_id, title } naming that chat; other errors have no detail.
// RequestInfo: { model, status, finish_reason?, http_status?, error_kind?, usage?,
//   rate_limit?: { limit_type?, limit?, current?, retry_after?, code?, message?, retry_after_header? } (on a 429 that
//   ended the request: limit_type from the body's field or its words, absent when it names none; code: error_code or
//   the error's code or type; message: the body's message, redacted, at most 200 characters; retry_after: the wait
//   Databricks asked for, from the Retry-After header or the body; retry_after_header: that header as sent, when
//   the wait came from it, not when the app could not read it, for example a date in the past),
//   reasoning?: { effort, format, fields, sent_effort?, effort_field?, skipped?: 'rejected'|'rejected_earlier',
//   rejected_field? } (absent when the request was at Default and sent nothing),
//   fallback_from?, fallback_reason? (a request Settings → Fallback endpoint answered: model is that endpoint,
//   fallback_from the chat's, which failed before the reply started; fallback_reason 'HTTP 503', 'connection
//   failure' or 'rate limit, HTTP 429') }.
//   error_kind includes 'rate_limit', 'payload_too_large' (over the 4 MB request limit; not sent) and
//   'output_tokens_over_limit' (Max output tokens above the endpoint's reported OTPM limit).
// ContextInfo: { estimated_tokens, input_budget, context_window, reply_reserve,
//   compactions, summarized_messages, estimate_method, instruction_files, warnings,
//   instruction_sources?: [{ path, scope ('.', a folder, 'files matching <globs>' for a project rule, 'all projects',
//   'this workspace', 'this project', 'this chat' (a skill selected for the chat: path 'Skill /<id>') or 'this
//   message' (a skill the latest message selected; it counts in messages, not system instructions)),
//   status: 'loaded'|'trimmed'|'omitted', estimated_tokens, reason? }],
//   prepared_for_next_turn?, breakdown?, tool_groups? ({ 'Built-in tools': tokens, 'MCP <server>': tokens, … }:
//   breakdown.tool_definitions split per MCP server; the parts add up to it), estimate_scale?,
//   estimate_anchor? ({ reported, added }: the estimate is the input Databricks reported for the chat's previous
//   request plus the scaled estimate of what was added since; added is that raw estimate, negative when the
//   request shrank), summary_adjustment?: 'condensed'|
//   'condensed_trimmed'|'trimmed' (the summary was over its byte limit: condensed by a request; condensed but still
//   over, so the app shortened its least important sections; or shortened without a successful condense),
//   summary_fitted_to_budget?: 'condensed'|'condensed_trimmed'|'trimmed' (shortened because the request after
//   compaction was over the input budget), handoff_path? (the handoff this compaction saved),
//   compactable? (false: Compact now has no earlier turn to summarize until the next user message; absent on
//   older meters, which allow it),
//   estimate_prior? (true: estimate_scale is the model's starting ratio, not yet from reported counts),
//   auto_compact_threshold?, auto_compact_percent?, compaction_deferred? (over the threshold but within the input
//   budget, and no cut brings it under the threshold: sent as it was), compaction?: Compaction (when this request compacted), last_request?: LastRequest,
//   next_estimate?: { tokens, scale?, anchor? ({ model, reported, added, raw }: anchored like estimate_anchor),
//   held_anchor? (the same, kept after a change to another model so a change back anchors again),
//   percent_of_input_budget, percent_of_threshold } (the app's estimate of the
//   next request before the user's next message; set when a turn ends), payload_bytes? (the size of the request
//   body sent), payload_limit_bytes? (the Databricks per-request limit, 4,000,000) }. After a model change or a
//   Settings save the budget fields, the threshold and next_estimate of an idle chat follow the new setup;
//   estimated_tokens, estimate_scale and last_request still describe the last request.
// Compaction: { number, trigger: 'manual'|'in_turn'|'auto', before_tokens, after_tokens, threshold_tokens,
//   summarized_messages, requests (summary and condense requests), summarize_requests?, condense_requests? (the
//   two apart; absent on older compactions), elided_bytes, superseded_results, cleared_results?, cleared_bytes? (tool
//   results of older turns the summary requests left out, and the bytes saved; only when there were any),
//   before_bytes? (the request's size when only the 4 MB request limit called for the compaction) }
// LastRequest (what Databricks reported for the latest agent request): { call_id, model, status, finish_reason?,
//   partial (ended without a finish reason: counts may be incomplete), input_tokens (= uncached + cache read +
//   cache write), uncached_input_tokens, cache_read_input_tokens, cache_creation_input_tokens, output_tokens?,
//   reasoning_tokens?, context_window, input_budget, auto_compact_threshold, auto_compact_percent, compactions
//   (the info's compactions when it was sent; a different value means a compaction ran since),
//   percent_of_input_budget, percent_of_context_budget, percent_of_threshold, model_window (null when unknown),
//   percent_of_model_window, estimated_tokens (the app's estimate of the same request), estimate_scale (the scale
//   that estimate used), next_estimate_scale? (the scale later estimates use, this report counted),
//   estimate_gap_percent }. Percentages are rounded to one decimal, or null without a denominator. The meter
//   treats it as current only while compactions and input_budget match the session's, and the model too once a
//   request to another model has been prepared (estimated_tokens no longer the report's).
// Metrics (GET /sessions/<id>/metrics): { scope, session_id, complete, totals, calls, by_purpose, by_model,
//   pricing, last_input: LastRequest | null, spending: Spending, turn_limits: TurnLimits }. A call has effort?, effort_field? and reasoning_skipped? like
//   RequestInfo.reasoning; retry_reason 'reasoning_rejected' marks the resend without reasoning fields, and
//   'cache_rejected' the resend with fewer cache markers, 'images_rejected' the resend with notes in place of
//   attached images, 'fallback' a request to Settings → Fallback endpoint
//   (its fallback_from names the chat's endpoint). Agent calls also have prompt_cache? ('full'|'messages'|'off':
//   the cache markers sent, on Claude endpoints), system_tail_changed? (true), usage_keys? (the raw usage key names),
//   reasoning_shape? and reasoning_signature? (true). A chat with subagents adds subagents: { totals (calls and token
//   sums of its subagents, null for a field one did not report), children: [{ id, agent_type, task, status,
//   estimated_dbu, usage: { calls, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens }
//   | null, deleted? }], without_usage (entries from before usage was kept) }. GET /sessions/<id>/usage.csv?
//   include_subagents=true adds the subagent chats' rows.
// Job: { id, session_id, workspace, command, state, created, updated, exit_code,
//   output?, truncated, timeout_seconds, max_output_bytes, background,
//   without_shell? (true: ran from an argument vector without a shell) }
// Composer (M4): Session.queued_messages?: QueuedMessage[]; Session.pending_context?: [{ kind: 'shell', command,
//   exit_code }] (! output attached to the next message; its text stays on the server). Pushed as
//   { type: 'pending_context', pending_context } when a command adds one and when a message takes them.
// POST /sessions/<id>/messages { text, verbatim?, uploads?: [{ id, as_path? }] } (verbatim: sent as plain text: no command
//   template, no skill, no @ file; uploads: files attached in the composer, at most 8, also with plain text).
// POST /sessions/<id>/uploads (body: the file's bytes, header X-File-Name: its URL-encoded name) -> UploadRecord:
//   { id (sha256), name, kind: 'document'|'image', format, media_type, bytes, estimated_tokens, a document's chars,
//   truncated, notes, units?, count?, unreadable? (a PDF without the documents extra: send it with as_path), an image's
//   width, height and vision_refusal? (the chat's endpoint does not take images) }; 400 or 413 with the reason.
// QueuedMessage: { id, text, created, paused, verbatim?, uploads?: [{ id, as_path?, name, kind }], command?: { name, path } (a command template expanded when it
//   was queued; the server keeps the expanded text) } (paused: left over from a stop or failure; sent only by
//   'Send queued messages'). POST /sessions/<id>/messages while busy -> { ok, queued: true, queued_messages };
//   otherwise { ok: true }. 400 when 3 are waiting or their text would exceed 50,000 characters.
//   DELETE /sessions/<id>/queue/<item id> -> { queued_messages } (404 'This queued message was already sent.');
//   POST /sessions/<id>/queue/send (idle only) sends all of them as one message.
//   Socket: { type: 'queue', queued_messages, error? } (error: why a queue could not start; stored as session.queue_error).
// User AgentEvent extras: attachments?: [{ kind: 'file', path, start_line, end_line, truncated } |
//   { kind: 'shell', command, exit_code }]; steered?, in_wire? (true: queued, sent mid-turn); queued? (sent from the
//   queue as the next turn); commands?: [{ name, path }] (command templates whose text the agent got after the message);
//   skills?: string[] (skills the message selected for itself with /<id>); verbatim? (sent as plain text).
// POST /sessions/<id>/shell { command (one line, 1..2000), timeout_seconds?, max_output_bytes? } -> 202 { ok }
//   (idle only; runs in every mode; shows a 'user_command' tool event).
// POST /sessions/<id>/side-question { question (1..4000) } -> { answer, request_info: { status, finish_reason, usage? } };
//   409 while another side question of the chat is answered.
// GET /sessions/<id>/mention-files?q= (or /mention-files?q= before a chat exists) -> [{ path, name }] (at most 30;
//   only paths an @ token can spell). Queued messages must also fit one message's @ limits together.
// Skill: { id, name, description, path, argument_hint?, disable_model_invocation? (true: only the user selects it),
//   user_invocable? (false: only the agent loads it), reserved? (true: a built-in or custom command has its name, so
//   only /skill <id> selects it) } (path is workspace-relative, ~/… for the user's own, or absolute for a shipped skill)
// GET /commands?session_id= -> { commands: [{ name, description, argument_hint, path, source: 'project'|'user', ignored:
//   string[] (frontmatter such as allowed-tools that is never used), notes: string[] }], errors: [{ path, error }], folders }
// AgentTypes (GET /agents?session_id=, the session optional): { built_in: [{ name, built_in: true, profile,
//   description, model, effort }] ('' model or effort: the chat's; Settings.subagent_defaults), custom: [{ name,
//   built_in: false, description, path (workspace-relative), profile, tools (null: every tool of the profile),
//   model, effort (null: the chat's), unused_model? (the file's endpoint, which the user does not use: model is
//   then null and a note says so), skills, max_steps, notes, instructions_chars, disallowed_tools, permission_mode
//   (null: the chat's; applies only when narrower), mcp_servers (null: every configured one), memory ('none' or null),
//   initial_prompt_chars, background, shipped? (true: agents/<name>.md of the app, such as plan) }], errors: [{ path, error }],
//   folders, index_bytes }. Settings.subagent_defaults: { explore|general|editor: { model, effort } }.
//   A delegation's progress (event.delegation) and a subagents index entry carry the model and effort it started with.
// Changes (pushed as { type: 'changes', changes } and returned by Keep/Undo): {
//   files: [{ path, status: 'created'|'modified'|'deleted'|'unavailable', added, removed,
//   current_hash: string | null, error?, via_session_id? (a subagent of this chat made the edit),
//   source? ('command': a command the agent ran changed the file) }], added, removed, error? }
// ReviewComment (reviewComments.js, page memory only): { id, path, line, kind: 'added'|'context'|'removed',
//   text (the line), comment }; sent to the agent as one message by 'Send N comments'.
// Task (pushed as { type: 'tasks', tasks } when they change and after the snapshot): { id, title,
//   description, status: 'pending'|'in_progress'|'completed'|'cancelled', depends_on: string[], created, updated }
// GET /sessions/<id>/transcript.md: the conversation as Markdown (text/markdown, attachment file name
//   <date>-<title>-<id8>.md), as written by scripts/conversation_transcript.py, with secrets redacted.
// ChangeDiff (GET /sessions/<id>/changes/diff?path=): { path, status, added, removed,
//   baseline_hash, current_hash, hunks: [{ index, line, added, removed }],
//   lines: [{ kind: 'context'|'removed'|'added', text, old?, new?, hunk?, newline? }] }
// Changes view actions (git_tools.py; 409 while a chat in the project replies or a command runs there):
//   POST /git/stage?session_id= { paths: string[] (1..4,000, workspace-relative files), staged: boolean } -> the new
//   GET /git/status answer (an entry with submodule: true is a submodule whose commit changed: it has no stage
//   button); POST /git/commit?session_id= { message (1..10,000) } -> { commit: { hash, subject },
//   status }. GET /git/pr?session_id=&branch=&refresh? -> { available: true, number, title, state:
//   'open'|'closed'|'merged'|…, draft, url: string | null, review_decision: string | null, checks: { total, passed,
//   failed, pending } } or { available: false, code: 'no_gh'|'refused'|'no_git'|'no_pr'|'not_signed_in'|'no_repo'|
//   'no_branch'|'no_remote'|'timeout'|'error', reason }.
// WorkspaceFolders (GET, POST { path } and DELETE { path } /workspace-folders?session_id=, the
//   session optional): the project's folders added to Files, view only (the agent's access is
//   unchanged). { workspace (absolute project path), folders: [{ path (absolute), missing: boolean }],
//   added? (POST only: the resolved path of the folder just added, or already listed) }

// Reasoning effort levels (backend reasoning.EFFORT_LEVELS) and their labels.
export const EFFORT_LABELS = { default: 'Default', low: 'Fast', medium: 'Medium', high: 'High', xhigh: 'Extra high', max: 'Max' }

export function modelLabel(model) {
  return model.replace(/^databricks-/, '').replace(/^system\.ai\./, '')
    .replace(/\b(\d+)-(\d+)\b/g, '$1.$2')
    .replace(/-/g, ' ').replace(/\b(gpt|oss)\b/gi, s => s.toUpperCase())
    .replace(/\b(\d+)b\b/g, '$1B').replace(/^\w/, s => s.toUpperCase())
    .replace(/\b(claude|opus|sonnet|haiku|llama|gemini|gemma)\b/g, s => s[0].toUpperCase() + s.slice(1))
}
