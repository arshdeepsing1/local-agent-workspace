// GitHub-flavored Markdown for model replies. Raw HTML in a reply is shown as
// text, and the generated HTML is sanitized before it reaches the page.
import { Marked } from '../vendor/marked.esm.js'
import DOMPurify from '../vendor/purify.es.js'
import hljs from '../vendor/highlight.min.js'
import { HIDDEN_CHAR_CLASS, RTL_RUN_CLASS, hiddenCharTitle, needsParts, visibleSegments, visibleText } from './lib.js'

const escapeHtml = text => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
const marked = new Marked({ gfm: true, renderer: { html: ({ text }) => escapeHtml(text) } })

// Nothing in a reply may make the browser fetch an address on its own: a model (or a prompt injection in a page, a file
// or a tool result) could write an image or a media element whose address carries chat content to any host, with no
// click and past the app's approval and address checks. These elements and attributes can fetch, so they are removed.
// Raw HTML never gets this far (the renderer above escapes it); this is the second line.
const FETCHING = {
  FORBID_TAGS: ['picture', 'source', 'video', 'audio', 'track', 'link', 'style', 'object', 'embed', 'iframe', 'frame',
    'input', 'form', 'svg', 'math', 'image', 'use', 'base', 'meta'],
  FORBID_ATTR: ['srcset', 'poster', 'background', 'style', 'ping', 'xlink:href'],
}
// The only image sources that load: bytes inside the page (data: image, blob:).
const INLINE_IMAGE = /^(?:data:image\/[a-z0-9.+-]+[;,]|blob:)/i

// Moves an image's address to data-src, so nothing loads until a caller decides. When allowInline is set, a data: image
// or a blob: URL keeps its src. Used for replies, plans and side answers (renderMarkdown) and the Markdown file preview.
function withholdImage(node, allowInline) {
  if (node.nodeName !== 'IMG') return
  const source = node.getAttribute('src')
  node.removeAttribute('srcset')
  if (allowInline && source && INLINE_IMAGE.test(source.trim())) return
  node.removeAttribute('src')
  if (source) node.setAttribute('data-src', source)
}

const replyPurify = DOMPurify(window)
replyPurify.addHook('afterSanitizeAttributes', node => withholdImage(node, true))

// The longest host the placeholder shows (a host name has at most 253 characters).
const HOST_LIMIT = 80

// The host an image address points to, or '' when it is on this page's own origin or has no host. The address is
// resolved against the page first, so "https:attacker.example/a.png" (no slashes) names its host and "/img/a.png" none.
function imageHost(address) {
  try {
    const url = new URL(address, location.href)
    if (!url.hostname || url.origin === location.origin) return ''
    const letters = Array.from(url.hostname)
    return letters.length > HOST_LIMIT ? `${letters.slice(0, HOST_LIMIT).join('')}…` : url.hostname
  } catch { return '' }
}

// The reply configuration of the sanitizer alone: raw HTML in, HTML out with every element and attribute that can fetch
// removed and every image address moved to data-src. renderMarkdown uses it; tests feed it raw HTML directly.
export const sanitizeReply = markup => replyPurify.sanitize(markup, FETCHING)

// The longest description of a withheld image the placeholder shows.
const ALT_LIMIT = 120

// A reply, plan or side answer: sanitized HTML. An image that would load from outside the page is replaced by a short
// text that names its host first and then its description in quotes (cut at 120 characters, hidden characters
// escaped, a quote inside it written as \"), so the reader sees that something was withheld and a description cannot pose as the host.
// The address stays in data-src. The text is set as text, never as HTML.
export function renderMarkdown(text) {
  const box = document.createElement('template')  // Inert: nothing in it loads.
  box.innerHTML = sanitizeReply(marked.parse(text || ''))
  for (const image of box.content.querySelectorAll('img:not([src])')) {
    const address = image.getAttribute('data-src') || ''
    const host = imageHost(address)
    const alt = Array.from((image.getAttribute('alt') || '').trim())
    const shownAlt = visibleText(alt.slice(0, ALT_LIMIT).join('')).replace(/"/g, '\\"')
    const described = alt.length ? `"${shownAlt}${alt.length > ALT_LIMIT ? '…' : ''}"` : 'no description'
    const note = document.createElement('span')
    note.className = 'file-preview-blocked'
    note.textContent = `Image not loaded${host ? ` from ${host}` : ''}: ${described}`
    if (address) note.setAttribute('data-src', address)
    image.replaceWith(note)
  }
  return box.innerHTML
}

// Shows hidden and direction-changing characters in the text of rendered Markdown (also inside code blocks and
// code spans) as visible escapes, each in its own element, so a plan waiting for approval reads as what it holds. A run
// of right-to-left letters becomes a <bdi class="rtl-run">, so it cannot reorder the text around it. The title of an
// element and the description of an image get the plain escaped text. Run it after enhanceCodeBlocks (highlighting
// rewrites a block from its raw text). Returns how many characters (and attribute values) it escaped; it throws
// when it cannot finish, and the caller then shows the plan as plain escaped text instead.
export function showHiddenCharacters(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const nodes = []
  while (walker.nextNode()) nodes.push(walker.currentNode)
  let escaped = 0
  for (const node of nodes) {
    if (!needsParts(node.textContent)) continue
    // A fragment gets the pieces one by one: a plan can hold far more pieces than a call can take as arguments.
    const fragment = document.createDocumentFragment()
    for (const piece of visibleSegments(node.textContent)) {
      if (piece.escaped) {
        const mark = document.createElement('span')
        mark.className = HIDDEN_CHAR_CLASS
        mark.title = hiddenCharTitle(piece.code)
        mark.textContent = piece.text
        fragment.appendChild(mark)
        escaped++
      } else if (piece.rtl) {
        const run = document.createElement('bdi')
        run.className = RTL_RUN_CLASS
        run.textContent = piece.text
        fragment.appendChild(run)
      } else fragment.appendChild(document.createTextNode(piece.text))
    }
    node.replaceWith(fragment)
  }
  for (const element of root.querySelectorAll('[title], img[alt]')) {
    for (const name of ['title', 'alt']) {
      const value = element.getAttribute(name)
      if (value === null || (name === 'alt' && element.nodeName !== 'IMG')) continue
      const shown = visibleText(value)
      if (shown !== value) { element.setAttribute(name, shown); escaped++ }
    }
  }
  return escaped
}

// A Markdown file in the Workspace preview: rendered and sanitized like a reply, but each image's address is moved
// to data-src before the HTML reaches the page, so nothing loads until the preview decides (FilePreview.js loads
// images from the project and none from the web).
const previewPurify = DOMPurify(window)
previewPurify.addHook('afterSanitizeAttributes', node => withholdImage(node, false))
export function renderMarkdownPreview(text) {
  return previewPurify.sanitize(marked.parse(text || ''), FETCHING)
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
    const fragment = document.createDocumentFragment()
    for (const part of parts) fragment.appendChild(part)
    node.replaceWith(fragment)
  }
}
