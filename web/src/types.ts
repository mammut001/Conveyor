import type { TranscriptMessage } from './runtime'

export type EventItem = {
  schema_version: number
  event_id: string
  sequence: number
  timestamp: string
  kind: string
  job_id: string
  payload: Record<string, unknown>
  tool_call_id?: string
}

export type Job = {
  id: string
  state: string
  mode: string
  channel: string
  chat_id: string
  operator_id?: string
  created_at: string
  updated_at?: string
  started_at?: string
  finished_at?: string
  prompt_preview: string
  metadata?: Record<string, string>
  latest_event?: EventItem
  error?: string
  changed_files?: { status: string; path: string }[]
  runtime?: Record<string, unknown>
}

export type Session = {
  id: string
  channel?: string
  title?: string
  created_at: string
  updated_at?: string
  last_activity: string
  operator_id?: string
  source_chat_id?: string
  job_count: number
  message_count?: number
  latest_job?: Job
}

export type SessionDetail = Session & {
  messages?: TranscriptMessage[]
  jobs?: Job[]
}

export type Approval = {
  id: string
  job_id: string
  action: string
  status: string
  created_at?: string
}

export type NodeInfo = {
  id: string
  name: string
  type: string
  status: string
  last_seen_at?: string
  capabilities: string[]
  metadata?: Record<string, unknown>
}

export type SystemStatus = {
  uptime_seconds: number
  load_average: number[]
  cpu_count: number
  memory: { total: number | null; available: number | null }
  disk: { total: number; used: number; free: number }
  queue: { depth: number; paused: boolean; states: Record<string, number> }
  channels: Record<string, { configured: boolean }>
  nodes: NodeInfo[]
}

export type ComputerStatus = {
  armed: boolean
  arm_remaining_seconds: number
  active_task?: Record<string, unknown> | null
  screenshots: { artifact_id: string; created_at?: string; width?: number; height?: number }[]
}

export type ProviderHealth = {
  status?: string
  failure_count?: number
  circuit_open_until?: string | null
  last_error_at?: string | null
  last_success_at?: string | null
}

export type ProviderConfig = {
  provider_id: string
  provider_name: string
  model: string
  reasoning_effort: string
  base_url: string
  wire_api: 'responses' | 'chat'
  env_key: string
  api_key_configured: boolean
  api_key_hint: string
  config_path: string
  health?: ProviderHealth
}
