import { html, useEffect, useRef, useState } from '../lib.js'
import { RefreshCw } from '../icons.js'
import { api } from '../api.js'

export default function TerminalPanel({ sessionId, workspace }) {
  return html`<${TerminalJobs} key=${JSON.stringify([sessionId, workspace])} sessionId=${sessionId} />`
}

const jobState = job => `${job.state.replace('_', ' ')}${job.exit_code === null ? '' : ` · exit ${job.exit_code}`}${job.background ? ' · background' : ''}${job.truncated ? ' · output truncated' : ''}`

function TerminalJobs({ sessionId }) {
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  const [command, setCommand] = useState('')
  const [timeout, setTimeoutSeconds] = useState(60)
  const [outputLimit, setOutputLimit] = useState(80000)
  const [background, setBackground] = useState(false)
  const [jobs, setJobs] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [refresh, setRefresh] = useState(0)
  const [launching, setLaunching] = useState(false)
  const [stopping, setStopping] = useState([])
  const [error, setError] = useState('')
  const alive = useRef(false)
  const mutations = useRef(0)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])

  useEffect(() => {
    let disposed = false
    let timer
    const load = async () => {
      const version = mutations.current
      try {
        const list = await api(`/jobs${query}`)
        if (disposed || version !== mutations.current) return
        setJobs(current => list.map(job => {
          const previous = current.find(item => item.id === job.id)
          return previous && previous.updated > job.updated ? previous : job
        }))
        setSelectedId(current => list.some(job => job.id === current) ? current : list[0]?.id || null)
        timer = setTimeout(() => void load(), list.some(job => job.state === 'running') ? 1000 : 3000)
      } catch (e) { if (!disposed) setError(e.message) }
    }
    void load()
    return () => { disposed = true; clearTimeout(timer) }
  }, [query, refresh])

  useEffect(() => {
    let disposed = false
    let timer
    setDetail(current => current?.id === selectedId ? current : null)
    const load = async () => {
      const version = mutations.current
      try {
        const job = await api(`/jobs/${selectedId}${query}`)
        if (disposed || version !== mutations.current) return
        setDetail(current => current?.id === job.id && current.updated > job.updated ? current : job)
        setJobs(current => current.map(item => item.id === job.id && item.updated <= job.updated ? job : item))
        if (job.state === 'running') timer = setTimeout(() => void load(), 500)
      } catch (e) { if (!disposed) setError(e.message) }
    }
    if (selectedId) void load()
    return () => { disposed = true; clearTimeout(timer) }
  }, [query, selectedId, refresh])

  const run = async () => {
    if (!command.trim() || launching || jobs.some(job => job.state === 'running' && !job.background)) return
    setLaunching(true); setError('')
    try {
      const job = await api(`/jobs${query}`, 'POST', {
        command, timeout_seconds: timeout, max_output_bytes: outputLimit, background,
      })
      if (!alive.current) return
      mutations.current++
      setJobs(current => [job, ...current.filter(item => item.id !== job.id)])
      setSelectedId(job.id); setDetail(job); setRefresh(current => current + 1)
    } catch (e) { if (alive.current) setError(e.message) }
    finally { if (alive.current) setLaunching(false) }
  }
  const stop = async id => {
    setStopping(current => [...current, id]); setError('')
    try {
      const job = await api(`/jobs/${id}/stop${query}`, 'POST')
      if (!alive.current) return
      mutations.current++
      setJobs(current => current.map(item => item.id === id ? job : item))
      setDetail(current => current?.id === id ? job : current)
      setRefresh(current => current + 1)
    } catch (e) { if (alive.current) setError(e.message) }
    finally { if (alive.current) setStopping(current => current.filter(item => item !== id)) }
  }
  const selected = detail?.id === selectedId ? detail : jobs.find(job => job.id === selectedId)
  const foregroundRunning = jobs.some(job => job.state === 'running' && !job.background)
  return html`<div class="terminal-view">
    <p>Run a command in your project folder.</p>
    <form class="terminal-run" onSubmit=${e => { e.preventDefault(); void run() }}>
      <div class="terminal-command"><span>$</span><input aria-label="Shell command" placeholder="git status" value=${command} onInput=${e => setCommand(e.currentTarget.value)} />
        <button class="primary" disabled=${launching || foregroundRunning || !command.trim()}>${launching ? 'Starting…' : 'Run'}</button></div>
      <div class="terminal-options">
        <label>Timeout (seconds)<input type="number" min=${1} max=${3600} step=${1} required value=${timeout || ''} onInput=${e => setTimeoutSeconds(Number(e.currentTarget.value))} /></label>
        <label>Output limit (bytes)<input type="number" min=${1024} max=${1000000} step=${1} required value=${outputLimit || ''} onInput=${e => setOutputLimit(Number(e.currentTarget.value))} /></label>
      </div>
      <label class="terminal-background"><input type="checkbox" checked=${background} onChange=${e => setBackground(e.currentTarget.checked)} />Run in background</label>
    </form>
    <p class="field-help">Commands can access your machine. Background jobs continue after Stop response; use Stop job to end one. Server shutdown also stops jobs.</p>
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
    <div class="terminal-history-heading"><span>Job history</span><button class="icon-button" aria-label="Refresh jobs" onClick=${() => { setError(''); setRefresh(current => current + 1) }}><${RefreshCw} size=${14} /></button></div>
    <div class="terminal-jobs" role="list" aria-label="Job history">
      ${jobs.map(job => html`<div class="terminal-job" role="listitem" key=${job.id}>
        <button class="terminal-job-select" aria-label=${`Show job ${job.command} · ${jobState(job)}`} aria-pressed=${job.id === selectedId} onClick=${() => setSelectedId(job.id)}><span>${job.command}</span><small>${jobState(job)}</small></button>
        ${job.state === 'running' ? html`<button class="outline terminal-stop" aria-label=${`Stop job ${job.command}`} disabled=${stopping.includes(job.id)} onClick=${() => void stop(job.id)}>${stopping.includes(job.id) ? 'Stopping…' : 'Stop job'}</button>` : null}
      </div>`)}
      ${!jobs.length ? html`<p>No jobs in this workspace scope yet.</p>` : null}
    </div>
    ${selected ? html`<p class="terminal-job-status" role="status">${jobState(selected)}</p>` : null}
    <pre class="terminal-output" aria-label="Job output">${selected ? `$ ${selected.command}\n\n${selected.output ?? 'Loading job output…'}` : 'Command output will appear here.'}</pre>
  </div>`
}
