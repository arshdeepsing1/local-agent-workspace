import { api, apiFile } from './api.js'
import { modelLabel } from './types.js'
import { BYPASS_CONFIRM, modes } from './components/PermissionsMenu.js'

// Built-in / commands. Typed at the start of the message box (or chosen in the / menu), they act in
// this page: they are never sent to the agent and never queued. The server keeps their names (and
// btw, skill) for them (backend extensions.RESERVED_COMMANDS): a skill or command template with such
// a name is reached with /skill <id>. args: 'none' (only "/id" alone is the command; "/help me…" is
// an ordinary message), 'optional' or 'required'. whileBusy: allowed while a reply runs.
export const COMMANDS = [
  { id: 'new', args: 'none', whileBusy: true, description: 'Start a new conversation.' },
  { id: 'clear', args: 'none', whileBusy: true, description: 'Start a new conversation with this one’s model, mode and effort; this one is kept.' },
  { id: 'compact', args: 'optional', hint: '[note]', description: 'Summarize earlier turns now; the note says what to keep.' },
  { id: 'model', args: 'optional', hint: '[name]', description: 'Switch this conversation’s model, or choose one in the list.' },
  { id: 'mode', args: 'optional', whileBusy: true, hint: '[mode]', description: 'Permission mode: manual, auto, accept-edits, plan, dont-ask or bypass.' },
  { id: 'context', args: 'none', whileBusy: true, description: 'Show what fills the context and when it is compacted.' },
  { id: 'usage', args: 'none', whileBusy: true, description: 'Show this conversation’s usage and estimated cost.' },
  { id: 'export', args: 'none', whileBusy: true, description: 'Export, copy or fork this conversation.' },
  { id: 'fork', args: 'none', description: 'Copy this completed conversation into a new one and open it.' },
  { id: 'rename', args: 'required', whileBusy: true, hint: '<title>', description: 'Rename this conversation.' },
  { id: 'copy', args: 'optional', whileBusy: true, hint: '[all]', description: 'Copy the last reply; /copy all copies the conversation as Markdown.' },
  { id: 'help', args: 'none', whileBusy: true, description: 'List the commands.' },
]

// The side question is built in too; the message box sends it to /side-question (Composer).
export const SIDE_QUESTION = { id: 'btw', args: 'required', whileBusy: true, hint: '<question>', name: 'Side question',
  description: 'Ask about this conversation without adding to it (one model request).' }

// { command, args } when text (trimmed) is a built-in command, else null. Only a whole word after the
// slash counts, so paths such as /tmp/x and unknown /words stay ordinary messages.
export function parseCommand(text) {
  const match = /^\/([a-z]+)(?:\s+([\s\S]*))?$/.exec(text || '')
  const command = match && COMMANDS.find(item => item.id === match[1])
  if (!command) return null
  const args = (match[2] || '').trim()
  if (command.args === 'none' && args) return null
  return { command, args }
}

// Typed names for the permission modes (and the menu's labels, in any case).
const MODE_NAMES = { manual: 'manual', auto: 'auto', 'accept-edits': 'acceptEdits', acceptedits: 'acceptEdits', accept: 'acceptEdits',
  plan: 'plan', 'dont-ask': 'dontAsk', dontask: 'dontAsk', "don't-ask": 'dontAsk', bypass: 'bypassPermissions',
  'bypass-permissions': 'bypassPermissions', bypasspermissions: 'bypassPermissions' }
export function modeNamed(text) {
  const key = text.trim().toLowerCase()
  return MODE_NAMES[key] || MODE_NAMES[key.replace(/\s+/g, '-')] || modes.find(item => item.label.toLowerCase() === key)?.value || null
}

// The endpoint a typed name means: an exact endpoint name or its label, in any case, among choices.
export function modelNamed(text, choices) {
  const key = text.trim().toLowerCase()
  return choices.find(model => model.toLowerCase() === key)
    || choices.find(model => modelLabel(model).toLowerCase() === key) || null
}

// The text of the last reply in a conversation's loaded events, or null.
export const lastReply = session => session?.events?.findLast(event => event.type === 'assistant' && event.text)?.text || null

// Copy text that is still loading. Safari allows a clipboard write only during the user's action, so the
// write starts at once and receives the text when it arrives.
export async function copyLoaded(load) {
  if (window.ClipboardItem && navigator.clipboard?.write) {
    const blob = load().then(text => new Blob([text], { type: 'text/plain' }))
    await navigator.clipboard.write([new ClipboardItem({ 'text/plain': blob })])
  } else await navigator.clipboard.writeText(await load())
}

