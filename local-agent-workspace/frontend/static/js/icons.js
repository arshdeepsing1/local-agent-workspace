// Lucide icons (ISC license, lucide-static 0.468.0; see ../vendor/LICENSE-lucide.txt).
// To add one, copy the inner SVG markup from https://lucide.dev/icons/<name>.
import { html } from './lib.js'

const icon = (slug, body) => function Icon({ size = 24, className = '', fill = 'none' }) {
  return html`<svg class=${`lucide lucide-${slug} ${className}`.trim()} xmlns="http://www.w3.org/2000/svg" width=${size} height=${size}
    viewBox="0 0 24 24" fill=${fill} stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
    aria-hidden="true" dangerouslySetInnerHTML=${{ __html: body }} />`
}

export const ArrowLeft = icon('arrow-left', '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>')
export const ArrowLeftRight = icon('arrow-left-right', '<path d="M8 3 4 7l4 4"/><path d="M4 7h16"/><path d="m16 21 4-4-4-4"/><path d="M20 17H4"/>')
export const ArrowUp = icon('arrow-up', '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>')
export const Check = icon('check', '<path d="M20 6 9 17l-5-5"/>')
export const ChevronDown = icon('chevron-down', '<path d="m6 9 6 6 6-6"/>')
export const ChevronRight = icon('chevron-right', '<path d="m9 18 6-6-6-6"/>')
export const ChevronUp = icon('chevron-up', '<path d="m18 15-6-6-6 6"/>')
export const Code2 = icon('code-xml', '<path d="m18 16 4-4-4-4"/><path d="m6 8-4 4 4 4"/><path d="m14.5 4-5 16"/>')
export const Copy = icon('copy', '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>')
export const File = icon('file', '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>')
export const FileDiff = icon('file-diff', '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M9 10h6"/><path d="M12 13V7"/><path d="M9 17h6"/>')
export const FileText = icon('file-text', '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>')
export const Folder = icon('folder', '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>')
export const FolderPlus = icon('folder-plus', '<path d="M12 10v6"/><path d="M9 13h6"/><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>')
export const GitBranch = icon('git-branch', '<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>')
export const Laptop = icon('laptop', '<path d="M20 16V7a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v9m16 0H4m16 0 1.28 2.55a1 1 0 0 1-.9 1.45H3.62a1 1 0 0 1-.9-1.45L4 16"/>')
export const LoaderCircle = icon('loader-circle', '<path d="M21 12a9 9 0 1 1-6.219-8.56"/>')
export const Menu = icon('menu', '<line x1="4" x2="20" y1="12" y2="12"/><line x1="4" x2="20" y1="6" y2="6"/><line x1="4" x2="20" y1="18" y2="18"/>')
export const PanelRight = icon('panel-right', '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M15 3v18"/>')
export const Pencil = icon('pencil', '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/>')
export const Plus = icon('plus', '<path d="M5 12h14"/><path d="M12 5v14"/>')
export const RefreshCw = icon('refresh-cw', '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>')
export const Save = icon('save', '<path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"/><path d="M7 3v4a1 1 0 0 0 1 1h7"/>')
export const Settings = icon('settings', '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>')
export const Square = icon('square', '<rect width="18" height="18" x="3" y="3" rx="2"/>')
export const Terminal = icon('terminal', '<polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/>')
export const Trash2 = icon('trash-2', '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/>')
export const Undo2 = icon('undo-2', '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"/>')
export const X = icon('x', '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>')
