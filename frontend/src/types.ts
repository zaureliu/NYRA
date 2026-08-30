export type EmotionalState =
  | 'neutral' | 'friendly' | 'focused' | 'confident' | 'positive'
  | 'happy' | 'relieved' | 'concerned' | 'warning' | 'serious'
  | 'empathetic' | 'curious' | 'surprised' | 'amused'
  | 'apologetic' | 'uncertain' | 'calm' | 'tired'

export type ActivityStatus = 'IDLE' | 'LISTENING' | 'USER_SPEAKING' | 'TRANSCRIBING' | 'THINKING' | 'TOOL_EXECUTION' | 'SPEAKING' | 'INTERRUPTED' | 'ERROR' | 'OFFLINE'
export type MouthState =
  | 'mouth_closed' | 'mouth_small' | 'mouth_medium' | 'mouth_open'
  | 'mouth_wide' | 'mouth_smile' | 'mouth_speaking_smile'
export type EyeState = 'open' | 'half' | 'closed' | 'blink' | 'look_left' | 'look_right' | 'look_up' | 'look_down'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: Date
  turnId?: string
  status?: 'streaming' | 'complete' | 'failed'
}

export interface ToolActivity {
  id: string
  command: string
  riskLevel: string
  status: 'running' | 'finished' | 'approval_required'
  tool?: 'system_shell' | 'remote_shell' | 'agent_run'
  host?: string
  detail?: string
  agentRunId?: string
  exitCode?: number | null
  durationMs?: number
  success?: boolean
}

export interface ChatResponse {
  response: string
  display_text: string
  speech_text: string
  state: EmotionalState
  emotion_intensity?: number
  audio_url: string | null
  audio_urls?: string[]
  response_id?: string | null
  turn_id?: string | null
  pipeline_status?: string
  tts_provider: string | null
  timing?: { llm_ms: number; tts_ms: number; total_ms: number }
}

export interface AvatarControl {
  eye_x: number; eye_y: number; head_x: number; head_y: number; head_tilt: number
  body_x: number; breathing: number; mouth_open: number; expression_weight: number
  neural_link: string; animation: string
}

export interface VoiceProfile {
  provider: 'chatterbox' | 'chatterbox_multilingual_v3' | 'chatterbox_ptbr' | 'kokoro' | 'edge_tts'
  voice: string
  speaking_rate: number
  temperature: number
  exaggeration: number
  cfg_weight: number
  seed: number
  sentence_pause_ms: number
  paragraph_pause_ms: number
  model?: string | null
  reference_file?: string | null
  edge_rate: string
  edge_pitch: string
  edge_volume: string
}

export interface InputMetrics {
  rms: number
  peak: number
  clipping: boolean
  speechDetected: boolean
  durationMs: number
}

export interface Health {
  status: string
  character: string
  llm: boolean
  llm_ready?: boolean
  ollama?: { state: string; ready: boolean; model: string; keep_alive: string }
  memory: boolean
  stt: boolean
  tts: boolean
  pronunciation_engine?: boolean
  always_listening?: boolean
  microphone?: boolean
  wake_word?: string
  network_watch?: boolean
  system_shell?: { enabled: boolean; default_shell: string }
  remote_shell?: { enabled: boolean; hosts: Array<{ id: string; enabled: boolean }> }
  agent?: { enabled: boolean; read_only: boolean; active_runs: string[] }
  sentinel_watch?: { enabled: boolean; state: string }
  providers?: Record<string, string>
  model?: string
}

export interface MemoryRecord {
  id: number
  category: string
  content: string
  importance: number
  role?: string
  created_at: string
}
