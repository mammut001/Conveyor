import { useEffect, useState } from 'react'

type LibraryData = {
  jobs: { id: string; state?: string; prompt_preview?: string; updated_at?: string; changed_files?: { status?: string; path: string }[] }[]
  routines: { id: number; name: string; schedule: string; enabled: boolean; next_run_at?: string | null; last_run_at?: string | null }[]
  memory: { profile: number; log: number } | null
  screenshots: { id: string; created_at?: string; width?: number; height?: number }[]
}

function when(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function Shot({ agentId, id, token }: { agentId: string; id: string; token: string }) {
  const [url, setUrl] = useState('')
  useEffect(() => {
    let active = true; let local = ''
    fetch(`/api/agents/${encodeURIComponent(agentId)}/screenshots/${encodeURIComponent(id)}`, { headers: { Authorization: `Bearer ${token}` } })
      .then(response => response.ok ? response.blob() : Promise.reject())
      .then(blob => { if (active) { local = URL.createObjectURL(blob); setUrl(local) } })
      .catch(() => {})
    return () => { active = false; if (local) URL.revokeObjectURL(local) }
  }, [agentId, id, token])
  return url ? <a href={url} target="_blank" rel="noreferrer" className="agent-library-shot"><img src={url} alt="Screenshot taken on this agent's desktop" /></a> : <span className="agent-library-shot loading" />
}

/** What an agent has accumulated: files it changed, its scheduled checks, memory, screenshots. */
export function AgentLibrary({ agentId, token }: { agentId: string; token: string }) {
  const [data, setData] = useState<LibraryData | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    const load = () => {
      fetch(`/api/agents/${encodeURIComponent(agentId)}/library`, { headers: { Authorization: `Bearer ${token}` } })
        .then(async response => {
          if (!response.ok) throw new Error(`Could not load the library (${response.status})`)
          return response.json() as Promise<LibraryData>
        })
        .then(value => { if (active) { setData(value); setError('') } })
        .catch(reason => { if (active) setError(reason instanceof Error ? reason.message : 'Could not load the library') })
    }
    load()
    const timer = window.setInterval(load, 15000)
    return () => { active = false; window.clearInterval(timer) }
  }, [agentId, token])

  if (error) return <p className="screen-request-status failed" role="alert">{error}</p>
  if (!data) return <p className="agent-library-empty">Loading…</p>
  const empty = !data.jobs.length && !data.routines.length && !data.screenshots.length && !(data.memory && (data.memory.profile + data.memory.log))
  if (empty) return <p className="agent-library-empty">Nothing yet. Files this agent changes, its scheduled checks, what it remembers and screenshots from its desktop will collect here.</p>

  return (
    <div className="agent-library">
      {data.memory && <section>
        <h4>Memory</h4>
        <p>{data.memory.profile} profile fact{data.memory.profile === 1 ? '' : 's'} · {data.memory.log} log entr{data.memory.log === 1 ? 'y' : 'ies'}</p>
      </section>}
      {data.routines.length > 0 && <section>
        <h4>Scheduled checks</h4>
        {data.routines.map(routine => (
          <div key={routine.id} className="agent-library-row">
            <strong>{routine.name}{routine.enabled ? '' : ' · paused'}</strong>
            <small><code>{routine.schedule}</code> · next {when(routine.next_run_at)} · last {when(routine.last_run_at)}</small>
          </div>
        ))}
      </section>}
      {data.jobs.length > 0 && <section>
        <h4>Files changed</h4>
        {data.jobs.map(job => (
          <div key={job.id} className="agent-library-row">
            <strong>{job.prompt_preview || job.id}</strong>
            <small>{job.state} · {when(job.updated_at)}</small>
            <div className="file-list">{(job.changed_files || []).map(file => <div key={file.path}><span className="file-status">{file.status || 'M'}</span><code>{file.path}</code></div>)}</div>
          </div>
        ))}
      </section>}
      {data.screenshots.length > 0 && <section>
        <h4>Screenshots</h4>
        <div className="agent-library-shots">{data.screenshots.map(shot => <Shot key={shot.id} agentId={agentId} id={shot.id} token={token} />)}</div>
      </section>}
    </div>
  )
}
