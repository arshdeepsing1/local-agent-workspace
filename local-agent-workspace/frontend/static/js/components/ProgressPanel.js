import { html, useEffect, useRef, useState } from '../lib.js'
import { Bot, Terminal } from '../icons.js'
import { api } from '../api.js'
import { useJobs } from '../useJobs.js'
import { agentLabel, elapsedText } from '../subagents.js'
import { turnSummary } from '../turnResult.js'
import { CONTEXT_EMPTY, LoadedInstructions, meterReading } from './ContextMeter.js'
import { DELEGATION_STATUS, SubagentControls } from './Conversation.js'
import { TaskList, taskProgress } from './TaskChecklist.js'
import { jobState } from './TerminalPanel.js'

// Ended background subagents and commands listed, newest first; Agent tools → Subagents and Terminal list them all.
const ENDED_SHOWN = 10
// The order of Background tasks: what waits for you, what runs, what waits for a free slot, then what ended.
const RANK = { waiting: 0, running: 1, queued: 2, ended: 3 }

const plural = (count, word) => `${Number.isInteger(count) ? count : 0} ${word}${count === 1 ? '' : 's'}`

// This chat's subagents. A loaded delegate_task card's delegation is pushed live, so it is used when there is one;
// otherwise the child chat's summary (app.sessions) has it, also when its card is on a page that is not loaded.
// A queued card has no chat yet, so it comes from the loaded cards only.
function subagentRows(session, sessions) {
  const cards = session.events.filter(event => event.type === 'tool' && event.name === 'delegate_task' && event.delegation)
  const byChild = new Map(cards.filter(card => card.child_session_id).map(card => [card.child_session_id, card]))
  const rows = []
  const seen = new Set()
  for (const child of sessions) {
    if (child.parent_session_id !== session.id) continue
    const card = byChild.get(child.id) || cards.find(item => item.id === child.parent_event_id)
    const progress = card?.delegation || child.delegation
    if (!progress) continue
    seen.add(child.id)
    if (card) seen.add(card.id)
    rows.push({ key: `subagent-${child.id}`, child: child.id, eventId: card?.id || child.parent_event_id, loaded: !!card,
      progress, task: card?.input?.task || child.title || '' })
  }
  for (const card of cards) {
    if (seen.has(card.id) || seen.has(card.child_session_id)) continue
    rows.push({ key: `card-${card.id}`, child: card.child_session_id || null, eventId: card.id, loaded: true,
      progress: card.delegation, task: card.input?.task || '' })
  }
  return rows
}

const phaseOf = progress => progress.terminal_reason ? 'ended'
  : progress.status === 'awaiting_approval' ? 'waiting' : progress.status === 'queued' ? 'queued' : 'running'

// The meter's reading (ContextMeter's meterReading, so the two agree), the auto-compact mark, compactions and
// warnings, the last turn's summary and the loaded instruction files.
function ContextSection({ session, onOpenContext }) {
  const info = session.context_info
  const last = session.last_turn_result || session.events.findLast(event => event.type === 'assistant' && event.turn_result)?.turn_result
  const summary = turnSummary(last)
  if (!info) return html`<p class="progress-empty">${CONTEXT_EMPTY}</p>`
  const reading = meterReading(info, session.model)
  const mark = typeof reading.mark === 'number' ? reading.mark : null
  const counts = [info.compactions > 0 ? plural(info.compactions, 'compaction') : '',
    info.warnings?.length ? plural(info.warnings.length, 'warning') : ''].filter(Boolean).join(' · ')
  return html`
    <p class="progress-context-line">${reading.label}</p>
    ${reading.legacyEstimate ? null : html`<span class=${`context-bar progress-context-bar${mark !== null && reading.value >= mark ? ' over' : ''}`}>
      <progress aria-label="Context used" aria-valuetext=${reading.ariaValueText} value=${Math.min(reading.value, 100)} max=${100} />
      ${mark !== null ? html`<span class="context-bar-mark" aria-hidden="true" style=${`left:${mark}%`} />` : null}</span>`}
    ${mark !== null ? html`<p class="progress-note">Compacts automatically at ${mark}% of the input budget.</p>` : null}
    ${counts ? html`<p class="progress-note">${counts}</p>` : null}
    ${summary ? html`<p class="progress-note" title=${summary.details}>Last turn: ${summary.text}</p>` : null}
    <h4 class="progress-subheading">Loaded instructions</h4>
    <div class="progress-instructions"><${LoadedInstructions} info=${info} note=${false} /></div>
    ${onOpenContext ? html`<button class="outline" onClick=${onOpenContext}>Context details</button>` : null}`
}

