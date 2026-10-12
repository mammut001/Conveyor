import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'

type StepKind = 'work' | 'browser' | 'documents' | 'human' | 'submission'
type Step = { ordinal: number; kind: StepKind; label: string; state: string; attempts: number }
type Workflow = {
  id: string; title: string; state: string; revision: number; updated_at: number
  steps: Step[]; replayed?: boolean
}
type Props = { token: string }

const demoPlan: { kind: StepKind; label: string }[] = [
  { kind: 'documents', label: 'Review generic supporting-document checklist (no personal files)' },
  { kind: 'browser', label: 'Simulate multi-page form draft in a local fixture' },
  { kind: 'human', label: 'Human-only MFA / identity verification checkpoint' },
  { kind: 'work', label: 'Cross-check draft fields against simulated source data' },
  { kind: 'submission', label: 'Human reviews payment and final submission checkpoint' },
]

const labels: Record<string, string> = {
  begin: 'Mark work started', complete: 'Mark step complete',
  fail: 'Mark failed', retry: 'Retry step', request_human: 'Request human help',
  confirm_human: 'I completed this checkpoint', resume_verified: 'I rechecked the remaining work', pause: 'Pause',
  resume: 'Resume', cancel: 'Cancel workflow',
}

// getRandomValues also works on HTTP private-LAN clients where randomUUID may not.
function requestId(): string {
  const bytes = new Uint8Array(16)
  window.crypto.getRandomValues(bytes)
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
}

type PendingCreate = { fingerprint: string; requestId: string }
type PendingAction = { workflowId: string; action: string; expectedRevision: number; requestId: string }
const CREATE_PENDING_KEY = 'conveyor-wf-create-pending-v1'
const ACTION_PENDING_KEY = 'conveyor-wf-action-pending-v1'

// Store only request metadata; form labels and contents must not enter sessionStorage.
function fingerprint(value: object): string {
  const payload = JSON.stringify(value)
  let hash = 2166136261
  for (let i = 0; i < payload.length; i += 1) {
    hash = Math.imul(hash ^ payload.charCodeAt(i), 16777619)
  }
  return `${payload.length}:${(hash >>> 0).toString(16)}`
}

function loadPending<T>(key: string): T | null {
  try {
    const raw = window.sessionStorage.getItem(key)
    return raw ? JSON.parse(raw) as T : null
  } catch {
    return null
  }
}

function savePending<T>(key: string, pending: T | null): void {
  try {
    if (pending === null) window.sessionStorage.removeItem(key)
    else window.sessionStorage.setItem(key, JSON.stringify(pending))
  } catch {
    // Private-mode storage restrictions must not disable an in-memory retry.
  }
}

function authoritativeHttpError(err: unknown): boolean {
  const status = (err as Error & { status?: number } | null)?.status
  return typeof status === 'number' && status >= 400 && status < 500
}

const workflowStates: Record<string, string> = {
  active: 'In progress', paused: 'Paused', failed: 'Needs attention',
  waiting_for_human: 'Waiting for you', needs_reverification: 'Needs review',
  completed: 'Completed', cancelled: 'Cancelled',
  ready: 'Ready', pending: 'Upcoming', running: 'In progress',
}

const kindLabels: Record<StepKind, string> = {
  work: 'Task', browser: 'Browser', documents: 'Documents',
  human: 'Human review', submission: 'Final review',
}

const primaryActions = new Set(['begin', 'complete', 'retry', 'resume', 'resume_verified', 'confirm_human'])
const secondaryActions = new Set(['pause', 'request_human'])
const cautionActions = new Set(['fail', 'cancel'])

function readableState(state: string): string {
  return workflowStates[state] ?? state.replaceAll('_', ' ')
}

function nextStep(item: Workflow): Step | undefined {
  return item.steps.find(step => step.state !== 'completed')
}

