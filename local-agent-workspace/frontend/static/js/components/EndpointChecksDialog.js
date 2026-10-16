import { html, useEffect, useRef, useState } from '../lib.js'
import { X } from '../icons.js'
import { api } from '../api.js'

// Settings → Model and context → Test model and Check endpoint…: what the checks send and their estimated cost
// (backend endpoint_probes.plan). Nothing is sent until Run is pressed, and every run is approved again: the
// request carries the plan_id and the estimate shown here. Props: model, mode ('test' | 'check'), onClose.
const STATES = { pending: 'Waiting', running: 'Sending…', done: 'Done', skipped: 'Not sent', not_sent: 'Not sent',
  stopped: 'Stopped' }
const RUN_STATUS = { running: 'Running', completed: 'Finished', stopped: 'Stopped', failed: 'Stopped early' }

export function dbuText(value) {
  if (typeof value !== 'number') return ''
  return value >= 1 ? value.toFixed(1) : value >= 0.1 ? value.toFixed(2) : value.toFixed(3)
}

const day = value => typeof value === 'string' ? value.slice(0, 10) : ''

// The capability table: capability | result | source (probed <date>, documented or unknown).
export function CapabilityTable({ rows, label = 'Endpoint capabilities' }) {
  if (!Array.isArray(rows) || !rows.length) return null
  return html`<table class="capability-table" aria-label=${label}>
    <thead><tr><th scope="col">Capability</th><th scope="col">Result</th><th scope="col">Source</th></tr></thead>
    <tbody>${rows.map(row => html`<tr key=${row.id} data-status=${row.status}>
      <th scope="row">${row.capability}</th><td>${row.result}</td><td>${row.source}</td></tr>`)}</tbody>
  </table>`
}

// The Test model result line: the smoke check's summary and when it ran.
export function testResultText(saved) {
  const basic = saved?.results?.basic
  if (!basic) return ''
  const label = basic.status === 'accepted' ? 'Works' : basic.status === 'rejected' ? 'Rejected' : 'Failed'
  return `${label}: ${basic.summary} (${day(basic.checked)})`
}

