import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const polling = vi.hoisted(() => ({
  states: new Map<string, { data: unknown; loading: boolean; error: string; refresh: () => void }>(),
}))

vi.mock('../hooks', () => ({
  usePolling: (path: string) => polling.states.get(path) ?? {
    data: null,
    loading: true,
    error: '',
    refresh: () => {},
  },
}))

import { ModelSelectorCard } from './ModelSelectorCard'

function setPolling(path: '/api/brain/models' | '/api/brain/status', data: unknown, error = '') {
  polling.states.set(path, { data, loading: false, error, refresh: () => {} })
}

describe('ModelSelectorCard (Configurações → IA)', () => {
  beforeEach(() => {
    polling.states.clear()
  })

  it('estado inicial consulta sem declarar Ollama offline', () => {
    const html = renderToStaticMarkup(<ModelSelectorCard />)
    expect(html).toContain('IA · Modelo LLM')
    expect(html).toContain('Atualizar modelos')
    expect(html).not.toContain('OLLAMA_OFFLINE')
    expect(html).not.toContain('undefined')
  })

  it('Ollama READY com modelo residente mostra o nome ativo real', () => {
    setPolling('/api/brain/models', {
      ollama_ready: true,
      ollama_state: 'READY',
      active_model: 'qwen3.5:9b',
      official_model: 'qwen3.5:9b',
      residency_known: true,
      models: [{ name: 'qwen3.5:9b', loaded: true, official: true }],
    })
    setPolling('/api/brain/status', {
      state: 'MODEL_READY',
      ollama_ready: true,
      active_model: 'qwen3.5:9b',
      residency_known: true,
    })

    const html = renderToStaticMarkup(<ModelSelectorCard />)
    expect(html).toContain('Ollama:')
    expect(html).toContain('READY')
    expect(html).toContain('Modelo ativo</dt><dd>qwen3.5:9b')
    expect(html).not.toContain('OLLAMA_OFFLINE')
  })

  it('Ollama READY sem modelo residente mantém o ativo vazio sem inventar seleção', () => {
    setPolling('/api/brain/models', {
      ollama_ready: true,
      ollama_state: 'READY',
      active_model: null,
      official_model: 'qwen3.5:9b',
      residency_known: true,
      models: [{ name: 'qwen3.5:9b', loaded: false, official: true }],
    })
    setPolling('/api/brain/status', {
      state: 'MODEL_AVAILABLE',
      ollama_ready: true,
      active_model: null,
      residency_known: true,
    })

    const html = renderToStaticMarkup(<ModelSelectorCard />)
    expect(html).toContain('READY')
    expect(html).toContain('Modelo ativo</dt><dd>—')
    expect(html).not.toContain('OLLAMA_OFFLINE')
  })

  it('Ollama realmente offline mostra estado claro e não lista modelos inventados', () => {
    setPolling('/api/brain/models', {
      ollama_ready: false,
      ollama_state: 'OFFLINE',
      active_model: null,
      official_model: 'qwen3.5:9b',
      residency_known: false,
      models: [],
    })
    setPolling('/api/brain/status', {
      state: 'OLLAMA_OFFLINE',
      ollama_ready: false,
      active_model: null,
      error_code: 'OLLAMA_OFFLINE',
    })

    const html = renderToStaticMarkup(<ModelSelectorCard />)
    expect(html).toContain('OFFLINE')
    expect(html).toContain('OLLAMA_OFFLINE')
    expect(html).not.toContain('<option')
  })

  it('lista somente modelos reais e mostra todos os metadados disponíveis', () => {
    setPolling('/api/brain/models', {
      ollama_ready: true,
      ollama_state: 'READY',
      active_model: 'qwen3.5:9b',
      official_model: 'qwen3.5:9b',
      residency_known: true,
      configured_model_not_installed: false,
      models: [
        {
          name: 'qwen3.5:9b',
          size: 6_594_474_711,
          family: 'qwen35',
          parameter_size: '9.7B',
          quantization_level: 'Q4_K_M',
          digest: '6488c96fa5faab64',
          modified_at: '2026-08-21T03:04:26Z',
          loaded: true,
          official: true,
        },
        { name: 'llama3.2:3b', loaded: false },
      ],
    })
    setPolling('/api/brain/status', {
      state: 'MODEL_READY', ollama_ready: true, active_model: 'qwen3.5:9b', residency_known: true,
    })

    const html = renderToStaticMarkup(<ModelSelectorCard />)
    expect(html).toContain('qwen3.5:9b · carregado · oficial')
    expect(html).toContain('llama3.2:3b')
    expect(html).toContain('Q4_K_M')
    expect(html).toContain('6488c96fa5faab64')
    expect(html).toContain('2026-08-21T03:04:26Z')
    expect(html).toContain('GB')
  })

  it('resposta de inventário válida nunca vira OFFLINE por schema antigo ou falha do status', () => {
    setPolling('/api/brain/models', {
      active_model: 'qwen3.5:9b',
      official_model: 'qwen3.5:9b',
      models: [{ name: 'qwen3.5:9b', loaded: true, official: true }],
    })
    setPolling('/api/brain/status', null, 'Not Found')

    const html = renderToStaticMarkup(<ModelSelectorCard />)
    expect(html).toContain('READY')
    expect(html).toContain('Modelo ativo</dt><dd>qwen3.5:9b')
    expect(html).not.toContain('OLLAMA_OFFLINE')
    expect(html).not.toContain('Not Found')
  })

  it('configured model not installed aparece como aviso acionável', () => {
    setPolling('/api/brain/models', {
      ollama_ready: true,
      models: [{ name: 'llama3.2:3b' }],
      official_model: 'phi4:14b',
      configured_model_not_installed: true,
    })
    setPolling('/api/brain/status', { state: 'MODEL_AVAILABLE', ollama_ready: true })
    const html = renderToStaticMarkup(<ModelSelectorCard />)
    expect(html).toContain('Configured model not installed')
    expect(html).toContain('phi4:14b')
  })
})