// One subagent: its type, task, status, actions, steps, current tool and time, with Open subagent, Show card (its
// card is loaded) and the subagent's own Allow, Decline, Review, Answer and Stop (SubagentControls posts to the child
// chat, so they work while its card is on a page that is not loaded).
function SubagentRow({ row, now, onSelectSession, onShowCard, onError }) {
  const progress = row.progress
  const live = !progress.terminal_reason
  const status = DELEGATION_STATUS[progress.status] || String(progress.status || '').replace(/_/g, ' ')
  const kind = progress.agent_type
  const started = typeof progress.started === 'number' ? progress.started : null
  // As on its card: no duration without an end time once it ended (a restart interrupted it).
  const elapsed = started == null ? null : typeof progress.ended === 'number' ? progress.ended - started
    : live ? Math.max(now, started) - started : null
  const tool = progress.last_tool ? progress.last_tool.replace(/_/g, ' ') : ''
  const subject = typeof progress.last_tool_subject === 'string' && progress.last_tool_subject ? ` · ${progress.last_tool_subject}` : ''
  const working = live && progress.status === 'running' && progress.last_tool_state === 'running'
  const steps = Number.isInteger(progress.steps) ? `Step ${progress.steps}${Number.isInteger(progress.max_steps) ? ` of ${progress.max_steps}` : ''}` : ''
  return html`<li class=${`progress-item ${phaseOf(progress)}`}>
    <div class="progress-item-head"><${Bot} size=${15} /><span class="sr-only">Subagent: </span>
      <span class="progress-item-title" title=${row.task}>${row.task || 'Subagent'}</span></div>
    <div class="progress-item-meta">
      ${kind ? html`<span class="progress-chip">${agentLabel(kind)}</span>` : null}
      ${live && !progress.background ? html`<span class="progress-chip" title="It runs as part of the agent's current turn.">In this turn</span>` : null}
      <span class="progress-status">${status}</span>
      <span>${plural(progress.completed_tools, 'action')}</span>
      ${steps ? html`<span>${steps}</span>` : null}
      ${tool ? html`<span class="progress-tool" title=${`${tool}${subject}`}>${working ? 'Now' : 'Last tool'}: ${tool}${subject}</span>` : null}
      ${elapsed != null ? html`<span class="progress-elapsed">${elapsedText(elapsed)}</span>` : null}
      ${progress.terminal_reason ? html`<span>Reason: ${progress.terminal_reason.replace(/_/g, ' ')}</span>` : null}
    </div>
    ${(row.child && onSelectSession) || row.loaded ? html`<div class="progress-actions">
      ${row.child && onSelectSession ? html`<button class="outline" onClick=${() => onSelectSession(row.child)}>Open subagent</button>` : null}
      ${row.loaded && onShowCard ? html`<button class="outline" onClick=${() => onShowCard(row.eventId)}>Show card</button>` : null}
    </div>` : null}
    ${row.child ? html`<${SubagentControls} event=${{ child_session_id: row.child }} progress=${progress} onSelectSession=${onSelectSession} onError=${onError} />` : null}
  </li>`
}

// One command: its text, state, how long it ran (or runs), and who started it, with Stop job and View output.
function JobRow({ job, agent, now, stopping, onStop, onViewOutput }) {
  const running = job.state === 'running'
  const seconds = Math.max(0, (running ? Math.max(now, job.updated) : job.updated) - job.created)
  return html`<li class=${`progress-item ${running ? 'running' : 'ended'}`}>
    <div class="progress-item-head"><${Terminal} size=${15} /><span class="sr-only">Command: </span>
      <code class="progress-item-title" title=${job.command}>${job.command}</code></div>
    <div class="progress-item-meta">
      <span class="progress-chip" title=${agent ? 'The agent started it.' : 'You started it (Terminal or a ! command).'}>${agent ? 'Agent' : 'You'}</span>
      <span class="progress-status">${jobState(job)}</span>
      <span class="progress-elapsed">${elapsedText(seconds)}</span>
    </div>
    <div class="progress-actions">
      ${running ? html`<button class="outline danger" aria-label=${`Stop job ${job.command}`} disabled=${stopping} onClick=${onStop}>${stopping ? 'Stopping…' : 'Stop job'}</button>` : null}
      ${onViewOutput ? html`<button class="outline" aria-label=${`View output of ${job.command}`} onClick=${onViewOutput}>View output</button>` : null}
    </div>
  </li>`
}

