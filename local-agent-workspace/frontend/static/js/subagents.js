// Shared labels for delegate_task subagents (the conversation's cards and Agent tools → Subagents).

// The agent types of delegate_task (backend planning.AGENT_TYPES).
export const AGENT_TYPE_LABELS = { explore: 'Explore', general: 'General', editor: 'Editor' }

// "45 s", "3 min 05 s", "1 h 02 min": how long a subagent ran or has been running.
export function elapsedText(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0))
  if (total < 60) return `${total} s`
  const minutes = Math.floor(total / 60)
  if (minutes < 60) return `${minutes} min ${String(total % 60).padStart(2, '0')} s`
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, '0')} min`
}
