import { useEffect, useState } from 'react'
import { apiSend } from '../../runtime/api'
import { usePolling } from '../hooks'
import { ActionButton, Card, ErrorAlert, KeyValue, StatusBadge } from '../ui'

interface OllamaModel {
  name: string
  size?: number | null
  family?: string | null
  parameter_size?: string | null
  quantization_level?: string | null
  modified_at?: string | null
  digest?: string | null
  loaded?: boolean
  official?: boolean
  active?: boolean
}

interface Inventory {
  ollama_ready?: boolean
  ollama_state?: string
  active_model?: string | null
  official_model?: string
  configured_model_not_installed?: boolean
  resident_models?: string[]
  residency_known?: boolean
  inventory_error_code?: string | null
  residency_error_code?: string | null
  models?: OllamaModel[]
}

interface BrainStatus {
  state: string
  ollama_ready?: boolean
  ollama_state?: string
  active_model?: string | null
  selected_model?: string
  resident_models?: string[]
  residency_known?: boolean
  error_code?: string | null
  last_error?: string | null
}

const STATE_LABEL: Record<string, string> = {
  MODEL_READY: 'READY',
  OLLAMA_OFFLINE: 'OFFLINE',
  NO_MODELS_INSTALLED: 'SEM MODELOS',
  MODEL_LOADING: 'CARREGANDO',
  MODEL_FAILED: 'FALHA',
  MODEL_AVAILABLE: 'DISPONIVEL',
  OLLAMA_ERROR: 'FALHA',
}

type OllamaUiState = 'READY' | 'OFFLINE' | 'ERROR' | 'UNKNOWN'

export function resolveOllamaState(
  inventory: Inventory | null,
  status: BrainStatus | null,
  models: OllamaModel[],
): OllamaUiState {
  const statusState = String(status?.state ?? '').toUpperCase()
  const inventoryState = String(inventory?.ollama_state ?? '').toUpperCase()
  const positiveState = ['MODEL_READY', 'MODEL_AVAILABLE', 'MODEL_LOADING', 'NO_MODELS_INSTALLED']
    .includes(statusState)
  if (status?.ollama_ready === true || positiveState) {
    return 'READY'
  }
  if (statusState === 'OLLAMA_OFFLINE' || status?.error_code === 'OLLAMA_OFFLINE'
      || (status?.ollama_ready === false && statusState !== 'OLLAMA_ERROR')) {
    return 'OFFLINE'
  }
  if (statusState === 'OLLAMA_ERROR' || Boolean(status?.error_code)) {
    return 'ERROR'
  }
  if (inventory?.ollama_ready === true || inventoryState === 'READY' || models.length > 0) return 'READY'
  if (inventoryState === 'OFFLINE'
      || (inventory?.ollama_ready === false && inventoryState !== 'ERROR')) return 'OFFLINE'
  if (inventoryState === 'ERROR' || Boolean(inventory?.inventory_error_code)) return 'ERROR'
  return 'UNKNOWN'
}

export function resolveActiveModel(
  inventory: Inventory | null,
  status: BrainStatus | null,
  models: OllamaModel[],
): string {
  const reported = status?.active_model || inventory?.active_model || ''
  const loaded = models.filter((model) => model.loaded === true).map((model) => model.name)
  const residencyKnown = status?.residency_known === true
    || inventory?.residency_known === true
    || models.some((model) => typeof model.loaded === 'boolean')
  if (!residencyKnown) return reported
  if (reported && loaded.includes(reported)) return reported
  return loaded[0] ?? ''
}

