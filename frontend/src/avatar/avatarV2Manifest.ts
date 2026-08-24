import type { EyeState } from '../types'
import type { AvatarExpression, BlinkFrame, GazeDirection } from './avatarState'

export interface AvatarPoint { x: number; y: number }
export interface AvatarBounds extends AvatarPoint { width: number; height: number }

export interface NyraAvatarV2Manifest {
  version: string
  pack: 'nyra_v2'
  renderer: 'unified-svg-layers'
  canvas: { width: number; height: number; viewBox: string; preserveAspectRatio: 'xMidYMid meet' }
  characterRoot: { origin: AvatarPoint; anchor: AvatarPoint }
  body: { bounds: AvatarBounds; breathingOrigin: AvatarPoint }
  head: { center: AvatarPoint; bounds: AvatarBounds; transformOrigin: AvatarPoint }
  face: { bounds: AvatarBounds; skin: { highlight: string; mid: string; shadow: string } }
  leftEye: { center: AvatarPoint; bounds: AvatarBounds; anchor: AvatarPoint }
  rightEye: { center: AvatarPoint; bounds: AvatarBounds; anchor: AvatarPoint }
  mouth: { center: AvatarPoint; bounds: AvatarBounds; anchor: AvatarPoint }
  headphones: {
    group: 'head'
    headbandBounds: AvatarBounds
    leftEarcup: { center: AvatarPoint; bounds: AvatarBounds }
    rightEarcup: { center: AvatarPoint; bounds: AvatarBounds }
    maxIndicatorScale: number
  }
  assets: {
    master: string
    eyes: Record<'open' | 'seventy_five' | 'half' | 'twenty_five' | 'gaze_base' | 'closed', string>
    mouth: Record<'closed' | 'small' | 'medium' | 'open' | 'wide' | 'smile' | 'speaking_smile', string>
    fallback: string
  }
  gaze: {
    deadZone: number
    returnDelayMs: number
    smoothing: number
    limits: { eyeX: number; eyeY: number; headX: number; headY: number; headTilt: number }
    directions: Record<GazeDirection, AvatarPoint>
  }
  blink: {
    sequence: BlinkFrame[]
    frameDurationsMs: number[]
    intervalMs: { minimum: number; maximum: number }
  }
  states: Record<string, { eyes: EyeState; expression: AvatarExpression; gaze: string; headphones: string; motion: string }>
}

let manifestPromise: Promise<NyraAvatarV2Manifest> | undefined
const preloadedAssets = new Set<string>()

function insideCanvas(point: AvatarPoint, manifest: NyraAvatarV2Manifest) {
  return point.x >= 0 && point.x <= manifest.canvas.width && point.y >= 0 && point.y <= manifest.canvas.height
}

export function validateNyraAvatarV2Manifest(value: unknown): NyraAvatarV2Manifest {
  if (!value || typeof value !== 'object') throw new Error('Manifest NYRA Avatar V2 inválido')
  const manifest = value as NyraAvatarV2Manifest
  const expectedViewBox = `0 0 ${manifest.canvas?.width} ${manifest.canvas?.height}`
  if (!manifest.version?.startsWith('2.') || manifest.pack !== 'nyra_v2') throw new Error('Pack NYRA Avatar V2 incompatível')
  if (manifest.renderer !== 'unified-svg-layers') throw new Error('Renderer V2 incompatível')
  if (manifest.canvas?.width <= 0 || manifest.canvas?.height <= 0 || manifest.canvas.viewBox !== expectedViewBox) throw new Error('Canvas V2 inconsistente')
  if (!manifest.assets?.master || !manifest.assets.eyes?.seventy_five || !manifest.assets.eyes?.half || !manifest.assets.eyes?.twenty_five || !manifest.assets.eyes?.gaze_base || !manifest.assets.eyes?.closed || !manifest.assets.mouth?.open || !manifest.assets.mouth?.wide || !manifest.assets.mouth?.smile || !manifest.assets.mouth?.speaking_smile) throw new Error('Asset V2 essencial ausente')
  if (!insideCanvas(manifest.leftEye.anchor, manifest) || !insideCanvas(manifest.rightEye.anchor, manifest) || !insideCanvas(manifest.mouth.anchor, manifest)) throw new Error('Landmark V2 fora do canvas')
  if (manifest.leftEye.center.x !== manifest.leftEye.anchor.x || manifest.leftEye.center.y !== manifest.leftEye.anchor.y) throw new Error('Âncora do olho esquerdo divergiu')
  if (manifest.rightEye.center.x !== manifest.rightEye.anchor.x || manifest.rightEye.center.y !== manifest.rightEye.anchor.y) throw new Error('Âncora do olho direito divergiu')
  if (manifest.mouth.center.x !== manifest.mouth.anchor.x || manifest.mouth.center.y !== manifest.mouth.anchor.y) throw new Error('Âncora da boca divergiu')
  if (manifest.headphones.group !== 'head' || manifest.headphones.maxIndicatorScale > 1.02) throw new Error('Headphones V2 não estão ancorados ao head')
  if (manifest.blink.sequence.join(',') !== 'open,seventy_five,half,twenty_five,closed,twenty_five,half,seventy_five,open') throw new Error('Sequência de blink V2 inválida')
  if (manifest.blink.frameDurationsMs.length !== manifest.blink.sequence.length - 1) throw new Error('Duração de blink V2 inválida')
  if (!manifest.gaze || Object.keys(manifest.gaze.directions).length !== 13) throw new Error('Direções de olhar V2 incompletas')
  if (manifest.gaze.deadZone < 0 || manifest.gaze.deadZone > .35 || manifest.gaze.returnDelayMs < 500) throw new Error('Mouse follow V2 fora dos limites')
  return manifest
}

export function preloadNyraAvatarV2Assets(manifest: NyraAvatarV2Manifest) {
  if (typeof Image === 'undefined') return
  const assets = new Set([
    manifest.assets.master,
    ...Object.values(manifest.assets.eyes),
    ...Object.values(manifest.assets.mouth),
  ])
  for (const asset of assets) {
    if (preloadedAssets.has(asset)) continue
    preloadedAssets.add(asset)
    const image = new Image()
    image.decoding = 'async'
    image.src = asset
  }
}

export function loadNyraAvatarV2Manifest(fetcher: typeof fetch = fetch): Promise<NyraAvatarV2Manifest> {
  if (!manifestPromise || fetcher !== fetch) {
    manifestPromise = fetcher('/avatar/nyra_v2/avatar-manifest.json')
      .then((response) => {
        if (!response.ok) throw new Error(`Manifest NYRA Avatar V2 HTTP ${response.status}`)
        return response.json()
      })
      .then(validateNyraAvatarV2Manifest)
      .then((manifest) => { preloadNyraAvatarV2Assets(manifest); return manifest })
  }
  return manifestPromise
}
