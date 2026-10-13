import { useState } from 'react'
import { call } from '../api/client'
import type { Engine, ValidationResult } from '../api/types'
import { ErrorBanner, OptionsField, ValidationView, parseOptions, useAction, useLoad } from './common'

export function EnginesPanel() {
  const engines = useLoad(() => call<Engine[]>('list_engines_api_v1_engines__get'), [])
  const [selected, setSelected] = useState<Engine | null>(null)
  const [options, setOptions] = useState('')
  const [validation, setValidation] = useState<ValidationResult | null>(null)
  const action = useAction()

  const open = (id: string) =>
    action.run(async () => {
      const engine = await call<Engine>('get_engine_api_v1_engines__engine_id__get', { path: { engine_id: id } })
      setSelected(engine)
      setOptions(JSON.stringify(engine.options, null, 2))
    })

  const patch = async (id: string, body: Record<string, unknown>) => {
    const engine = await call<Engine>('update_engine_api_v1_engines__engine_id__patch', { path: { engine_id: id }, body })
    if (selected?.engine_id === id) setSelected(engine)
    await engines.reload()
  }
  const update = (id: string, body: Record<string, unknown>) => action.run(() => patch(id, body))

  const validate = (id: string) =>
    action.run(async () => {
      setValidation(await call<ValidationResult>('validate_engine_api_v1_engines__engine_id__validate_post', { path: { engine_id: id } }))
    })

  return (
    <section>
      <h2>Engines</h2>
      <ErrorBanner error={engines.error} />
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Name</th>
            <th>Enabled</th>
            <th>Available</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {engines.data.map((e) => (
            <tr key={e.engine_id}>
              <td>{e.engine_id}</td>
              <td title={e.description ?? ''}>{e.name}</td>
              <td>{e.enabled ? 'yes' : 'no'}</td>
              <td>{e.available ? 'yes' : 'no'}</td>
              <td className="actions">
                <button onClick={() => void update(e.engine_id, { enabled: !e.enabled })}>{e.enabled ? 'Disable' : 'Enable'}</button>
                <button onClick={() => void open(e.engine_id)}>Configure</button>
                <button onClick={() => void validate(e.engine_id)}>Validate</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ErrorBanner error={action.error} />
      <ValidationView result={validation} />
      {selected && (
        <div className="card">
          <h3>{selected.name} options</h3>
          <p className="muted">Keys are merged; set a key to null to remove it.</p>
          <OptionsField label="Options (JSON)" value={options} onChange={setOptions} />
          <button onClick={() => void action.run(() => patch(selected.engine_id, { options: parseOptions(options) ?? {} }))}>
            Save options
          </button>
          <pre>{JSON.stringify(selected.metadata, null, 2)}</pre>
        </div>
      )}
    </section>
  )
}
