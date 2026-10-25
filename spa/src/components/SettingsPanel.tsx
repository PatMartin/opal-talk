import { useState, type FormEvent } from 'react'
import { call } from '../api/client'
import type { Setting } from '../api/types'
import { ErrorBanner, useAction, useLoad } from './common'

export function SettingsPanel() {
  const settings = useLoad(() => call<Record<string, unknown>>('get_all_settings_api_v1_settings__get'), {})
  const [key, setKey] = useState('')
  const [value, setValue] = useState('')
  const [detail, setDetail] = useState<Setting | null>(null)
  const action = useAction()

  const save = (e: FormEvent) => {
    e.preventDefault()
    void action.run(async () => {
      setDetail(await call<Setting>('upsert_setting_api_v1_settings__key__put', { path: { key }, body: { value } }))
      setKey('')
      setValue('')
      await settings.reload()
    })
  }

  const show = (k: string) =>
    action.run(async () => setDetail(await call<Setting>('get_setting_api_v1_settings__key__get', { path: { key: k } })))

  const remove = (k: string) =>
    action.run(async () => {
      await call('delete_setting_api_v1_settings__key__delete', { path: { key: k } })
      await settings.reload()
    })

  return (
    <section>
      <h2>Settings</h2>
      <p className="muted">
        Known keys: max_text_chars, chunk_max_chars, default_speed, default_volume, default_output_format. Deleting a setting restores its default.
        <button
          onClick={() =>
            void action.run(async () => {
              await call('patch_talk_settings', {
                body: {
                  max_text_chars: '20000',
                  chunk_max_chars: '400',
                  default_speed: '1',
                  default_volume: '1',
                  default_output_format: 'wav',
                },
              })
              await settings.reload()
            })
          }
        >
          Reset to defaults
        </button>
      </p>
      <ErrorBanner error={settings.error} />
      <table>
        <thead>
          <tr>
            <th>Key</th>
            <th>Value</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {Object.entries(settings.data).map(([k, v]) => (
            <tr key={k}>
              <td>{k}</td>
              <td className="mono">{String(v)}</td>
              <td className="actions">
                <button onClick={() => (setKey(k), setValue(String(v)))}>Edit</button>
                <button onClick={() => void show(k)}>Details</button>
                <button onClick={() => void remove(k)}>Delete</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ErrorBanner error={action.error} />
      {detail && (
        <p className="muted" data-testid="setting-detail">
          #{detail.id} {detail.key} = {detail.value} (updated {detail.updated_at})
        </p>
      )}
      <form onSubmit={save} className="row">
        <label>
          Key
          <input required value={key} onChange={(e) => setKey(e.target.value)} />
        </label>
        <label>
          Value
          <input value={value} onChange={(e) => setValue(e.target.value)} />
        </label>
        <button type="submit">Set</button>
      </form>
    </section>
  )
}
