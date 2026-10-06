import { html, useState } from '../lib.js'

export const CONTINUE_MESSAGE = 'Continue from where you stopped.'

// Why the latest turn of an idle conversation stopped before it finished, or
// null when it completed (or failed for another reason). The signals, from
// backend/local_agent/agents.py:
// - session.terminal_reason: in the snapshot (public_session) and in the idle
//   { type: 'status', status: 'idle', terminal_reason } message after each turn.
//   'step_limit': the agent step limit was reached (that notice, or a subagent's
//     error, also carries terminal_reason: 'step_limit').
//   'stopped': Stop ("Stopped. You can continue this conversation.").
//   'interrupted': a server restart (or an import) found the turn still running
//     and marked its events cancelled / its request_info interrupted.
//   'inference_error' with an error event whose error_kind is 'output_limit':
//     the reply still hit the output-token limit after automatic retries.
// - Older records cut short by a restart keep a stale terminal_reason; their
//   latest turn has request_info { status: 'interrupted', error_kind:
//   'incomplete_response' }. (An automatic output-limit retry marks a reply
//   'interrupted' without error_kind, and its turn goes on.)
export function stopReason(session) {
  if (!session || session.status !== 'idle') return null
  const events = session.events || []
  const start = events.findLastIndex(event => event.type === 'user')
  if (start < 0) return null
  const turn = events.slice(start + 1)
  const reason = session.terminal_reason
  if (reason === 'step_limit') return 'The agent reached its step limit.'
  if (reason === 'stopped') return 'The response was stopped.'
  if (reason === 'interrupted') return 'The response was interrupted.'
  if (reason === 'inference_error' && turn.findLast(event => event.type === 'error')?.error_kind === 'output_limit') {
    return 'The reply reached the output limit.'
  }
  if (turn.some(event => event.request_info?.status === 'interrupted' && event.request_info.error_kind === 'incomplete_response')) {
    return 'The response was interrupted.'
  }
  return null
}

// Props: session, disabled, onSend(text) -> Promise, onError(message)
export default function ContinueButton({ session, disabled, onSend, onError }) {
  const [sending, setSending] = useState(false)
  const reason = stopReason(session)
  if (!reason) return null
  const send = async () => {
    setSending(true)
    try {
      await onSend(CONTINUE_MESSAGE)
      // This button leaves once the turn starts; keep keyboard focus in the composer.
      document.querySelector('textarea[aria-label="Message"]')?.focus()
    }
    catch (e) { onError(e.message) }
    finally { setSending(false) }
  }
  return html`<div class="continue-bar" role="status"><span>${reason}</span>
    <button class="outline" title=${`Send “${CONTINUE_MESSAGE}”`} disabled=${disabled || sending} onClick=${() => void send()}>Continue</button></div>`
}