const NEEDS_CHAT = 'Start a conversation first: this command works on an existing conversation.'

// Runs one command. ctx: { app (useWorkspace), session (the open chat or null), busy, models (choices for
// /model), choose(name) (opens the model list), openMenu() (the permission menu), openContext(),
// openAgentTools(tab), rename(id, title), notify(text), help() }. Throws an Error whose message the
// message box shows; the typed command then stays in the box.
export async function runCommand(id, args, ctx) {
  const command = COMMANDS.find(item => item.id === id)
  if (!command) throw new Error(`/${id} is not a command.`)
  const { app, session } = ctx
  if (ctx.busy && !command.whileBusy) throw new Error(`Stop the response first: /${id} runs when the conversation is idle.`)
  if (command.args === 'required' && !args) throw new Error(`Add ${command.hint} after /${id}.`)
  const chat = () => { if (!session) throw new Error(NEEDS_CHAT); return session }
  switch (id) {
    case 'new': app.newConversation(); return
    case 'clear': {
      if (!session) { app.newConversation(); return }
      let mode = session.permission_mode || 'manual'
      // A new chat starts in Bypass only after the same confirmation the permission menu asks for.
      if (mode === 'bypassPermissions' && !window.confirm(BYPASS_CONFIRM)) mode = 'manual'
      app.newConversation({ model: session.model, mode, effort: session.reasoning_effort })
      return
    }
    case 'compact': {
      const current = chat()
      if ([...args].length > 1000) throw new Error('A preservation note has at most 1,000 characters.')
      if (current.context_info?.compactable === false) {
        throw new Error(current.context_info.compactions > 0
          ? 'Nothing to compact yet: earlier turns are already summarized and the latest turn is kept whole. Compact again after your next message.'
          : 'Nothing to compact yet: this conversation has one turn, and the latest turn is kept whole. Compact after your next message.')
      }
      await api(`/sessions/${encodeURIComponent(current.id)}/compact`, 'POST', { preservation_note: args })
      return
    }
    case 'model': {
      if (!args) { ctx.choose(); return }
      const model = modelNamed(args, ctx.models)
      if (!model) throw new Error(`No model named “${args}”. Choose one of: ${ctx.models.map(modelLabel).join(', ')}.`)
      await app.changeModel(model)
      return
    }
    case 'mode': {
      if (!args) { ctx.openMenu(); return }
      const mode = modeNamed(args)
      if (!mode) throw new Error(`No permission mode named “${args}”. Use manual, auto, accept-edits, plan, dont-ask or bypass.`)
      const current = app.permissionMode
      if (mode === 'bypassPermissions' && current !== 'bypassPermissions') {
        if (!window.confirm(BYPASS_CONFIRM)) return
        await app.setPermissionMode(mode, { confirm: true })
      } else await app.setPermissionMode(mode)
      return
    }
    case 'context': {
      if (!chat().context_info) throw new Error('Context usage is estimated after the first turn.')
      ctx.openContext()
      return
    }
    case 'usage': ctx.openAgentTools('Usage'); return
    case 'export': ctx.openAgentTools('Export'); return
    case 'fork': {
      const created = await api(`/sessions/${encodeURIComponent(chat().id)}/fork`, 'POST')
      app.setActiveId(created.id)
      void app.refreshSessions().catch(() => {})
      return
    }
    case 'rename': {
      const title = args.split(/\s+/).join(' ')
      if (title.length > 160) throw new Error('A conversation title has at most 160 characters.')
      await ctx.rename(chat().id, title)
      return
    }
    case 'copy': {
      if (args && args.toLowerCase() !== 'all') throw new Error('Use /copy for the last reply or /copy all for the conversation.')
      if (args) {
        const path = `/sessions/${encodeURIComponent(chat().id)}/transcript.md`
        await copyLoaded(async () => (await apiFile(path)).text)
        ctx.notify('Conversation copied as Markdown.')
        return
      }
      const text = lastReply(session)
      if (!text) throw new Error('There is no reply to copy yet.')
      await navigator.clipboard.writeText(text)
      ctx.notify('Last reply copied.')
      return
    }
    case 'help': ctx.help(); return
  }
}
