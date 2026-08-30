export type OpsView =
  | 'overview'
  | 'conversation'
  | 'capabilities'
  | 'autonomy'
  | 'tasks'
  | 'homelab'
  | 'network'
  | 'integrations'
  | 'sentinel'
  | 'voice'
  | 'usb'
  | 'settings'
  | 'developer'
  | 'about'

export const OPS_VIEWS: readonly OpsView[] = [
  'overview', 'conversation', 'capabilities', 'autonomy', 'tasks',
  'homelab', 'network', 'integrations', 'sentinel', 'voice', 'usb',
  'settings', 'developer', 'about',
]

interface NavItem {
  view: OpsView
  label: string
  icon: string
}

interface NavGroup {
  label: string
  items: NavItem[]
}

export const NAV_GROUPS: NavGroup[] = [
  {
    label: 'Operação',
    items: [
      { view: 'overview', label: 'Visão geral', icon: 'OV' },
      { view: 'conversation', label: 'Conversa', icon: 'CV' },
      { view: 'capabilities', label: 'Capabilities', icon: 'CP' },
      { view: 'autonomy', label: 'Autonomia', icon: 'AT' },
      { view: 'tasks', label: 'Tarefas', icon: 'TK' },
    ],
  },
  {
    label: 'Infraestrutura',
    items: [
      { view: 'homelab', label: 'Homelab', icon: 'HL' },
      { view: 'network', label: 'Rede', icon: 'NW' },
      { view: 'integrations', label: 'Integrações', icon: 'IN' },
      { view: 'sentinel', label: 'Sentinel', icon: 'SN' },
    ],
  },
  {
    label: 'Sistema',
    items: [
      { view: 'voice', label: 'Voz', icon: 'VZ' },
      { view: 'usb', label: 'Dispositivos USB', icon: 'US' },
      { view: 'settings', label: 'Configurações', icon: 'CF' },
      { view: 'developer', label: 'Developer', icon: 'DV' },
      { view: 'about', label: 'Sobre', icon: 'AB' },
    ],
  },
]

export function Sidebar({ active, collapsed, badges, onNavigate, onToggleCollapse }: {
  active: OpsView
  collapsed: boolean
  badges?: Partial<Record<OpsView, number>>
  onNavigate: (view: OpsView) => void
  onToggleCollapse: () => void
}) {
  return (
    <nav className={`ops-sidebar${collapsed ? ' collapsed' : ''}`} aria-label="Navegação principal">
      <div className="ops-nav-groups">
        {NAV_GROUPS.map((group) => (
          <div key={group.label}>
            <div className="ops-nav-group-label">{group.label}</div>
            {group.items.map((item) => (
              <button
                key={item.view}
                type="button"
                className={`ops-nav-item${active === item.view ? ' active' : ''}`}
                title={collapsed ? item.label : undefined}
                aria-current={active === item.view ? 'page' : undefined}
                onClick={() => onNavigate(item.view)}
              >
                <span className="ops-nav-icon" aria-hidden="true">{item.icon}</span>
                {!collapsed && <span className="nav-text">{item.label}</span>}
                {!collapsed && badges?.[item.view] ? (
                  <span className="ops-nav-badge">{badges[item.view]}</span>
                ) : null}
              </button>
            ))}
          </div>
        ))}
      </div>
      <div className="ops-sidebar-footer">
        <button type="button" className="ops-collapse-btn" onClick={onToggleCollapse}
          title={collapsed ? 'Expandir menu' : 'Colapsar menu'}>
          {collapsed ? '»' : '« Colapsar'}
        </button>
      </div>
    </nav>
  )
}
