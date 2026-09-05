import { useState } from 'react'

export type AppView =
  | 'dashboard'
  | 'chat'
  | 'pronunciation'
  | 'integrations'
  | 'benchmark'
  | 'settings'
  | 'developer'

interface NavigationItem { id: AppView; label: string; icon: string }
interface NavigationGroup { id: string; label: string; items: NavigationItem[] }

const GROUPS: NavigationGroup[] = [
  { id: 'core', label: 'Principal', items: [
    { id: 'dashboard', label: 'Visão geral', icon: 'OV' },
    { id: 'chat', label: 'Conversa', icon: 'CH' },
  ] },
  { id: 'voice', label: 'Voz', items: [
    { id: 'pronunciation', label: 'Pronúncia', icon: 'PR' },
    { id: 'settings', label: 'Áudio e conversa', icon: 'AU' },
  ] },
  { id: 'system', label: 'Sistema', items: [
    { id: 'integrations', label: 'Integrações', icon: 'IN' },
    { id: 'benchmark', label: 'Benchmark', icon: 'BM' },
    { id: 'developer', label: 'Developer', icon: 'DV' },
  ] },
]

interface Props {
  active: AppView
  collapsed: boolean
  mobileOpen: boolean
  onNavigate: (view: AppView) => void
  onCollapsed: (collapsed: boolean) => void
  onMobileClose: () => void
}

export function AppNavigation({ active, collapsed, mobileOpen, onNavigate, onCollapsed, onMobileClose }: Props) {
  const [closedGroups, setClosedGroups] = useState<Record<string, boolean>>({})
  const navigate = (view: AppView) => { onNavigate(view); onMobileClose() }

  return <>
    <button className={`nav-backdrop ${mobileOpen ? 'visible' : ''}`} aria-label="Fechar navegação" onClick={onMobileClose}/>
    <aside className={`app-sidebar ${collapsed ? 'is-collapsed' : ''} ${mobileOpen ? 'is-mobile-open' : ''}`}>
      <div className="sidebar-brand">
        <span className="brand-orbit" aria-hidden="true"><i/></span>
        <span className="brand-copy"><strong>KAZUMI</strong><small>NEURAL OPERATIONS</small></span>
        <button className="sidebar-collapse" onClick={() => onCollapsed(!collapsed)} aria-label={collapsed ? 'Expandir menu' : 'Recolher menu'} title={collapsed ? 'Expandir menu' : 'Recolher menu'}>{collapsed ? '›' : '‹'}</button>
      </div>

      <nav className="sidebar-nav" aria-label="Navegação principal">
        {GROUPS.map((group) => {
          const groupClosed = Boolean(closedGroups[group.id]) && !collapsed
          return <section className="nav-group" key={group.id}>
            <button className="nav-group-toggle" onClick={() => setClosedGroups((current) => ({ ...current, [group.id]: !current[group.id] }))} aria-expanded={!groupClosed} title={collapsed ? group.label : undefined}>
              <span>{group.label}</span><i>{groupClosed ? '+' : '−'}</i>
            </button>
            {!groupClosed && <div className="nav-items">
              {group.items.map((item) => <button key={item.id} className={`nav-item ${active === item.id ? 'active' : ''}`} onClick={() => navigate(item.id)} aria-current={active === item.id ? 'page' : undefined} title={collapsed ? item.label : undefined}>
                <span className="nav-icon" aria-hidden="true">{item.icon}</span><span className="nav-label">{item.label}</span>
              </button>)}
            </div>}
          </section>
        })}
      </nav>

      <div className="sidebar-foot"><span className="local-dot"/><span className="brand-copy"><strong>LOCAL-FIRST</strong><small>PRIVATE RUNTIME</small></span></div>
    </aside>
  </>
}
