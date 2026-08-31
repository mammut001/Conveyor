import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { RuntimeOwnerCard } from './components/RuntimeOwnerCard'
import { TranscriptPanel } from './components/TranscriptPanel'
import { runtimeOwnerFromJob, terminalJobState, type TranscriptMessage } from './runtime'
import './v2.css'

type EventItem = {
  schema_version: number; event_id: string; sequence: number; timestamp: string
  kind: string; job_id: string; payload: Record<string, unknown>; tool_call_id?: string
}
type Job = {
  id: string; state: string; mode: string; channel: string; chat_id: string; operator_id?: string
  created_at: string; updated_at?: string; started_at?: string; finished_at?: string
  prompt_preview: string; metadata?: Record<string, string>; latest_event?: EventItem
  error?: string
  changed_files?: { status: string; path: string }[]
  runtime?: Record<string, unknown>
}
type Session = {
  id: string; channel?: string; title?: string; created_at: string; updated_at?: string; last_activity: string
  operator_id?: string; source_chat_id?: string; job_count: number; message_count?: number; latest_job?: Job
}
type SessionDetail = Session & { messages?: TranscriptMessage[]; jobs?: Job[] }
type Approval = { id: string; job_id: string; action: string; status: string; created_at?: string }
type NodeInfo = {
  id: string; name: string; type: string; status: string; last_seen_at?: string
  capabilities: string[]; metadata?: Record<string, unknown>
}
type SystemStatus = {
  uptime_seconds: number; load_average: number[]; cpu_count: number
  memory: { total: number | null; available: number | null }
  disk: { total: number; used: number; free: number }
  queue: { depth: number; paused: boolean; states: Record<string, number> }
  channels: Record<string, { configured: boolean }>; nodes: NodeInfo[]
}
type ComputerStatus = {
  armed: boolean; arm_remaining_seconds: number; active_task?: Record<string, unknown> | null
  screenshots: { artifact_id: string; created_at?: string; width?: number; height?: number }[]
}

const statusOrder = ['running', 'queued', 'interrupted', 'failed', 'cancelled', 'completed']

