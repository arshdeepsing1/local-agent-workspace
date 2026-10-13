import { html, useEffect, useRef, useState } from '../lib.js'
import { Check } from '../icons.js'
import { api } from '../api.js'
import { expiryTime } from './Conversation.js'

const MAX_OTHER = 1000

function questionsOf(event) {
  const questions = event.input?.questions
  return Array.isArray(questions) ? questions.filter(item => item && typeof item === 'object') : []
}

const optionsOf = question => Array.isArray(question.options) ? question.options.filter(item => item && typeof item === 'object') : []

// A choice is complete when it names exactly one answer (single-select) or at least one (multi-select).
// A chosen Other needs its text.
function complete(question, choice) {
  const other = choice.other ? choice.text.trim() : ''
  if (choice.other && !other) return false
  const count = choice.selected.length + (choice.other ? 1 : 0)
  return question.multi_select === true ? count >= 1 : count === 1
}

function answerText(answer) {
  const parts = [...(answer?.selected || [])]
  if (answer?.other) parts.push(`Other: ${answer.other}`)
  return parts.join(', ')
}

function Legend({ question }) {
  return html`<legend>${question.header ? html`<span class="question-chip">${question.header}</span>` : null}<span>${question.question}</span></legend>`
}

// An ask_user tool event: the questions with their options while it waits, then what the user answered.
export default function QuestionCard({ event, sessionId, onError }) {
  const questions = questionsOf(event)
  const [choices, setChoices] = useState(() => questions.map(() => ({ selected: [], other: false, text: '' })))
  const [sending, setSending] = useState(false)
  const [focus, setFocus] = useState(null)
  const others = useRef([])
  const pointer = useRef(false)  // The last choice came from a pointer, not from the keyboard.
  const pending = event.state === 'pending'
  useEffect(() => {
    if (focus === null) return
    others.current[focus]?.focus()
    setFocus(null)
  }, [focus])
  const refused = event.state === 'error' || event.state === 'rejected'
  const heading = refused ? (questions.length === 1 ? 'Question not asked' : 'Questions not asked')
    : questions.length === 1 ? 'Local has a question' : `Local has ${questions.length} questions`
  const route = `/sessions/${encodeURIComponent(sessionId)}/questions/${encodeURIComponent(event.id)}`
  const update = (index, change) => setChoices(current => current.map((choice, at) => at === index ? { ...choice, ...change(choice) } : choice))
  const send = async body => {
    setSending(true)
    try { await api(route, 'POST', body) }
    catch (e) { onError(e.message) }
    finally { setSending(false) }
  }
  const ready = questions.length > 0 && choices.length === questions.length && questions.every((question, index) => complete(question, choices[index]))
  const submit = () => void send({ answers: choices.map(choice => ({ selected: choice.selected,
    ...(choice.other && choice.text.trim() ? { other: choice.text.trim() } : {}) })) })

  if (!pending) {
    const answers = Array.isArray(event.answers) ? event.answers : []
    const outcome = event.state === 'cancelled' ? 'Question cancelled.'
      : event.question_outcome === 'skipped' && event.skipped_reason === 'dont_ask' ? 'Skipped automatically: nobody answers questions in Don\'t ask mode.'
      : event.question_outcome === 'skipped' ? 'You skipped these questions.'
      : event.question_outcome === 'expired' ? 'Not answered in time.'
      : refused ? event.output || 'The question could not be asked.' : ''
    return html`<section class="question-card" aria-label=${heading}>
      <h3>${heading}</h3>
      ${questions.map((question, index) => html`<div class="question-summary" key=${index}>
        <p>${question.header ? html`<span class="question-chip">${question.header}</span>` : null}${question.question}</p>
        ${event.question_outcome === 'answered' && answers[index] ? html`<p class="question-answer">You answered: ${answerText(answers[index])}</p>` : null}
      </div>`)}
      ${outcome ? html`<p class="question-outcome">${outcome}</p>` : null}
    </section>`
  }

  return html`<section class="question-card needs-answer" aria-label=${heading}>
    <h3>${heading}</h3>
    ${event.approval_expires ? html`<p class="muted">If you do not answer by ${expiryTime(event.approval_expires)}, the agent continues without your answer.</p>` : null}
    ${questions.map((question, index) => {
      const choice = choices[index] || { selected: [], other: false, text: '' }
      const multi = question.multi_select === true
      const type = multi ? 'checkbox' : 'radio'
      const name = `question-${event.id}-${index}`
      const choose = label => update(index, current => multi
        ? { selected: current.selected.includes(label) ? current.selected.filter(item => item !== label) : [...current.selected, label] }
        : { selected: [label], other: false })
      // Only a pointer choice moves focus to the text box: arrow keys keep moving within the group,
      // and Tab reaches the enabled text box next.
      const chooseOther = () => {
        update(index, current => multi ? { other: !current.other } : { selected: [], other: true })
        if (pointer.current) setFocus(index)
        pointer.current = false
      }
      return html`<fieldset key=${index} disabled=${sending} onKeyDown=${() => { pointer.current = false }}><${Legend} question=${question} />
        ${multi ? html`<p class="muted question-hint">Choose one or more.</p>` : null}
        ${optionsOf(question).map((option, at) => {
          const description = option.description ? `${name}-${at}-description` : undefined
          return html`<label class="question-option" key=${at}>
            <input type=${type} name=${name} aria-label=${option.label} aria-describedby=${description}
              checked=${choice.selected.includes(option.label)} onChange=${() => choose(option.label)} />
            <span><span>${option.label}</span>${option.description ? html`<small class="muted" id=${description}>${option.description}</small>` : null}</span>
          </label>`
        })}
        <div class="question-option question-other">
          <label onPointerDown=${() => { pointer.current = true }}><input type=${type} name=${name} aria-label="Other" checked=${choice.other} onChange=${chooseOther} /><span>Other</span></label>
          <input type="text" ref=${element => { others.current[index] = element }} aria-label=${`Other answer for: ${question.question}`}
            maxLength=${MAX_OTHER} spellcheck=${false} disabled=${!choice.other || sending} value=${choice.text}
            onInput=${e => { const text = e.currentTarget.value; update(index, () => ({ text })) }} />
        </div>
      </fieldset>`
    })}
    <div class="approval-buttons">
      <button class="outline" disabled=${sending} onClick=${() => void send({ skip: true })}>Skip</button>
      <button class="primary" disabled=${sending || !ready} onClick=${submit}><${Check} size=${15} />Send answers</button>
    </div>
  </section>`
}
