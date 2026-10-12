import { html, useState } from '../lib.js'
import { ChevronDown, ChevronRight, Circle, CircleCheck, CircleDot, CircleSlash } from '../icons.js'

const STATUS = {
  pending: { icon: Circle, label: 'Pending' },
  in_progress: { icon: CircleDot, label: 'In progress' },
  completed: { icon: CircleCheck, label: 'Done' },
  cancelled: { icon: CircleSlash, label: 'Cancelled' },
}

// The conversation's tasks (create_task / update_task, or Agent tools → Tasks),
// pushed live as { type: 'tasks', tasks }. Above the message box, under the
// changes bar; hidden while there are none. Cancelled tasks are listed but not
// counted. Props: tasks: Task[]
export default function TaskChecklist({ tasks }) {
  const [open, setOpen] = useState(false)
  if (!tasks?.length) return null
  const counted = tasks.filter(task => task.status !== 'cancelled')
  const done = counted.filter(task => task.status === 'completed').length
  const current = tasks.find(task => task.status === 'in_progress')
  return html`<section class="task-checklist" aria-label="Tasks">
    <button class="task-checklist-toggle" aria-expanded=${open} onClick=${() => setOpen(value => !value)}>
      ${open ? html`<${ChevronDown} size=${15} />` : html`<${ChevronRight} size=${15} />`}
      <span>Tasks ${done}/${counted.length} done</span>
      ${current && !open ? html`<span class="task-checklist-current" title=${current.title}>· ${current.title}</span>` : null}
    </button>
    ${open ? html`<ul class="task-checklist-list">${tasks.map(task => {
      const status = STATUS[task.status] || STATUS.pending
      const Icon = status.icon
      return html`<li key=${task.id} class=${`task-checklist-item ${task.status}`}>
        <${Icon} size=${14} className="task-checklist-icon" /><span class="sr-only">${status.label}: </span>
        <span class="task-checklist-title" title=${task.description || task.title}>${task.title}</span>
      </li>`
    })}</ul>` : null}
  </section>`
}
