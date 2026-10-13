// Shapes of the main API objects, for reference while editing (JSDoc only).
//
// Settings: { workspace, model, env_file, context_window, max_output_tokens,
//   max_agent_steps, compaction_handoffs?, approval_timeout_minutes? (0 = no limit),
//   system_notifications? (server notifications while no page is open), host, configured }
// Bootstrap (GET /bootstrap): { token, settings: Settings, storage_problems: StorageProblem[] }
// StorageProblem: { file, reason } a conversation file in the state folder that could not
//   be read; it is hidden and left unchanged. reason never quotes the file's contents.
// Connection: { connected, models: string[], error: string | null }
// Session: { id, title, workspace, model, status, events: AgentEvent[], updated,
//   permission_mode, allowed_directories, allow_rules: AllowRule[], context_info?, active_skills?,
//   parent_session_id?, is_subagent?, delegation?, tool_profile?, subagent_tool_profile?,
//   terminal_reason?: how the last turn ended: 'completed'|'stopped'|'error'|'inference_error'|
//   'tool_error'|'step_limit'|'interrupted' (also in the /api/sessions summaries),
//   turn_ended?: epoch seconds when the last turn ended; null while a turn runs (also in summaries),
//   turn_delegated?: true while the last turn is one a parent's delegate_task started (also in summaries) }
//   GET /sessions list items omit events and allow_rules.
// AllowRule ("Allow for this chat", exact matches only): { tool: 'run_command', command }
//   | { tool: 'edits' } (write_file, edit_file, insert_activity_log) | { tool: 'mcp__<server>__<tool>' }
//   | { tool: 'hook', hook: <hook id> }. Removed with DELETE /sessions/<id>/allow-rules (body: the rule).
// Approval answer (POST /sessions/<id>/approvals/<event id>): { allowed, remember?: true (approve only),
//   note?: string (decline only, at most 2,000 characters) }
// The 'permissions' socket message: { type: 'permissions', permission_mode, allowed_directories, allow_rules }
// Status message: { type: 'status', status, terminal_reason?, turn_ended? (sent with 'idle'
//   when a turn ended; a manual compaction leaves turn_ended unchanged), turn_delegated? (true, with
//   turn_ended, when a parent's delegate_task started that turn) }
// /api/presence: a WebSocket (same protocols as the stream) each page holds open; no messages.
// AgentEvent: { id, type: 'user'|'assistant'|'tool'|'notice'|'error', text?,
//   reasoning_summary?, reasoning_truncated?, child_session_id?, request_info?,
//   name?, input?, output?, preview?, state?, delegation?, origin?,
//   approval?: 'approved'|'declined'|'expired', approval_expires? (epoch seconds, only with a limit),
//   decline_note? (the user's note sent with Decline), allowed_by_rule? (true: ran by an AllowRule, no card) }
// ApiError: { message, status, detail? }. A 409 that a busy chat causes has
//   detail { message, session_id, title } naming that chat; other errors have no detail.
// RequestInfo: { model, status, finish_reason?, http_status?, error_kind?, usage? }
// ContextInfo: { estimated_tokens, input_budget, context_window, reply_reserve,
//   compactions, summarized_messages, estimate_method, instruction_files, warnings,
//   instruction_sources?, prepared_for_next_turn?, breakdown?, estimate_scale?, summary_adjustment? }
// Job: { id, session_id, workspace, command, state, created, updated, exit_code,
//   output?, truncated, timeout_seconds, max_output_bytes, background,
//   without_shell? (true: ran from an argument vector without a shell) }
// Skill: { id, name, description, path } (path is workspace-relative, or absolute
//   for a skill shipped with the app)
// Changes (pushed as { type: 'changes', changes } and returned by Keep/Undo): {
//   files: [{ path, status: 'created'|'modified'|'deleted'|'unavailable', added, removed,
//   current_hash: string | null, error? }], added, removed, error? }
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

export function modelLabel(model) {
  return model.replace(/^databricks-/, '').replace(/^system\.ai\./, '')
    .replace(/-/g, ' ').replace(/\b(gpt|oss)\b/gi, s => s.toUpperCase())
    .replace(/\b(\d+)b\b/g, '$1B').replace(/^\w/, s => s.toUpperCase())
}
