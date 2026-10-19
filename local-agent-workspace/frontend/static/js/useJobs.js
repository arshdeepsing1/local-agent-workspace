import { useEffect, useState } from './lib.js'
import { api } from './api.js'

// Whether the page is shown (not a hidden browser tab or a minimized window).
function usePageVisible() {
  const [visible, setVisible] = useState(() => document.visibilityState !== 'hidden')
  useEffect(() => {
    const update = () => setVisible(document.visibilityState !== 'hidden')
    document.addEventListener('visibilitychange', update)
    return () => document.removeEventListener('visibilitychange', update)
  }, [])
  return visible
}

// The longest wait between two tries after failed requests (they wait 3 s, 6 s, 12 s, 24 s, then 30 s).
const RETRY_MAX = 30000

// A chat's jobs (GET /jobs?session_id=, the rows Terminal lists) for Workspace → Progress. It polls only while
// enabled and the page is shown: every second while a job runs, else every 3 s. signal: a value that changes when
// the chat's events name another job (a command card or a background job's end notice); it fetches at once. After
// a failed request it keeps trying, waiting longer each time; retry() tries at once. Terminal polls on its own.
// Returns { jobs, error (the last request's, until one succeeds), loaded (a request succeeded), retry }.
export function useJobs(sessionId, { enabled = true, signal = 0 } = {}) {
  const visible = usePageVisible()
  const active = !!sessionId && enabled && visible
  // The list is kept with the chat it belongs to, so another chat never shows it.
  const [state, setState] = useState({ id: null, jobs: [], error: '', loaded: false })
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    if (!active) return undefined
    let disposed = false
    let timer
    let failures = 0
    const load = async () => {
      try {
        const answer = await api(`/jobs?session_id=${encodeURIComponent(sessionId)}`)
        if (disposed) return
        const jobs = Array.isArray(answer) ? answer : []
        failures = 0
        setState({ id: sessionId, jobs, error: '', loaded: true })
        timer = setTimeout(() => void load(), jobs.some(job => job.state === 'running') ? 1000 : 3000)
      } catch (e) {
        if (disposed) return
        failures++
        setState(current => ({ ...(current.id === sessionId ? current : { id: sessionId, jobs: [], loaded: false }), error: e.message }))
        timer = setTimeout(() => void load(), Math.min(RETRY_MAX, 3000 * 2 ** (failures - 1)))
      }
    }
    void load()
    return () => { disposed = true; clearTimeout(timer) }
  }, [sessionId, active, signal, attempt])
  const own = state.id === sessionId
  return { jobs: own ? state.jobs : [], error: own ? state.error : '', loaded: own && state.loaded, retry: () => setAttempt(value => value + 1) }
}
