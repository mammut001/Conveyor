import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ContextDrawer, type DrawerKind } from './components/ContextDrawer'
import { ProviderSettings } from './components/ProviderSettings'
import { TranscriptPanel } from './components/TranscriptPanel'
import { WorkspaceSidebar } from './components/WorkspaceSidebar'
import { runtimeOwnerFromJob, terminalJobState, type TranscriptMessage } from './runtime'
import type { Approval, ComputerStatus, EventItem, Job, NodeInfo, ProviderConfig, Session, SessionDetail, SystemStatus } from './types'

function formatTime(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function stateLabel(state?: string) {
  return (state || 'unknown').replaceAll('_', ' ')
}

function sessionLabel(session: Session | undefined, fallbackJob?: Job) {
  return session?.title || session?.latest_job?.prompt_preview || fallbackJob?.prompt_preview || 'New chat'
}

function eventText(item: EventItem) {
  return String(item.payload.text || item.payload.output || item.payload.result || item.payload.error || '')
}

function StatusBadge({ state }: { state: string }) {
  return <span className={`v3-status-badge ${state}`}><i />{stateLabel(state)}</span>
}

function Empty({ title, text }: { title: string; text: string }) {
  return <div className="v3-empty"><div className="v3-empty-mark">C</div><h2>{title}</h2><p>{text}</p></div>
}

function ActivityEvent({ item }: { item: EventItem }) {
  const text = eventText(item)
  const isTool = item.kind.startsWith('tool.')
  const failed = item.kind.endsWith('failed') || item.kind.includes('error')
  return <div className={`v3-activity-row ${failed ? 'failed' : ''}`}>
    <span className={`v3-activity-icon ${isTool ? 'tool' : ''}`}>{isTool ? '⌘' : '·'}</span>
    <span className="v3-activity-copy"><strong>{isTool ? String(item.payload.name || 'Tool') : stateLabel(item.kind)}</strong>{text && <small>{text.slice(0, 180)}</small>}</span>
    <time>{formatTime(item.timestamp)}</time>
  </div>
}

function ApprovalCard({ approval, busy, onAction }: { approval: Approval; busy: boolean; onAction: (path: string, body?: object) => Promise<void> }) {
  const apply = approval.action === 'apply'
  return <article className="v3-approval-card">
    <div className="v3-approval-icon">!</div>
    <div className="v3-approval-copy"><span className="v3-kicker">APPROVAL REQUIRED</span><h3>{apply ? 'Apply these changes?' : 'Discard this worktree?'}</h3><p>This decision is scoped to the current task and expires automatically.</p></div>
    <div className="v3-approval-actions"><button disabled={busy} onClick={() => void onAction(`/api/approvals/${approval.id}/reject`)}>Reject</button><button className="v3-primary" disabled={busy} onClick={() => void onAction(`/api/approvals/${approval.id}/approve`)}>Approve</button></div>
  </article>
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
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [providerConfig, setProviderConfig] = useState<ProviderConfig | null>(null)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => window.innerWidth <= 900)
  const [drawer, setDrawer] = useState<DrawerKind | null>(null)
  const lastSequence = useRef(0)
  const streamRef = useRef<HTMLDivElement>(null)
  const composerRef = useRef<HTMLTextAreaElement>(null)

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
        api<{ sessions: Session[] }>('/api/sessions'),
        api<{ jobs: Job[] }>('/api/jobs'),
        api<{ approvals: Approval[] }>('/api/approvals'),
        api<{ nodes: NodeInfo[] }>('/api/nodes'),
        api<SystemStatus>('/api/system/status'),
        api<ComputerStatus>('/api/computer/status'),
      ])
      setSessions(sessionData.sessions)
      setJobs(jobData.jobs)
      setApprovals(approvalData.approvals)
      setNodes(nodeData.nodes)
      setSystem(systemData)
      setComputer(computerData)
      setAuthenticated(true)
      setError('')
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
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not connect')
    }
  }, [api, creatingSession, selectedJobId, selectedSessionId, token])

  const refreshTranscript = useCallback(async () => {
    if (!authenticated || !selectedSessionId) { setTranscript([]); return }
    try {
      const session = await api<SessionDetail>(`/api/sessions/${encodeURIComponent(selectedSessionId)}`)
      setTranscript(session.messages || [])
    } catch {
      setTranscript([])
    }
  }, [api, authenticated, selectedSessionId])

  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => {
    if (!authenticated) return
    const timer = window.setInterval(() => { void refresh(); void refreshTranscript() }, 3_000)
    return () => window.clearInterval(timer)
  }, [authenticated, refresh, refreshTranscript])
  useEffect(() => { void refreshTranscript() }, [refreshTranscript])

  useEffect(() => {
    if (!authenticated || providerConfig) return
    void api<ProviderConfig>('/api/config/provider').then(setProviderConfig).catch(() => {})
  }, [api, authenticated, providerConfig])

  useEffect(() => {
    if (!authenticated || !selectedJobId) return
    let stopped = false
    let controller: AbortController | null = null
    let retry: number | undefined
    lastSequence.current = 0
    setEvents([])
    setDiff('')

    void api<{ events: EventItem[] }>(`/api/jobs/${selectedJobId}/events`).then(({ events: initial }) => {
      if (stopped) return
      const unique = [...new Map(initial.map(item => [item.event_id, item])).values()]
      setEvents(unique.slice(-1000))
      lastSequence.current = unique.at(-1)?.sequence || 0
    }).catch(reason => setError(String(reason)))
    void api<{ diff: string }>(`/api/jobs/${selectedJobId}/diff`).then(data => !stopped && setDiff(data.diff)).catch(() => {})

    const connect = async () => {
      controller = new AbortController()
      try {
        const response = await fetch(`/api/events/stream?job_id=${encodeURIComponent(selectedJobId)}&after=${lastSequence.current}`, {
          headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
        })
        if (!response.ok || !response.body) throw new Error('Realtime unavailable')
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        while (!stopped) {
          const { value, done } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const frames = buffer.split('\n\n')
          buffer = frames.pop() || ''
          for (const frame of frames) {
            const line = frame.split('\n').find(part => part.startsWith('data: '))
            if (!line) continue
            const event = JSON.parse(line.slice(6)) as EventItem
            lastSequence.current = Math.max(lastSequence.current, event.sequence)
            setEvents(previous => previous.some(item => item.event_id === event.event_id) ? previous : [...previous, event].slice(-1000))
            if (event.kind.startsWith('assistant.') || event.kind.startsWith('task.')) {
              void refresh()
              void refreshTranscript()
            }
          }
        }
      } catch { /* reconnect unless selection changed */ }
      if (!stopped) retry = window.setTimeout(() => void connect(), 1500)
    }
    void connect()
    return () => { stopped = true; controller?.abort(); if (retry) window.clearTimeout(retry) }
  }, [api, authenticated, refresh, refreshTranscript, selectedJobId, token])

  const selectedJob = useMemo(() => jobs.find(job => job.id === selectedJobId), [jobs, selectedJobId])
  const selectedSession = useMemo(() => sessions.find(session => session.id === selectedSessionId), [sessions, selectedSessionId])
  const pendingForJob = approvals.filter(item => item.job_id === selectedJobId && item.status === 'pending')
  const runtimeOwner = runtimeOwnerFromJob(selectedJob)
  const terminalHasTranscript = Boolean(selectedJob && terminalJobState(selectedJob.state)
    && transcript.some(message => message.role === 'assistant' && (!message.job_id || message.job_id === selectedJob.id)))
  const activityEvents = events.filter(item => !item.kind.startsWith('assistant.') && !item.kind.startsWith('approval.'))
  const completedAssistant = [...events].reverse().find(item => item.kind === 'assistant.completed')
  const liveAssistantText = terminalHasTranscript ? '' : completedAssistant
    ? eventText(completedAssistant)
    : events.filter(item => item.kind === 'assistant.delta').map(eventText).join('')
  const latestTool = [...activityEvents].reverse().find(item => item.kind.startsWith('tool.'))

  useEffect(() => {
    if (!selectedJob || creatingSession) return
    const matching = sessions.find(session => session.channel === selectedJob.channel
      && session.operator_id === selectedJob.operator_id
      && session.source_chat_id === selectedJob.chat_id)
    const target = matching?.id || selectedJob.chat_id
    if (target && target !== selectedSessionId) setSelectedSessionId(target)
  }, [creatingSession, selectedJob, selectedSessionId, sessions])

  useEffect(() => {
    const node = streamRef.current
    if (node) node.scrollTo({ top: node.scrollHeight, behavior: 'smooth' })
  }, [events.length, pendingForJob.length, transcript.length, selectedJobId])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      if (settingsOpen) { setSettingsOpen(false); return }
      if (drawer) { setDrawer(null); return }
      if (!sidebarCollapsed && window.innerWidth <= 900) setSidebarCollapsed(true)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [drawer, settingsOpen, sidebarCollapsed])

  useEffect(() => {
    const collapseOnNarrow = () => {
      if (window.innerWidth <= 900) setSidebarCollapsed(true)
    }
    window.addEventListener('resize', collapseOnNarrow)
    return () => window.removeEventListener('resize', collapseOnNarrow)
  }, [])

  async function openSettings() {
    setSettingsOpen(true)
    setError('')
    try { setProviderConfig(await api<ProviderConfig>('/api/config/provider')) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not load settings') }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!prompt.trim() || busy) return
    setBusy(true)
    setError('')
    try {
      const result = await api<{ job_id: string; session_id?: string }>('/api/tasks', {
        method: 'POST', body: JSON.stringify({ prompt: prompt.trim(), mode, session_id: selectedSessionId || undefined }),
      })
      setPrompt('')
      await refresh()
      setCreatingSession(false)
      if (result.session_id) setSelectedSessionId(result.session_id)
      if (result.job_id) setSelectedJobId(result.job_id)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Submit failed')
    } finally {
      setBusy(false)
      composerRef.current?.focus()
    }
  }

  async function action(path: string, body: object = {}) {
    setBusy(true)
    setError('')
    try {
      await api(path, { method: 'POST', body: JSON.stringify(body) })
      await refresh()
      await refreshTranscript()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Action failed')
    } finally {
      setBusy(false)
    }
  }

  function newSession() {
    setCreatingSession(true)
    if (window.innerWidth <= 900) setSidebarCollapsed(true)
    setSelectedSessionId('')
    setSelectedJobId('')
    setTranscript([])
    setEvents([])
    setDiff('')
    setPrompt('')
    setDrawer(null)
    window.setTimeout(() => composerRef.current?.focus(), 0)
  }

  function selectSession(session: Session) {
    setCreatingSession(false)
    if (window.innerWidth <= 900) setSidebarCollapsed(true)
    setSelectedSessionId(session.id)
    setSelectedJobId(session.latest_job?.id || '')
    setDrawer(null)
  }

  function unlock(event: FormEvent) {
    event.preventDefault()
    const value = tokenDraft.trim()
    if (!value) return
    sessionStorage.setItem('conveyor-token', value)
    setToken(value)
    setTokenDraft('')
  }

  if (!authenticated) return <main className="unlock-shell">
    <form className="unlock-card" onSubmit={unlock}>
      <div className="brand-mark">C</div><p className="eyebrow">SECURE CONTROL PLANE</p>
      <h1>Open Conveyor</h1><p>Enter the bearer token configured on your VPS. It stays in this browser tab only.</p>
      <label>Console token<input type="password" autoFocus value={tokenDraft} onChange={event => setTokenDraft(event.target.value)} placeholder="32+ character token" /></label>
      {error && <div className="error-banner">{error}</div>}<button className="primary" type="submit">Unlock console</button>
    </form>
  </main>

  const changeCount = selectedJob?.changed_files?.length || 0
  const providerLabel = providerConfig ? `${providerConfig.provider_name} · ${providerConfig.model}` : 'Provider'

  return <main className={`v3-app ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
    <WorkspaceSidebar
      sessions={sessions}
      selectedSessionId={selectedSessionId}
      system={system}
      collapsed={sidebarCollapsed}
      onToggleCollapsed={() => setSidebarCollapsed(value => !value)}
      onNewSession={newSession}
      onSelectSession={selectSession}
    />

    <section className="v3-main">
      <header className="v3-global-header">
        <div className="v3-mobile-brand"><button className="v3-icon-button" onClick={() => setSidebarCollapsed(value => !value)} aria-label={sidebarCollapsed ? 'Open sidebar' : 'Close sidebar'}>☰</button><strong>Conveyor</strong></div>
        <div className="v3-global-actions">
          <button className="v3-provider-pill" onClick={() => void openSettings()}><span className={`v3-provider-dot ${providerConfig?.health?.status || 'healthy'}`} />{providerLabel}</button>
          <button className="v3-icon-button wide" onClick={() => setDrawer('computer')}>Computer</button>
          <button className="v3-icon-button wide" onClick={() => setDrawer('system')}>System</button>
          <button className="v3-icon-button" onClick={() => void openSettings()} aria-label="Settings">⚙</button>
        </div>
      </header>
      {error && <div className="v3-error-banner"><span>{error}</span><button onClick={() => setError('')}>×</button></div>}

      <header className="v3-conversation-header">
        <div className="v3-conversation-identity"><span className="v3-agent-avatar">C</span><div><h1>{creatingSession ? 'New chat' : sessionLabel(selectedSession, selectedJob)}</h1><p>{selectedJob ? `${selectedJob.channel} · ${selectedJob.mode === 'fix' ? 'Fix' : 'Ask'}` : 'Ready for a new task'}</p></div></div>
        <div className="v3-conversation-actions">
          {selectedJob && <StatusBadge state={selectedJob.state} />}
          {selectedJob && <button onClick={() => setDrawer('changes')} className={changeCount ? 'has-changes' : ''}>Changes{changeCount ? ` ${changeCount}` : ''}</button>}
          {selectedJob && <button onClick={() => setDrawer('details')}>Details</button>}
        </div>
      </header>

      <div className="v3-thread" ref={streamRef}>
        <div className="v3-thread-inner">
          {selectedJob?.state === 'failed' && <div className="v3-task-notice failed"><span>!</span><div><strong>Task failed</strong><p>{selectedJob.error || 'Open task details for the failure information.'}</p></div><button onClick={() => setDrawer('details')}>Details</button></div>}
          {selectedJob?.state === 'cancelled' && <div className="v3-task-notice"><span>×</span><div><strong>Task cancelled</strong><p>The task is terminal. You can continue the conversation with a new message.</p></div></div>}

          {transcript.length > 0 && <TranscriptPanel messages={transcript} />}
          {selectedJob && !transcript.length && <article className="v3-message user"><header><span>You</span><time>{formatTime(selectedJob.created_at)}</time></header><div>{selectedJob.prompt_preview}</div></article>}
          {liveAssistantText && <article className="v3-message assistant live"><header><span><i className="v3-live-dot" />Conveyor</span><time>working</time></header><div>{liveAssistantText}</div></article>}

          {pendingForJob.map(approval => <ApprovalCard key={approval.id} approval={approval} busy={busy} onAction={action} />)}

          {selectedJob && activityEvents.length > 0 && <details className="v3-activity-card" open={['running', 'failed', 'interrupted'].includes(selectedJob.state)}>
            <summary><span className={`v3-activity-state ${selectedJob.state}`}><i /></span><span className="v3-activity-summary"><strong>{selectedJob.state === 'running' ? (latestTool ? `Working · ${String(latestTool.payload.name || 'tool')}` : 'Working') : 'Execution activity'}</strong><small>{activityEvents.length} event{activityEvents.length === 1 ? '' : 's'} · click to {['running','failed','interrupted'].includes(selectedJob.state) ? 'collapse' : 'expand'}</small></span><span>⌄</span></summary>
            <div className="v3-activity-list">{activityEvents.slice(-80).map(item => <ActivityEvent key={item.event_id} item={item} />)}</div>
          </details>}

          {selectedJob && changeCount > 0 && <article className="v3-changes-card"><div><span className="v3-kicker">WORKTREE CHANGES</span><h3>{changeCount} file{changeCount === 1 ? '' : 's'} changed</h3><p>Review the diff before deciding whether these changes should land.</p></div><div><button onClick={() => setDrawer('changes')}>View changes</button><button className="v3-danger" disabled={busy} onClick={() => void action(`/api/jobs/${selectedJob.id}/discard`)}>Discard…</button><button className="v3-primary" disabled={busy} onClick={() => void action(`/api/jobs/${selectedJob.id}/apply`)}>Apply…</button></div></article>}

          {!selectedJob && !selectedSessionId && <Empty title="What should Conveyor do?" text="Ask a question or start a Fix task. The conversation will stay clean while execution details remain available on demand." />}
          {selectedJob && !events.length && !transcript.length && <div className="v3-loading-row"><span className="v3-spinner" /> Waiting for the first event…</div>}
        </div>
      </div>

      <form className="v3-composer-wrap" onSubmit={submit}>
        <div className="v3-composer">
          <textarea ref={composerRef} value={prompt} onChange={event => setPrompt(event.target.value)} placeholder={mode === 'fix' ? 'Describe what should change…' : 'Ask Conveyor…'} rows={1} maxLength={8000} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} />
          <div className="v3-composer-footer">
            <div className="v3-mode-switch"><button type="button" className={mode === 'run' ? 'active' : ''} onClick={() => setMode('run')}>Ask</button><button type="button" className={mode === 'fix' ? 'active' : ''} onClick={() => setMode('fix')}>Fix</button></div>
            <span className="v3-composer-hint">Shift ↵ for newline</span>
            <button className="v3-send" disabled={!prompt.trim() || busy} aria-label="Send">{busy ? '…' : '↑'}</button>
          </div>
        </div>
      </form>
    </section>

    <ContextDrawer kind={drawer} job={selectedJob} diff={diff} runtimeOwner={runtimeOwner} computer={computer} nodes={nodes} system={system} token={token} busy={busy} onClose={() => setDrawer(null)} onAction={action} />

    {settingsOpen && <ProviderSettings config={providerConfig} busy={busy} onClose={() => setSettingsOpen(false)} onSave={async payload => {
      setBusy(true)
      setError('')
      try {
        const result = await api<{ config: ProviderConfig }>('/api/config/provider', { method: 'POST', body: JSON.stringify(payload) })
        setProviderConfig(result.config)
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : 'Could not save settings')
        throw reason
      } finally {
        setBusy(false)
      }
    }} />}
  </main>
}
