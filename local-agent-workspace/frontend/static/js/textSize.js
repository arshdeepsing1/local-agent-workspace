// Settings → Appearance → Text size: a multiplier for every font size of the page (--text-scale in styles.css), kept
// in this browser's localStorage. Browser zoom (Ctrl or Cmd and +) scales the whole page instead.
export const TEXT_SIZES = [90, 100, 110, 125, 140, 150]
const KEY = 'local.textSize'

export function textSize() {
  try {
    const value = Number(window.localStorage.getItem(KEY))
    return TEXT_SIZES.includes(value) ? value : 100
  } catch { return 100 }  // Storage blocked (private windows): the default.
}

export function applyTextSize(value) {
  const size = TEXT_SIZES.includes(value) ? value : 100
  document.documentElement.style.setProperty('--text-scale', String(size / 100))
  try {
    if (size === 100) window.localStorage.removeItem(KEY)
    else window.localStorage.setItem(KEY, String(size))
  } catch { /* Kept for this page only. */ }
  return size
}
