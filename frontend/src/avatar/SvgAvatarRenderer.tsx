import type { AvatarRendererProps } from './AvatarRenderer'
export function SvgAvatarRenderer({ state, status, mouth, className }: AvatarRendererProps) {
  return <span className={`${className ?? ''} nyra-svg-fallback`} data-state={state} data-status={status.toLowerCase()} data-mouth={mouth} data-renderer="v2-static-fallback">
    <img className="nyra-avatar-base" src="/avatar/nyra_v2/master/nyra-avatar-master.png" alt={`NYRA ${state}`} draggable={false}/>
  </span>
}
