// How the transcript lays out its events (Conversation.js): consecutive finished read-only tool cards fold under one
// head ("Ran 3 commands, read 2 files"), and the delegate_task cards of one multi-agent run share a head. Pure: the
// same events give the same layout, and nothing here reads the clock.
//
// The transcript stays flat: every event keeps its own item, in order, keyed by its id, and a group or a run only
// adds one head item before its first member. So no card is moved into a new parent (a card keeps its state), the
// pending cards keep their order on the page, and Load earlier keeps its scroll anchor.
//
// RenderItem:
//   { kind: 'event', event, group?, run?, role?, quiet?, cls? }
//     group: the key of the group the event belongs to (a member, or a quiet reply between two members);
//     run: the key of the run whose span holds it; role: 'member' (one of its delegate_task cards) or 'hook' (a hook
//     card between them), with cls 'run-member'; quiet: a hook card that decided nothing (a closed run hides it).
//   { kind: 'group', key: 'group:<first member id>', members: [ids], counted: [ids], phrase, lastId, steps, commands }
//     members: the tool cards (counted ones and quiet hook cards); counted: those that count toward GROUP_MIN;
//     lastId: the last counted card (the head shows its subject); steps: the model responses the group spans;
//     commands: every counted card is a command.
//   { kind: 'run', key: 'run:<batch id or first member id>', members: [ids], batch: { id, index, size } | null }

export const GROUP_MIN = 2

// The tools that can fold, and the phrase each adds to the head.
const GROUPABLE = new Map([
  ['run_command', 'command'], ['read_file', 'read'], ['outline_file', 'read'], ['notebook_read', 'read'],
  ['list_files', 'list'], ['project_overview', 'list'], ['find_files', 'search'], ['search_files', 'search'],
  ['list_jobs', 'jobs'], ['get_job_output', 'jobs'], ['wait_job', 'jobs'], ['web_fetch', 'fetch'], ['web_search', 'web'],
  ['list_tasks', 'lookup'], ['get_task', 'lookup'], ['list_skills', 'lookup'], ['get_tool_output', 'lookup'],
  ['get_subagent_result', 'lookup'],
])
// Job tools whose neutral label reports the job, not the tool ("job exit 1", "wait timed out"): they still fold.
const NEUTRAL_JOB_TOOLS = new Set(['wait_job', 'get_job_output', 'list_jobs'])
const QUIET = 'quiet', HOOK = 'hook', DELEGATE = 'delegate', BREAK = 'break'

