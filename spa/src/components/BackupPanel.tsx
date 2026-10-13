import { useState } from 'react'
import { buildUrl, call } from '../api/client'
import type { Backup, ValidationResult } from '../api/types'
import { ErrorBanner, ValidationView, useAction, useLoad } from './common'

export function BackupPanel() {
  const backups = useLoad(() => call<Backup[]>('list_backups_api_v1_backup_list_get'), [])
  const [message, setMessage] = useState('')
  const [validation, setValidation] = useState<ValidationResult | null>(null)
  const action = useAction()

  const run = (fn: () => Promise<void>) =>
    action.run(async () => {
      setMessage('')
      setValidation(null)
      await fn()
    })

  const create = () =>
    run(async () => {
      const result = await call<{ filename: string; message?: string }>('create_backup_api_v1_backup_create_post')
      setMessage(`${result.message ?? 'Backup created'}: ${result.filename}`)
      await backups.reload()
    })

  const restore = (filename: string) =>
    run(async () => {
      if (!window.confirm(`Restore configuration from ${filename}? Current configuration will be replaced.`)) return
      const result = await call<{ message: string }>('restore_backup_api_v1_backup_restore__filename__post', { path: { filename } })
      setMessage(result.message)
    })

  const remove = (filename: string) =>
    run(async () => {
      await call('delete_backup_api_v1_backup__filename__delete', { path: { filename } })
      await backups.reload()
    })

  const exportConfig = () =>
    run(async () => {
      const data = await call('export_config_api_v1_backup_export_get')
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
      const a = document.createElement('a')
      a.href = url
      a.download = 'opal-talk-config.json'
      a.click()
      setMessage('Configuration exported.')
    })

  const importConfig = () =>
    run(async () => {
      const result = await call<{ success: boolean; validation: ValidationResult; message?: string | null }>(
        'import_config_api_v1_backup_import_post',
      )
      setMessage(result.message ?? (result.success ? 'Imported.' : 'Import failed.'))
      setValidation(result.validation)
    })

  const validate = () => run(async () => setValidation(await call<ValidationResult>('validate_config_api_v1_backup_validate_get')))

  return (
    <section>
      <h2>Backup &amp; configuration</h2>
      <div className="row">
        <button onClick={() => void create()}>Create backup</button>
        <button onClick={() => void exportConfig()}>Export JSON</button>
        <button onClick={() => void importConfig()}>Import ./config</button>
        <button onClick={() => void validate()}>Validate config</button>
      </div>
      {message && (
        <p className="ok" role="status">
          {message}
        </p>
      )}
      <ErrorBanner error={action.error || backups.error} />
      <ValidationView result={validation} />
      <table>
        <thead>
          <tr>
            <th>Backup</th>
            <th>Created</th>
            <th>Size</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {backups.data.map((b) => (
            <tr key={b.filename}>
              <td className="mono">{b.filename}</td>
              <td>{b.created_at}</td>
              <td>{b.size_bytes ?? '-'}</td>
              <td className="actions">
                <a href={buildUrl('download_backup_api_v1_backup_download__filename__get', { path: { filename: b.filename } })} download>
                  Download
                </a>
                <button onClick={() => void restore(b.filename)}>Restore</button>
                <button onClick={() => void remove(b.filename)}>Delete</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
