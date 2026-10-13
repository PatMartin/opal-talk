import { useCallback, useEffect, useRef, useState } from 'react'
import { type AudioMeta } from '../api/client'
import type { ValidationResult } from '../api/types'

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null
  const text = error instanceof Error ? error.message : String(error)
  return (
    <div role="alert" className="error">
      {text}
    </div>
  )
}

export function ValidationView({ result }: { result: ValidationResult | null }) {
  if (!result) return null
  return (
    <div className={result.valid ? 'ok' : 'error'} data-testid="validation">
      <strong>{result.valid ? 'Valid' : 'Invalid'}</strong>
      <ul>
        {result.errors.map((e, i) => (
          <li key={`e${i}`}>
            error: {e.code}
            {e.field ? ` [${e.field}]` : ''}: {e.message}
          </li>
        ))}
        {result.warnings.map((w, i) => (
          <li key={`w${i}`}>
            warning: {w.code}
            {w.field ? ` [${w.field}]` : ''}: {w.message}
          </li>
        ))}
      </ul>
    </div>
  )
}

export interface AudioClip {
  url: string
  meta: AudioMeta
  bytes: number
  filename: string
}

export function AudioResult({ clip }: { clip: AudioClip | null }) {
  if (!clip) return null
  const m = clip.meta
  return (
    <div className="audio" data-testid="audio-result">
      <audio controls src={clip.url} />
      <div className="muted">
        {clip.bytes} bytes · {m.format} · {m.sampleRate} Hz · voice {m.voiceId} · engine {m.engineId}
        {m.profileId ? ` · profile ${m.profileId}` : ''} ·{' '}
        <a href={clip.url} download={clip.filename}>
          download
        </a>
      </div>
    </div>
  )
}

/** Textarea for opaque engine options (JSON object). Returns undefined when empty. */
export function parseOptions(text: string): Record<string, unknown> | undefined {
  if (!text.trim()) return undefined
  const value = JSON.parse(text)
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('engine options must be a JSON object')
  return value as Record<string, unknown>
}

export function OptionsField({ value, onChange, label = 'Engine options (JSON)' }: {
  value: string
  onChange: (v: string) => void
  label?: string
}) {
  return (
    <label>
      {label}
      <textarea rows={2} value={value} placeholder="{}" onChange={(e) => onChange(e.target.value)} />
    </label>
  )
}

/** Load data on mount and expose a reload function (always using the latest loader). */
export function useLoad<T>(loader: () => Promise<T>, initial: T) {
  const [data, setData] = useState<T>(initial)
  const [error, setError] = useState<unknown>(null)
  const loaderRef = useRef(loader)
  loaderRef.current = loader
  const reload = useCallback(async () => {
    try {
      setData(await loaderRef.current())
      setError(null)
    } catch (e) {
      setError(e)
    }
  }, [])
  useEffect(() => {
    void reload()
  }, [reload])
  return { data, error, reload, setError }
}

/** Run an action, capturing errors for display. */
export function useAction() {
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const run = useCallback(async (fn: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }, [])
  return { error, busy, run, setError }
}
