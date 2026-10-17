import { html, useEffect, useMemo, useRef, useState } from '../lib.js'
import { apiBlob } from '../api.js'
import { enhanceCodeBlocks, renderMarkdownPreview } from '../markdown.js'

// The Workspace panel's file preview (M8g2). Nothing in it runs as a page of the app's origin:
// - images (PNG, JPEG, GIF and WebP) show in an <img> from a blob; an SVG from a data: URL (see previewUrl);
// - a PDF shows in the browser's viewer, from a blob typed application/pdf;
// - Markdown is rendered and sanitized like a reply; images beside the file (in the project, or in a folder outside it
//   that Files shows) are loaded from it, images from the web are not loaded, and web links open in a new tab;
// - an HTML file shows in a sandboxed frame, which has an origin of its own (it cannot reach the app, its token or
//   the API) and loads nothing (its policy, and the page's frame-src that keeps it from navigating). Its scripts
//   run only after Run scripts, for that file until the page is reloaded; they can still open WebRTC connections,
//   which no policy or sandbox flag blocks, and the note says so.
const KINDS = { png: 'image', jpg: 'image', jpeg: 'image', gif: 'image', webp: 'image', svg: 'image', pdf: 'pdf',
  md: 'markdown', markdown: 'markdown', html: 'html', htm: 'html' }
const TYPES = { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp',
  svg: 'image/svg+xml', pdf: 'application/pdf' }
const extension = path => (/\.([^./]+)$/.exec(path || '')?.[1] || '').toLowerCase()
export const previewKind = path => KINDS[extension(path)] || null
// Images and PDF open in the preview; Markdown and HTML open in the editor, with Preview.
export const opensInPreview = path => ['image', 'pdf'].includes(previewKind(path))
const fileName = path => String(path || '').split('/').pop() || 'download'
const rawPath = (path, query, download = false) => `/file/raw?path=${encodeURIComponent(path)}${download ? '&download=1' : ''}${query ? `&${query}` : ''}`

// The file's bytes saved by the browser (GET /file/raw with download=1, so the server sends any file as an attachment).
export async function downloadFile(path, query) {
  const { blob } = await apiBlob(rawPath(path, query, true))
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url; link.download = fileName(path)
  document.body.appendChild(link)
  try { link.click() } finally { link.remove(); URL.revokeObjectURL(url) }
}

// The URL a file's bytes are shown from, typed by its extension (never by the server or the bytes): a blob URL, or
// a data: URL for an SVG. Opened on its own (Open image in new tab), a blob URL is a page of the app's origin, where
// an SVG's scripts would run and could read the app's token; a data: URL is a page with an origin of its own.
async function previewUrl(blob, path) {
  const typed = new Blob([blob], { type: TYPES[extension(path)] || 'application/octet-stream' })
  if (typed.type !== 'image/svg+xml') return URL.createObjectURL(typed)
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result)
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(typed)
  })
}
const revoke = url => { if (url?.startsWith('blob:')) URL.revokeObjectURL(url) }

// The preview URL of a file, revoked when unused; onOpened(path) gets the path the server read it by.
function useFileUrl(path, query, version, onOpened) {
  const [state, setState] = useState({ url: null, error: '', bytes: 0 })
  useEffect(() => {
    let active = true, url = null
    setState({ url: null, error: '', bytes: 0 })
    apiBlob(rawPath(path, query)).then(async ({ blob, opened }) => {
      const shown = await previewUrl(blob, path)
      if (!active) return revoke(shown)
      url = shown
      setState({ url, error: '', bytes: blob.size })
      if (opened) onOpened?.(opened)
    }).catch(error => { if (active) setState({ url: null, error: error.message, bytes: 0 }) })
    return () => { active = false; revoke(url) }
  }, [path, query, version])
  return state
}

