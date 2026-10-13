import { vi } from 'vitest'

export interface RecordedCall {
  method: string
  path: string
  query: URLSearchParams
  body: unknown
}

type Handler = (call: RecordedCall) => Response | Promise<Response>

export function json(data: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(status === 204 ? null : JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

export function audio(bytes: Uint8Array<ArrayBuffer>, headers: Record<string, string>, type = 'audio/wav'): Response {
  return new Response(bytes, { status: 200, headers: { 'Content-Type': type, ...headers } })
}

/** Stub global fetch with a `METHOD /path` routing table; records every call. */
export function mockApi(routes: Record<string, Handler | unknown>) {
  const calls: RecordedCall[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    const method = (init?.method ?? 'GET').toUpperCase()
    const call: RecordedCall = {
      method,
      path: url.pathname,
      query: url.searchParams,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    }
    calls.push(call)
    const route = routes[`${method} ${url.pathname}`]
    if (route === undefined) return json({ code: 'not_found', message: `no mock for ${method} ${url.pathname}` }, 404)
    return typeof route === 'function' ? (route as Handler)(call) : json(route)
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

export const AUDIO_HEADERS = {
  'X-Opal-Audio-Format': 'wav',
  'X-Opal-Sample-Rate': '22050',
  'X-Opal-Channels': '1',
  'X-Opal-Sample-Format': 's16le',
  'X-Opal-Engine-Id': 'piper',
  'X-Opal-Voice-Id': 'amy',
}

export const VOICE = {
  voice_id: 'amy',
  name: 'Amy',
  description: null,
  engine_id: 'piper',
  engine_voice_ref: 'en_US-amy-low',
  language: 'en-US',
  enabled: true,
  native_sample_rate: 16000,
  engine_options: {},
  metadata: {},
  created_at: '2026-10-04T00:00:00Z',
  updated_at: '2026-10-04T00:00:00Z',
}

export const ENGINE = {
  engine_id: 'piper',
  name: 'Piper',
  description: null,
  enabled: true,
  available: true,
  options: { voices_dir: '/v' },
  metadata: { version: '1.8' },
}

export const PROFILE = {
  id: 1,
  name: 'default',
  description: null,
  is_active: false,
  voice_id: 'amy',
  speed: 1,
  volume: 1,
  output_format: 'wav',
  sample_rate: null,
  engine_options: {},
  created_at: '2026-10-04T00:00:00Z',
  updated_at: '2026-10-04T00:00:00Z',
}