function formatTime(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
function bytes(value: number | null) {
  if (value == null) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']; let n = value; let i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(i > 1 ? 1 : 0)} ${units[i]}`
}
function stateLabel(state?: string) {
  return (state || 'unknown').replace('_', ' ')
}
function sessionLabel(session: Session | undefined) {
  return session?.title || session?.latest_job?.prompt_preview || 'New session'
}

export default function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem('conveyor-token') || '')
  const [tokenDraft, setTokenDraft] = useState('')
  const [authenticated, setAuthenticated] = useState(false)
  const [sessions, setSessions] = useState<Session[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [selectedSessionId, setSelectedSessionId] = useState('')
  const [selectedJobId, setSelectedJobId] = useState('')
  const [creatingSession, setCreatingSession] = useState(false)
  const [transcript, setTranscript] = useState<TranscriptMessage[]>([])
  const [events, setEvents] = useState<EventItem[]>([])
  const [approvals, setApprovals] = useState<Approval[]>([])
  const [nodes, setNodes] = useState<NodeInfo[]>([])
  const [system, setSystem] = useState<SystemStatus | null>(null)
  const [computer, setComputer] = useState<ComputerStatus | null>(null)
  const [diff, setDiff] = useState('')
  const [prompt, setPrompt] = useState('')
  const [mode, setMode] = useState<'run' | 'fix'>('run')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const lastSequence = useRef(0)

  const api = useCallback(async <T,>(path: string, init?: RequestInit): Promise<T> => {
    const response = await fetch(path, {
      ...init,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers || {}) },
    })
    if (response.status === 401) { setAuthenticated(false); throw new Error('Token rejected') }
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.error || body.message || `Request failed (${response.status})`)
    return body as T
  }, [token])

  const refresh = useCallback(async () => {
    if (!token) return
    try {
      const [sessionData, jobData, approvalData, nodeData, systemData, computerData] = await Promise.all([
        api<{ sessions: Session[] }>('/api/sessions'), api<{ jobs: Job[] }>('/api/jobs'),
        api<{ approvals: Approval[] }>('/api/approvals'), api<{ nodes: NodeInfo[] }>('/api/nodes'),
        api<SystemStatus>('/api/system/status'), api<ComputerStatus>('/api/computer/status'),
      ])
      setSessions(sessionData.sessions); setJobs(jobData.jobs); setApprovals(approvalData.approvals)
      setNodes(nodeData.nodes); setSystem(systemData); setComputer(computerData); setAuthenticated(true); setError('')
      if (!creatingSession && !selectedSessionId) {
        const initialSession = sessionData.sessions[0]
        if (initialSession) {
          setSelectedSessionId(initialSession.id)
          setSelectedJobId(initialSession.latest_job?.id || '')
        } else if (jobData.jobs[0]) {
          setSelectedSessionId(jobData.jobs[0].chat_id)
          setSelectedJobId(jobData.jobs[0].id)
        }
      } else if (!creatingSession && !selectedJobId && selectedSessionId) {
        const session = sessionData.sessions.find(item => item.id === selectedSessionId)
        if (session?.latest_job) setSelectedJobId(session.latest_job.id)
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not connect') }
  }, [api, creatingSession, selectedJobId, selectedSessionId, token])

  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => {
    if (!authenticated) return
    const timer = window.setInterval(() => void refresh(), 15_000)
    return () => window.clearInterval(timer)
  }, [authenticated, refresh])

  useEffect(() => {
    if (!authenticated || !selectedSessionId) { setTranscript([]); return }
    let stopped = false
    void api<SessionDetail>(`/api/sessions/${encodeURIComponent(selectedSessionId)}`)
      .then(session => { if (!stopped) setTranscript(session.messages || []) })
      .catch(() => { if (!stopped) setTranscript([]) })
    return () => { stopped = true }
  }, [api, authenticated, selectedSessionId])

  useEffect(() => {
    if (!authenticated || !selectedJobId) return
    let stopped = false; let controller: AbortController | null = null; let retry: number | undefined
    lastSequence.current = 0; setEvents([]); setDiff('')
    void api<{ events: EventItem[] }>(`/api/jobs/${selectedJobId}/events`).then(({ events: initial }) => {
      if (stopped) return
      const unique = [...new Map(initial.map(item => [item.event_id, item])).values()]
      setEvents(unique.slice(-1000)); lastSequence.current = unique.at(-1)?.sequence || 0
    }).catch(reason => setError(String(reason)))
    void api<{ diff: string }>(`/api/jobs/${selectedJobId}/diff`).then(data => !stopped && setDiff(data.diff)).catch(() => {})

    const connect = async () => {
      controller = new AbortController()
      try {
        const response = await fetch(`/api/events/stream?job_id=${encodeURIComponent(selectedJobId)}&after=${lastSequence.current}`, {
          headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
        })
        if (!response.ok || !response.body) throw new Error('Realtime unavailable')
        const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''
        while (!stopped) {
          const { value, done } = await reader.read(); if (done) break
          buffer += decoder.decode(value, { stream: true })
          const frames = buffer.split('\n\n'); buffer = frames.pop() || ''
          for (const frame of frames) {
            const line = frame.split('\n').find(part => part.startsWith('data: ')); if (!line) continue
            const event = JSON.parse(line.slice(6)) as EventItem
            lastSequence.current = Math.max(lastSequence.current, event.sequence)
            setEvents(previous => previous.some(item => item.event_id === event.event_id) ? previous : [...previous, event].slice(-1000))
          }
        }
      } catch { /* reconnect below unless the selection changed */ }
      if (!stopped) retry = window.setTimeout(() => void connect(), 1500)
    }
    void connect()
    return () => { stopped = true; controller?.abort(); if (retry) window.clearTimeout(retry) }
  }, [api, authenticated, selectedJobId, token])

  const selectedJob = useMemo(() => jobs.find(job => job.id === selectedJobId), [jobs, selectedJobId])
  const selectedSession = useMemo(() => sessions.find(session => session.id === selectedSessionId), [sessions, selectedSessionId])
  useEffect(() => {
    if (!selectedJob || creatingSession) return
    const matching = sessions.find(session => session.channel === selectedJob.channel
      && session.operator_id === selectedJob.operator_id
      && session.source_chat_id === selectedJob.chat_id)
    const target = matching?.id || selectedJob.chat_id
    if (target && target !== selectedSessionId) setSelectedSessionId(target)
  }, [creatingSession, selectedJob, selectedSessionId, sessions])
  const pendingForJob = approvals.filter(item => item.job_id === selectedJobId)
  const runtimeOwner = runtimeOwnerFromJob(selectedJob)
  const terminalHasTranscript = Boolean(selectedJob && terminalJobState(selectedJob.state)
    && transcript.some(message => message.role === 'assistant'
      && (!message.job_id || message.job_id === selectedJob.id)))
  const visibleEvents = terminalHasTranscript
    ? events.filter(item => !item.kind.startsWith('assistant.'))
    : events

  async function submit(event: FormEvent) {
    event.preventDefault(); if (!prompt.trim() || busy) return
    setBusy(true); setError('')
    try {
      const result = await api<{ job_id: string; session_id?: string }>('/api/tasks', {
        method: 'POST', body: JSON.stringify({ prompt: prompt.trim(), mode, session_id: selectedSessionId || undefined }),
      })
      setPrompt(''); await refresh()
      setCreatingSession(false)
      if (result.session_id) setSelectedSessionId(result.session_id)
      if (result.job_id) setSelectedJobId(result.job_id)
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Submit failed') }
    finally { setBusy(false) }
  }
  async function action(path: string, body: object = {}) {
    setBusy(true); setError('')
    try { await api(path, { method: 'POST', body: JSON.stringify(body) }); await refresh() }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Action failed') }
    finally { setBusy(false) }
  }
  function unlock(event: FormEvent) {
    event.preventDefault(); const value = tokenDraft.trim(); if (!value) return
    sessionStorage.setItem('conveyor-token', value); setToken(value); setTokenDraft('')
  }

  if (!authenticated) return <main className="unlock-shell">
    <form className="unlock-card" onSubmit={unlock}>
      <div className="brand-mark">C</div><p className="eyebrow">SECURE CONTROL PLANE</p>
      <h1>Open Conveyor</h1><p>Enter the bearer token configured on your VPS. It stays in this browser tab only.</p>
      <label>Console token<input type="password" autoFocus value={tokenDraft} onChange={event => setTokenDraft(event.target.value)} placeholder="32+ character token" /></label>
      {error && <div className="error-banner">{error}</div>}<button className="primary" type="submit">Unlock console</button>
    </form>
  </main>

  return <main className="app-shell">
    <header className="topbar">
      <div className="brand"><span className="brand-mark small">C</span><div><strong>Conveyor</strong><small>CONTROL CONSOLE</small></div></div>
      <div className="top-status"><span className="live-dot" /> Online <span className="separator" /> Queue {system?.queue.depth ?? 0}</div>
    </header>
    {error && <div className="error-banner global">{error}<button onClick={() => setError('')}>×</button></div>}
    <section className="workspace">
      <aside className="sessions-panel panel">
        <div className="panel-heading"><div><p className="eyebrow">WORKSPACES</p><h2>Sessions</h2></div><button className="icon-button" onClick={() => { setCreatingSession(true); setSelectedSessionId(''); setSelectedJobId(''); setTranscript([]); setPrompt('') }} aria-label="New session">＋</button></div>
        <div className="session-list">
          {sessions.map(session => <button key={session.id} className={`session-item ${session.id === selectedSessionId ? 'active' : ''}`} onClick={() => { setCreatingSession(false); setSelectedSessionId(session.id); setSelectedJobId(session.latest_job?.id || '') }}>
            <span className={`status-rail ${session.latest_job?.state || ''}`} /><span><strong>{session.title || 'Untitled session'}</strong><small>{session.message_count ?? 0} messages · {session.job_count} job{session.job_count === 1 ? '' : 's'} · {formatTime(session.last_activity)}</small></span>
          </button>)}
          {!sessions.length && <Empty text="No sessions yet" />}
        </div>
        <div className="queue-summary"><p className="eyebrow">ACTIVE QUEUE</p>{(['running', 'queued'] as const).map(state => <div key={state}><span>{state}</span><strong>{system?.queue.states[state] || 0}</strong></div>)}<p className="history-note">History · {(['interrupted', 'failed', 'cancelled', 'completed'] as const).reduce((total, state) => total + (system?.queue.states[state] || 0), 0)} terminal tasks</p></div>
      </aside>

      <section className="stream-panel panel">
        <div className="stream-header">
          <div><p className="eyebrow">CONVERSATION + LIVE EXECUTION</p><h2>{creatingSession ? 'New session' : sessionLabel(selectedSession)}</h2></div>
          {selectedJob && <StatusBadge state={selectedJob.state} />}
        </div>
        <div className="event-stream">
          {selectedJob?.state === 'failed' && <div className="job-notice failed"><strong>Task failed</strong><span>{selectedJob.error || 'See the execution timeline below for details.'}</span></div>}
          {selectedJob?.state === 'cancelled' && <div className="job-notice"><strong>Task cancelled</strong><span>This task is terminal; start a new message to try again.</span></div>}
          {transcript.length > 0 && <><div className="stream-divider">Conversation history</div><TranscriptPanel messages={transcript} /></>}
          {selectedJob && <div className="stream-divider">Job {selectedJob.id} execution</div>}
          {selectedJob && !transcript.length && <article className="event-card user-event"><div className="event-meta"><span>YOU</span><time>{formatTime(selectedJob.created_at)}</time></div><p>{selectedJob.prompt_preview}</p></article>}
          {visibleEvents.map(item => <EventCard key={item.event_id} item={item} />)}
          {!selectedJob && !selectedSessionId && <div className="welcome-state"><div className="brand-mark">C</div><h2>What should Conveyor do?</h2><p>Start a task below. It will enter the same persistent queue used by Telegram and Feishu.</p></div>}
          {selectedJob && !events.length && <Empty text="Waiting for the first event…" />}
        </div>
        <form className="composer" onSubmit={submit}>
          <div className="mode-switch"><button type="button" className={mode === 'run' ? 'active' : ''} onClick={() => setMode('run')}>Ask</button><button type="button" className={mode === 'fix' ? 'active' : ''} onClick={() => setMode('fix')}>Fix</button></div>
          <textarea value={prompt} onChange={event => setPrompt(event.target.value)} placeholder="Ask Conveyor…" rows={2} maxLength={8000} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} />
          <button className="send-button" disabled={!prompt.trim() || busy}>{busy ? '…' : 'Send'} <span>↗</span></button>
        </form>
      </section>

      <aside className="context-panel panel">
        <ContextSection title="Job">
          {selectedJob ? <>
            <KeyValue label="ID" value={selectedJob.id} mono /><KeyValue label="State" value={selectedJob.state} />
            <KeyValue label="Provider" value="Codex" /><KeyValue label="Mode" value={selectedJob.mode} />
            <KeyValue label="Started" value={formatTime(selectedJob.started_at)} />
            <RuntimeOwnerCard owner={runtimeOwner} state={selectedJob.state} />
            <div className="action-row"><button disabled={busy || !['queued','running'].includes(selectedJob.state)} onClick={() => action(`/api/jobs/${selectedJob.id}/cancel`)}>Cancel</button></div>
          </> : <Empty text="Select a job" />}
        </ContextSection>
        {pendingForJob.map(approval => <section className="approval-card" key={approval.id}><p className="eyebrow">APPROVAL REQUIRED</p><h3>{approval.action === 'apply' ? 'Apply changes' : 'Discard worktree'}?</h3><p>This decision is scoped to job <code>{approval.job_id}</code> and expires automatically.</p><div className="action-row"><button className="danger" onClick={() => action(`/api/approvals/${approval.id}/reject`)}>Reject</button><button className="primary" onClick={() => action(`/api/approvals/${approval.id}/approve`)}>Approve</button></div></section>)}
        <ContextSection title="Changes">
          <div className="file-list">{selectedJob?.changed_files?.map(file => <div key={file.path}><span className="file-status">{file.status || 'M'}</span><code>{file.path}</code></div>)}{selectedJob && !selectedJob.changed_files?.length && <Empty text="No changed files" />}</div>
          {selectedJob && <><details className="diff-view"><summary>Unified diff</summary><pre>{diff || 'No diff available.'}</pre></details><div className="action-row"><button className="danger" disabled={busy} onClick={() => action(`/api/jobs/${selectedJob.id}/discard`)}>Discard…</button><button className="primary" disabled={busy} onClick={() => action(`/api/jobs/${selectedJob.id}/apply`)}>Apply…</button></div></>}
        </ContextSection>
        <ContextSection title="Computer">
          <KeyValue label="CUA" value={computer?.armed ? `Armed · ${computer.arm_remaining_seconds}s` : 'Disarmed'} />
          {computer?.active_task && <KeyValue label="Task" value={String(computer.active_task.status || computer.active_task.task_id || 'active')} />}
          {computer?.screenshots[0] && <AuthenticatedImage artifact={computer.screenshots[0]} token={token} />}
          {nodes.map(node => <div className="node-card" key={node.id}><div><span className={`node-dot ${node.status}`} /><strong>{node.name}</strong></div><small>{node.type} · {node.status}<br />Last seen {formatTime(node.last_seen_at)}</small></div>)}
          {!nodes.length && <Empty text="No execution nodes" />}
          <button className="emergency" onClick={() => action('/api/computer/stop')}>■ Emergency stop</button>
        </ContextSection>
        <ContextSection title="System">
          <KeyValue label="Load" value={system?.load_average.slice(0, 2).map(n => n.toFixed(2)).join(' / ') || '—'} />
          <KeyValue label="Memory free" value={bytes(system?.memory.available ?? null)} /><KeyValue label="Disk free" value={bytes(system?.disk.free ?? null)} />
          <KeyValue label="Telegram" value={system?.channels.telegram.configured ? 'Configured' : 'Off'} /><KeyValue label="Feishu" value={system?.channels.feishu.configured ? 'Configured' : 'Off'} />
        </ContextSection>
      </aside>
    </section>
  </main>
}

