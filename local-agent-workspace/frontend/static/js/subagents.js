// Shared labels for delegate_task subagents (the conversation's cards and Agent tools → Subagents).
import { EFFORT_LABELS, modelLabel } from './types.js'

// The built-in agent types of delegate_task (backend planning.AGENT_TYPES); a custom agent shows its name.
export const AGENT_TYPE_LABELS = { explore: 'Explore', general: 'General', editor: 'Editor' }

export const agentLabel = kind => AGENT_TYPE_LABELS[kind] || kind

// "Fast · Claude Haiku 5.5": the effort and model a subagent started with (its delegation progress or
// index entry; subagents from before custom agents recorded neither).
export function agentSettingsText(effort, model) {
  return [EFFORT_LABELS[effort] || '', typeof model === 'string' && model ? modelLabel(model) : ''].filter(Boolean).join(' · ')
}

// "45 s", "3 min 05 s", "1 h 02 min": how long a subagent ran or has been running.
export function elapsedText(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0))
  if (total < 60) return `${total} s`
  const minutes = Math.floor(total / 60)
  if (minutes < 60) return `${minutes} min ${String(total % 60).padStart(2, '0')} s`
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, '0')} min`
}
