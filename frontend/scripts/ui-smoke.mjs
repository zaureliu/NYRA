import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import path from 'node:path'

const edge = process.env.NYRA_EDGE_PATH || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const port = 9337
const viewportWidth = Number(process.env.NYRA_UI_WIDTH || 1440)
const viewportHeight = Number(process.env.NYRA_UI_HEIGHT || 900)
const diagnosticOnly = process.env.NYRA_UI_DIAGNOSTIC === '1'
const profile = path.resolve(process.cwd(), '..', '.tmp', `edge-ui-smoke-${Date.now()}`)
const browser = spawn(edge, [
  '--headless=new', '--disable-gpu', '--hide-scrollbars', '--disable-background-timer-throttling',
  '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows', `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`, `--window-size=${viewportWidth},${viewportHeight}`, 'http://127.0.0.1:5173/#conversation',
], { stdio: 'ignore', windowsHide: true })

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds))
const getPage = async () => {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const pages = await fetch(`http://127.0.0.1:${port}/json/list`).then((response) => response.json())
      const page = pages.find((item) => item.type === 'page' && item.url.includes('127.0.0.1:5173'))
      if (page) return page
    } catch { /* browser is starting */ }
    await delay(125)
  }
  throw new Error('Edge DevTools endpoint did not become ready')
}

let socket
try {
  const page = await getPage()
  socket = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }) })
  let requestId = 0
  const pending = new Map()
  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data)
    if (!message.id || !pending.has(message.id)) return
    const { resolve, reject } = pending.get(message.id)
    pending.delete(message.id)
    if (message.error) reject(new Error(message.error.message)); else resolve(message.result)
  })
  const cdp = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++requestId
    pending.set(id, { resolve, reject })
    socket.send(JSON.stringify({ id, method, params }))
  })
  const evaluate = async (expression) => {
    for (let attempt = 0; attempt < 12; attempt += 1) {
      try {
        const result = await cdp('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
        if (result.exceptionDetails) throw new Error(result.exceptionDetails.text)
        return result.result.value
      } catch (error) {
        const transientContext = String(error).includes('Execution context was destroyed')
          || String(error).includes('Cannot find default execution context')
        if (!transientContext || attempt === 11) throw error
        await delay(250)
      }
    }
  }

  await cdp('Page.enable')
  await cdp('Runtime.enable')
  if (viewportWidth < 500) {
    await cdp('Emulation.setDeviceMetricsOverride', { width: viewportWidth, height: viewportHeight, deviceScaleFactor: 1, mobile: false })
  }
  await evaluate(`new Promise(resolve => document.readyState === 'complete' ? setTimeout(resolve, 500) : addEventListener('load', () => setTimeout(resolve, 500), {once:true}))`)

  const composerMetrics = await evaluate(`(() => {
    const textarea = document.querySelector('.composer textarea');
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
    setter.call(textarea, Array.from({length: 24}, (_, i) => 'Linha de validação ' + (i + 1)).join('\\n'));
    textarea.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText'}));
    return new Promise(resolve => requestAnimationFrame(() => resolve({
      clientHeight: textarea.clientHeight,
      scrollHeight: textarea.scrollHeight,
      overflowY: getComputedStyle(textarea).overflowY,
      pageHeight: document.documentElement.scrollHeight,
      viewportHeight: innerHeight,
      messageOverflow: getComputedStyle(document.querySelector('.messages')).overflowY,
      chatHeight: document.querySelector('.conversation-panel').getBoundingClientRect().height,
    })));
  })()`)
  const layoutMetrics = await evaluate(`(() => {
    const rect = selector => { const value=document.querySelector(selector)?.getBoundingClientRect(); return value ? {x:value.x,width:value.width,right:value.right} : null };
    return {innerWidth, documentWidth:document.documentElement.scrollWidth, stage:rect('.ops-topbar'), content:rect('.ops-main'), view:rect('.ops-content'), chat:rect('.chat-workspace'), panel:rect('.conversation-panel'), composer:rect('.composer'), field:rect('.composer-field'), actions:rect('.composer-actions')};
  })()`)
  assert.equal(layoutMetrics.documentWidth, layoutMetrics.innerWidth, 'layout created horizontal document overflow')
  assert.ok(layoutMetrics.actions.right <= layoutMetrics.innerWidth, 'composer actions overflowed the viewport')
  let mobileNavigation = null
  if (viewportWidth < 900) {
    // Shell V3: sidebar colapsa automaticamente para modo ícone em telas estreitas
    mobileNavigation = await evaluate(`(() => {
      const rect = document.querySelector('.ops-sidebar').getBoundingClientRect();
      return { width: rect.width, collapsedByCss: rect.width <= 58 };
    })()`)
    assert.ok(mobileNavigation.collapsedByCss, `sidebar não colapsou em tela estreita: ${JSON.stringify(mobileNavigation)}`)
  }
  if (diagnosticOnly) {
    console.log(JSON.stringify({ ok: true, composer: composerMetrics, layout: layoutMetrics, mobileNavigation }, null, 2))
  } else {
  assert.ok(composerMetrics.clientHeight <= 144, `composer exceeded limit: ${composerMetrics.clientHeight}`)
  assert.ok(composerMetrics.scrollHeight > composerMetrics.clientHeight, 'composer did not create internal overflow')
  assert.equal(composerMetrics.overflowY, 'auto')
  assert.equal(composerMetrics.messageOverflow, 'auto')
  assert.ok(composerMetrics.pageHeight <= composerMetrics.viewportHeight + 1, 'document grew beyond viewport')

  await evaluate(`(() => {
    const textarea = document.querySelector('.composer textarea');
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
    setter.call(textarea, 'Nyra, responda apenas: interface validada.');
    textarea.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText'}));
    document.querySelector('.send-button').click();
  })()`)

  let messageCount = 0
  for (let attempt = 0; attempt < 120; attempt += 1) {
    messageCount = await evaluate(`document.querySelectorAll('.message').length`)
    if (messageCount >= 2) break
    await delay(500)
  }
  assert.ok(messageCount >= 2, `chat response did not arrive; messages=${messageCount}`)

  const sidebar = await evaluate(`(() => {
    const before = document.querySelector('.ops-sidebar').getBoundingClientRect().width;
    document.querySelector('.ops-collapse-btn').click();
    return new Promise(resolve => setTimeout(() => resolve({before, after:document.querySelector('.ops-sidebar').getBoundingClientRect().width}), 260));
  })()`)
  assert.ok(sidebar.before > 200 && sidebar.after < 100, `sidebar did not collapse: ${JSON.stringify(sidebar)}`)

  console.log(JSON.stringify({ ok: true, composer: composerMetrics, layout: layoutMetrics, messageCount, sidebar }, null, 2))
  }
  await cdp('Browser.close').catch(() => undefined)
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close()
  if (!browser.killed) browser.kill()
}
