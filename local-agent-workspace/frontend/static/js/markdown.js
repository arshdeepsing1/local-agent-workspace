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

// A whole inline code span that names a file (with an extension), optionally with :line or :line:column.
const CODE_PATH = /^(?:~\/|\.{1,2}\/|\/)?(?:[\w@+-][\w.@+-]*\/)*[\w@+-][\w.@+-]*\.[A-Za-z][A-Za-z0-9_+-]{0,11}(?::(\d{1,7})(?::\d{1,7})?)?$/
// In prose: a path with a slash, or a bare file name followed by :line. URLs do not match.
const TEXT_PATH = /(?<![\w\/.~:@+-])((?:~\/|\.{1,2}\/|\/)?(?:[\w@+-][\w.@+-]*\/)+[\w@+-][\w.@+-]*\.[A-Za-z][A-Za-z0-9_+-]{0,11}|[\w@+-][\w.@+-]*\.[A-Za-z][A-Za-z0-9_+-]{0,11}(?=:\d))(?::(\d{1,7})(?::\d{1,7})?)?(?![\w\/@+-])/g

// A name without a folder (`config.py`, `app.ts:12`) is a file only with one of these
// extensions, so identifiers (`os.path.join`, `console.log`) and hosts (`db.example.com:5432`)
// stay text. A path with a slash may have any extension.
const FILE_EXTENSIONS = new Set(`py pyi ipynb js mjs cjs jsx ts mts cts tsx json jsonc jsonl md mdx rst txt csv tsv
  yaml yml toml ini cfg xml html htm css scss sass less svg sh bash zsh fish ps1 bat sql go rs java kt kts scala
  groovy gradle c h cc cpp cxx hpp cs swift rb php lua dart exs erl clj vue svelte astro tf hcl
  proto graphql gql mk cmake sbt tex`.split(/\s+/))

function isFilePath(path) {
  return path.includes('/') || FILE_EXTENSIONS.has(path.slice(path.lastIndexOf('.') + 1).toLowerCase())
}

function pathLink(path, line) {
  const link = document.createElement('a')
  link.className = 'path-link'
  link.href = '#'
  link.dataset.path = path
  if (line) link.dataset.line = line
  link.title = line ? `Open ${path} at line ${line}` : `Open ${path}`
  return link
}

// Turns file paths in rendered Markdown into links (a.path-link with data-path and
// data-line) that the page opens in the Workspace editor. Elements are built with
// DOM APIs only, so a path is never parsed as markup. Code blocks and existing links
// are left alone; running it again changes nothing.
export function linkPaths(root) {
  for (const code of root.querySelectorAll('code')) {
    if (code.closest('pre, a')) continue
    const match = CODE_PATH.exec(code.textContent)
    const path = code.textContent.split(':')[0]
    if (!match || !isFilePath(path)) continue
    const link = pathLink(path, match[1])
    code.replaceWith(link)
    link.append(code)
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const nodes = []
  while (walker.nextNode()) {
    const node = walker.currentNode
    if (!node.parentElement?.closest('code, pre, a')) nodes.push(node)
  }
  for (const node of nodes) {
    const text = node.textContent
    const parts = []
    let last = 0
    for (const match of text.matchAll(TEXT_PATH)) {
      if (!isFilePath(match[1])) continue
      if (match.index > last) parts.push(document.createTextNode(text.slice(last, match.index)))
      const link = pathLink(match[1], match[2])
      link.textContent = match[0]
      parts.push(link)
      last = match.index + match[0].length
    }
    if (!parts.length) continue
    if (last < text.length) parts.push(document.createTextNode(text.slice(last)))
    node.replaceWith(...parts)
  }
}
