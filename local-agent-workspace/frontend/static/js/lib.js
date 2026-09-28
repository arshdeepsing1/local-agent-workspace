// One import point for the UI runtime. Preact has React's component and hook
// model; htm gives JSX-like templates without a compiler:
//   html`<button class="primary" onClick=${save}>Save</button>`
// Differences from React worth remembering when editing components:
// - use `class`, and `onInput` for live text input (`onChange` fires on blur);
// - booleans such as `disabled=${busy}` and `aria-*` values work as in React;
// - SVG attributes keep their SVG spelling, for example `text-anchor`.
import { h, render } from 'preact'
import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks'
import htm from 'htm'

export const html = htm.bind(h)
export { render, useCallback, useEffect, useMemo, useRef, useState }

// Apply a React-style state update (a value or an updater function).
export const resolve = (value, current) => typeof value === 'function' ? value(current) : value

// Focus an element once when it mounts (React's autoFocus).
export function useAutoFocus() {
  const ref = useRef(null)
  useEffect(() => { ref.current?.focus() }, [])
  return ref
}
