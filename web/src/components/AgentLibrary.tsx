import { useEffect, useState } from 'react'
import { formatDateTime, formatUpcoming, formatWhen, friendlyError, BadShapeError, useLoadingTimeout, jobStateLabel, scheduleLabel, type FriendlyError } from '../ux/knowledgeKit'

type LibraryData = {
  jobs: { id: string; state?: string; prompt_preview?: string; updated_at?: string; changed_files?: { status?: string; path: string }[] }[]
  routines: { id: number; name: string; schedule: string; enabled: boolean; next_run_at?: string | null; last_run_at?: string | null }[]
  memory: { profile: number; log: number } | null
  screenshots: { id: string; created_at?: string; width?: number; height?: number }[]
}

const FILE_STATUS: Record<string, { label: string; tone: string }> = {
  M: { label: '修改', tone: 'modified' },
  A: { label: '新增', tone: 'added' },
  '??': { label: '新增', tone: 'added' },
  D: { label: '删除', tone: 'deleted' },
  R: { label: '改名', tone: 'renamed' },
  C: { label: '复制', tone: 'added' },
}

function fileStatus(status?: string) {
  const key = (status || 'M').trim().toUpperCase()
  return FILE_STATUS[key] || FILE_STATUS[key[0]] || { label: key, tone: 'modified' }
}

function Shot({ agentId, id, token, createdAt }: { agentId: string; id: string; token: string; createdAt?: string }) {
  const [url, setUrl] = useState('')
  useEffect(() => {
    let active = true; let local = ''
    fetch(`/api/agents/${encodeURIComponent(agentId)}/screenshots/${encodeURIComponent(id)}`, { headers: { Authorization: `Bearer ${token}` } })
      .then(response => response.ok ? response.blob() : Promise.reject())
      .then(blob => { if (active) { local = URL.createObjectURL(blob); setUrl(local) } })
      .catch(() => {})
    return () => { active = false; if (local) URL.revokeObjectURL(local) }
  }, [agentId, id, token])
  const when = formatWhen(createdAt)
  return (
    <figure className="kn-shot">
      {url
        ? <a href={url} target="_blank" rel="noreferrer" className="agent-library-shot" title="在新标签页打开大图"><img src={url} alt={`助手电脑上的截图${when ? `（${when}）` : ''}`} /></a>
        : <span className="agent-library-shot loading" aria-label="截图加载中" />}
      {when && <figcaption title={formatDateTime(createdAt)}>{when}</figcaption>}
    </figure>
  )
}

