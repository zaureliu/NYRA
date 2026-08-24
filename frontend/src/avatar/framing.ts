import type { AvatarManifest, AvatarRendererProps, CharacterView } from './AvatarRenderer'

export function resolveAvatarFraming(
  manifest: AvatarManifest,
  variant: AvatarRendererProps['variant'],
  characterView: CharacterView = 'bust',
) {
  const id = variant === 'dashboard' ? 'portrait' : characterView
  const config = manifest.framing.variants[id]
  return { id, config, source: manifest.assets[config.asset] }
}
