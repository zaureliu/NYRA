// Pure logic for the Model Benchmark panel (spec Parte AZ) — kept framework-free
// so tests follow the repo convention (no jsdom/testing-library dependency).

export type ProfilesOverview = {
  current_official_model?: string
  active_model?: string
  candidates?: { profile_id: string; label: string; installed: boolean; resolved_model?: string | null; display_state: string }[]
}

export type StartRunPayload = {
  run_id?: string
  error_code?: string
  model_id?: string
}

export type StartRunFeedback = { error?: string; notice?: string }

export function fmtBytes(value?: number | null): string {
  if (!value || value <= 0) return '—'
  const gb = value / (1024 * 1024 * 1024)
  return gb >= 1 ? `${gb.toFixed(2)} GB` : `${(value / (1024 * 1024)).toFixed(0)} MB`
}

export function startRunUserFeedback(
  payload: StartRunPayload,
  modelId: string,
  kind: string,
): StartRunFeedback {
  if (payload.error_code === 'MODEL_NOT_INSTALLED') {
    // Ausência é estado válido: mensagem clara, nenhum download automático (§69/§70).
    return { error: `${modelId}: modelo NÃO instalado (estado válido, nada foi baixado).` }
  }
  if (!payload.run_id) {
    return { error: 'Não foi possível iniciar o benchmark.' }
  }
  return { notice: `Benchmark ${kind.toUpperCase()} iniciado (${payload.run_id}). O chat segue livre.` }
}

export function candidateBadge(candidate: { installed: boolean }): 'INSTALLED' | 'NOT INSTALLED' {
  return candidate.installed ? 'INSTALLED' : 'NOT INSTALLED'
}