/** What an agent has accumulated: files it changed, its scheduled checks, memory, screenshots. */
export function AgentLibrary({ agentId, token }: { agentId: string; token: string }) {
  const [data, setData] = useState<LibraryData | null>(null)
  const [error, setError] = useState<FriendlyError | null>(null)
  const [attempt, setAttempt] = useState(0)

  useLoadingTimeout(!data && !error, () => setError(friendlyError('timeout', '加载太久了，请稍后重试')))

  useEffect(() => {
    let active = true
    const load = () => {
      fetch(`/api/agents/${encodeURIComponent(agentId)}/library`, { headers: { Authorization: `Bearer ${token}` } })
        .then(async response => {
          if (!response.ok) {
            const body = await response.json().catch(() => ({} as { error?: string }))
            throw new Error(typeof body?.error === 'string' && body.error ? body.error : `HTTP ${response.status}`)
          }
          const body = await response.json().catch(() => null) as Partial<LibraryData> | null
          if (!body || typeof body !== 'object' || Array.isArray(body)) throw new BadShapeError('library')
          return {
            ...body,
            jobs: Array.isArray(body.jobs) ? body.jobs : [],
            routines: Array.isArray(body.routines) ? body.routines : [],
            screenshots: Array.isArray(body.screenshots) ? body.screenshots : [],
            memory: body.memory && typeof body.memory === 'object' ? body.memory : null,
          } as LibraryData
        })
        .then(value => { if (active) { setData(value); setError(null) } })
        .catch(reason => { if (active) setError(friendlyError(reason, '资料暂时读取不到，稍后会自动重试')) })
    }
    load()
    const timer = window.setInterval(load, 15000)
    return () => { active = false; window.clearInterval(timer) }
  }, [agentId, token, attempt])

  if (error && !data) {
    return (
      <p className="kn-library-error" role="alert" title={error.raw || undefined}>
        {error.message}{error.detail && <small>{error.detail}</small>}
        <button type="button" className="kn-btn" onClick={() => { setError(null); setAttempt(n => n + 1) }}>重试</button>
      </p>
    )
  }
  if (!data) return <p className="agent-library-empty">加载中…</p>
  const memoryCount = data.memory ? (Number(data.memory.profile) || 0) + (Number(data.memory.log) || 0) : 0
  const empty = !data.jobs.length && !data.routines.length && !data.screenshots.length && !memoryCount
  if (empty) {
    return (
      <div className="agent-library-empty kn-library-empty">
        <p>还没有资料。</p>
        <p>这个助手改过的文件、它的定时任务、记住的内容和电脑截图，都会汇总在这里。</p>
      </div>
    )
  }

  return (
    <div className="agent-library kn-library">
      {error && <p className="kn-library-stale" role="status" title={error.raw || undefined}>刷新失败，显示的是上次的内容。</p>}
      {data.memory && <section aria-label="记忆">
        <h4>记忆</h4>
        {memoryCount
          ? <p className="kn-library-memory"><span><strong>{data.memory.profile}</strong> 条常用档案</span><span><strong>{data.memory.log}</strong> 条记忆日志</span></p>
          : <p className="kn-library-muted">还没有记住任何内容。</p>}
      </section>}
      {data.routines.length > 0 && <section aria-label="定时任务">
        <h4>定时任务<span className="kn-library-count">{data.routines.length}</span></h4>
        {data.routines.map(routine => (
          <div key={routine.id} className="agent-library-row kn-library-row">
            <strong>{routine.name}{!routine.enabled && <span className="kn-library-tag">已暂停</span>}</strong>
            <small>
              {scheduleLabel(routine.schedule)}
              {routine.enabled && routine.next_run_at ? ` · 下次 ${formatUpcoming(routine.next_run_at)}` : ''}
            </small>
            {routine.last_run_at && <small>上次 {formatWhen(routine.last_run_at)}</small>}
          </div>
        ))}
      </section>}
      {data.jobs.length > 0 && <section aria-label="改过的文件">
        <h4>改过的文件</h4>
        {data.jobs.map(job => {
          const files = job.changed_files || []
          return (
            <div key={job.id} className="agent-library-row kn-library-row">
              <strong className="kn-library-job" title={job.prompt_preview || job.id}>{job.prompt_preview || job.id}</strong>
              <small>
                {job.state && <span className={job.state === 'failed' ? 'kn-tone-danger' : undefined}>{jobStateLabel(job.state)}</span>}
                {[formatWhen(job.updated_at), files.length ? `${files.length} 个文件` : ''].filter(Boolean).map((part, index) => (
                  <span key={index}>{job.state || index ? ' · ' : ''}{part}</span>
                ))}
              </small>
              {files.length > 0 && <ul className="kn-library-files" aria-label="文件列表">
                {files.map(file => {
                  const status = fileStatus(file.status)
                  return (
                    <li key={file.path}>
                      <span className={`kn-file-status is-${status.tone}`}>{status.label}</span>
                      <code title={file.path}>{file.path}</code>
                    </li>
                  )
                })}
              </ul>}
            </div>
          )
        })}
      </section>}
      {data.screenshots.length > 0 && <section aria-label="电脑截图">
        <h4>电脑截图</h4>
        <div className="agent-library-shots">{data.screenshots.map(shot => <Shot key={shot.id} agentId={agentId} id={shot.id} token={token} createdAt={shot.created_at} />)}</div>
      </section>}
    </div>
  )
}
