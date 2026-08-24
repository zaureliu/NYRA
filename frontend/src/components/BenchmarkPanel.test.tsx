import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { BenchmarkPanel } from './BenchmarkPanel'
import { candidateBadge, fmtBytes, startRunUserFeedback } from './benchmarkLogic'

describe('benchmarkLogic', () => {
  it('MODEL_NOT_INSTALLED gera mensagem clara sem sugerir download (§69/§70)', () => {
    const feedback = startRunUserFeedback({ error_code: 'MODEL_NOT_INSTALLED' }, 'qwen3:14b', 'perf')
    expect(feedback.error).toContain('NÃO instalado')
    expect(feedback.error).toContain('nada foi baixado')
    expect(feedback.notice).toBeUndefined()
  })

  it('run aceito produz notice com run_id', () => {
    const feedback = startRunUserFeedback({ run_id: 'bm_123' }, 'qwen3:8b', 'perf')
    expect(feedback.error).toBeUndefined()
    expect(feedback.notice).toContain('bm_123')
    expect(feedback.notice).toContain('O chat segue livre')
  })

  it('payload sem run_id vira erro genérico', () => {
    const feedback = startRunUserFeedback({}, 'qwen3:8b', 'quality')
    expect(feedback.error).toContain('Não foi possível')
  })

  it('fmtBytes formata VRAM/RAM e trata ausência', () => {
    expect(fmtBytes(null)).toBe('—')
    expect(fmtBytes(0)).toBe('—')
    expect(fmtBytes(6_186_378_198)).toMatch(/GB/)
    expect(fmtBytes(512 * 1024 * 1024)).toMatch(/MB/)
  })

  it('badge do candidato futuro é NOT INSTALLED quando ausente (§99)', () => {
    expect(candidateBadge({ installed: false })).toBe('NOT INSTALLED')
    expect(candidateBadge({ installed: true })).toBe('INSTALLED')
  })
})

describe('BenchmarkPanel render', () => {
  it('renderiza estrutura inicial sem quebrar (estado vazio, pré-fetch)', () => {
    const html = renderToStaticMarkup(<BenchmarkPanel/>)
    expect(html).toContain('Model Benchmark')
    expect(html).toContain('Rodar Performance')
    expect(html).toContain('Rodar Qualidade')
    expect(html).toContain('Rodar Completo')
    expect(html).toContain('model_id')
    expect(html).not.toContain('undefined')
  })
})