const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`
const times = (count, verb) => count === 1 ? `${verb} once` : `${verb} ${count} times`
const PHRASES = {
  command: count => `ran ${plural(count, 'command', 'commands')}`,
  read: count => `read ${plural(count, 'file', 'files')}`,
  search: count => times(count, 'searched'),
  list: count => `listed ${plural(count, 'folder', 'folders')}`,
  jobs: count => times(count, 'checked jobs'),
  fetch: count => `fetched ${plural(count, 'page', 'pages')}`,
  web: count => times(count, 'searched the web'),
  lookup: count => `looked up ${plural(count, 'item', 'items')}`,
  hook: count => `ran ${plural(count, 'hook', 'hooks')}`,
}
// Reads and listings count distinct paths: two pages of one file read one file.
const DISTINCT = {
  read: event => typeof event.input?.path === 'string' ? event.input.path : event.id,
  list: event => typeof event.input?.path === 'string' && event.input.path ? event.input.path : '.',
}

const isHookCard = event => event.type === 'tool' && String(event.name || '').startsWith('hook_')

// A reply that shows nothing of its own: no text, thinking or turn footer, and not streaming. Between two members it
// folds with them; before the first or after the last it stays, since it belongs to that step or the next.
const quietReply = event => !event.text && !event.reasoning_summary && event.reasoning_started == null
  && !event.turn_result && event.request_info?.status !== 'running'

// What an event is for the layout. state: its Conversation toolState ({ result, status, hooked }).
function classify(event, state) {
  if (event.type === 'assistant') return quietReply(event) ? QUIET : BREAK
  if (event.type !== 'tool') return BREAK
  const name = String(event.name || '')
  if (name === 'delegate_task') return DELEGATE
  const status = state?.status
  if (event.state !== 'completed' || !status) return BREAK
  // A hook card that decided and changed nothing joins a group without counting toward it.
  if (name.startsWith('hook_')) return status.tone === 'ok' && !state.hooked ? HOOK : BREAK
  const kind = GROUPABLE.get(name)
  if (!kind) return BREAK
  if (!(status.tone === 'ok' || (status.tone === 'neutral' && NEUTRAL_JOB_TOOLS.has(name)))) return BREAK
  // A background command goes on after its card: it never folds, also once it ended.
  if (name === 'run_command' && (event.input?.background || state.result?.job?.background || status.label === 'started in background')) return BREAK
  // Cards that tell you something the head would hide.
  if (event.result_withheld || event.result_shortened || event.edited || event.sibling_change || state.hooked
    || (Array.isArray(event.secret_files) && event.secret_files.length)) return BREAK
  return kind
}

// Classified once per event object, like the statuses (an updated event is a new object).
const kinds = new WeakMap()
function kindOf(event, state) {
  let kind = kinds.get(event)
  if (kind === undefined) kinds.set(event, kind = classify(event, state))
  return kind
}

// "Ran 3 commands, read 2 files": parts in the order first seen, hooks last, at most three, then "and N more"
// (the actions the other parts hold).
function phraseOf(events, kindList, members) {
  const parts = new Map()
  for (const index of members) {
    const kind = kindList[index]
    let part = parts.get(kind)
    if (!part) parts.set(kind, part = { actions: 0, keys: DISTINCT[kind] ? new Set() : null })
    part.actions++
    part.keys?.add(DISTINCT[kind](events[index]))
  }
  const ordered = [...parts].sort(([a], [b]) => (a === HOOK) - (b === HOOK))
  const shown = ordered.slice(0, 3).map(([kind, part]) => PHRASES[kind](part.keys ? part.keys.size : part.actions))
  const more = ordered.slice(3).reduce((sum, [, part]) => sum + part.actions, 0)
  const text = shown.join(', ') + (more ? ` and ${more} more` : '')
  return text[0].toUpperCase() + text.slice(1)
}

const batchOf = event => {
  const batch = event.delegation_batch
  return batch && typeof batch === 'object' && typeof batch.id === 'string' && batch.id && Number.isInteger(batch.size) && batch.size >= 2 ? batch : null
}

// The runs: the delegate_task cards of one batch (delegation_batch), or, in chats from before batches, two or more
// delegate_task cards with only hook cards and notices without a subagent between them. A run that would overlap an
// earlier one is dropped (its cards render as they are).
function findRuns(events, kindList) {
  const runs = []
  const batches = new Map()
  let chain = null
  const endChain = () => {
    if (chain?.length >= 2) runs.push({ key: `run:${events[chain[0]].id}`, members: chain, batch: null })
    chain = null
  }
  events.forEach((event, index) => {
    if (kindList[index] === DELEGATE) {
      const batch = batchOf(event)
      if (!batch) {
        if (chain) chain.push(index)
        else chain = [index]
        return
      }
      endChain()
      let run = batches.get(batch.id)
      if (!run) {
        batches.set(batch.id, run = { key: `run:${batch.id}`, members: [], batch })
        runs.push(run)
      }
      run.members.push(index)
    } else if (chain && !isHookCard(event) && !(event.type === 'notice' && !event.delegation)) endChain()
  })
  endChain()
  runs.sort((a, b) => a.members[0] - b.members[0])
  let end = -1
  return runs.filter(run => {
    if (run.members[0] <= end) return false
    end = run.members.at(-1)
    return true
  })
}

// toolStates: Map<event id, toolState> (Conversation.js). group: false lays out every event as it is (Settings →
// Appearance → Group tool calls off), so the transcript renders as it did before groups.
export function layoutTranscript(events, toolStates, { group = true } = {}) {
  if (!group) return events.map(event => ({ kind: 'event', event }))
  const kindList = events.map(event => kindOf(event, toolStates?.get(event.id)))
  const runAt = new Map(findRuns(events, kindList).map(run => [run.members[0], run]))
  const items = []
  // The group being built: its members, the order of its items (members and the quiet replies between them) and the
  // quiet replies after its last member so far (they join only when another member follows).
  let open = null
  const close = () => {
    if (!open) return
    const counted = open.members.filter(index => kindList[index] !== HOOK)
    if (counted.length >= GROUP_MIN) {
      const key = `group:${events[open.members[0]].id}`
      items.push({ kind: 'group', key, members: open.members.map(index => events[index].id), counted: counted.map(index => events[index].id),
        phrase: phraseOf(events, kindList, open.members), lastId: events[counted.at(-1)].id,
        steps: 1 + open.order.filter(index => kindList[index] === QUIET).length,
        commands: counted.every(index => kindList[index] === 'command') })
      for (const index of open.order) items.push({ kind: 'event', event: events[index], group: key })
    } else for (const index of open.order) items.push({ kind: 'event', event: events[index] })
    for (const index of open.tail) items.push({ kind: 'event', event: events[index] })
    open = null
  }
  for (let index = 0; index < events.length; index++) {
    const run = runAt.get(index)
    if (run) {
      close()
      const members = new Set(run.members)
      const last = run.members.at(-1)
      items.push({ kind: 'run', key: run.key, members: run.members.map(at => events[at].id), batch: run.batch })
      for (let at = index; at <= last; at++) {
        const event = events[at]
        const role = members.has(at) ? 'member' : isHookCard(event) ? 'hook' : null
        items.push(role ? { kind: 'event', event, run: run.key, role, quiet: kindList[at] === HOOK, cls: 'run-member' }
          : { kind: 'event', event, run: run.key })
      }
      index = last
      continue
    }
    const kind = kindList[index]
    if (PHRASES[kind]) {  // a groupable card or a quiet hook card
      open ??= { members: [], order: [], tail: [] }
      open.order.push(...open.tail, index)
      open.tail = []
      open.members.push(index)
    } else if (kind === QUIET && open) open.tail.push(index)
    else {
      close()
      items.push({ kind: 'event', event: events[index] })
    }
  }
  close()
  return items
}
