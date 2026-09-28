// GitHub-flavored Markdown for model replies. Raw HTML in a reply is shown as
// text, and the generated HTML is sanitized before it reaches the page.
import { Marked } from '../vendor/marked.esm.js'
import DOMPurify from '../vendor/purify.es.js'

const escapeHtml = text => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
const marked = new Marked({ gfm: true, renderer: { html: ({ text }) => escapeHtml(text) } })

export function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(text || ''))
}
