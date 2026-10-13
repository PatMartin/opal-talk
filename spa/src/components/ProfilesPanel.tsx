import { useState, type FormEvent } from 'react'
import { call } from '../api/client'
import type { Profile, Voice } from '../api/types'
import { ErrorBanner, OptionsField, parseOptions, useAction, useLoad } from './common'

interface ProfileForm {
  name: string
  description: string
  voice_id: string
  speed: string
  volume: string
  output_format: string
  sample_rate: string
  is_active: boolean
  options: string
}

const EMPTY: ProfileForm = {
  name: '', description: '', voice_id: '', speed: '1', volume: '1', output_format: 'wav', sample_rate: '', is_active: false, options: '',
}

export function ProfilesPanel() {
  const profiles = useLoad(() => call<Profile[]>('list_profiles_api_v1_profiles__get'), [])
  const voices = useLoad(() => call<Voice[]>('list_voices_api_v1_voices__get'), [])
  const [form, setForm] = useState<ProfileForm>(EMPTY)
  const [editing, setEditing] = useState<number | null>(null)
  const [selected, setSelected] = useState<Profile | null>(null)
  const action = useAction()
  const set = (patch: Partial<ProfileForm>) => setForm((f) => ({ ...f, ...patch }))

  const submit = (e: FormEvent) => {
    e.preventDefault()
    void action.run(async () => {
      const body = {
        name: form.name,
        description: form.description || null,
        voice_id: form.voice_id,
        speed: Number(form.speed),
        volume: Number(form.volume),
        output_format: form.output_format,
        sample_rate: form.sample_rate ? Number(form.sample_rate) : null,
        is_active: form.is_active,
        engine_options: parseOptions(form.options) ?? {},
      }
      if (editing !== null) {
        await call('update_profile_api_v1_profiles__profile_id__patch', { path: { profile_id: editing }, body })
      } else {
        await call('create_profile_api_v1_profiles__post', { body })
      }
      setForm(EMPTY)
      setEditing(null)
      await profiles.reload()
    })
  }

  const edit = (p: Profile) => {
    setEditing(p.id)
    setForm({
      name: p.name,
      description: p.description ?? '',
      voice_id: p.voice_id,
      speed: String(p.speed),
      volume: String(p.volume),
      output_format: p.output_format,
      sample_rate: p.sample_rate ? String(p.sample_rate) : '',
      is_active: p.is_active,
      options: Object.keys(p.engine_options).length ? JSON.stringify(p.engine_options) : '',
    })
  }

  const show = (id: number) =>
    action.run(async () => setSelected(await call<Profile>('get_profile_api_v1_profiles__profile_id__get', { path: { profile_id: id } })))

  const activate = (id: number) =>
    action.run(async () => {
      await call('activate_profile_api_v1_profiles__profile_id__activate_post', { path: { profile_id: id } })
      await profiles.reload()
    })

  const remove = (id: number) =>
    action.run(async () => {
      await call('delete_profile_api_v1_profiles__profile_id__delete', { path: { profile_id: id } })
      await profiles.reload()
    })

  return (
    <section>
      <h2>Profiles</h2>
      <ErrorBanner error={profiles.error} />
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Name</th>
            <th>Active</th>
            <th>Voice</th>
            <th>Speed</th>
            <th>Volume</th>
            <th>Format</th>
            <th>Rate</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {profiles.data.map((p) => (
            <tr key={p.id}>
              <td>{p.id}</td>
              <td>{p.name}</td>
              <td>{p.is_active ? '●' : ''}</td>
              <td>{p.voice_id}</td>
              <td>{p.speed}</td>
              <td>{p.volume}</td>
              <td>{p.output_format}</td>
              <td>{p.sample_rate ?? 'native'}</td>
              <td className="actions">
                <button onClick={() => void show(p.id)}>Details</button>
                <button onClick={() => edit(p)}>Edit</button>
                {!p.is_active && <button onClick={() => void activate(p.id)}>Activate</button>}
                <button onClick={() => void remove(p.id)}>Delete</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ErrorBanner error={action.error} />
      {selected && <pre data-testid="profile-details">{JSON.stringify(selected, null, 2)}</pre>}

      <h3>{editing !== null ? `Edit profile ${editing}` : 'Create profile'}</h3>
      <form onSubmit={submit} className="grid">
        <label>
          Name
          <input required value={form.name} onChange={(e) => set({ name: e.target.value })} />
        </label>
        <label>
          Voice
          <select required value={form.voice_id} onChange={(e) => set({ voice_id: e.target.value })}>
            <option value="">choose…</option>
            {voices.data.map((v) => (
              <option key={v.voice_id} value={v.voice_id}>
                {v.name} ({v.voice_id})
              </option>
            ))}
          </select>
        </label>
        <label>
          Description
          <input value={form.description} onChange={(e) => set({ description: e.target.value })} />
        </label>
        <label>
          Speed
          <input type="number" step="0.05" min="0.25" max="4" value={form.speed} onChange={(e) => set({ speed: e.target.value })} />
        </label>
        <label>
          Volume
          <input type="number" step="0.05" min="0" max="2" value={form.volume} onChange={(e) => set({ volume: e.target.value })} />
        </label>
        <label>
          Format
          <select value={form.output_format} onChange={(e) => set({ output_format: e.target.value })}>
            <option value="wav">wav</option>
            <option value="pcm">pcm</option>
          </select>
        </label>
        <label>
          Sample rate (Hz)
          <input type="number" min="8000" max="192000" placeholder="native" value={form.sample_rate} onChange={(e) => set({ sample_rate: e.target.value })} />
        </label>
        <label className="check">
          <input type="checkbox" checked={form.is_active} onChange={(e) => set({ is_active: e.target.checked })} /> Active
        </label>
        <OptionsField value={form.options} onChange={(options) => set({ options })} />
        <div className="wide">
          <button type="submit">{editing !== null ? 'Save' : 'Create'}</button>
          {editing !== null && (
            <button type="button" onClick={() => (setEditing(null), setForm(EMPTY))}>
              Cancel
            </button>
          )}
        </div>
      </form>
    </section>
  )
}
