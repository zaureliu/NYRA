import { NetworkWatchPanel } from '../../components/NetworkWatchPanel'
import { NetworkWatchSettings } from '../../components/NetworkWatchSettings'

export function NetworkPage() {
  return <div className="network-page">
    <header className="ops-page-header">
      <div><h1 className="ops-page-title">Rede</h1><p className="ops-page-subtitle">Saúde, qualidade, tráfego real da interface e eventos de conectividade em uma única visão.</p></div>
    </header>
    <NetworkWatchPanel />
    <h2 className="ops-section-title">Configuração</h2>
    <NetworkWatchSettings />
  </div>
}
