// Shapes of the main API objects, for reference while editing (JSDoc only).
//
// Settings: { workspace, model, env_file, context_window, max_output_tokens,
//   max_agent_steps, compaction_handoffs?, host, configured }
// Connection: { connected, models: string[], error: string | null }
// Session: { id, title, workspace, model, status, events: AgentEvent[], updated,
//   permission_mode, allowed_directories, context_info?, active_skills?,
//   parent_session_id?, is_subagent?, delegation?, tool_profile?, subagent_tool_profile? }
// AgentEvent: { id, type: 'user'|'assistant'|'tool'|'notice'|'error', text?,
//   reasoning_summary?, reasoning_truncated?, child_session_id?, request_info?,
//   name?, input?, output?, preview?, state?, delegation?, origin? }
// RequestInfo: { model, status, finish_reason?, http_status?, error_kind?, usage? }
// ContextInfo: { estimated_tokens, input_budget, context_window, reply_reserve,
//   compactions, summarized_messages, estimate_method, instruction_files, warnings,
//   instruction_sources?, prepared_for_next_turn?, breakdown?, estimate_scale?, summary_adjustment? }
// Job: { id, session_id, workspace, command, state, created, updated, exit_code,
//   output?, truncated, timeout_seconds, max_output_bytes, background }

export function modelLabel(model) {
  return model.replace(/^databricks-/, '').replace(/^system\.ai\./, '')
    .replace(/-/g, ' ').replace(/\b(gpt|oss)\b/gi, s => s.toUpperCase())
    .replace(/\b(\d+)b\b/g, '$1B').replace(/^\w/, s => s.toUpperCase())
}