function formatBytes(value?: number | null): string {
  if (!value || value <= 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(1)} ${units[unit]}`
}

export function ModelSelectorCard() {
  const inventory = usePolling<Inventory>('/api/brain/models', 15000)
  const status = usePolling<BrainStatus>('/api/brain/status', 5000)
  const [selected, setSelected] = useState('')
  const [busyKey, setBusyKey] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const models = inventory.data?.models ?? []
  const official = inventory.data?.official_model ?? ''
  const ollamaState = resolveOllamaState(inventory.data, status.data, models)
  const activeModel = resolveActiveModel(inventory.data, status.data, models)
  // seleção efetiva: escolha explícita > oficial > ativa > primeira real
  const effectiveSelected =
    selected || official || status.data?.active_model || models[0]?.name || ''

  useEffect(() => {
    if (!selected && effectiveSelected) {
      setSelected(effectiveSelected)
    }
  }, [effectiveSelected, selected])

  const selectedModel = models.find((item) => item.name === effectiveSelected)

  const run = async (
    key: 'save' | 'load' | 'reset',
    path: string,
    body: unknown,
    timeoutMs: number,
    message: string,
  ) => {
    setBusyKey(key)
    setError('')
    setNotice('')
    try {
      await apiSend(path, 'POST', body, timeoutMs)
      setNotice(message)
      inventory.refresh()
      status.refresh()
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusyKey('')
    }
  }

  const busy = busyKey !== ''
  const effectiveModelState = status.data?.state
    ?? (activeModel ? 'MODEL_READY' : ollamaState === 'READY' ? 'MODEL_AVAILABLE' : ollamaState)
  const stateText = STATE_LABEL[effectiveModelState] ?? effectiveModelState ?? '—'
  const loading = status.data?.state === 'MODEL_LOADING' || busyKey === 'load'
  const statusErrorCode = status.data?.error_code
    ?? inventory.data?.inventory_error_code
    ?? inventory.data?.residency_error_code
  const fetchError = !inventory.data
    ? inventory.error
    : (!status.data && !activeModel ? status.error : '')

  return (
    <Card title="IA · Modelo LLM (Ollama)" sub="Descoberta real via /api/tags do Ollama local">
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 }}>
        <span>Ollama:</span>
        <StatusBadge
          state={ollamaState}
          label={ollamaState === 'ERROR' ? 'FALHA' : ollamaState === 'UNKNOWN' ? '—' : ollamaState}
        />
        <StatusBadge state={effectiveModelState} label={stateText} />
        <div style={{ flex: 1 }} />
        <ActionButton small onClick={() => { inventory.refresh(); status.refresh() }} disabled={busy}>
          Atualizar modelos
        </ActionButton>
      </div>

      {ollamaState === 'OFFLINE' && (
        <ErrorAlert message="OLLAMA_OFFLINE — não foi possível falar com o Ollama local." hint="Confirme que o serviço está em pé e clique em Atualizar modelos." />
      )}
      {statusErrorCode && statusErrorCode !== 'OLLAMA_OFFLINE' && (
        <ErrorAlert message={`${statusErrorCode} — a API do Ollama respondeu, mas o estado não pôde ser interpretado.`} />
      )}
      {fetchError && ollamaState !== 'OFFLINE' && (
        <ErrorAlert message={`Falha ao consultar o estado de modelos: ${fetchError}`} />
      )}
      {inventory.data?.configured_model_not_installed && (
        <ErrorAlert message={`Configured model not installed (${official}).`} hint="Escolha outro modelo instalado abaixo e salve." />
      )}
      {!inventory.data && inventory.loading && (
        <div className="ops-empty"><span className="ops-loading" /> Consultando modelos instalados…</div>
      )}

      {models.length === 0 && ollamaState === 'READY' && !inventory.data?.inventory_error_code ? (
        <div className="ops-empty">NO_MODELS_INSTALLED — nenhum modelo encontrado no Ollama.</div>
      ) : (
        <>
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 10 }}>
            <KeyValue rows={[
              ['Modelo ativo', activeModel || '—'],
              ['Seleção salva', official || '—'],
            ]} />
          </div>

          <label style={{ display: 'block', fontSize: 12, opacity: 0.75, marginBottom: 4 }}>
            Modelo selecionado
          </label>
          <select
            value={effectiveSelected}
            disabled={busy || models.length === 0}
            onChange={(event) => { setSelected(event.target.value); setNotice('') }}
            style={{ width: '100%', maxWidth: 420, background: 'var(--ops-bg-2)', border: '1px solid var(--ops-line-strong)', color: 'var(--ops-text)', borderRadius: 6, height: 32, fontSize: 13 }}
          >
            {models.map((model) => (
              <option key={model.name} value={model.name}>
                {model.name}{model.loaded ? ' · carregado' : ''}{model.official ? ' · oficial' : ''}
              </option>
            ))}
          </select>

          {selectedModel && (
            <div style={{ marginTop: 10 }}>
              <KeyValue rows={[
                ['Tamanho', formatBytes(selectedModel.size)],
                ['Família', selectedModel.family ?? '—'],
                ['Parâmetros', selectedModel.parameter_size ?? '—'],
                ['Quantização', selectedModel.quantization_level ?? '—'],
                ['Digest', selectedModel.digest ?? '—'],
                ['Modificado em', selectedModel.modified_at ?? '—'],
              ]} />
            </div>
          )}

          {loading && <div className="ops-alert info" style={{ marginTop: 10 }}>Carregando… (cold load pode levar minutos neste host)</div>}
          {notice && <div className="ops-alert info" style={{ marginTop: 10 }}>{notice}</div>}
          <ErrorAlert message={error} />

          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 12 }}>
            <ActionButton small disabled={busy || !selectedModel} busy={busyKey === 'save'}
              onClick={() => void run('save', '/api/brain/select',
                { model: selected || selectedModel?.name || '', confirmed: true }, 20000,
                `Seleção salva: ${selected || selectedModel?.name}. Use Carregar modelo para aplicar agora.`)}>
              Salvar seleção
            </ActionButton>
            <ActionButton small variant="primary" disabled={busy || !selectedModel} busy={busyKey === 'load'}
              onClick={() => void run('load', '/api/brain/model/load',
                { model: selected || selectedModel?.name || '' }, 480000,
                `Modelo carregado, warm-up concluído e residente no Ollama.`)}>
              Carregar modelo
            </ActionButton>
            <ActionButton small disabled={busy} busy={busyKey === 'reset'}
              onClick={() => {
                if (!window.confirm('Restaurar o padrão oficial qwen3:8b (salvar + carregar)?')) return
                void run('reset', '/api/brain/reset-default', undefined, 480000,
                  'Padrão qwen3:8b restaurado, carregado e confirmado.')
              }}>
              Restaurar padrão
            </ActionButton>
          </div>
          {status.data?.last_error && (
            <div className="ops-hint" style={{ marginTop: 8 }}>Último erro do warm manager: {status.data.last_error}</div>
          )}
        </>
      )}
    </Card>
  )
}
