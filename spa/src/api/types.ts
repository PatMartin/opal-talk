/** Types mirroring opal_talk.yaml component schemas used by the SPA. */
export type AudioFormat = 'wav' | 'pcm'
export type Options = Record<string, unknown>

export interface Engine {
  engine_id: string
  name: string
  description?: string | null
  enabled: boolean
  available: boolean
  options: Options
  metadata: Options
}

export interface Voice {
  voice_id: string
  name: string
  description?: string | null
  engine_id: string
  engine_voice_ref: string
  language?: string | null
  enabled: boolean
  native_sample_rate?: number | null
  engine_options: Options
  metadata: Options
  created_at: string
  updated_at: string
}

export interface Profile {
  id: number
  name: string
  description?: string | null
  is_active: boolean
  voice_id: string
  speed: number
  volume: number
  output_format: AudioFormat
  sample_rate?: number | null
  engine_options: Options
  created_at: string
  updated_at: string
}

export interface ValidationMessage {
  code: string
  message: string
  field?: string | null
}

export interface ValidationResult {
  valid: boolean
  errors: ValidationMessage[]
  warnings: ValidationMessage[]
  details: Options
}

export interface Setting {
  id: number
  key: string
  value: string
  updated_at: string
}

export interface Health {
  status: 'ok' | 'degraded' | 'error'
  version: string
  active_profile_id?: number | null
  engines: { engine_id: string; enabled: boolean; available: boolean; message?: string | null }[]
  details?: Options
}

export interface Backup {
  filename: string
  created_at: string
  size_bytes?: number | null
}

export interface ServiceInfo {
  name: string
  version: string
  api_version: string
  description?: string
}
