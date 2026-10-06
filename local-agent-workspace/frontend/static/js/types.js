// Shapes of the main API objects, for reference while editing (JSDoc only).
//
// Settings: { workspace, model, env_file, context_window, max_output_tokens,
//   max_agent_steps, compaction_handoffs?, approval_timeout_minutes? (0 = no limit), host, configured }
// Bootstrap (GET /bootstrap): { token, settings: Settings, storage_problems: StorageProblem[] }
// StorageProblem: { file, reason } a conversation file in the state folder that could not
//   be read; it is hidden and left unchanged. reason never quotes the file's contents.
// Connection: { connected, models: string[], error: string | null }
// Session: { id, title, workspace, model, status, events: AgentEvent[], updated,
//   permission_mode, allowed_directories, allow_rules: AllowRule[], context_info?, active_skills?,
//   parent_session_id?, is_subagent?, delegation?, tool_profile?, subagent_tool_profile? }
//   GET /sessions list items omit events and allow_rules.
// AllowRule ("Allow for this chat", exact matches only): { tool: 'run_command', command }
//   | { tool: 'edits' } (write_file, edit_file, insert_activity_log) | { tool: 'mcp__<server>__<tool>' }
//   | { tool: 'hook', hook: <hook id> }. Removed with DELETE /sessions/<id>/allow-rules (body: the rule).
// Approval answer (POST /sessions/<id>/approvals/<event id>): { allowed, remember?: true (approve only),
//   note?: string (decline only, at most 2,000 characters) }
// The 'permissions' socket message: { type: 'permissions', permission_mode, allowed_directories, allow_rules }
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
//   output?, truncated, timeout_seconds, max_output_bytes, background }
// Skill: { id, name, description, path } (path is workspace-relative, or absolute
//   for a skill shipped with the app)
// Changes (pushed as { type: 'changes', changes } and returned by Keep/Undo): {
//   files: [{ path, status: 'created'|'modified'|'deleted'|'unavailable', added, removed,
//   current_hash: string | null, error? }], added, removed, error? }
// ChangeDiff (GET /sessions/<id>/changes/diff?path=): { path, status, added, removed,
//   baseline_hash, current_hash, hunks: [{ index, line, added, removed }],
//   lines: [{ kind: 'context'|'removed'|'added', text, old?, new?, hunk?, newline? }] }

export function modelLabel(model) {
  return model.replace(/^databricks-/, '').replace(/^system\.ai\./, '')
    .replace(/-/g, ' ').replace(/\b(gpt|oss)\b/gi, s => s.toUpperCase())
    .replace(/\b(\d+)b\b/g, '$1B').replace(/^\w/, s => s.toUpperCase())
}
