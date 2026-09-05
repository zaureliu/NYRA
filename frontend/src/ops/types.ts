export interface Capability {
  id: string
  name: string
  category: string
  description: string
  consumer: string
  toggleable: boolean
  enabled: boolean
  runtime_state: string
  health: string
  last_error: string | null
  configured: boolean
  restart_required: boolean
  hot_reload: boolean
}

export interface CapabilitiesResponse {
  capabilities: Capability[]
  summary: {
    total: number
    enabled: number
    disabled: number
    degraded: number
    failed: number
    unconfigured: number
    restart_required: number
  }
}

export interface IntelligenceCapability {
  id: string
  name: string
  description: string
  state: string
  health: string | null
  configured: boolean
  last_error: string | null
  dependencies: string[]
}

export interface IntelligenceStatus {
  state: string
  started_at: string
  storage: { ok: boolean; state: string; schema_version: number; quick_check?: string }
  counts: { memory: number; documents: number; chunks: number; tasks: number; events: number; traces: number; goals: number; open_loops: number }
  capabilities: {
    capabilities: IntelligenceCapability[]
    summary: Record<string, number>
    observed_at: string
  }
  model_router: {
    last_route: { selected_model?: string | null; task_type?: string; reason?: string } | null
  }
  context: { budget_characters: number; recent_assemblies: unknown[] }
  rag: { allowed_roots?: string[]; dimensions?: number }
  vision: { state: string; health: string; details?: { models?: string[]; structural_vision_available?: boolean } }
  tasks: { active_or_queued: number }
  trace: { dropped_events: number }
  diagnostic_domains: string[]
  evaluation: { total?: number; passed?: number; failed?: number } | null
  world_state?: {
    health: { state: string; average_snapshot_latency_ms: number }
    snapshot: {
      current_app?: WorldStateValue | null
      current_focus?: WorldStateValue | null
      active_tasks?: WorldStateValue | null
      active_monitors?: WorldStateValue | null
      active_goal?: WorldStateValue | null
      open_loop_count?: WorldStateValue | null
      waiting_loop_count?: WorldStateValue | null
      most_relevant_open_loop?: WorldStateValue | null
      recent_events?: Array<{ event_type: string; summary: string; observed_at: string }>
    }
  } | null
  open_loops?: {
    state: string
    counts: { open: number; waiting: number; blocked: number; recent_resolved: number }
    sections: Record<'open' | 'waiting' | 'blocked' | 'recent_resolved', Array<{
      id: string
      title: string
      state: string
      priority: number
      updated_at: string
      waiting_for?: Record<string, unknown> | null
      next_possible_action?: string | null
    }>>
    last_error?: string | null
    dropped_events?: number
  }
  persona_runtime?: {
    state: string
    emotion: { primary: string; intensity: number; confidence: number; reason: string }
    dialogue_policy: { mode: string }
    performance: { average_overhead_ms: number; samples: number }
  } | null
  emotional_presence?: {
    state: string
    emotion: string
    intensity: number
    voice: { delivery: string; emotion_support: 'FULL' | 'PARTIAL' | 'NONE'; voice_identity: string }
    avatar?: { state_expression: string; vts_kind: string; vts_target?: string | null; fallback?: string | null } | null
    vts: { state: string; model?: string | null; hotkeys: unknown[]; expressions: unknown[] }
    performance: { average_sync_ms: number; samples: number; sync_count: number }
  } | null
}

export interface WorldStateValue {
  value: unknown
  source: string
  observed_at: string
  confidence: number
  freshness: string
  verified: boolean
}

export interface SettingEntry {
  key: string
  category: string
  type: string
  current: unknown
  default: unknown
  sensitive: boolean
  requires_restart: boolean
  description: string
  options: string[] | null
  minimum: number | null
  maximum: number | null
  configure_via?: string
}

export interface SettingsV3Response {
  settings: SettingEntry[]
  categories: string[]
}

export interface SelfDevStatus {
  state: string
  mode: string
  active_issue_id: string | null
  queue_size: number
  unread_notifications: number
  repository_files: number
  workspace_ready: boolean
  github_status: string
  last_error_code: string | null
}

export interface SelfDevIssue {
  issue_id: string
  type: string
  title: string
  description: string
  status: string
  risk: string
  priority: number
  occurrences: number
  last_seen: string
  failure_reasons: string[]
}

export interface SelfDevNotification {
  notification_id: string
  type: string
  issue_id: string | null
  title: string
  message: string
  created_at: string
  read: boolean
}

export interface IntegrationCard {
  id: string
  name: string
  enabled: boolean
  configured: boolean
  connected: boolean
  state: string
  health: string
  latency_ms: number | null
  last_sync: string | number | null
  last_error: string | null
  auth_configured?: boolean
  authentication?: string
  last_test?: number | null
  last_success?: number | null
  core_version?: string | null
  api_state?: string | null
  entity_count?: number | null
  version?: string | null
  node_count?: number | null
  qemu_count?: number | null
  lxc_count?: number | null
  storage_count?: number | null
  active_profile?: string | null
  open_url?: string | null
  realtime_events?: string
  bridge_version?: string
  sentinel_version?: string
  events_received?: number
  host?: string | null
  address?: string
}

export interface IntegrationsStatusResponse {
  generated_at: number
  integrations: Record<string, IntegrationCard>
  summary: { total: number; ready: number; unconfigured: number; disabled: number; failing: number }
}

