import { useEffect, useState } from './lib.js'

// Line comments on the agent's changes, written in the change review and sent to the agent as
// one message. They live in this page's memory per conversation: they survive switching files
// and closing the Workspace panel, but not a reload.
// Comment: { id, path, line, kind: 'added'|'context'|'removed', text (the line), comment }
const comments = new Map()  // sessionId -> Comment[]
const subscribers = new Set()
let nextId = 0

export const MAX_COMMENTS = 50
export const MAX_COMMENT_LENGTH = 2000
export const MAX_MESSAGE_LENGTH = 50000
export const TOO_MANY = 'Too many comments to send at once; send some first.'

const save = (sessionId, list) => {
  if (list.length) comments.set(sessionId, list)
  else comments.delete(sessionId)
  subscribers.forEach(notify => notify())
}

// The line as inline code, clipped to 120 characters; double backticks when it contains one.
const code = text => {
  const clipped = text.length > 120 ? text.slice(0, 120) : text
  return clipped.includes('`') ? `\`\` ${clipped} \`\`` : `\`${clipped}\``
}

// The message sent to the agent, in path then line order.
export function reviewMessage(list) {
  const sorted = [...list].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : a.line - b.line))
  return 'Review comments on your changes (file:line, from the review panel):\n\n'
    + sorted.map(item => `- \`${item.path}:${item.line}\` (${item.kind} line: ${code(item.text)}): ${item.comment.replace(/\n/g, '\n  ')}`).join('\n')
    + '\n\nAddress each comment, then summarize what you changed.'
}

// { comments, add({ path, line, kind, text, comment }), update(id, comment), remove(id), clear(ids?) }
export function useReviewComments(sessionId) {
  const [, setVersion] = useState(0)
  useEffect(() => {
    const notify = () => setVersion(value => value + 1)
    subscribers.add(notify)
    return () => subscribers.delete(notify)
  }, [])
  const current = () => comments.get(sessionId) || []
  return {
    comments: current(),
    add: item => { const added = { ...item, id: `comment-${++nextId}` }; save(sessionId, [...current(), added]); return added },
    update: (id, comment) => save(sessionId, current().map(item => item.id === id ? { ...item, comment } : item)),
    remove: id => save(sessionId, current().filter(item => item.id !== id)),
    // Without ids, every comment of this conversation; with ids (the ones just sent), only those.
    clear: ids => save(sessionId, ids ? current().filter(item => !ids.includes(item.id)) : []),
  }
}