function actions(item: Workflow): string[] {
  const current = nextStep(item)
  if (!current || ['completed', 'cancelled'].includes(item.state)) return []
  if (item.state === 'paused') return ['resume', 'cancel']
  if (item.state === 'needs_reverification') return ['resume_verified', 'pause', 'cancel']
  if (item.state === 'waiting_for_human') return ['confirm_human', 'cancel']
  if (item.state === 'failed') return ['retry', 'cancel']
  if (current.state === 'ready') return ['begin', 'pause', 'cancel']
  if (current.state === 'running') return ['complete', 'request_human', 'fail', 'pause', 'cancel']
  return []
}

export function WorkflowPanel({ token }: Props) {
  const [items, setItems] = useState<Workflow[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [title, setTitle] = useState('New personal workflow')
  const [draftSteps, setDraftSteps] = useState('documents|Prepare materials checklist\nbrowser|Complete a draft in a test portal\nhuman|Identity / MFA checkpoint\nwork|Review all fields\nsubmission|User-controlled final submission checkpoint')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [formOpen, setFormOpen] = useState(false)
  const createPendingRef = useRef<PendingCreate | null>(loadPending<PendingCreate>(CREATE_PENDING_KEY))
  const actionPendingRef = useRef<PendingAction | null>(loadPending<PendingAction>(ACTION_PENDING_KEY))
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(actionPendingRef.current)

  const rememberCreate = (value: PendingCreate | null) => {
    createPendingRef.current = value
    savePending(CREATE_PENDING_KEY, value)
  }
  const rememberAction = (value: PendingAction | null) => {
    actionPendingRef.current = value
    setPendingAction(value)
    savePending(ACTION_PENDING_KEY, value)
  }


  const request = useCallback(async <T,>(path: string, method: 'GET' | 'POST' = 'GET', body?: object): Promise<T> => {
    const response = await fetch(path, {
      method,
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      ...(body ? { body: JSON.stringify(body) } : {}),
    })
    const json = await response.json().catch(() => ({})) as { error?: string }
    if (!response.ok) {
      const err = new Error(json.error || `Workflow request failed (${response.status})`) as Error & { status?: number }
      err.status = response.status
      throw err
    }
    return json as unknown as T
  }, [token])

  const refresh = useCallback(async () => {
    try {
      const data = await request<{ items: Workflow[] }>('/api/workflows')
      setItems(data.items)
      setSelectedId(prev => data.items.some(item => item.id === prev) ? prev : data.items[0]?.id || '')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load workflows')
    } finally {
      setLoading(false)
    }
  }, [request])

  useEffect(() => { void refresh() }, [refresh])
  const current = items.find(item => item.id === selectedId)
  const step = current ? nextStep(current) : undefined
  const completedCount = current?.steps.filter(item => item.state === 'completed').length ?? 0
  const progressPercent = current ? Math.round((completedCount / current.steps.length) * 100) : 0
  const availableActions = current && !pendingAction ? actions(current) : []
  const mainActions = availableActions.filter(action => primaryActions.has(action))
  const supportingActions = availableActions.filter(action => secondaryActions.has(action))
  const cautionOptions = availableActions.filter(action => cautionActions.has(action))
  const showForm = formOpen || (!loading && items.length === 0)


  const create = async (steps: { kind: StepKind; label: string }[], planTitle: string) => {
    setBusy(true)
    setError('')
    const identity = fingerprint({ title: planTitle, steps })
    const saved = createPendingRef.current
    const pending: PendingCreate = saved?.fingerprint === identity
      ? saved : { fingerprint: identity, requestId: requestId() }
    rememberCreate(pending)
    try {
      const item = await request<Workflow>('/api/workflows', 'POST', {
        title: planTitle, steps, request_id: pending.requestId,
      })
      // Once an authoritative success arrives, new intentional creates may use a fresh ID.
      rememberCreate(null)
      setFormOpen(false)
      await refresh()
      setSelectedId(item.id)
    } catch (err) {
      if (authoritativeHttpError(err)) rememberCreate(null)
      setError(err instanceof Error ? err.message : 'Unable to create workflow; retry uses the same request ID')
    } finally {
      setBusy(false)
    }
  }

  const onCreate = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    try {
      const steps = draftSteps.split('\n').map(line => line.trim()).filter(Boolean).map(line => {
        const split = line.indexOf('|')
        if (split < 0) throw new Error('Use kind|step label, one step per line')
        const kind = line.slice(0, split).trim() as StepKind
        const label = line.slice(split + 1).trim()
        if (!['work', 'browser', 'documents', 'human', 'submission'].includes(kind) || !label) {
          throw new Error('Invalid kind or empty step label')
        }
        return { kind, label }
      })
      void create(steps, title)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Invalid steps')
    }
  }

  const sendPendingAction = async (pending: PendingAction) => {
    setBusy(true)
    setError('')
    try {
      await request<Workflow>(`/api/workflows/${encodeURIComponent(pending.workflowId)}/action`, 'POST', {
        action: pending.action, expected_revision: pending.expectedRevision, request_id: pending.requestId,
      })
      rememberAction(null)
      await refresh()
    } catch (err) {
      if (authoritativeHttpError(err)) {
        rememberAction(null)
        await refresh()
      }
      setError(err instanceof Error ? err.message : 'Operation outcome unknown; retry the pending action')
    } finally {
      setBusy(false)
    }
  }

  const advance = async (action: string) => {
    if (!current) return
    if (actionPendingRef.current) {
      setError('Resolve the previous uncertain action before beginning another step.')
      return
    }
    if (['confirm_human', 'resume_verified', 'cancel'].includes(action)) {
      const message = action === 'confirm_human'
        ? 'This ONLY records human checkpoint completion; no payment, MFA or external submission is performed. Did you complete this checkpoint?'
        : action === 'resume_verified'
          ? 'Did you inspect the remaining interrupted step after human takeover? This only resumes the checklist: it does not verify the actual browser or complete the remaining work.'
          : 'Cancel this workflow? This does not undo external actions.'
      if (!window.confirm(message)) return
    }
    const pending: PendingAction = {
      workflowId: current.id, action, expectedRevision: current.revision, requestId: requestId(),
    }
    rememberAction(pending)
    await sendPendingAction(pending)
  }

  return (
    <div className="workflow-panel">
      <header className="workflow-intro">
        <div className="workflow-intro-copy">
          <span className="workflow-eyebrow">WORKFLOW STUDIO · PREVIEW</span>
          <h2>Keep the work moving.</h2>
          <p>Plan, pause and resume multi-step work with a clear record of what comes next.</p>
        </div>
        <div className="workflow-intro-actions">
          <button type="button" className="workflow-button workflow-button-primary" disabled={busy}
            onClick={() => void create(demoPlan, 'Australia Visitor 600 — simulated workflow')}>
            Create safe visa demo
          </button>
          <button type="button" className="workflow-button workflow-button-quiet"
            onClick={() => { setFormOpen(value => !value) }} aria-expanded={showForm} aria-controls="workflow-create-form">
            {showForm ? 'Close editor' : '+ New workflow'}
          </button>
          <button type="button" className="workflow-button workflow-button-quiet" disabled={busy}
            onClick={() => void refresh()}>Refresh</button>
        </div>
      </header>

      <div className="workflow-safety-note" role="note">
        <span className="workflow-safety-symbol" aria-hidden="true">◇</span>
        <div>
          <strong>Durable Workflows · checkpoint preview</strong>
          <p>This tracks progress only. It does not open portals, upload files, perform MFA,
            make payments or submit an application. Do not enter personal, financial or identity details.</p>
        </div>
      </div>

      {showForm && (
        <form id="workflow-create-form" className="workflow-create" onSubmit={onCreate}>
          <div className="workflow-section-heading">
            <div>
              <span className="workflow-eyebrow">COMPOSE</span>
              <h3>Create a workflow</h3>
              <p>Use generic checklist descriptions only. No credentials or personal documents.</p>
            </div>
            {items.length > 0 && <button type="button" className="workflow-button workflow-button-quiet"
              onClick={() => setFormOpen(false)}>Close editor</button>}
          </div>
          <div className="workflow-form-grid">
            <label htmlFor="wf-title">Workflow title</label>
            <input id="wf-title" value={title} maxLength={100}
              onChange={event => setTitle(event.target.value)} />
            <label htmlFor="wf-steps">Steps <span className="workflow-field-hint">kind|label · one per line</span></label>
            <textarea id="wf-steps" value={draftSteps} rows={5}
              onChange={event => setDraftSteps(event.target.value)}
              spellCheck={false} />
            <p className="workflow-field-hint">Available kinds: work, documents, browser, human, submission.</p>
            <div className="workflow-form-actions">
              <button className="workflow-button workflow-button-primary" type="submit"
                disabled={busy || !title.trim() || !draftSteps.trim()}>Save checklist</button>
            </div>
          </div>
        </form>
      )}

      {error && <div className="workflow-error" role="alert">{error}</div>}
      {pendingAction && (
        <div className="workflow-pending" role="status">
          <div>
            <strong>Action awaiting confirmation</strong>
            <p>The network result is uncertain. Other step actions are locked until this exact request is replayed.</p>
          </div>
          <button type="button" className="workflow-button workflow-button-primary"
            disabled={busy} onClick={() => void sendPendingAction(pendingAction)}>
            Retry exactly the same request
          </button>
        </div>
      )}

      {loading && <div className="workflow-empty" role="status">Loading workflows…</div>}
      {!loading && !items.length && (
        <section className="workflow-empty">
          <span className="workflow-empty-icon" aria-hidden="true">◎</span>
          <h3>No workflows yet</h3>
          <p>Start with a safe demo or save your own checklist above.</p>
        </section>
      )}

      {items.length > 0 && (
        <div className="workflow-library">
          <label htmlFor="wf-select">SELECT WORKFLOW <span>{items.length} saved</span></label>
          <select id="wf-select" value={selectedId} onChange={event => setSelectedId(event.target.value)}>
            {items.map(item => (
              <option key={item.id} value={item.id}>{item.title} · {readableState(item.state)}</option>
            ))}
          </select>
        </div>
      )}

      {current && (
        <section className="workflow-detail" aria-label="Workflow checkpoint details">
          <div className="workflow-detail-heading">
            <div className="workflow-heading-copy">
              <span className="workflow-eyebrow">WORKFLOW OVERVIEW</span>
              <h3>{current.title}</h3>
              <span className="workflow-revision">Revision {current.revision} · Progress saved</span>
            </div>
            <span className={`workflow-state workflow-state--${current.state}`}>
              <span aria-hidden="true" className="workflow-state-dot" />
              {readableState(current.state)}
            </span>
          </div>

          <div className="workflow-progress">
            <div className="workflow-progress-meta">
              <span>Progress</span>
              <strong>{completedCount} of {current.steps.length} steps</strong>
            </div>
            <div className="workflow-progress-track" role="progressbar" aria-label="Completed steps"
              aria-valuemin={0} aria-valuemax={current.steps.length} aria-valuenow={completedCount}>
              <div style={{ width: `${progressPercent}%` }} />
            </div>
          </div>

          <ol className="workflow-steps" aria-label="Workflow steps">
            {current.steps.map(entry => {
              const isActive = step?.ordinal === entry.ordinal && !['completed', 'cancelled'].includes(current.state)
              return (
                <li key={entry.ordinal}
                  className={`workflow-step workflow-step--${entry.state}${isActive ? ' workflow-step--current' : ''}`}
                  aria-current={isActive ? 'step' : undefined}>
                  <span className="workflow-step-index" aria-hidden="true">
                    {entry.state === 'completed' ? '✓' : String(entry.ordinal + 1).padStart(2, '0')}
                  </span>
                  <div className="workflow-step-content">
                    <span className="workflow-step-kind">{kindLabels[entry.kind]}</span>
                    <strong>{entry.label}</strong>
                    {entry.attempts > 0 && <small>{entry.attempts} {entry.attempts === 1 ? 'attempt' : 'attempts'}</small>}
                  </div>
                  <span className={`workflow-step-state workflow-step-state--${entry.state}`}>
                    {readableState(entry.state)}
                  </span>
                </li>
              )
            })}
          </ol>

          {step && !['completed', 'cancelled'].includes(current.state) && (
            <div className={`workflow-next workflow-next--${current.state}`}>
              <span className="workflow-eyebrow">
                {current.state === 'needs_reverification' ? 'REVIEW BEFORE CONTINUING'
                  : current.state === 'waiting_for_human' ? 'YOUR ACTION IS NEEDED'
                    : current.state === 'failed' ? 'ATTENTION REQUIRED' : 'NEXT STEP'}
              </span>
              <strong>{step.label}</strong>
              {current.state === 'needs_reverification' && (
                <p>The human handoff is finished, but this step is <b>not complete</b>. Inspect the remaining work, resume the same step, then finish it separately. This preview cannot verify a real browser cursor.</p>
              )}
              {current.state === 'waiting_for_human' && (
                <p>Complete this checkpoint yourself. Confirmation records checklist progress only—it does not launch Secure Human Takeover, verify identity, pay or submit anything.</p>
              )}
              {current.state === 'failed' && (
                <p>This step failed. Review what happened before retrying; retrying does not execute any external action in this preview.</p>
              )}
              {current.state === 'paused' && (
                <p>Progress is preserved. Resume when you are ready.</p>
              )}
              {step.kind === 'submission' && current.state === 'waiting_for_human' && (
                <p className="workflow-submission-disclaimer">Final review only · Nothing will be paid or submitted.</p>
              )}
            </div>
          )}

          {current.state === 'completed' && (
            <div className="workflow-finished" role="status">
              <strong>All checklist steps completed</strong>
              <p>This confirms your recorded steps, not an external submission or successful visa application.</p>
            </div>
          )}

          {current.state === 'cancelled' && (
            <div className="workflow-finished workflow-finished--cancelled" role="status">
              <strong>Workflow cancelled</strong>
              <p>Recorded progress is preserved. Cancellation does not reverse any work outside Conveyor.</p>
            </div>
          )}

          {(mainActions.length > 0 || supportingActions.length > 0 || cautionOptions.length > 0) && (
            <div className="workflow-controls" aria-label="Workflow actions">
              <div className="workflow-main-actions">
                {mainActions.map(action => (
                  <button type="button" key={action} className="workflow-button workflow-button-primary"
                    disabled={busy} onClick={() => void advance(action)}>{labels[action]}</button>
                ))}
                {supportingActions.map(action => (
                  <button type="button" key={action} className="workflow-button workflow-button-secondary"
                    disabled={busy} onClick={() => void advance(action)}>{labels[action]}</button>
                ))}
              </div>
              {cautionOptions.length > 0 && (
                <details className="workflow-more-actions">
                  <summary>More actions</summary>
                  <div className="workflow-more-buttons">
                    {cautionOptions.map(action => (
                      <button type="button" key={action}
                        className={`workflow-button workflow-button-${action === 'cancel' ? 'danger' : 'secondary'}`}
                        disabled={busy} onClick={() => void advance(action)}>{labels[action]}</button>
                    ))}
                  </div>
                </details>
              )}
            </div>
          )}
        </section>
      )}
    </div>
  )
}