const sizeText = bytes => bytes >= 1_000_000 ? `${(bytes / 1_000_000).toFixed(1)} MB` : bytes >= 1000 ? `${Math.round(bytes / 1000)} KB` : `${bytes} bytes`

function ImagePreview({ path, source, query, version, onOpened }) {
  const { url, error, bytes } = useFileUrl(source || path, query, version, onOpened)
  const [size, setSize] = useState(null)
  if (error) return html`<div class="panel-error" role="alert">${error}</div>`
  if (!url) return html`<p class="panel-empty">Loading preview…</p>`
  return html`<div class="file-preview file-preview-image">
    <img src=${url} alt=${fileName(path)} onLoad=${event => setSize([event.currentTarget.naturalWidth, event.currentTarget.naturalHeight])} />
    <p class="file-preview-note">${size ? `${size[0]} × ${size[1]} px · ` : ''}${sizeText(bytes)}</p></div>`
}

// A browser's viewer can name a PDF from a blob by the blob's ID, also in its own Download, and nothing can rename it.
function PdfPreview({ path, source, query, version, onOpened }) {
  const { url, error, bytes } = useFileUrl(source || path, query, version, onOpened)
  if (error) return html`<div class="panel-error" role="alert">${error}</div>`
  if (!url) return html`<p class="panel-empty">Loading preview…</p>`
  return html`<div class="file-preview file-preview-pdf">
    <p class="file-preview-note">${sizeText(bytes)} · The viewer may name the file by an ID: <strong>Download</strong> above saves it as ${fileName(path)}.</p>
    <iframe class="file-preview-frame pdf" src=${url} title=${`Preview of ${path}`} /></div>`
}

