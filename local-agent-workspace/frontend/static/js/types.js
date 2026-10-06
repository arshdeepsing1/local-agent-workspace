// Shapes of the main API objects, for reference while editing (JSDoc only).
//
// Settings: { workspace, model, env_file, context_window, max_output_tokens,
//   max_agent_steps, compaction_handoffs?, approval_timeout_minutes? (0 = no limit),
//   system_notifications? (server notifications while no page is open), host, configured }
// Connection: { connected, models: string[], error: string | null }
// Session: { id, title, workspace, model, status, events: AgentEvent[], updated,
//   permission_mode, allowed_directories, context_info?, active_skills?,
//   parent_session_id?, is_subagent?, delegation?, tool_profile?, subagent_tool_profile?,
//   terminal_reason?: how the last turn ended: 'completed'|'stopped'|'error'|'inference_error'|
//   'tool_error'|'step_limit'|'interrupted' (also in the /api/sessions summaries) }
// Status message: { type: 'status', status, terminal_reason? (sent with 'idle') }
// AgentEvent: { id, type: 'user'|'assistant'|'tool'|'notice'|'error', text?,
//   reasoning_summary?, reasoning_truncated?, child_session_id?, request_info?,
//   name?, input?, output?, preview?, state?, delegation?, origin?,
//   approval?: 'approved'|'declined'|'expired', approval_expires? (epoch seconds, only with a limit) }
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