export default function EndpointChecksDialog({ model, mode = 'check', onClose }) {
  const dialog = useRef(null)
  const [plan, setPlan] = useState(null)
  const [planFor, setPlanFor] = useState(null)  // The choice of checks the shown plan was computed for.
  const [available, setAvailable] = useState(null)  // The checks that apply to this endpoint (the first plan's).
  const [chosen, setChosen] = useState(null)
  const [checks, setChecks] = useState(null)
  const [serving, setServing] = useState(null)
  const [error, setError] = useState('')
  const [starting, setStarting] = useState(false)
  const test = mode === 'test'
  const run = checks?.run
  const running = run?.status === 'running'
  const query = `/endpoints/${encodeURIComponent(model)}`
  const choice = `${model}|${test ? 'basic' : (chosen || []).join()}`
  // While the plan for a changed choice is computed, the shown one is stale: Run waits for the new estimate.
  const planReady = !!plan && planFor === choice
  useEffect(() => {
    const node = dialog.current
    node?.showModal()
    return () => { if (node?.open) node.close() }
  }, [])
  // The plan for the chosen checks: computed by the server, no request to the endpoint.
  useEffect(() => {
    let current = true
    const probes = test ? ['basic'] : chosen
    api('/endpoints/plan', 'POST', { model, ...(probes ? { probes } : {}) }).then(value => {
      if (!current) return
      setPlan(value)
      setPlanFor(choice)
      setAvailable(previous => previous || value.probes)
    }, e => { if (current) { setPlan(null); setError(e.message) } })
    return () => { current = false }
  }, [model, test, choice])
  // Saved results and the live progress of a run (read again every second while one runs).
  const load = () => api(`${query}/checks`).then(value => setChecks(value), e => setError(e.message))
  useEffect(() => { void load() }, [model])
  useEffect(() => {
    if (!running) return
    const timer = setInterval(() => { void load() }, 1000)
    return () => clearInterval(timer)
  }, [running, model])
  // The endpoint's serving state, read without a model request.
  useEffect(() => { api(`${query}/state`).then(setServing, () => {}) }, [model])
  const toggle = id => {
    const all = (available || []).map(probe => probe.id)
    const current = chosen || all
    const next = current.includes(id) ? current.filter(item => item !== id) : all.filter(item => item === id || current.includes(item))
    setChosen(next.length ? next : [...current])  // The last check stays chosen (and its box checked).
  }
  const start = async () => {
    if (!planReady || starting) return
    setStarting(true); setError('')
    try {
      const body = { model, plan_id: plan.plan_id, approved_dbu: plan.priced ? plan.total_dbu : 0,
        ...(!test && chosen ? { probes: chosen } : {}) }
      const started = await api(test ? '/endpoints/test' : '/endpoints/run', 'POST', body)
      setChecks(current => ({ ...(current || {}), run: started }))
    } catch (e) { setError(e.message) }
    finally { setStarting(false) }
  }
  const stop = async () => {
    try {
      const result = await api('/endpoints/run/stop', 'POST')
      setChecks(current => ({ ...(current || {}), run: result.run }))
      void load()
    } catch (e) { setError(e.message) }
  }
  const cost = !planReady ? 'updating the estimate…' : plan.priced ? `about ${dbuText(plan.total_dbu)} DBU` : 'no price data'
  const title = test ? 'Test model' : 'Check endpoint capabilities'
  const finishedRun = run && !running ? run : null
  const close = () => { if (!starting) onClose() }
  return html`<dialog ref=${dialog} class="settings-dialog endpoint-checks-dialog" aria-label=${title}
    onCancel=${event => { event.preventDefault(); close() }}>
    <div class="dialog-content">
      <header><div><h2>${title}</h2><p>${model}</p></div>
        <button type="button" class="icon-button" onClick=${close} aria-label=${`Close ${title.toLowerCase()}`}><${X} /></button></header>
      <p class="field-help">${test
        ? 'Sends one short streamed request to this endpoint to check that it answers, and how fast.'
        : 'Each check sends a few small requests to this endpoint and records what it accepts. The results stay in this app\'s state folder; they change agent requests only where noted in the README (cache markers, images, earlier thinking).'}
        ${' '}These requests are billed like any other; nothing is sent until you press Run, and each run asks again.</p>
      ${serving?.ready ? html`<p class="field-help endpoint-serving">Serving state: ${serving.ready}${serving.task ? ` (${serving.task})` : ''}</p>` : null}
      ${!plan ? html`<p class="field-help">${error ? '' : 'Computing the estimate…'}</p>` : html`<ul class="endpoint-plan" aria-label="Requests">
        ${(test ? plan.probes : available || plan.probes).map(probe => {
          const planned = plan.probes.find(item => item.id === probe.id)
          return html`<li key=${probe.id} class="endpoint-probe">
            ${test ? html`<p class="endpoint-probe-title">${probe.title}</p>` : html`<label class="settings-checkbox">
              <input type="checkbox" checked=${!chosen || chosen.includes(probe.id)} disabled=${running || starting}
                onChange=${() => toggle(probe.id)} />${probe.title}</label>`}
            <p class="field-help">${probe.question}</p>
            ${planned ? html`<ul class="endpoint-requests">${planned.requests.map(request => html`<li key=${request.id}>
              <span>${request.label}</span>
              <span class="endpoint-cost">max ${request.max_tokens.toLocaleString('en-US')} output tokens${plan.priced
                ? ` · at most ${dbuText(request.estimated_dbu)} DBU` : ''}</span></li>`)}</ul>` : null}
          </li>`
        })}</ul>
        <p class="endpoint-total" role="status">${plan.priced
          ? `At most ${dbuText(plan.total_dbu)} DBU at list prices (an estimate; a rejected request is not billed).`
          : `No price data for this endpoint: up to ${plan.max_output_tokens.toLocaleString('en-US')} output tokens in all.`}</p>`}
      ${run ? html`<div class="endpoint-progress" aria-label="Progress">
        <p class="endpoint-progress-title">${RUN_STATUS[run.status] || run.status}${run.reported_dbu ? ` · ${dbuText(run.reported_dbu)} DBU reported so far` : ''}${run.waiting ? ` · waiting ${run.waiting} s for a rate limit` : ''}</p>
        <ul>${run.requests.map(entry => html`<li key=${entry.id} data-state=${entry.state}>
          <span>${entry.label}</span><span class="endpoint-state">${STATES[entry.state] || entry.state}${entry.summary ? ` · ${entry.summary}` : ''}</span></li>`)}</ul>
        ${run.error ? html`<p class="form-error" role="alert">${run.error}</p>` : null}
      </div>` : null}
      ${test && checks?.saved?.results?.basic ? html`<p class="endpoint-test-result" role="status">${testResultText(checks.saved)}</p>` : null}
      ${!test && checks?.capabilities ? html`<h3 class="endpoint-results-title">Results${checks.saved ? ` (last check ${day(checks.saved.checked)})` : ''}</h3>
        <${CapabilityTable} rows=${checks.capabilities} />` : null}
      ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
      <footer>
        <button type="button" class="outline" disabled=${starting} onClick=${close}>${finishedRun ? 'Close' : 'Cancel'}</button>
        ${running ? html`<button type="button" class="primary" onClick=${() => void stop()}>Stop</button>`
          : html`<button type="button" class="primary" disabled=${!planReady || starting} onClick=${() => void start()}>
            ${starting ? 'Starting…' : `Run (${cost})`}</button>`}
      </footer>
    </div>
  </dialog>`
}