// Workspace → Progress: the chat's context, its task list (as the checklist above the message box shows it) and
// its background tasks. session: the open chat (null: none); sessions: app.sessions (the subagent chats' summaries,
// which outlast paging and reconnects); tasks: the chat's tasks. onSelectSession(id) opens a subagent;
// onShowCard(eventId) shows its card in the transcript; onOpenContext() opens the meter's details;
// onViewOutput(jobId) shows a job in Terminal; onError(message) reports a failed action.
export default function ProgressPanel({ session, sessions = [], tasks = [], onSelectSession, onShowCard, onOpenContext, onViewOutput, onError }) {
  const ids = useRef(`progress-${Math.random().toString(36).slice(2, 10)}`).current
  const events = session?.events || []
  // Fetch the jobs again at once when a command card or a background job's end notice appears.
  const signal = events.filter(event => event.job_id).length
  const { jobs, error, loaded, retry } = useJobs(session?.id || null, { signal })
  const [stopping, setStopping] = useState([])
  const rows = session ? subagentRows(session, sessions) : []
  const live = rows.some(row => !row.progress.terminal_reason && typeof row.progress.started === 'number')
    || jobs.some(job => job.state === 'running')
  // One clock for every row's time, ticking only while something runs.
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    if (!live) return undefined
    setNow(Date.now() / 1000)
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => clearInterval(timer)
  }, [live])
  if (!session) return html`<div class="progress-panel"><p class="panel-empty">Open a chat to see its progress.</p></div>`

  const stop = async id => {
    setStopping(current => [...current, id])
    try { await api(`/jobs/${encodeURIComponent(id)}/stop?session_id=${encodeURIComponent(session.id)}`, 'POST') }
    catch (e) { onError?.(e.message) }
    finally { setStopping(current => current.filter(item => item !== id)); retry() }
  }
  // Who started a job: its origin, or for older jobs without one, a loaded command card of the agent that has it.
  const agentJobs = new Set(events.filter(event => event.type === 'tool' && event.name === 'run_command' && event.job_id).map(event => event.job_id))
  const byAgent = job => job.origin ? job.origin === 'agent' : agentJobs.has(job.id)

  const endedSubagents = rows.filter(row => row.progress.terminal_reason && row.progress.background)
    .sort((a, b) => (b.progress.ended || 0) - (a.progress.ended || 0))
  const endedJobs = jobs.filter(job => job.state !== 'running' && job.background && byAgent(job))
    .sort((a, b) => b.updated - a.updated)
  const items = [
    ...rows.filter(row => !row.progress.terminal_reason).map(row => ({ key: row.key, rank: RANK[phaseOf(row.progress)], time: row.progress.started || 0, row })),
    ...jobs.filter(job => job.state === 'running').map(job => ({ key: `job-${job.id}`, rank: RANK.running, time: job.created, job })),
    ...endedSubagents.slice(0, ENDED_SHOWN).map(row => ({ key: row.key, rank: RANK.ended, time: row.progress.ended || 0, row })),
    ...endedJobs.slice(0, ENDED_SHOWN).map(job => ({ key: `job-${job.id}`, rank: RANK.ended, time: job.updated, job })),
  ].sort((a, b) => a.rank - b.rank || (a.rank === RANK.ended ? b.time - a.time : a.time - b.time))
  const moreSubagents = endedSubagents.length - ENDED_SHOWN
  const moreJobs = endedJobs.length - ENDED_SHOWN
  const progress = taskProgress(tasks)

  return html`<div class="progress-panel">
    <section class="progress-section" aria-labelledby=${`${ids}-context`}>
      <h3 id=${`${ids}-context`}>Context</h3>
      <${ContextSection} session=${session} onOpenContext=${onOpenContext} />
    </section>
    <section class="progress-section" aria-labelledby=${`${ids}-tasks`}>
      <h3 id=${`${ids}-tasks`}>Tasks</h3>
      ${tasks.length ? html`<p class="progress-note">${progress.done}/${progress.counted} done${progress.current ? ` · now: ${progress.current.title}` : ''}</p>
        <div class="progress-tasks"><${TaskList} tasks=${tasks} /></div>`
        : html`<p class="progress-empty">No tasks in this chat. When the agent plans its work as tasks, its checklist shows here and above the message box.</p>`}
    </section>
    <section class="progress-section" aria-labelledby=${`${ids}-background`}>
      <h3 id=${`${ids}-background`}>Background tasks</h3>
      ${error ? html`<div class="progress-error" role="alert"><span>Could not load this chat's commands: ${error}</span>
        <button class="outline" onClick=${retry}>Retry</button></div>` : null}
      ${items.length ? html`<ul class="progress-list" aria-label="Background tasks">${items.map(item => item.row
        ? html`<${SubagentRow} key=${item.key} row=${item.row} now=${now} onSelectSession=${onSelectSession} onShowCard=${onShowCard} onError=${onError} />`
        : html`<${JobRow} key=${item.key} job=${item.job} agent=${byAgent(item.job)} now=${now} stopping=${stopping.includes(item.job.id)}
            onStop=${() => void stop(item.job.id)} onViewOutput=${onViewOutput ? () => onViewOutput(item.job.id) : null} />`)}</ul>`
        : loaded ? html`<p class="progress-empty">Nothing runs in the background in this chat. Background subagents and background commands appear here.</p>` : null}
      ${moreSubagents > 0 ? html`<p class="progress-note">${plural(moreSubagents, 'more ended subagent')} in Agent tools → Subagents.</p>` : null}
      ${moreJobs > 0 ? html`<p class="progress-note">${plural(moreJobs, 'more ended command')} in Terminal.</p>` : null}
      <p class="progress-note">Scheduled runs are not listed here: each one is a chat of its own (Agent tools → Schedules).</p>
    </section>
  </div>`
}