function StatusBadge({ state }: { state: string }) { return <span className={`status-badge ${state}`}><i />{stateLabel(state)}</span> }
function Empty({ text }: { text: string }) { return <div className="empty">{text}</div> }
function KeyValue({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) { return <div className="key-value"><span>{label}</span><strong className={mono ? 'mono' : ''}>{value}</strong></div> }
function ContextSection({ title, children }: { title: string; children: React.ReactNode }) { return <section className="context-section"><p className="eyebrow">{title.toUpperCase()}</p>{children}</section> }
function AuthenticatedImage({ artifact, token }: { artifact: ComputerStatus['screenshots'][number]; token: string }) {
  const [url, setUrl] = useState('')
  useEffect(() => {
    let active = true; let localUrl = ''
    void fetch(`/api/artifacts/${encodeURIComponent(artifact.artifact_id)}`, { headers: { Authorization: `Bearer ${token}` } })
      .then(response => response.ok ? response.blob() : Promise.reject())
      .then(blob => { if (active) { localUrl = URL.createObjectURL(blob); setUrl(localUrl) } })
      .catch(() => {})
    return () => { active = false; if (localUrl) URL.revokeObjectURL(localUrl) }
  }, [artifact.artifact_id, token])
  return url ? <figure className="screenshot"><img src={url} alt="Latest Mac node screenshot" /><figcaption>Latest screenshot · {formatTime(artifact.created_at)}</figcaption></figure> : null
}
function EventCard({ item }: { item: EventItem }) {
  const isTool = item.kind.startsWith('tool.'); const text = String(item.payload.text || item.payload.output || item.payload.result || item.payload.error || '')
  if (isTool) return <details className={`event-card tool-event ${item.kind.endsWith('failed') ? 'failed' : ''}`} open={item.kind.endsWith('failed')}>
    <summary><span className="tool-icon">⌘</span><span><strong>{String(item.payload.name || 'Tool')}</strong><small>{item.kind.replace('tool.', '')}</small></span><time>{formatTime(item.timestamp)}</time><b>⌄</b></summary>
    {text && <pre>{text.slice(0, 12000)}</pre>}
  </details>
  if (item.kind.startsWith('approval.')) return <article className="event-card approval-event"><div className="event-meta"><span>APPROVAL</span><time>{formatTime(item.timestamp)}</time></div><p>{item.kind.replace('.', ' ')} · {String(item.payload.action || '')}</p></article>
  return <article className={`event-card ${item.kind.startsWith('assistant.') ? 'assistant-event' : 'system-event'}`}><div className="event-meta"><span>{item.kind.startsWith('assistant.') ? 'CONVEYOR' : item.kind.toUpperCase()}</span><time>{formatTime(item.timestamp)}</time></div><p>{text || item.kind.replace('.', ' ')}</p></article>
}
