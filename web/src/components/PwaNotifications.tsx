import { useEffect, useState } from 'react'
import { enablePush, setupWorker } from '../pwa'

export function PwaNotifications({ token }: { token: string }) {
  const [state, setState] = useState<'idle' | 'busy' | 'active' | 'unsupported'>('idle')
  useEffect(() => {
    void setupWorker().catch(() => null)
  }, [])
  return <button className="settings-button" disabled={state === 'busy' || state === 'active'}
    title="Optional generic approval alerts. Never approves from a notification."
    onClick={() => {
      setState('busy')
      void enablePush(token).then(ok => setState(ok ? 'active' : 'unsupported'))
        .catch(() => setState('unsupported'))
    }}>
    {state === 'active' ? '🔔 Alerts on' : state === 'busy' ? 'Enabling…' :
      state === 'unsupported' ? 'Push unavailable' : '🔔 Enable alerts'}
  </button>
}
