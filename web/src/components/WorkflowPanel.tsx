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
    <div style={{ padding: 18, overflow: 'auto', height: '100%', display: 'grid', gap: 16, alignContent: 'start' }}>
      <div role="note" style={{ border: '1px solid var(--border, #8885)', borderRadius: 12, padding: 14 }}>
        <strong>Durable Workflows · checkpoint preview</strong>
        <p style={{ marginBottom: 0 }}>Progress persists across restarts. This v1 engine does <strong>not</strong> open portals,
          upload documents, conduct MFA, submit forms or pay fees. Never put passport details,
          financial information, credentials, or private documents in titles or step labels.</p>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <button type="button" disabled={busy} onClick={() => void create(demoPlan, 'Australia Visitor 600 — simulated workflow')}>Create safe visa demo</button>
        <button type="button" disabled={busy} onClick={() => void refresh()}>Refresh</button>
      </div>
      <form onSubmit={onCreate} style={{ display: 'grid', gap: 8 }}>
        <label htmlFor="wf-title"><strong>Create another workflow</strong></label>
        <input id="wf-title" value={title} maxLength={100} onChange={event => setTitle(event.target.value)} />
        <label htmlFor="wf-steps">One step per line: kind|label (work, documents, browser, human, submission)</label>
        <textarea id="wf-steps" value={draftSteps} rows={5} onChange={event => setDraftSteps(event.target.value)} />
        <button type="submit" disabled={busy || !title.trim() || !draftSteps.trim()}>Save checklist</button>
      </form>
      {error && <p role="alert" style={{ color: 'var(--danger, #c43)' }}>{error}</p>}
      {pendingAction && (
        <div role="status" style={{ border: '1px solid var(--border, #8885)', borderRadius: 10, padding: 10 }}>
          An earlier action has an uncertain network result. Do not advance another step until this request is reconciled.
          <button type="button" disabled={busy} onClick={() => void sendPendingAction(pendingAction)}>
            Retry exactly the same request
          </button>
        </div>
      )}
      {loading && <p>Loading workflows…</p>}
      {!loading && !items.length && <p>No saved workflows yet.</p>}
      {items.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 12 }}>
          <label htmlFor="wf-select">Saved workflows</label>
          <select id="wf-select" value={selectedId} onChange={event => setSelectedId(event.target.value)}>
            {items.map(item => <option key={item.id} value={item.id}>{item.title} · {item.state}</option>)}
          </select>
        </div>
      )}
      {current && <section aria-label="Workflow checkpoint details" style={{ display: 'grid', gap: 12 }}>
        <h3 style={{ margin: 0 }}>{current.title}</h3>
        <p style={{ margin: 0 }}>State: <strong>{current.state}</strong> · revision {current.revision}</p>
        <ol style={{ display: 'grid', gap: 7, paddingLeft: 22 }}>
          {current.steps.map(entry => (
            <li key={entry.ordinal}>
              <strong>{entry.label}</strong> <span style={{ opacity: .75 }}>({entry.kind} · {entry.state} · attempts {entry.attempts})</span>
            </li>
          ))}
        </ol>
        {step && <p style={{ margin: 0 }}>Next: {step.label}</p>}
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {!pendingAction && actions(current).map(action => <button type="button" key={action} disabled={busy}
            onClick={() => void advance(action)}>{labels[action]}</button>)}
        </div>
        {current.state === 'needs_reverification' && (
          <p role="note">The human assisted during a running step. The remainder is NOT complete. Review the still-open work,
            then explicitly resume it; the bridge must later verify the real browser cursor.</p>
        )}
        {current.state === 'waiting_for_human' && (
          <p role="note">Human checkpoint: complete sensitive work yourself through the existing Secure Human Takeover.
            This button records your manual confirmation only; it does not open or bypass Takeover.</p>
        )}
      </section>}
    </div>
  )
}
