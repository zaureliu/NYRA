// Smoke real da Operations UI V3 (shell sem backend): navegação + estados de erro honestos.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import path from 'node:path'

const edge = process.env.KAZUMI_EDGE_PATH || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const cdpPort = process.argv[2] || '9361'
const profile = path.resolve(process.cwd(), '..', '.tmp', `edge-ops-smoke-${Date.now()}`)
const browser = spawn(edge, [
  '--headless=new', '--disable-gpu', '--hide-scrollbars', `--remote-debugging-port=${cdpPort}`,
  `--user-data-dir=${profile}`, '--window-size=1366,768', 'about:blank',
], { stdio: 'ignore', windowsHide: true })

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
let socket
try {
  let page
  for (let attempt = 0; attempt < 100 && !page; attempt += 1) {
    try {
      const pages = await fetch(`http://127.0.0.1:${cdpPort}/json/list`).then((r) => r.json())
      page = pages.find((item) => item.type === 'page')
    } catch { /* subindo */ }
    if (!page) await delay(150)
  }
  assert.ok(page, 'Edge DevTools não ficou pronto')
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
    const result = await cdp('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text ?? 'evaluate falhou')
    return result.result.value
  }

  await cdp('Page.enable')
  // Navegação explícita (evita corrida com o dev server subindo).
  await cdp('Page.navigate', { url: 'http://127.0.0.1:5173/#overview' })
  await delay(1000)
  // Vite transforma módulos no primeiro acesso: aguarda o shell montar de verdade.
  let overviewState = null
  let lastDebug = ''
  for (let attempt = 0; attempt < 60 && !overviewState?.shell; attempt += 1) {
    await delay(500)
    overviewState = await evaluate(`(() => ({
      shell: Boolean(document.querySelector('.ops-shell')),
      topChips: document.querySelectorAll('.ops-topbar-chips .ops-chip').length,
      navItems: document.querySelectorAll('.ops-sidebar .ops-nav-item').length,
      activeNav: document.querySelector('.ops-nav-item.active')?.textContent?.trim() ?? '',
      fontFloorOk: [...document.querySelectorAll('.ops-content *')].every((el) => {
        const size = parseFloat(getComputedStyle(el).fontSize)
        return !Number.isFinite(size) || size >= 10.5
      }),
    }))()`)
    lastDebug = await evaluate(`(() => JSON.stringify({
      bodyLen: document.body?.innerHTML?.length ?? -1,
      rootLen: document.getElementById('root')?.innerHTML?.length ?? -1,
      overlay: Boolean(document.querySelector('vite-error-overlay')),
      url: location.href,
    }))()`)
  }
  assert.ok(overviewState.shell, `shell V3 não montou após polling: ${lastDebug}`)
  assert.ok(overviewState.topChips >= 5, `top bar com poucos chips: ${overviewState.topChips}`)
  assert.equal(overviewState.navItems, 13, `esperava 13 itens de navegação`)
  assert.ok(overviewState.activeNav.includes('Visão geral'), `view ativa errada: ${overviewState.activeNav}`)
  assert.ok(overviewState.fontFloorOk, 'fonte abaixo do piso legível')

  // Navegar por todas as páginas via cliques reais e coletar títulos
  const views = ['Conversa', 'Capabilities', 'Autonomia', 'Tarefas', 'Homelab', 'Rede',
    'Integrações', 'Sentinel', 'Voz', 'Configurações', 'Developer', 'Sobre']
  const visited = []
  for (const label of views) {
    const clicked = await evaluate(`(() => {
      const item = [...document.querySelectorAll('.ops-nav-item')].find((el) => el.textContent.trim().includes('${label}'));
      if (!item) return false; item.click(); return true;
    })()`)
    assert.ok(clicked, `não achei item de menu: ${label}`)
    await delay(420)
    const title = await evaluate(`(document.querySelector('.ops-page-title')?.textContent?.trim()) ?? (document.querySelector('.chat-workspace') ? '(chat)' : '')`)
    visited.push({ label, title: String(title) })
  }
  const missingTitles = visited.filter((v) => v.label !== 'Conversa' && !v.title)
  assert.deepEqual(missingTitles, [], `páginas sem título: ${JSON.stringify(missingTitles)}`)

  // Capabilities deve mostrar estado honesto com backend offline (alerta, não crash)
  await evaluate(`[...document.querySelectorAll('.ops-nav-item')].find((el) => el.textContent.trim().includes('Capabilities')).click()`)
  let capsState = null
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await delay(400)
    capsState = await evaluate(`(() => ({
      ready: Boolean(document.querySelector('.ops-alert.error') || document.querySelector('.ops-card')),
      loading: Boolean(document.querySelector('.ops-loading')),
      title: document.querySelector('.ops-page-title')?.textContent?.trim() || '',
      text: document.querySelector('main')?.textContent?.trim().slice(0, 180) || '',
    }))()`)
    if (capsState.ready) break
  }
  assert.ok(capsState?.ready, `Capabilities não exibiu cards/erro no prazo: ${JSON.stringify(capsState)}`)

  // Colapsar sidebar
  const collapse = await evaluate(`(async () => {
    const before = document.querySelector('.ops-sidebar').getBoundingClientRect().width;
    document.querySelector('.ops-collapse-btn').click();
    await new Promise(r => setTimeout(r, 300));
    const after = document.querySelector('.ops-sidebar').getBoundingClientRect().width;
    return { before, after };
  })()`)
  assert.ok(collapse.before > 180 && collapse.after < 100, `sidebar não colapsou: ${JSON.stringify(collapse)}`)

  console.log(JSON.stringify({ ok: true, overviewState, visited }, null, 2))
  await cdp('Browser.close').catch(() => undefined)
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close()
  if (!browser.killed) browser.kill()
}
