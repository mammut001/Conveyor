import { useEffect, useState } from 'react'
import { enablePush, setupWorker } from '../pwa'

export function PwaNotifications({ token }: { token: string }) {
  const [state, setState] = useState<'idle' | 'busy' | 'active' | 'unsupported'>('idle')
  useEffect(() => {
    void setupWorker().catch(() => null)
  }, [])
  return <button className="settings-button" disabled={state === 'busy' || state === 'active'}
    title="可选：有事项等你审批时发提醒。通知里不能直接批准。"
    onClick={() => {
      setState('busy')
      void enablePush(token).then(ok => setState(ok ? 'active' : 'unsupported'))
        .catch(() => setState('unsupported'))
    }}>
    {state === 'active' ? '🔔 提醒已开启' : state === 'busy' ? '正在开启…' :
      state === 'unsupported' ? '无法开启提醒' : '🔔 开启提醒'}
  </button>
}
