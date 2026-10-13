import { useEffect, useState, type FormEvent } from 'react'
import { audioMeta, call, callRaw } from '../api/client'
import type { Engine, ValidationResult, Voice } from '../api/types'
import { pcmToWav } from '../audio'
import { AudioResult, ErrorBanner, OptionsField, ValidationView, parseOptions, useAction, useLoad, type AudioClip } from './common'

interface VoiceForm {
  voice_id: string
  name: string
  description: string
  engine_id: string
  engine_voice_ref: string
  language: string
  enabled: boolean
  options: string
}

const EMPTY: VoiceForm = { voice_id: '', name: '', description: '', engine_id: '', engine_voice_ref: '', language: '', enabled: true, options: '' }

export function VoicesPanel() {
  const [engineFilter, setEngineFilter] = useState('')
  const [enabledFilter, setEnabledFilter] = useState('')
  const voices = useLoad(
    () => call<Voice[]>('list_voices_api_v1_voices__get', { query: { engine_id: engineFilter, enabled: enabledFilter } }),
    [],
  )
  const engines = useLoad(() => call<Engine[]>('list_engines_api_v1_engines__get'), [])
  const { reload: reloadVoices } = voices
  useEffect(() => {
    void reloadVoices()
  }, [engineFilter, enabledFilter, reloadVoices])
  const [form, setForm] = useState<VoiceForm>(EMPTY)
  const [editing, setEditing] = useState<string | null>(null)
  const [selected, setSelected] = useState<Voice | null>(null)
  const [validation, setValidation] = useState<ValidationResult | null>(null)
  const [clip, setClip] = useState<AudioClip | null>(null)
  const action = useAction()

  const set = (patch: Partial<VoiceForm>) => setForm((f) => ({ ...f, ...patch }))

  const submit = (e: FormEvent) => {
    e.preventDefault()
    void action.run(async () => {
      const body: Record<string, unknown> = {
        name: form.name,
        engine_id: form.engine_id,
        engine_voice_ref: form.engine_voice_ref,
        enabled: form.enabled,
        description: form.description || null,
        language: form.language || null,
        engine_options: parseOptions(form.options) ?? {},
      }
      if (editing) {
        await call('update_voice_api_v1_voices__voice_id__patch', { path: { voice_id: editing }, body })
      } else {
        await call('create_voice_api_v1_voices__post', { body: { voice_id: form.voice_id, ...body } })
      }
      setForm(EMPTY)
      setEditing(null)
      await voices.reload()
    })
  }

  const edit = (v: Voice) => {
    setEditing(v.voice_id)
    setForm({
      voice_id: v.voice_id,
      name: v.name,
      description: v.description ?? '',
      engine_id: v.engine_id,
      engine_voice_ref: v.engine_voice_ref,
      language: v.language ?? '',
      enabled: v.enabled,
      options: Object.keys(v.engine_options).length ? JSON.stringify(v.engine_options) : '',
    })
  }

  const show = (id: string) =>
    action.run(async () => {
      setSelected(await call<Voice>('get_voice_api_v1_voices__voice_id__get', { path: { voice_id: id } }))
    })

  const remove = (id: string) =>
    action.run(async () => {
      await call('delete_voice_api_v1_voices__voice_id__delete', { path: { voice_id: id } })
      await voices.reload()
    })

  const validate = (id: string) =>
    action.run(async () => {
      setValidation(await call<ValidationResult>('validate_voice_api_v1_voices__voice_id__validate_post', { path: { voice_id: id } }))
    })

  const test = (id: string) =>
    action.run(async () => {
      const resp = await callRaw('test_voice_api_v1_voices__voice_id__test_post', { path: { voice_id: id }, body: {} })
      const meta = audioMeta(resp)
      const raw = new Uint8Array(await resp.arrayBuffer())
      const blob = meta.format === 'pcm' ? pcmToWav(raw, meta.sampleRate) : new Blob([raw], { type: 'audio/wav' })
      setClip({ url: URL.createObjectURL(blob), meta, bytes: raw.byteLength, filename: `${id}-test.wav` })
    })

  return (
    <section>
      <h2>Voices</h2>
      <div className="row">
        <label>
          Engine
          <select value={engineFilter} onChange={(e) => setEngineFilter(e.target.value)}>
            <option value="">all</option>
            {engines.data.map((en) => (
              <option key={en.engine_id} value={en.engine_id}>
                {en.engine_id}
              </option>
            ))}
          </select>
        </label>
        <label>
          State
          <select value={enabledFilter} onChange={(e) => setEnabledFilter(e.target.value)}>
            <option value="">all</option>
            <option value="true">enabled</option>
            <option value="false">disabled</option>
          </select>
        </label>
        <button type="button" onClick={() => void voices.reload()}>
          Refresh
        </button>
      </div>
      <ErrorBanner error={voices.error} />
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Name</th>
            <th>Engine</th>
            <th>Reference</th>
            <th>Lang</th>
            <th>Rate</th>
            <th>Enabled</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {voices.data.map((v) => (
            <tr key={v.voice_id}>
              <td>{v.voice_id}</td>
              <td>{v.name}</td>
              <td>{v.engine_id}</td>
              <td className="mono">{v.engine_voice_ref}</td>
              <td>{v.language ?? '-'}</td>
              <td>{v.native_sample_rate ?? '-'}</td>
              <td>{v.enabled ? 'yes' : 'no'}</td>
              <td className="actions">
                <button onClick={() => void show(v.voice_id)}>Details</button>
                <button onClick={() => edit(v)}>Edit</button>
                <button onClick={() => void validate(v.voice_id)}>Validate</button>
                <button onClick={() => void test(v.voice_id)}>Test</button>
                <button onClick={() => void remove(v.voice_id)}>Unregister</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ErrorBanner error={action.error} />
      <ValidationView result={validation} />
      <AudioResult clip={clip} />
      {selected && <pre data-testid="voice-details">{JSON.stringify(selected, null, 2)}</pre>}

      <h3>{editing ? `Edit voice ${editing}` : 'Register voice'}</h3>
      <form onSubmit={submit} className="grid">
        <label>
          Voice ID
          <input required disabled={!!editing} value={form.voice_id} onChange={(e) => set({ voice_id: e.target.value })} />
        </label>
        <label>
          Name
          <input required value={form.name} onChange={(e) => set({ name: e.target.value })} />
        </label>
        <label>
          Engine
          <select required value={form.engine_id} onChange={(e) => set({ engine_id: e.target.value })}>
            <option value="">choose…</option>
            {engines.data.map((en) => (
              <option key={en.engine_id} value={en.engine_id}>
                {en.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Engine voice reference
          <input required value={form.engine_voice_ref} onChange={(e) => set({ engine_voice_ref: e.target.value })} />
        </label>
        <label>
          Language
          <input value={form.language} onChange={(e) => set({ language: e.target.value })} />
        </label>
        <label>
          Description
          <input value={form.description} onChange={(e) => set({ description: e.target.value })} />
        </label>
        <label className="check">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set({ enabled: e.target.checked })} /> Enabled
        </label>
        <OptionsField value={form.options} onChange={(options) => set({ options })} />
        <div className="wide">
          <button type="submit">{editing ? 'Save' : 'Register'}</button>
          {editing && (
            <button type="button" onClick={() => (setEditing(null), setForm(EMPTY))}>
              Cancel
            </button>
          )}
        </div>
      </form>
    </section>
  )
}
