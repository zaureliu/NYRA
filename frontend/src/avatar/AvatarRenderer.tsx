import { Component, type ErrorInfo, type ReactNode, useEffect, useState } from 'react'
import type { ActivityStatus, AvatarControl, EmotionalState, EyeState, MouthState } from '../types'
import { LayeredRenderer } from './LayeredRenderer'
import { NyraAvatarV2Renderer } from './NyraAvatarV2Renderer'
import { PngRenderer } from './PngRenderer'
import { SvgAvatarRenderer } from './SvgAvatarRenderer'
import { loadNyraAvatarV2Manifest, type NyraAvatarV2Manifest } from './avatarV2Manifest'
import './avatar.css'

export type AvatarRendererId = 'svg' | 'png' | 'layered' | 'future-live2d'
export type AvatarVariant = 'desktop' | 'dashboard'
export type AvatarPointerSource = 'web' | 'desktop-global'
export type CharacterView = 'bust' | 'full_body'

interface FramingVariant {
  asset: 'bust' | 'portrait' | 'full_body'
  fit: 'contain'
  anchor: { x: number; y: number }
  face: { eyeY: number; mouthY: number; linkY: number }
}

export interface AvatarManifest {
  version: string
  pack: string
  renderer: AvatarRendererId
  renderers: AvatarRendererId[]
  assets: { desktop: string; bust: string; portrait: string; full_body: string; symbol: string; fallback: string }
  expressions: Record<EmotionalState, string>
  eyes: Record<EyeState, string>
  mouths: Record<string, string>
  states: Record<string, { eye: EyeState; neural_link: string; motion: string }>
  scale: { default: number; minimum: number; maximum: number; steps: number[] }
  framing: { default: CharacterView; variants: { bust: FramingVariant; portrait: FramingVariant; full_body: FramingVariant } }
  fallback: { renderer: 'svg'; asset: string; on: string[] }
}

export interface AvatarRendererProps {
  state: EmotionalState
  status: ActivityStatus
  mouth: MouthState
  eye?: EyeState
  variant?: AvatarVariant
  characterView?: CharacterView
  renderer?: AvatarRendererId
  avatarVersion?: 'v2' | 'v3'
  className?: string
  idleAnimations?: boolean
  eyeMovement?: boolean
  blink?: boolean
  debug?: boolean
  control?: Partial<AvatarControl>
  pointerSource?: AvatarPointerSource
}

let manifestPromise: Promise<AvatarManifest> | undefined

export function validateAvatarManifest(value: unknown): AvatarManifest {
  if (!value || typeof value !== 'object') throw new Error('Manifest V3 inválido')
  const manifest = value as Partial<AvatarManifest>
  if (!manifest.version?.startsWith('3.') || manifest.pack !== 'nyra_v3') throw new Error('Avatar pack incompatível')
  if (!manifest.assets?.bust || !manifest.assets?.portrait || !manifest.assets?.full_body || !manifest.assets?.fallback) throw new Error('Asset essencial ausente no manifest')
  if (manifest.framing?.default !== 'bust' || !manifest.framing.variants?.bust || !manifest.framing.variants?.full_body) throw new Error('Framing V3.4 ausente no manifest')
  if (!manifest.renderers?.includes('layered') || !manifest.fallback) throw new Error('Renderer/fallback ausente no manifest')
  return manifest as AvatarManifest
}

export function loadAvatarManifest(fetcher: typeof fetch = fetch): Promise<AvatarManifest> {
  if (!manifestPromise || fetcher !== fetch) {
    manifestPromise = fetcher('/avatar/nyra_v3/manifest.json')
      .then((response) => {
        if (!response.ok) throw new Error(`Manifest HTTP ${response.status}`)
        return response.json()
      })
      .then(validateAvatarManifest)
  }
  return manifestPromise
}

class RendererBoundary extends Component<{ fallback: ReactNode; children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('NYRA avatar renderer fallback', error, info.componentStack) }
  render() { return this.state.failed ? this.props.fallback : this.props.children }
}

export function AvatarRenderer(props: AvatarRendererProps) {
  const [manifest, setManifest] = useState<AvatarManifest | null>(null)
  const [v2Manifest, setV2Manifest] = useState<NyraAvatarV2Manifest | null>(null)
  const [failedVersion, setFailedVersion] = useState<'v2' | 'v3' | null>(null)
  const fallback = <SvgAvatarRenderer {...props} />
  const avatarVersion = props.avatarVersion ?? 'v2'

  useEffect(() => {
    let active = true
    if (props.renderer === 'svg') return
    if (avatarVersion === 'v2') {
      loadNyraAvatarV2Manifest()
        .then((value) => { if (active) { setV2Manifest(value); setFailedVersion((current) => current === 'v2' ? null : current) } })
        .catch((error) => { console.error('NYRA Avatar V2 manifest fallback', error); if (active) setFailedVersion('v2') })
    } else {
      loadAvatarManifest()
        .then((value) => { if (active) { setManifest(value); setFailedVersion((current) => current === 'v3' ? null : current) } })
        .catch((error) => { console.error('NYRA V3 manifest fallback', error); if (active) setFailedVersion('v3') })
    }
    return () => { active = false }
  }, [avatarVersion, props.renderer])

  if (props.renderer === 'svg' || failedVersion === avatarVersion) return fallback
  if (avatarVersion === 'v2') {
    if (!v2Manifest) return <span className={`${props.className ?? ''} avatar-loading`} aria-label="Carregando NYRA Avatar V2" />
    return <RendererBoundary fallback={fallback}><NyraAvatarV2Renderer {...props} manifest={v2Manifest}/></RendererBoundary>
  }
  if (!manifest) return <span className={`${props.className ?? ''} avatar-loading`} aria-label="Carregando avatar NYRA" />

  const selected = props.renderer ?? manifest.renderer
  const rendered = selected === 'png'
    ? <PngRenderer {...props} manifest={manifest} />
    : selected === 'future-live2d'
      ? fallback
      : <LayeredRenderer {...props} manifest={manifest} />

  return <RendererBoundary fallback={fallback}>{rendered}</RendererBoundary>
}
