import { html, render } from './lib.js'
import App from './App.js'

render(html`<${App} />`, document.getElementById('root'))