export interface HAProfile {
  profile_id: string
  name: string
  enabled: boolean
  url: string
  tls: boolean
  priority: number
  auth_configured: boolean
  status: string
  last_test: HATestResult | null
}

export interface HATestResult {
  ok?: boolean
  error_code?: string
  core_version?: string
  state?: string
  entity_count?: number
  latency_ms?: number
  tested_at?: number
}

export interface HAProfilesResponse {
  active_profile: string | null
  profiles: HAProfile[]
}

export interface HADiagnostics {
  id?: string
  status?: Record<string, unknown>
  unified?: Record<string, unknown>
  profiles?: HAProfilesResponse
}

export interface HATestDetail {
  profile_id?: string
  ok?: boolean
  error_code?: string
  http_status?: number
  authenticated?: boolean
  core_version?: string
  state?: string
  location_name?: string
  entity_count?: number
  latency_ms?: number
  tested_at?: number
}

export interface HAEntityRow {
  entity_id: string
  state: string
  friendly_name: string
  domain: string
  last_changed: string
  last_updated?: string
}

export interface HAEntitiesResponse {
  entities: HAEntityRow[]
  count: number
  domains_present: string[]
}

export interface HAEntityDetail {
  entity_id: string
  state: string
  attributes?: Record<string, unknown>
  safe_attributes?: Record<string, unknown>
  supported_services?: string[]
  last_changed: string
  last_updated: string
}

export interface HAActionResponse {
  success?: boolean
  error_code?: string
  message?: string
  approval_required?: boolean
  approval_id?: string
  effect_verified?: boolean | null
  verification_status?: string
  note?: string
  risk_level?: string
  target_entity?: string | null
}

export interface ProxmoxConfigStatus {
  id: string
  enabled: boolean
  configured: boolean
  url: string
  url_configured: boolean
  verify_ssl: boolean
  preferred_node: string
  timeout_seconds: number
  token_id_configured: boolean
  token_secret_configured: boolean
  auth_configured: boolean
  authenticated: boolean
  state: string
  health: string
  latency_ms: number | null
  version: string | null
  node_count: number | null
  qemu_count: number | null
  lxc_count: number | null
  storage_count: number | null
  last_test: number | null
  last_success: number | null
  last_error: string | null
  open_url: string | null
}

export interface ProxmoxNodeRow {
  node: string
  state: string
  cpu_percent: number
  memory_used_bytes: number | null
  memory_total_bytes: number | null
  uptime_s: number | null
}

export interface ProxmoxGuest {
  vmid: number | null
  name: string
  type: 'qemu' | 'lxc'
  node: string
  status: string
  cpu_percent: number
  memory_used_bytes: number | null
  memory_total_bytes: number | null
  uptime_s: number | null
}

export interface ProxmoxStorageRow {
  storage: string
  type: string
  node: string
  total_bytes: number
  used_bytes: number
  usage_percent: number | null
}

export interface ProxmoxInventory {
  nodes: ProxmoxNodeRow[]
  qemu: ProxmoxGuest[]
  lxc: ProxmoxGuest[]
  storage: ProxmoxStorageRow[]
  generated_at: number
}

export interface OpenWrtConfigStatus {
  id: string
  enabled: boolean
  configured: boolean
  url: string
  url_configured: boolean
  username: string
  username_configured: boolean
  password_configured: boolean
  auth_configured: boolean
  authenticated: boolean
  state: string
  health: string
  latency_ms: number | null
  version: string | null
  uptime_s: number | null
  last_test: number | null
  last_success: number | null
  last_error: string | null
}

export interface VoiceBridgeStatus {
  enabled: boolean
  configured: boolean
  protocol: string
  endpoint: string
  autostart: boolean
  health: string
  connected: boolean
  fallback_internal_active: boolean
  capabilities: Record<string, boolean>
  processor_name: string
  latency_ms: number | null
  last_probe_at: number | null
  last_error: string | null
}

export interface AboutInfo {
  version: string
  model: string
  components: Record<string, string>
  license_note: string
}

export interface ReleaseCriterion {
  id: string
  state: string
  detail: string
  source?: string
  artifact_age_seconds?: number | null
}

export interface ReleaseRevalidationInfo {
  state?: 'IDLE' | 'RUNNING' | 'DONE' | 'DONE_WITH_FAILURES' | 'TIMEOUT' | 'FAILED'
  started_at?: number
  started_iso?: string
  finished_at?: number
  exit_code?: number
  pid?: number | null
  current_step?: string
  step_index?: number
  total_steps?: number
  progress?: { step_index?: number; total_steps?: number; current_step?: string } | null
  already_running?: boolean
  error?: string
}

export interface ReleaseHealthInfo {
  state: 'GREEN' | 'YELLOW' | 'RED'
  generated_at?: number
  git_head?: string | null
  freshness?: 'FRESH' | 'STALE'
  revalidation?: ReleaseRevalidationInfo
  criteria: ReleaseCriterion[]
}

export interface SubsystemHealthEntry {
  name: string
  state: string
  healthy?: boolean
  last_error?: string | null
  observed_at?: string | null
  dependencies?: string[]
}

export interface HealthReport {
  overall: string
  summary: Record<string, number>
  subsystems: Record<string, SubsystemHealthEntry>
  generated_at: string
}
