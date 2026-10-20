// Settings → Appearance → Theme: Light (the default), Dark, or Match system (the operating system's light or dark mode).
// Kept in this browser's localStorage like Text size, and applied as data-theme on <html>: styles.css swaps its colour
// tokens for dark and for system (under prefers-color-scheme: dark). index.html applies a stored choice before the
// stylesheet loads, so a dark page never flashes light.
export const THEMES = [['light', 'Light'], ['dark', 'Dark'], ['system', 'Match system']]
const KEY = 'local.theme'
const CANVAS = { light: '#faf9f5', dark: '#262624' }  // <meta name="theme-color">: the browser's title bar on phones and installed apps
const known = value => THEMES.some(([name]) => name === value)
const darkQuery = () => window.matchMedia?.('(prefers-color-scheme: dark)')

export function theme() {
  try {
    const value = window.localStorage.getItem(KEY)
    return known(value) ? value : 'light'
  } catch { return 'light' }  // Storage blocked (private windows): the default.
}

function updateThemeColor() {
  const value = document.documentElement.dataset.theme
  const dark = value === 'dark' || (value === 'system' && !!darkQuery()?.matches)
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', dark ? CANVAS.dark : CANVAS.light)
}

let watching = false

export function applyTheme(value) {
  const chosen = known(value) ? value : 'light'
  document.documentElement.dataset.theme = chosen
  updateThemeColor()
  if (!watching) {  // Match system follows the operating system while the page is open.
    watching = true
    darkQuery()?.addEventListener?.('change', updateThemeColor)
  }
  try {
    if (chosen === 'light') window.localStorage.removeItem(KEY)
    else window.localStorage.setItem(KEY, chosen)
  } catch { /* Kept for this page only. */ }
  return chosen
}