// A Markdown link or image address relative to the Markdown file's folder (no scheme, not from the host's root, not
// only a fragment), as a path like the file's own: a project path, or an absolute path for a file outside the project
// (the server's Files checks apply to it); null otherwise. '..' cannot leave the project, or go above the root.
function projectPath(address, from) {
  if (!address || /^[a-z][a-z0-9+.-]*:/i.test(address) || address.startsWith('/') || address.startsWith('#')) return null
  let value
  try { value = decodeURI(address.split(/[?#]/)[0]) } catch { return null }
  if (!value) return null
  const parts = from.split('/').slice(0, -1).filter(Boolean)
  for (const part of value.split('/')) {
    if (part === '..') { if (!parts.length) return null; parts.pop() } else if (part && part !== '.') parts.push(part)
  }
  return parts.length ? `${from.startsWith('/') ? '/' : ''}${parts.join('/')}` : null
}

function MarkdownPreview({ path, content, query, onOpenPath }) {
  const element = useRef(null)
  const markup = useMemo(() => renderMarkdownPreview(content), [content])
  useEffect(() => {
    const root = element.current
    if (!root) return undefined
    enhanceCodeBlocks(root)
    const urls = []
    let active = true
    for (const image of root.querySelectorAll('img[data-src]')) {
      const address = image.dataset.src
      const local = projectPath(address, path)
      if (/^data:image\/(?:png|jpeg|gif|webp);/i.test(address)) image.src = address
      else if (local && previewKind(local) === 'image') {
        apiBlob(rawPath(local, query)).then(({ blob }) => previewUrl(blob, local)).then(url => {
          if (!active) return revoke(url)
          urls.push(url)
          image.src = url
        }, () => { if (active) image.replaceWith(blocked(image, 'Image not found')) })
      } else image.replaceWith(blocked(image, local ? 'Not an image file'
        : /^(?:https?:)?\/\//i.test(address) ? 'Image from the web, not loaded' : 'Image not loaded'))
    }
    for (const link of root.querySelectorAll('a[href]')) {
      const address = link.getAttribute('href')
      const local = projectPath(address, path)
      if (local) { link.dataset.path = local; link.title = `Open ${local}` }
      else if (/^https?:/i.test(address)) { link.target = '_blank'; link.rel = 'noopener noreferrer' }
    }
    return () => { active = false; urls.forEach(revoke) }
  }, [markup, path, query])
  const click = event => {
    const link = event.target.closest?.('a[href]')
    if (!link || !element.current?.contains(link)) return
    if (link.dataset.path) { event.preventDefault(); onOpenPath?.(link.dataset.path) }
    else if (!/^https?:/i.test(link.getAttribute('href'))) event.preventDefault()  // Fragments and other schemes.
  }
  return html`<div class="file-preview file-preview-markdown markdown" ref=${element} onClick=${click}
    dangerouslySetInnerHTML=${{ __html: markup }} />`
}

function blocked(image, reason) {
  const note = document.createElement('span')
  note.className = 'file-preview-blocked'
  note.textContent = `${reason}: ${image.getAttribute('alt') || image.dataset.src}`
  note.title = image.dataset.src
  return note
}

// The frame's own policy, first in its head (after a doctype, so the page keeps its standards mode): no requests at
// all except inline styles and scripts and data: images, fonts and media. A browser ignores the policy outside the
// head, so it follows a doctype only when nothing but HTML's own whitespace comes before it (JavaScript's \s also
// matches a no-break space, before which the parser opens the body); anything else, a comment included, comes after
// it. A byte order mark is dropped first, as a browser drops it when it opens the file.
const POLICY = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; media-src data:; '
  + 'font-src data:; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; form-action \'none\'; base-uri \'none\'">'
export function framedHtml(text) {
  const page = text.replace(/^\ufeff/, '')
  const doctype = /^[\t\n\f\r ]*<!doctype[^>]*>/i.exec(page)
  return doctype ? doctype[0] + POLICY + page.slice(doctype[0].length) : POLICY + page
}

const scriptFiles = new Set()  // Files whose preview runs scripts, until the page is reloaded.

function HtmlPreview({ path, content, query }) {
  const key = `${query}\n${path}`
  const [scripts, setScripts] = useState(() => scriptFiles.has(key))
  useEffect(() => { setScripts(scriptFiles.has(key)) }, [key])
  const toggle = () => {
    if (scripts) scriptFiles.delete(key); else scriptFiles.add(key)
    setScripts(!scripts)
  }
  const page = useMemo(() => framedHtml(content), [content])
  return html`<div class="file-preview file-preview-html">
    <p class="file-preview-note"><span>${scripts ? 'Scripts run in a sandbox without access to the app. Requests are blocked, WebRTC connections are not: run scripts only in files you trust.' : 'Scripts are off. Nothing is loaded from the network.'}</span>
      <button type="button" class="outline" aria-pressed=${scripts} onClick=${toggle}>${scripts ? 'Stop scripts' : 'Run scripts'}</button></p>
    <iframe key=${scripts ? 'scripts' : 'static'} class="file-preview-frame" sandbox=${scripts ? 'allow-scripts' : ''}
      referrerpolicy="no-referrer" title=${`Preview of ${path}`} srcdoc=${page} /></div>`
}

// kind: previewKind(path); content: the text of a Markdown or HTML file (the editor's, unsaved edits included);
// version changes when the file changed on disk; onOpenPath(path) opens a file a Markdown link names. An image or
// PDF is read by source (default: path), and onOpened(path) gets the path the server read it by.
export default function FilePreview({ path, source, kind, content = '', query = '', version = 0, onOpenPath, onOpened }) {
  if (kind === 'image') return html`<${ImagePreview} path=${path} source=${source} query=${query} version=${version} onOpened=${onOpened} />`
  if (kind === 'pdf') return html`<${PdfPreview} path=${path} source=${source} query=${query} version=${version} onOpened=${onOpened} />`
  if (kind === 'markdown') return html`<${MarkdownPreview} path=${path} content=${content} query=${query} onOpenPath=${onOpenPath} />`
  if (kind === 'html') return html`<${HtmlPreview} path=${path} content=${content} query=${query} />`
  return null
}
