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
