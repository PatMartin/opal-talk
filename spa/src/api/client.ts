import operations from './operations.json'

/** REST operationId (from opal_talk.yaml). The SPA talks to the service only through these. */
export type OpId = keyof typeof operations

export interface ErrorResponse {
  code: string
  message: string
  details?: Record<string, unknown>
}

/** Shared OPAL error envelope body (`{"error": OpalError}`), contract 1.2.0. */
export interface OpalError extends ErrorResponse {
  request_id: string
  transaction_id: string
  session_id?: string | null
  causation_id?: string | null
}

export interface OpalErrorEnvelope {
  error: OpalError
}

export interface FieldError {
  loc: (string | number)[]
  msg: string
  type: string
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
  ) {
    super(describe(status, body))
  }
}

function describe(status: number, body: unknown): string {
  if (body && typeof body === 'object') {
    const raw = body as Record<string, unknown>
    const err = raw.error && typeof raw.error === 'object' ? (raw.error as Record<string, unknown>) : raw
    const details = err.details as { errors?: FieldError[] } | undefined
    const fieldErrors = Array.isArray(details?.errors) ? details.errors : Array.isArray(err.detail) ? (err.detail as FieldError[]) : null
    if (fieldErrors) return `${status} validation error: ` + fieldErrors.map((d) => `${d.loc.join('.')}: ${d.msg}`).join('; ')
    if (typeof err.code === 'string') return `${status} ${err.code}: ${typeof err.message === 'string' ? err.message : ''}`
  }
  return `${status}: ${typeof body === 'string' ? body : JSON.stringify(body)}`
}

export function apiBase(): string {
  const configured = import.meta.env.VITE_OPAL_TALK_API_URL as string | undefined
  if (configured) return configured.replace(/\/$/, '')
  return import.meta.env.DEV ? 'http://127.0.0.1:8001' : ''
}

export interface CallOptions {
  path?: Record<string, string | number>
  query?: Record<string, string | number | boolean | undefined | null>
  body?: unknown
  signal?: AbortSignal
}

export function buildUrl(op: OpId, opts: CallOptions = {}): string {
  let path = operations[op].path
  for (const [k, v] of Object.entries(opts.path ?? {})) path = path.replace(`{${k}}`, encodeURIComponent(String(v)))
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(opts.query ?? {})) if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
  const query = qs.toString()
  return `${apiBase()}${path}${query ? `?${query}` : ''}`
}

/** Mutations on logical configuration carrying the mandatory If-Match precondition. */
const PRECONDITION_OPS = new Set<OpId>([
  'update_engine_api_v1_engines__engine_id__patch',
  'update_voice_api_v1_voices__voice_id__patch',
  'delete_voice_api_v1_voices__voice_id__delete',
  'update_profile_api_v1_profiles__profile_id__patch',
  'delete_profile_api_v1_profiles__profile_id__delete',
  'upsert_setting_api_v1_settings__key__put',
  'delete_setting_api_v1_settings__key__delete',
  'patch_talk_settings',
])

async function send(op: OpId, opts: CallOptions, ifMatch: string | null): Promise<Response> {
  const headers: Record<string, string> = {}
  if (ifMatch) headers['If-Match'] = ifMatch
  const init: RequestInit = { method: operations[op].method, signal: opts.signal, headers }
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(opts.body)
  }
  return fetch(buildUrl(op, opts), init)
}

async function configEtag(): Promise<string> {
  const resp = await send('get_all_settings_api_v1_settings__get', {}, null)
  return resp.headers.get('ETag') ?? ''
}

/** Perform an operation and return the raw Response (throws ApiError for non-2xx). */
export async function callRaw(op: OpId, opts: CallOptions = {}): Promise<Response> {
  let resp = await send(op, opts, PRECONDITION_OPS.has(op) ? await configEtag() : null)
  if (resp.status === 412) resp = await send(op, opts, await configEtag()) // stale ETag: refresh once, retry
  if (!resp.ok) {
    const text = await resp.text()
    let body: unknown = text
    try {
      body = JSON.parse(text)
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, body)
  }
  return resp
}

/** Perform an operation returning JSON (or undefined for 204). */
export async function call<T = unknown>(op: OpId, opts: CallOptions = {}): Promise<T> {
  const resp = await callRaw(op, opts)
  if (resp.status === 204) return undefined as T
  return (await resp.json()) as T
}

export function audioMeta(resp: Response) {
  const h = resp.headers
  return {
    format: h.get('X-Opal-Audio-Format') ?? '',
    sampleRate: Number(h.get('X-Opal-Sample-Rate') ?? 0),
    engineId: h.get('X-Opal-Engine-Id') ?? '',
    voiceId: h.get('X-Opal-Voice-Id') ?? '',
    profileId: h.get('X-Opal-Profile-Id'),
  }
}
export type AudioMeta = ReturnType<typeof audioMeta>
