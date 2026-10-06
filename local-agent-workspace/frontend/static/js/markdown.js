// GitHub-flavored Markdown for model replies. Raw HTML in a reply is shown as
// text, and the generated HTML is sanitized before it reaches the page.
import { Marked } from '../vendor/marked.esm.js'
import DOMPurify from '../vendor/purify.es.js'
import hljs from '../vendor/highlight.min.js'

const escapeHtml = text => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
const marked = new Marked({ gfm: true, renderer: { html: ({ text }) => escapeHtml(text) } })

export function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(text || ''))
}

// A block without a language tag is highlighted only up to this size:
// guessing its language runs every grammar over the whole text.
const AUTO_DETECT_LIMIT = 20 * 1024

// Syntax highlighting and a Copy code button for each code block of rendered
// Markdown, once renderMarkdown()'s sanitized HTML is in the page. highlight.js
// reads a block's text and returns escaped markup, so no unsanitized HTML is
// inserted. Blocks done before are skipped, so calling it again is cheap.
export function enhanceCodeBlocks(root) {
  for (const code of root.querySelectorAll('pre > code')) {
    const pre = code.parentElement
    if (pre.parentElement?.classList.contains('code-block')) continue
    const text = code.textContent
    const language = /(?:^|\s)language-(\S+)/.exec(code.className)?.[1]
    if (language ? hljs.getLanguage(language) : text.length <= AUTO_DETECT_LIMIT) {
      try {
        // Highlight with the tag as written (aliases such as c# and c++ resolve
        // here); highlightElement re-reads the class and would take "c#" as "c".
        // highlight.js escapes the text it marks up.
        const result = language ? hljs.highlight(text, { language, ignoreIllegals: true }) : hljs.highlightAuto(text)
        code.innerHTML = result.value
        code.classList.add('hljs')
        if (!language && result.language) code.classList.add(`language-${result.language}`)
      } catch { code.textContent = text /* Leave the block as plain text. */ }
    }
    const block = document.createElement('div')
    block.className = 'code-block'
    pre.replaceWith(block)
    // marked ends each block with a newline that is not part of the code.
    block.append(pre, copyButton(text.replace(/\n$/, '')))
  }
}

function copyButton(text) {
  const button = document.createElement('button')
  button.type = 'button'
  button.className = 'code-copy'
  button.textContent = 'Copy code'
  button.setAttribute('aria-live', 'polite')
  let timer
  const show = label => {
    button.textContent = label
    clearTimeout(timer)
    timer = setTimeout(() => { button.textContent = 'Copy code' }, 1500)
  }
  button.addEventListener('click', () => {
    Promise.resolve().then(() => navigator.clipboard.writeText(text)).then(() => show('Copied'), () => show('Copy failed'))
  })
  return button
}
