import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'

const edge = process.env.NYRA_EDGE_PATH || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const port = 9341
const projectRoot = path.resolve(process.cwd(), '..')
const outputDirectory = path.join(projectRoot, 'docs', 'screenshots', 'avatar-v2')
const profile = path.join(projectRoot, '.tmp', `edge-avatar-v2-${Date.now()}`)
await mkdir(outputDirectory, { recursive: true })

const browser = spawn(edge, [
  '--headless=new', '--disable-gpu', '--hide-scrollbars', '--disable-background-timer-throttling',
  '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows', `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`, '--window-size=1920,1080', 'http://127.0.0.1:5173/#dashboard',
], { stdio: ['ignore', 'inherit', 'inherit'], windowsHide: true })

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds))
const getPage = async () => {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const pages = await fetch(`http://127.0.0.1:${port}/json/list`).then((response) => response.json())
      const page = pages.find((item) => item.type === 'page' && item.url.includes('127.0.0.1:5173'))
      if (page) return page
    } catch { /* browser is starting */ }
    await delay(100)
  }
  throw new Error('Edge DevTools endpoint did not become ready')
}

let socket
try {
  const page = await getPage()
  socket = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true })
    socket.addEventListener('error', reject, { once: true })
  })
  let requestId = 0
  const pending = new Map()
  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data)
    if (!message.id || !pending.has(message.id)) return
    const { resolve, reject } = pending.get(message.id)
    pending.delete(message.id)
    if (message.error) reject(new Error(message.error.message)); else resolve(message.result)
  })
  socket.addEventListener('close', () => {
    for (const { reject } of pending.values()) reject(new Error('Edge DevTools socket closed before the command completed'))
    pending.clear()
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
  await evaluate(`new Promise(resolve => document.readyState === 'complete' ? setTimeout(resolve, 700) : addEventListener('load', () => setTimeout(resolve, 700), {once:true}))`)
  await evaluate(`new Promise((resolve, reject) => {
    const started = performance.now();
    const check = () => {
      if (document.querySelector('.nyra-avatar-v2[data-pack="nyra_v2"]')) return resolve(true);
      if (performance.now() - started > 7000) return reject(new Error('NYRA Avatar V2 did not render'));
      setTimeout(check, 50);
    };
    check();
  })`)

  const setViewport = async (width, height) => {
    await cdp('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false })
    await evaluate(`new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))`)
  }
  const setAvatar = async ({ status, eye, mouth }) => evaluate(`(() => {
    const avatar = document.querySelector('.nyra-avatar-v2');
    if (!avatar) throw new Error('Avatar V2 root missing');
    avatar.dataset.status = ${JSON.stringify(status)};
    avatar.dataset.eye = ${JSON.stringify(eye)};
    avatar.dataset.mouth = ${JSON.stringify(mouth)};
    const label = ${JSON.stringify(status)}.toUpperCase();
    const readout = document.querySelector('.avatar-readout');
    const readoutStatus = readout?.querySelector('strong');
    const readoutDot = readout?.querySelector('.activity-dot');
    const contextBadge = document.querySelector('.context-badge');
    if (readoutStatus) readoutStatus.textContent = label;
    if (readoutDot) readoutDot.className = 'activity-dot status-' + ${JSON.stringify(status)};
    if (contextBadge) contextBadge.textContent = 'LOCAL · ' + label;
    avatar.scrollIntoView({block:'center', inline:'nearest'});
    return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  })()`)
  const captureAvatar = async (filename) => {
    const clip = await evaluate(`(() => {
      const rect = document.querySelector('.avatar-panel').getBoundingClientRect();
      return {x: Math.max(0, rect.x), y: Math.max(0, rect.y), width: rect.width, height: rect.height, scale: 1};
    })()`)
    const screenshot = await cdp('Page.captureScreenshot', { format: 'png', fromSurface: true, captureBeyondViewport: true, clip })
    await writeFile(path.join(outputDirectory, filename), Buffer.from(screenshot.data, 'base64'))
  }
  const captureViewport = async (filename) => {
    const screenshot = await cdp('Page.captureScreenshot', { format: 'png', fromSurface: true, captureBeyondViewport: false })
    await writeFile(path.join(outputDirectory, filename), Buffer.from(screenshot.data, 'base64'))
  }
  const measure = () => evaluate(`(() => {
    const avatar = document.querySelector('.nyra-avatar-v2');
    const canvas = avatar.querySelector('.nyra-v2-canvas');
    const layers = [...avatar.querySelectorAll('.nyra-v2-master, .nyra-v2-gaze-base, .nyra-v2-eye-layer, .nyra-v2-mouth-layer')];
    const rect = canvas.getBoundingClientRect();
    return {
      innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      viewBox: canvas.getAttribute('viewBox'),
      preserveAspectRatio: canvas.getAttribute('preserveAspectRatio'),
      renderer: avatar.dataset.renderer,
      pack: avatar.dataset.pack,
      rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
      layerSizes: layers.map(layer => [Number(layer.getAttribute('width')), Number(layer.getAttribute('height'))]),
      headphonesParent: avatar.querySelector('[data-layer="headphones"]')?.parentElement?.dataset.layer,
    };
  })()`)

  const layouts = []
  for (const viewport of [[1920, 1080], [1366, 768], [768, 1024], [390, 844]]) {
    await setViewport(...viewport)
    const metrics = await measure()
    assert.equal(metrics.documentWidth, metrics.innerWidth, `horizontal overflow at ${viewport[0]}px`)
    assert.equal(metrics.viewBox, '0 0 1086 1448')
    assert.equal(metrics.preserveAspectRatio, 'xMidYMid meet')
    assert.equal(metrics.renderer, 'unified-svg-layers')
    assert.equal(metrics.pack, 'nyra_v2')
    assert.equal(metrics.headphonesParent, 'head')
    assert.ok(metrics.rect.width > 0 && metrics.rect.height > 0, `avatar hidden at ${viewport[0]}px`)
    assert.ok(metrics.layerSizes.every(([width, height]) => width === 1086 && height === 1448), `layer canvas diverged at ${viewport[0]}px`)
    layouts.push({ viewport, ...metrics })
  }

  await setViewport(1440, 1000)
  const captures = [
    ['nyra-avatar-v2-idle.png', 'idle', 'open', 'mouth_closed'],
    ['nyra-avatar-v2-blink-75.png', 'idle', 'seventy_five', 'mouth_closed'],
    ['nyra-avatar-v2-blink-half.png', 'idle', 'half', 'mouth_closed'],
    ['nyra-avatar-v2-blink-25.png', 'idle', 'twenty_five', 'mouth_closed'],
    ['nyra-avatar-v2-blink-closed.png', 'idle', 'closed', 'mouth_closed'],
    ['nyra-avatar-v2-listening.png', 'listening', 'open', 'mouth_closed'],
    ['nyra-avatar-v2-thinking.png', 'thinking', 'open', 'mouth_closed'],
    ['nyra-avatar-v2-speaking-small.png', 'speaking', 'open', 'mouth_small'],
    ['nyra-avatar-v2-speaking-medium.png', 'speaking', 'open', 'mouth_medium'],
    ['nyra-avatar-v2-speaking-open.png', 'speaking', 'open', 'mouth_open'],
    ['nyra-avatar-v2-speaking-wide.png', 'speaking', 'open', 'mouth_wide'],
    ['nyra-avatar-v2-smile.png', 'idle', 'open', 'mouth_smile'],
    ['nyra-avatar-v2-speaking-smile.png', 'speaking', 'open', 'mouth_speaking_smile'],
  ]
  for (const [filename, status, eye, mouth] of captures) {
    await setAvatar({ status, eye, mouth })
    await captureAvatar(filename)
  }

  await setAvatar({ status: 'idle', eye: 'open', mouth: 'mouth_closed' })
  const gazePositions = [
    ['front', 720, 500],
    ['left_light', 450, 500], ['left', 30, 500],
    ['right_light', 990, 500], ['right', 1410, 500],
    ['up_light', 720, 330], ['up', 720, 30],
    ['down_light', 720, 670], ['down', 720, 970],
    ['up_left', 30, 30], ['up_right', 1410, 30],
    ['down_left', 30, 970], ['down_right', 1410, 970],
  ]
  const gazeCaptures = []
  for (const [direction, x, y] of gazePositions) {
    const observed = await evaluate(`new Promise(resolve => {
      const move = () => window.dispatchEvent(new PointerEvent('pointermove', {clientX:${x}, clientY:${y}, pointerType:'mouse'}));
      move();
      setTimeout(() => resolve(document.querySelector('.nyra-avatar-v2')?.dataset.pointerTarget), 80);
    })`)
    assert.equal(observed, direction, `web mouse follow classified ${x},${y} as ${observed}`)
    await evaluate(`(() => {
      const vectors = ${JSON.stringify({
        front: {x:0,y:0}, left_light:{x:-.38,y:0}, left:{x:-.82,y:0}, right_light:{x:.38,y:0}, right:{x:.82,y:0},
        up_light:{x:0,y:-.34}, up:{x:0,y:-.74}, down_light:{x:0,y:.34}, down:{x:0,y:.74},
        up_left:{x:-.62,y:-.52}, up_right:{x:.62,y:-.52}, down_left:{x:-.62,y:.52}, down_right:{x:.62,y:.52},
      })};
      const avatar = document.querySelector('.nyra-avatar-v2');
      const vector = vectors[${JSON.stringify(direction)}];
      avatar.style.setProperty('--nyra-eye-follow-x', (vector.x * 13) + 'px');
      avatar.style.setProperty('--nyra-eye-follow-y', (vector.y * 9) + 'px');
      avatar.style.setProperty('--nyra-pointer-head-x', (vector.x * 7) + 'px');
      avatar.style.setProperty('--nyra-pointer-head-y', (vector.y * 4) + 'px');
      avatar.style.setProperty('--nyra-pointer-head-tilt', (vector.x * .75) + 'deg');
      avatar.dataset.gaze = ${JSON.stringify(direction)};
    })()`)
    const filename = `nyra-avatar-v2-look-${direction.replaceAll('_', '-')}.png`
    await captureAvatar(filename)
    gazeCaptures.push(filename)
  }

  await setViewport(390, 844)
  await setAvatar({ status: 'idle', eye: 'open', mouth: 'mouth_closed' })
  await captureViewport('nyra-avatar-v2-mobile.png')

  console.log(JSON.stringify({ ok: true, outputDirectory, layouts, captures: captures.map(([filename]) => filename).concat(gazeCaptures, 'nyra-avatar-v2-mobile.png') }, null, 2))
  await cdp('Browser.close').catch(() => undefined)
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close()
  if (!browser.killed) browser.kill()
}
