import { NetworkWatchPanel } from '../../components/NetworkWatchPanel'
import { NetworkWatchSettings } from '../../components/NetworkWatchSettings'

export function NetworkPage() {
  return (
    <div>
      <header className="ops-page-header">
        <div>
          <h1 className="ops-page-title">Rede</h1>
          <p className="ops-page-subtitle">
            Latência, jitter e perda de pacotes medidos localmente. Alertas respeitam modo silencioso.
          </p>
        </div>
      </header>

      <NetworkWatchPanel />
      <h2 className="ops-section-title">Configuração</h2>
      <NetworkWatchSettings />
    </div>
  )
}
