// Renders one UI module in isolation for a test:
//   await preload('components/Conversation.js'); mount('components/Conversation.js', () => ({ session, onError: fake.spy('onError') }))
//   rerender(() => ({ ...newProps }))   unmount()
//   await preload('useWorkspace.js', 'useWorkspace'); probe('useWorkspace.js', 'useWorkspace')  then read window.hook
import { h, options, render } from 'preact'
import { useState } from 'preact/hooks'

// Run effects right after each render instead of after the next animation
// frame, so tests do not depend on frame timing or on a faked clock.
options.requestAnimationFrame = callback => queueMicrotask(callback)

const root = document.getElementById('root')
let setProps = null

async function load(module, name = 'default') {
  const loaded = await import(`/static/js/${module}`)
  if (!loaded[name]) throw new Error(`${module} has no export ${name}`)
  return loaded[name]
}

function Host({ component, initial }) {
  const [props, update] = useState(initial)
  setProps = update
  return h(component, props)
}

// Modules are imported first (window.preload, the only asynchronous step); mount and
// probe then render synchronously, so the test's evaluate call returns no promise.
// A promise that resolves while the rendered app's effects and fetches still fill the
// microtask queue can be garbage collected before DevTools reads it ("Resulting promise
// was garbage collected"), which made tests that mount the whole app flaky.
const loaded = new Map()
window.preload = async (module, name = 'default') => { loaded.set(`${module}#${name}`, await load(module, name)) }
// Runs `use` with the export now when it was preloaded, else after importing it (a promise).
const withExport = (module, name, use) => {
  const value = loaded.get(`${module}#${name}`)
  return value ? use(value) : load(module, name).then(use)
}

// Rendering is synchronous, so each helper returns once the component is on the page.
window.mount = (module, makeProps = () => ({}), name = 'default') =>
  withExport(module, name, component => { render(h(Host, { component, initial: makeProps() }), root) })
window.rerender = makeProps => setProps(makeProps())
window.unmount = () => render(null, root)

// Mounts a component that calls a hook and publishes its latest result.
window.probe = (module, name) => withExport(module, name, hook => {
  function Probe() { window.hook = hook(); return null }
  render(h(Probe), root)
})
window.harnessReady = true
