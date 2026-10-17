// Files attached to a message from the composer (paste, drop or Attach files): documents, whose text the agent
// gets after the message, and images, sent as image parts to endpoints that take them (uploads.py on the server).
// The limits mirror uploads.py and documents.py; the server checks them again.

export const MAX_UPLOAD_BYTES = 20_000_000
export const MAX_IMAGE_BYTES = 2_500_000
export const MAX_MESSAGE_FILES = 8
export const MAX_MESSAGE_IMAGES = 4
const DOCUMENTS = { pdf: 'PDF', docx: 'Word', xlsx: 'Excel', pptx: 'PowerPoint', csv: 'CSV', tsv: 'TSV', txt: 'Text',
  text: 'Text', log: 'Text', md: 'Markdown', markdown: 'Markdown' }
const IMAGES = { 'image/png': 'PNG', 'image/jpeg': 'JPEG', 'image/gif': 'GIF', 'image/webp': 'WebP' }
const IMAGE_EXTENSIONS = { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp' }
const UNITS = { page: 'page', sheet: 'sheet', slide: 'slide' }
// The file picker's filter: the document formats and image types above.
export const ACCEPT = [...Object.keys(DOCUMENTS).map(extension => `.${extension}`), ...Object.keys(IMAGES)].join(',')

const extension = name => (/\.([^./\\]+)$/.exec(name || '')?.[1] || '').toLowerCase()
const plural = (count, unit) => `${count.toLocaleString('en-US')} ${unit}${count === 1 ? '' : 's'}`

// 'image', 'document' or null (a type the app does not take), from the file's type or name.
export function fileKind(file) {
  if (IMAGES[file.type] || IMAGE_EXTENSIONS[extension(file.name)]) return 'image'
  return DOCUMENTS[extension(file.name)] ? 'document' : null
}

// Why this file cannot be attached before it is sent to the server, or null.
export function fileProblem(file) {
  const kind = fileKind(file)
  if (!kind) return `${file.name || 'This file'} cannot be attached: attach PDF, Word (.docx), Excel (.xlsx), PowerPoint (.pptx), CSV, TSV, text or Markdown files, or PNG, JPEG, GIF or WebP images.`
  if (!file.size) return `${file.name} is empty.`
  if (file.size > MAX_UPLOAD_BYTES) return `${file.name} is larger than 20 MB, the most a file attached to a message can be.`
  if (kind === 'image' && file.size > MAX_IMAGE_BYTES) {
    return `${file.name} is larger than 2.5 MB, the most an image can be. The app does not shrink images: attach a smaller copy (for example a JPEG or a cropped screenshot).`
  }
  return null
}

// What a chip says about an uploaded file (UploadRecord): its type, pages, size in pixels and estimated tokens.
export function recordDetail(record) {
  if (!record) return ''
  const parts = [record.kind === 'image' ? IMAGES[record.media_type] || 'Image' : DOCUMENTS[record.format] || 'Document']
  // A queued message's files come back with their name and kind only.
  if (record.kind === 'image') { if (Number.isInteger(record.width) && Number.isInteger(record.height)) parts.push(`${record.width}×${record.height}`) }
  else if (UNITS[record.units] && Number.isInteger(record.count)) parts.push(plural(record.count, UNITS[record.units]))
  if (record.unreadable) parts.push('text not read')
  else if (Number.isInteger(record.estimated_tokens)) parts.push(`~${record.estimated_tokens.toLocaleString('en-US')} tokens`)
  return parts.join(' · ')
}

// The uploads a message sends: [{ id, as_path? }] of the composer's ready chips.
export function uploadRefs(items) {
  return (items || []).filter(item => item.record?.id).map(item => ({ id: item.record.id, ...(item.asPath ? { as_path: true } : {}) }))
}

// The files a user message was sent with (its event's document and image attachments), to send them again
// (Retry, Edit message).
export function sentUploads(event) {
  return (Array.isArray(event?.attachments) ? event.attachments : [])
    .filter(item => (item?.kind === 'document' || item?.kind === 'image') && typeof item.upload === 'string')
    .map(item => ({ id: item.upload, ...(item.as_path ? { as_path: true } : {}) }))
}

// A user message's chip for an attached document or image (metadata only).
export function sentLabel(item) {
  if (item.kind === 'image') {
    const size = Number.isInteger(item.width) && Number.isInteger(item.height) ? ` · ${item.width}×${item.height}` : ''
    return `${item.name || 'image'}${size}`
  }
  const units = UNITS[item.units] && Number.isInteger(item.count) ? ` · ${plural(item.count, UNITS[item.units])}` : ''
  return `${item.name || 'document'}${units}${item.as_path ? ' · as a path' : item.truncated ? ' · shortened' : ''}`
}
