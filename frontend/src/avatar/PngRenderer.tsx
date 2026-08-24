import type { AvatarManifest, AvatarRendererProps } from './AvatarRenderer'
import { resolveAvatarFraming } from './framing'

export function PngRenderer({ manifest, variant = 'dashboard', characterView = 'bust', className, state, status }: AvatarRendererProps & { manifest: AvatarManifest }) {
  const framing = resolveAvatarFraming(manifest, variant, characterView)
  return <span className={`nyra-avatar nyra-avatar-${variant} nyra-avatar-view-${framing.id} ${className ?? ''}`} data-state={state} data-status={status.toLowerCase()} data-renderer="png">
    <img className="nyra-avatar-base" src={framing.source} alt={`NYRA ${state}`} draggable={false} />
  </span>
}
