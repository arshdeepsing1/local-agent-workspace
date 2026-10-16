// Shapes of the main API objects, for reference while editing (JSDoc only).
//
// Settings: { workspace, model, env_file, context_window, max_output_tokens,
//   max_agent_steps, compaction_handoffs?, auto_compact_percent? (50-100, default 90: automatic compaction runs
//   when the next request's estimate passes this share of the input budget), approval_timeout_minutes? (0 = no limit),
//   system_notifications? (server notifications while no page is open),
//   auto_keep_changes? (true: Keep every listed change when a chat's reply ends), host, configured,
//   spending_limit_dbu? (0-100000, default 0 = none: the spending limit of chats without their own; spending.py),
//   coding_dir (default folder for new standalone code: the saved or .env value, '' = off),
//   python_venv (virtual environment the agent's commands, Verify, hooks and the Terminal use, '' = off),
//   coding_dir_status: { path, state: 'off'|'ready'|'missing'|'invalid', message, source: 'settings'|'env'|'none' }
//     (path: the checked absolute folder; the raw value only when invalid; null when off),
//   python_venv_status: { path, state: 'off'|'ready'|'invalid', message, python, version, pip (when ready: bool, or null if no site-packages folder is found), source },
//   default_model (the app's default endpoint),
//   default_limits: { context_window, max_output_tokens } (the saved model's defaults; budgets equal to them
//     follow a model change), model_migrated_from? (set while a legacy settings file at the old default
//   model is moved at load; cleared by the next save, which writes settings_version 2),
//   reasoning_effort: Effort (new chats' level, default 'default'), reasoning_format: 'auto'|'anthropic'|'openai'|'none'
//   (how agent requests ask for it; 'auto' = per endpoint), thinking_summaries: boolean (thinking.display 'summarized'
//   for adaptive Claude endpoints; default true), prompt_caching: 'auto'|'messages'|'off' (cache markers on agent
//   requests to Databricks-hosted Claude endpoints; 'messages' = only the documented forms; default 'auto'),
//   endpoint_profiles: [{ model, label }] (endpoints with built-in facts) }
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
// POST /settings/limits { model, context_window, max_output_tokens } -> LimitsCheck:
//   { profile: { label, context_max, output_max, itpm, otpm, recommended_context_window,
//       recommended_max_output_tokens, thinking_reserve, model, output_concurrency_limit, context_rate_limit } | null,
//     recommended: { context_window, max_output_tokens } | null,
//     defaults: { context_window, max_output_tokens } (this endpoint's defaults, with or without a profile),
//     effective: { context_window, max_output_tokens, clamped }, warnings: [{ code, level: 'error'|'warning'|'note', text }],
//     note }
// Bootstrap (GET /bootstrap): { token, settings: Settings, storage_problems: StorageProblem[] }
// StorageProblem: { file, reason } a conversation file in the state folder that could not
//   be read; it is hidden and left unchanged. reason never quotes the file's contents.
// Connection: { connected, models: string[], error: string | null,
//   error_kind?: 'credentials'|'http'|'network'|'invalid_response' (which step failed; absent on success),
//   http_status?: number (with error_kind 'http') }
// Session: { id, title, workspace, model, status, events: AgentEvent[], updated,
//   permission_mode: 'manual'|'auto'|'acceptEdits'|'dontAsk'|'plan'|'bypassPermissions', allowed_directories,
//   allow_rules: Rule[] (this chat's rules), disabled_tools?: string[] (turned-off tools), context_info?, active_skills?,
//   parent_session_id?, is_subagent?, delegation?, tool_profile?, subagent_tool_profile?,
//   agent_type?: 'explore'|'general'|'editor' or a custom agent's name (a subagent), subagents?: SubagentEntry[] (a parent's index of its
//   subagent chats; left out of GET /sessions list items),
//   terminal_reason?: how the last turn ended: 'completed'|'stopped'|'error'|'inference_error'|
//   'tool_error'|'step_limit'|'spending_limit'|'interrupted'|'hook_blocked' (a user_prompt hook blocked the
//   message; also in the /api/sessions summaries),
//   turn_ended?: epoch seconds when the last turn ended; null while a turn runs (also in summaries),
//   turn_delegated?: true while the last turn is one a parent's delegate_task started (also in summaries),
//   reasoning_effort?: Effort (the chat's own level, saved in its header; absent on chats saved before it existed,
//   which use Settings.reasoning_effort; kept by forks and imports; also in summaries),
//   spending_limit_dbu?: number (the chat's own spending limit; 0 = none; absent: Settings.spending_limit_dbu;
//   kept by forks and imports) }
// POST /sessions { permission_mode, reasoning_effort? } creates a chat (Settings' effort when absent).
// PUT /sessions/<id>/reasoning-effort { reasoning_effort } -> Session, also while busy: the agent's next model request
//   (and a running subagent's) uses it. Pushed as { type: 'reasoning_effort', reasoning_effort }.
// PUT /sessions/<id>/spending-limit { spending_limit_dbu: number 0..100000 | null (follow Settings) } -> Session, also
//   while busy (400 for a subagent). Pushed as { type: 'spending_limit', spending_limit_dbu, spending: Spending }.
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
//   path?: string, hook?: string }. A rule without behavior (an M1 "Allow for this chat" rule) means allow.
//   tool: 'run_command' (command = M1 exact text, chat and allow only; argv = exact argv; prefix with flags =
//   the prefix followed only by those options, '--name=' allowing any value; no matcher = every command,
//   deny and ask only) | 'edits' (every edit tool; path = an absolute folder or file) | 'hook' (hook = id)
//   | 'delegate_task' | 'stop_job' | 'mcp__<server>__<tool>' | 'mcp__<server>__*' (deny and ask only)
//   | another tool name (deny only). Deny wins over ask, ask over allow. A deny rule for read_file also covers
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
//   turn_ended, when a parent's delegate_task started that turn) }
// /api/presence: a WebSocket (same protocols as the stream) each page holds open; no messages.
// AgentEvent: { id, type: 'user'|'assistant'|'tool'|'notice'|'error', text?,
//   reasoning_summary? (the reply's thinking text the endpoint returned, redacted, at most 12,000 characters),
//   reasoning_truncated?, reasoning_started? (epoch seconds when its thinking began), reasoning_seconds? (how long it
//   thought: until answer text or a tool call began, or the response ended; also set when the endpoint reported
//   reasoning tokens without text), child_session_id?, request_info?,
//   name?, input?, output?, preview?, state?, delegation?, origin?,
//   (delegation: Delegation, on a delegate_task card or a notice with child_session_id),
//   approval?: 'approved'|'declined'|'expired'|'blocked', approval_expires? (epoch seconds, only with a limit),
//   decline_note? (the user's note sent with Decline), allowed_by_rule? (true: ran by a Rule, no card),
//   rule_scope?: 'chat'|'project'|'user' (with allowed_by_rule), read_only_command? (true: ran from the fixed
//   read-only command list), approved_by_mode? (the mode whose switch approved a waiting card),
//   without_shell? (true: the command ran as an argv without a shell), edited? (true: the user edited the
//   action before approving it), blocked_reason? (with approval 'blocked': why it did not run),
//   protected? (the reason a file is protected), model_output? (what the agent received when it differs from
//   output), result_withheld?: { estimated_tokens, allowance_tokens } (a read whose result did not fit the
//   response's context allowance: the agent got a result_deferred message), result_shortened?: true (an action's
//   result was shortened for the agent),
//   and on pending cards only: rememberable?: ('chat'|'project'|'user')[], rule_options?: { argv: string[] | null,
//   prefixes: [{ prefix: string[], flags: string[] }], reason: string | null } (commands) | { folder } (edits),
//   editable?: ('command'|'content'|'new_text')[], uncertain?: true (a command that could not be checked
//   exactly against deny and ask rules) }
//   ask_user events: { name: 'ask_user', input: { questions }, answers?: [{ question, selected, other }],
//   question_outcome?: 'answered'|'skipped'|'expired', skipped_reason?: 'dont_ask', approval_expires? };
//   answered with
//   POST /sessions/<id>/questions/<event id> { answers?: [{ selected, other? }], skip?: true }.
//   submit_plan events: { name: 'submit_plan', input: { plan, title? }, plan_outcome?: 'approved'|'keep_planning'|
//   'expired'|'superseded', plan_mode?, feedback?, approval_expires? }; answered with
//   POST /sessions/<id>/plans/<event id> { choice, feedback? }.
//   User events also carry in_wire? (true once the text reached the model history as its own message; false
//   when the turn failed before that; absent on older events), steered? (true: sent while the agent was
//   working) and attachments?: Attachment[]. The 'Stopped.' notice carries stop_phase?: 'streaming'|'tools'|'waiting'.
//   A compaction's notice carries compaction?: Compaction (one notice per compaction).
//   Hooks (lifecycle_hooks.py): a hook card (name 'hook_<event>', input { hook_id, tool?, call_id?, event?,
//   subagent_session_id? }) carries hook_result?: { json: true, decision?, reason?, context?, arguments?: { before,
//   after }, prompt?: { before, after }, report?, ignored?: string[] } when the hook printed one JSON object. A tool
//   card can carry hook_change?: { hooks: string[], original } (a before-tool hook replaced the arguments; input is
//   what ran), hook_ask?: { hook_id, reason } (a hook asked for its card) and hook_blocked?: { hook_id, reason }.
//   A user event can carry hook_blocked? (never sent), hook_edit?: { hooks, original } (text is the hook's
//   replacement) and hook_notes?: [{ event, hook_id, context }] (context sent to the model after the message).
// Delegation: { status: 'queued'|'running'|'awaiting_approval'|...|'completed'|'failed'|'cancelled'|'interrupted',
//   completed_tools, last_tool?, terminal_reason?, agent_type?, started?, ended? (epoch seconds) }. A finished
//   delegate_task card's output is the JSON result { child_session_id, agent_type, tool_profile, status,
//   terminal_reason, steps_used, tool_calls, changed_files, changed_files_total?, transcript, output }.
// SubagentEntry: { id, task (200 characters), agent_type, status, created, ended, transcript ('conversations/<id>.jsonl'
//   in the state folder, null once deleted), steps_used?, tool_calls?, deleted? }.
// GET /sessions/<id>/children -> list items of its subagent chats, each with status, transcript and subagent?: SubagentEntry.
// Attachment (metadata only, never content): { kind: 'file', path, start_line, end_line, truncated? }
//   | { kind: 'shell', command (at most 2,000 characters), exit_code }
// RewindPreview (GET /sessions/<id>/rewind-preview?before_event_id=<user event id>; read-only, also while busy):
//   { before_event_id, conversation: { possible, reason: string | null, events_removed, messages_removed,
//   summary_dropped }, code: { possible, reason: string | null, files: [{ path, status: 'restore'|'unchanged'|
//   'drifted'|'interleaved'|'unavailable', error? }] } }
// Rewind (POST /sessions/<id>/rewind { before_event_id, conversation?: boolean, code?: boolean, resend_text? };
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
//   rate_limit?: { limit_type, limit?, current?, retry_after? } (only on a final, not retried, 429),
//   reasoning?: { effort, format, fields, sent_effort?, effort_field?, skipped?: 'rejected'|'rejected_earlier',
//   rejected_field? } (absent when the request was at Default and sent nothing) }.
//   error_kind includes 'rate_limit', 'payload_too_large' (over the 4 MB request limit; not sent) and
//   'output_tokens_over_limit' (Max output tokens above the endpoint's reported OTPM limit).
// ContextInfo: { estimated_tokens, input_budget, context_window, reply_reserve,
//   compactions, summarized_messages, estimate_method, instruction_files, warnings,
//   instruction_sources?, prepared_for_next_turn?, breakdown?, estimate_scale?, summary_adjustment? (the summary was over its
//   byte limit), summary_fitted_to_budget?: 'condensed'|'trimmed' (shortened because the request after compaction
//   was over the input budget),
//   estimate_prior? (true: estimate_scale is the model's starting ratio, not yet from reported counts),
//   auto_compact_threshold?, auto_compact_percent?, compaction_deferred? (over the threshold but within the input
//   budget, and no cut brings it under the threshold: sent as it was), compaction?: Compaction (when this request compacted), last_request?: LastRequest,
//   next_estimate?: { tokens, scale?, percent_of_input_budget, percent_of_threshold } (the app's estimate of the
//   next request before the user's next message; set when a turn ends), payload_bytes? (the size of the request
//   body sent), payload_limit_bytes? (the Databricks per-request limit, 4,000,000) }. After a model change or a
//   Settings save the budget fields, the threshold and next_estimate of an idle chat follow the new setup;
//   estimated_tokens, estimate_scale and last_request still describe the last request.
// Compaction: { number, trigger: 'manual'|'in_turn'|'auto', before_tokens, after_tokens, threshold_tokens,
//   summarized_messages, requests (summary and condense requests), elided_bytes, superseded_results }
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
//   pricing, last_input: LastRequest | null }. A call has effort?, effort_field? and reasoning_skipped? like
//   RequestInfo.reasoning; retry_reason 'reasoning_rejected' marks the resend without reasoning fields, and
//   'cache_rejected' the resend with fewer cache markers. Agent calls also have prompt_cache? ('full'|'messages'|'off':
//   the cache markers sent, on Claude endpoints), system_tail_changed? (true), usage_keys? (the raw usage key names),
//   reasoning_shape? and reasoning_signature? (true).
// Job: { id, session_id, workspace, command, state, created, updated, exit_code,
//   output?, truncated, timeout_seconds, max_output_bytes, background,
//   without_shell? (true: ran from an argument vector without a shell) }
// Composer (M4): Session.queued_messages?: QueuedMessage[]; Session.pending_context?: [{ kind: 'shell', command,
//   exit_code }] (! output attached to the next message; its text stays on the server). Pushed as
//   { type: 'pending_context', pending_context } when a command adds one and when a message takes them.
// QueuedMessage: { id, text, created, paused } (paused: left over from a stop or failure; sent only by
//   'Send queued messages'). POST /sessions/<id>/messages while busy -> { ok, queued: true, queued_messages };
//   otherwise { ok: true }. 400 when 3 are waiting or their text would exceed 50,000 characters.
//   DELETE /sessions/<id>/queue/<item id> -> { queued_messages } (404 'This queued message was already sent.');
//   POST /sessions/<id>/queue/send (idle only) sends all of them as one message.
//   Socket: { type: 'queue', queued_messages, error? } (error: why a queue could not start; stored as session.queue_error).
// User AgentEvent extras: attachments?: [{ kind: 'file', path, start_line, end_line, truncated } |
//   { kind: 'shell', command, exit_code }]; steered?, in_wire? (true: queued, sent mid-turn).
// POST /sessions/<id>/shell { command (one line, 1..2000), timeout_seconds?, max_output_bytes? } -> 202 { ok }
//   (idle only; runs in every mode; shows a 'user_command' tool event).
// POST /sessions/<id>/side-question { question (1..4000) } -> { answer, request_info: { status, finish_reason, usage? } };
//   409 while another side question of the chat is answered.
// GET /sessions/<id>/mention-files?q= (or /mention-files?q= before a chat exists) -> [{ path, name }] (at most 30;
//   only paths an @ token can spell). Queued messages must also fit one message's @ limits together.
// Skill: { id, name, description, path } (path is workspace-relative, or absolute
//   for a skill shipped with the app)
// AgentTypes (GET /agents?session_id=, the session optional): { built_in: [{ name, built_in: true, profile,
//   description, model, effort }] ('' model or effort: the chat's; Settings.subagent_defaults), custom: [{ name,
//   built_in: false, description, path (workspace-relative), profile, tools (null: every tool of the profile),
//   model, effort (null: the chat's), unused_model? (the file's endpoint, which the user does not use: model is
//   then null and a note says so), skills, max_steps, notes, instructions_chars }], errors: [{ path, error }],
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
