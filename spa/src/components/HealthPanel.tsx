import { call } from '../api/client'
import type { Health } from '../api/types'
import { ErrorBanner, useLoad } from './common'

export function HealthPanel() {
  const health = useLoad(() => call<Health | null>('health_check_api_v1_health__get'), null)
  const probes = useLoad(async () => {
    const [live, ready] = await Promise.all([
      call<{ status: string; ready: boolean }>('get_talk_health_live'),
      call<{ status: string; ready: boolean }>('get_talk_health_ready'),
    ])
    return { live, ready }
  }, null)
  const h = health.data
  return (
    <section>
      <h2>Health</h2>
      <button onClick={() => void health.reload()}>Refresh</button>
      <ErrorBanner error={health.error} />
      {probes.data && (
        <p>
          live: {probes.data.live.status} · ready: {probes.data.ready.status} ({String(probes.data.ready.ready)})
        </p>
      )}
      {h && (
        <>
          <p>
            Status: <strong className={`status-${h.status}`}>{h.status}</strong> · version {h.version} · active profile{' '}
            {h.active_profile_id ?? 'none'}
          </p>
          <table>
            <thead>
              <tr>
                <th>Engine</th>
                <th>Enabled</th>
                <th>Available</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody>
              {h.engines.map((e) => (
                <tr key={e.engine_id}>
                  <td>{e.engine_id}</td>
                  <td>{e.enabled ? 'yes' : 'no'}</td>
                  <td>{e.available ? 'yes' : 'no'}</td>
                  <td>{e.message ?? ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}
