import { useCallback, useEffect, useState } from 'react'
import '../ux/workers.css'

export type WorkerQuota = {
  state: 'available' | 'cooling'
  reset_at: number | null
  reason: string | null
}

export type Worker = {
  id: string
  label: string
  installed: boolean
  version: string | null
  path: string | null
  auth: 'ok' | 'needs_login' | 'unknown'
  login_command: string | null
  install_command: string | null
  install_supported: boolean
  runnable: boolean
  enabled: boolean
  priority: number
  quota: WorkerQuota
  models_supported?: boolean
  model?: string | null
  login_supported?: boolean
}

export type WorkersPayload = {
  enabled: boolean
  install_enabled: boolean
  workers: Worker[]
  active: string | null
}

type Problem = { message: string; detail?: string }

function formatCoolingChip(resetAt: number | null): string {
  if (!resetAt) return '额度用完·冷却中'
  const now = Date.now() / 1000
  const leftSec = resetAt - now
  if (leftSec <= 0) return '额度用完·即将恢复'
  const minutes = Math.round(leftSec / 60)
  if (minutes < 60) return `额度用完·约 ${Math.max(1, minutes)} 分钟后恢复`
  const hours = Math.round(minutes / 60)
  return `额度用完·约 ${Math.max(1, hours)} 小时后恢复`
}

function formatRelativeTime(resetAt: number | null): string {
  if (!resetAt) return '冷却中'
  const now = Date.now() / 1000
  const leftSec = resetAt - now
  if (leftSec <= 0) return '即将恢复'
  const minutes = Math.round(leftSec / 60)
  if (minutes < 60) return `约 ${Math.max(1, minutes)} 分钟后恢复`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `约 ${Math.max(1, hours)} 小时后恢复`
  const days = Math.round(hours / 24)
  return `约 ${days} 天后恢复`
}

async function readError(response: Response): Promise<{ error?: string; code?: string }> {
  try {
    const body = await response.json()
    if (body && typeof body === 'object') {
      return {
        error: typeof body.error === 'string' ? body.error : undefined,
        code: typeof body.code === 'string' ? body.code : undefined,
      }
    }
  } catch {
    // ignore json parse error
  }
  return { error: `HTTP ${response.status}` }
}

function describeServerError(err: { error?: string; code?: string }, fallback: string): Problem {
  if (err.code === 'install_disabled') return { message: '服务器未开启在线安装功能。', detail: err.code }
  if (err.code === 'install_unsupported') return { message: '该执行者不支持在线安装。', detail: err.code }
  if (err.code === 'confirm_required') return { message: '安装操作需要确认。', detail: err.code }
  if (err.code === 'unknown_worker') return { message: '未找到指定的执行者。', detail: err.code }
  if (err.code === 'bad_code') return { message: '登录码格式不正确。', detail: err.code }
  if (err.code === 'unknown_model') return { message: '未找到指定的模型。', detail: err.code }
  if (err.code === 'models_unsupported') return { message: '该执行者不支持切换模型。', detail: err.code }
  if (err.error === 'not found') return { message: '执行者功能未开启。' }
  return { message: fallback, detail: err.code || err.error }
}

type LoginSessionState = {
  session: string
  state: 'waiting_url' | 'open_url' | 'needs_code' | 'done' | 'failed'
  url: string | null
  message: string
  codeDraft: string
  submittingCode: boolean
  canceling: boolean
}

type WorkerModelsData = {
  models: { id: string; label: string }[]
  cached_at?: number
}

export function WorkersPanel({ token }: { token: string }) {
  const [workers, setWorkers] = useState<Worker[]>([])
  const [activeWorkerId, setActiveWorkerId] = useState<string | null>(null)
  const [installEnabled, setInstallEnabled] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [featureOff, setFeatureOff] = useState(false)
  const [loadProblem, setLoadProblem] = useState<Problem | null>(null)
  const [actionProblem, setActionProblem] = useState<Problem | null>(null)
  const [notice, setNotice] = useState('')
  const [detecting, setDetecting] = useState(false)
  const [savingOrder, setSavingOrder] = useState(false)
  const [orderDirty, setOrderDirty] = useState(false)
  const [confirmInstallId, setConfirmInstallId] = useState('')
  const [installingId, setInstallingId] = useState('')
  const [installOutput, setInstallOutput] = useState<Record<string, { ok: boolean; output: string }>>({})
  const [copiedCommand, setCopiedCommand] = useState('')
  const [resettingId, setResettingId] = useState('')
  const [loginSessions, setLoginSessions] = useState<Record<string, LoginSessionState>>({})
  const [startingLoginId, setStartingLoginId] = useState('')
  const [workerModels, setWorkerModels] = useState<Record<string, WorkerModelsData>>({})
  const [loadingModelsId, setLoadingModelsId] = useState('')
  const [savingModelId, setSavingModelId] = useState('')
  const [savedModelTickId, setSavedModelTickId] = useState('')

  const load = useCallback(async () => {
    try {
      const response = await fetch('/api/workers', {
        headers: { Authorization: 'Bearer ' + token },
        cache: 'no-store',
      })
      if (response.status === 404) {
        setFeatureOff(true)
        setLoadProblem(null)
        setLoaded(true)
        return
      }
      if (!response.ok) {
        const err = await readError(response)
        setLoadProblem(describeServerError(err, '无法读取执行者列表，请稍后重试。'))
        setLoaded(true)
        return
      }
      const data = await response.json() as WorkersPayload
      setWorkers(Array.isArray(data.workers) ? data.workers : [])
      setActiveWorkerId(data.active || null)
      setInstallEnabled(Boolean(data.install_enabled))
      setFeatureOff(false)
      setLoadProblem(null)
      setOrderDirty(false)
    } catch {
      setLoadProblem({ message: '无法连接服务器，执行者列表暂时读取不到。' })
    } finally {
      setLoaded(true)
    }
  }, [token])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    const activeIds = Object.keys(loginSessions).filter(id => {
      const s = loginSessions[id]
      return s && (s.state === 'waiting_url' || s.state === 'open_url' || s.state === 'needs_code')
    })
    if (activeIds.length === 0) return

    const interval = setInterval(() => {
      activeIds.forEach(async id => {
        const current = loginSessions[id]
        if (!current || !current.session) return
        try {
          const response = await fetch(`/api/workers/${encodeURIComponent(id)}/login/${encodeURIComponent(current.session)}`, {
            headers: { Authorization: 'Bearer ' + token },
            cache: 'no-store',
          })
          if (!response.ok) {
            const err = await readError(response)
            setLoginSessions(prev => {
              const cur = prev[id]
              if (!cur || cur.session !== current.session) return prev
              return {
                ...prev,
                [id]: {
                  ...cur,
                  state: 'failed',
                  message: err.error || '检查登录状态失败',
                },
              }
            })
            return
          }
          const data = await response.json() as {
            session: string
            state: 'waiting_url' | 'open_url' | 'needs_code' | 'done' | 'failed'
            url?: string | null
            message?: string
          }
          setLoginSessions(prev => {
            const cur = prev[id]
            if (!cur || cur.session !== current.session) return prev
            return {
              ...prev,
              [id]: {
                ...cur,
                state: data.state,
                url: data.url !== undefined ? (data.url || null) : cur.url,
                message: data.message !== undefined ? (data.message || '') : cur.message,
              },
            }
          })
          if (data.state === 'done') {
            void load()
          }
        } catch {
          // keep polling
        }
      })
    }, 2000)

    return () => clearInterval(interval)
  }, [loginSessions, token, load])

  async function detect() {
    setDetecting(true)
    setActionProblem(null)
    setNotice('')
    try {
      const response = await fetch('/api/workers/detect', {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token },
      })
      if (!response.ok) {
        const err = await readError(response)
        setActionProblem(describeServerError(err, '检测执行者失败，请稍后重试。'))
        return
      }
      const data = await response.json() as WorkersPayload
      setWorkers(Array.isArray(data.workers) ? data.workers : [])
      setActiveWorkerId(data.active || null)
      setInstallEnabled(Boolean(data.install_enabled))
      setOrderDirty(false)
      setNotice('已完成执行者检测。')
    } catch {
      setActionProblem({ message: '无法连接服务器，检测未完成。' })
    } finally {
      setDetecting(false)
    }
  }

  function moveWorker(index: number, direction: -1 | 1) {
    const target = index + direction
    if (target < 0 || target >= workers.length) return
    const next = [...workers]
    const [moved] = next.splice(index, 1)
    next.splice(target, 0, moved)
    setWorkers(next)
    setOrderDirty(true)
  }

  function toggleWorker(id: string) {
    setWorkers(prev => prev.map(w => w.id === id ? { ...w, enabled: !w.enabled } : w))
    setOrderDirty(true)
  }

  async function saveOrder() {
    setSavingOrder(true)
    setActionProblem(null)
    setNotice('')
    try {
      const payload = {
        order: workers.map(w => w.id),
        enabled: workers.filter(w => w.enabled).map(w => w.id),
      }
      const response = await fetch('/api/workers/order', {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) {
        const err = await readError(response)
        setActionProblem(describeServerError(err, '保存执行者顺序失败，请稍后重试。'))
        return
      }
      const data = await response.json() as WorkersPayload
      setWorkers(Array.isArray(data.workers) ? data.workers : [])
      setActiveWorkerId(data.active || null)
      setInstallEnabled(Boolean(data.install_enabled))
      setOrderDirty(false)
      setNotice('已保存执行者顺序。')
    } catch {
      setActionProblem({ message: '无法连接服务器，顺序没有保存。' })
    } finally {
      setSavingOrder(false)
    }
  }

  async function installWorker(worker: Worker) {
    setInstallingId(worker.id)
    setActionProblem(null)
    try {
      const response = await fetch(`/api/workers/${encodeURIComponent(worker.id)}/install`, {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: true }),
      })
      const data = await response.json().catch(() => null) as { ok?: boolean; output?: string; error?: string; code?: string } | null
      const output = data?.output || data?.error || (response.ok ? '安装完成。' : `HTTP ${response.status}`)
      setInstallOutput(prev => ({
        ...prev,
        [worker.id]: { ok: Boolean(data?.ok), output },
      }))
      if (!response.ok || !data?.ok) {
        setActionProblem(describeServerError(data || { error: `HTTP ${response.status}` }, `安装 ${worker.label} 失败。`))
      } else {
        setNotice(`已安装 ${worker.label}。`)
        await load()
      }
    } catch {
      setActionProblem({ message: `无法连接服务器，${worker.label} 安装失败。` })
    } finally {
      setInstallingId('')
      setConfirmInstallId('')
    }
  }

  async function resetQuota(id: string) {
    setResettingId(id)
    setActionProblem(null)
    try {
      const response = await fetch(`/api/workers/${encodeURIComponent(id)}/reset-quota`, {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token },
      })
      if (!response.ok) {
        const err = await readError(response)
        setActionProblem(describeServerError(err, '重置额度状态失败。'))
        return
      }
      const data = await response.json() as WorkersPayload
      setWorkers(Array.isArray(data.workers) ? data.workers : [])
      setActiveWorkerId(data.active || null)
      setInstallEnabled(Boolean(data.install_enabled))
      const found = workers.find(w => w.id === id)
      setNotice(`已重置 ${found?.label || id} 的额度状态。`)
    } catch {
      setActionProblem({ message: '无法连接服务器，重置额度状态失败。' })
    } finally {
      setResettingId('')
    }
  }

  async function startLogin(workerId: string) {
    setStartingLoginId(workerId)
    setActionProblem(null)
    try {
      const response = await fetch(`/api/workers/${encodeURIComponent(workerId)}/login/start`, {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token },
      })
      if (!response.ok) {
        const err = await readError(response)
        setActionProblem(describeServerError(err, '发起登录失败，请稍后重试。'))
        return
      }
      const data = await response.json() as {
        session: string
        state: 'waiting_url' | 'open_url' | 'needs_code' | 'done' | 'failed'
        url?: string | null
        message?: string
      }
      setLoginSessions(prev => ({
        ...prev,
        [workerId]: {
          session: data.session,
          state: data.state || 'waiting_url',
          url: data.url || null,
          message: data.message || '',
          codeDraft: '',
          submittingCode: false,
          canceling: false,
        },
      }))
      if (data.state === 'done') {
        await load()
      }
    } catch {
      setActionProblem({ message: '无法连接服务器，发起登录失败。' })
    } finally {
      setStartingLoginId('')
    }
  }

  async function submitCode(workerId: string) {
    const session = loginSessions[workerId]
    if (!session || !session.codeDraft.trim() || session.submittingCode) return
    setLoginSessions(prev => ({
      ...prev,
      [workerId]: { ...prev[workerId], submittingCode: true },
    }))
    try {
      const code = session.codeDraft.trim()
      const response = await fetch(`/api/workers/${encodeURIComponent(workerId)}/login/${encodeURIComponent(session.session)}/code`, {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
      })
      if (!response.ok) {
        const err = await readError(response)
        setLoginSessions(prev => ({
          ...prev,
          [workerId]: {
            ...prev[workerId],
            submittingCode: false,
            state: 'failed',
            message: err.code === 'bad_code' ? '登录码格式不正确。' : (err.error || '提交登录码失败。'),
          },
        }))
        return
      }
      const checkRes = await fetch(`/api/workers/${encodeURIComponent(workerId)}/login/${encodeURIComponent(session.session)}`, {
        headers: { Authorization: 'Bearer ' + token },
        cache: 'no-store',
      })
      if (checkRes.ok) {
        const data = await checkRes.json() as {
          session: string
          state: 'waiting_url' | 'open_url' | 'needs_code' | 'done' | 'failed'
          url?: string | null
          message?: string
        }
        setLoginSessions(prev => ({
          ...prev,
          [workerId]: {
            ...prev[workerId],
            submittingCode: false,
            state: data.state,
            url: data.url !== undefined ? (data.url || null) : prev[workerId].url,
            message: data.message !== undefined ? (data.message || '') : prev[workerId].message,
          },
        }))
        if (data.state === 'done') {
          await load()
        }
      } else {
        setLoginSessions(prev => ({
          ...prev,
          [workerId]: { ...prev[workerId], submittingCode: false },
        }))
      }
    } catch {
      setLoginSessions(prev => ({
        ...prev,
        [workerId]: { ...prev[workerId], submittingCode: false, message: '提交失败，请重试。' },
      }))
    }
  }

  async function cancelLogin(workerId: string) {
    const session = loginSessions[workerId]
    if (!session) return
    setLoginSessions(prev => ({
      ...prev,
      [workerId]: { ...prev[workerId], canceling: true },
    }))
    try {
      const response = await fetch(`/api/workers/${encodeURIComponent(workerId)}/login/${encodeURIComponent(session.session)}/cancel`, {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token },
      })
      const data = await response.json().catch(() => null) as { state?: string; message?: string } | null
      setLoginSessions(prev => ({
        ...prev,
        [workerId]: {
          ...prev[workerId],
          canceling: false,
          state: 'failed',
          message: data?.message || '已取消',
        },
      }))
    } catch {
      setLoginSessions(prev => ({
        ...prev,
        [workerId]: {
          ...prev[workerId],
          canceling: false,
          state: 'failed',
          message: '已取消',
        },
      }))
    }
  }

  async function loadModels(workerId: string) {
    if (workerModels[workerId] || loadingModelsId === workerId) return
    setLoadingModelsId(workerId)
    try {
      const response = await fetch(`/api/workers/${encodeURIComponent(workerId)}/models`, {
        headers: { Authorization: 'Bearer ' + token },
        cache: 'no-store',
      })
      if (response.ok) {
        const data = await response.json() as WorkerModelsData
        setWorkerModels(prev => ({ ...prev, [workerId]: data }))
      }
    } catch {
      // ignore
    } finally {
      setLoadingModelsId('')
    }
  }

  async function setWorkerModel(workerId: string, model: string | null) {
    setSavingModelId(workerId)
    setActionProblem(null)
    try {
      const response = await fetch(`/api/workers/${encodeURIComponent(workerId)}/model`, {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ model }),
      })
      if (!response.ok) {
        const err = await readError(response)
        setActionProblem(describeServerError(err, '保存模型设置失败。'))
        return
      }
      const data = await response.json().catch(() => null) as WorkersPayload | null
      if (data && Array.isArray(data.workers)) {
        setWorkers(data.workers)
      } else {
        setWorkers(prev => prev.map(w => w.id === workerId ? { ...w, model } : w))
      }
      setSavedModelTickId(workerId)
      setTimeout(() => {
        setSavedModelTickId(cur => cur === workerId ? '' : cur)
      }, 2000)
    } catch {
      setActionProblem({ message: '无法连接服务器，保存模型失败。' })
    } finally {
      setSavingModelId('')
    }
  }

  async function copyCommand(text: string) {
    let success = false
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
        success = true
      }
    } catch {
      // fallback
    }
    if (!success) {
      try {
        const ta = document.createElement('textarea')
        ta.value = text
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        success = document.execCommand('copy')
        document.body.removeChild(ta)
      } catch {
        success = false
      }
    }
    setCopiedCommand(text)
    setTimeout(() => setCopiedCommand(cur => cur === text ? '' : cur), 2000)
  }

  function renderStatusChip(worker: Worker) {
    if (!worker.installed) {
      return <span className="guard-badge guard-badge-muted">未安装</span>
    }
    if (worker.quota?.state === 'cooling') {
      return <span className="guard-badge guard-badge-danger">{formatCoolingChip(worker.quota.reset_at)}</span>
    }
    if (worker.auth === 'needs_login') {
      return <span className="guard-badge guard-sev-warning">需要登录</span>
    }
    if (worker.auth === 'unknown') {
      return <span className="guard-badge guard-badge-muted">登录状态未知</span>
    }
    return <span className="guard-badge guard-badge-ok">可用</span>
  }

  if (featureOff) {
    return (
      <div className="guard-page guard-workers">
        <div className="guard-stack">
          <p className="guard-empty guard-muted">执行者功能未开启</p>
        </div>
      </div>
    )
  }

  return (
    <div className="guard-page guard-workers">
      <div className="guard-stack workers-stack">
        <div className="workers-actions-row" role="toolbar" aria-label="执行者操作">
          <button
            type="button"
            className="guard-btn"
            disabled={detecting || savingOrder}
            aria-busy={detecting || undefined}
            onClick={() => { void detect() }}
          >
            {detecting ? '检测中…' : '重新检测'}
          </button>
          <button
            type="button"
            className={`guard-btn ${orderDirty ? 'guard-btn-primary' : ''}`}
            disabled={savingOrder || detecting}
            aria-busy={savingOrder || undefined}
            onClick={() => { void saveOrder() }}
          >
            {savingOrder ? '保存中…' : '保存顺序'}
          </button>
        </div>

        {(notice || actionProblem) && (
          <div aria-live="polite">
            {notice && <p role="status" className="guard-feedback guard-feedback-ok">{notice}</p>}
            {actionProblem && (
              <p role="alert" className="guard-feedback guard-feedback-error">
                {actionProblem.message}
                {actionProblem.detail && <span className="guard-tech">{actionProblem.detail}</span>}
              </p>
            )}
          </div>
        )}

        {!loaded ? (
          <p className="guard-empty">加载中…</p>
        ) : loadProblem ? (
          <div className="guard-empty" role="alert">
            <p>{loadProblem.message}</p>
            {loadProblem.detail && <p className="guard-tech">{loadProblem.detail}</p>}
            <button type="button" className="guard-btn" onClick={() => { void load() }}>重试</button>
          </div>
        ) : workers.length === 0 ? (
          <p className="guard-empty">暂无可用执行者</p>
        ) : (
          <ul className="workers-list" aria-label="执行者列表">
            {workers.map((worker, index) => {
              const pv = [worker.version, worker.path].filter(Boolean).join(' · ')
              const isConfirmingInstall = confirmInstallId === worker.id
              const isInstalling = installingId === worker.id
              const isResetting = resettingId === worker.id
              const out = installOutput[worker.id]
              const session = loginSessions[worker.id]
              const hasBody =
                !worker.installed ||
                Boolean(worker.installed && worker.models_supported) ||
                (Boolean(worker.login_supported) && (worker.auth === 'needs_login' || worker.auth === 'unknown')) ||
                Boolean(session) ||
                worker.auth === 'needs_login' ||
                worker.quota?.state === 'cooling' ||
                Boolean(out)

              return (
                <li
                  key={worker.id}
                  className={`worker-card ${activeWorkerId === worker.id ? 'is-active' : ''}`}
                  aria-label={worker.label}
                >
                  <div className="worker-card-head">
                    <div className="worker-meta">
                      <span className="worker-priority" title={`优先级第 ${index + 1}`}>#{index + 1}</span>
                      <strong className="worker-label">{worker.label}</strong>
                      {pv && <span className="worker-path-version">{pv}</span>}
                      {renderStatusChip(worker)}
                      {activeWorkerId === worker.id && <span className="guard-badge guard-badge-ok">当前选用</span>}
                      <span className={`worker-runnable ${worker.runnable ? 'is-runnable' : 'is-not-runnable'}`}>
                        {worker.runnable ? 'Conveyor 可直接调用' : '暂只检测，不参与轮换'}
                      </span>
                    </div>
                    <div className="worker-controls">
                      <label className="worker-toggle" title={worker.enabled ? '已启用，参与轮换' : '已停用，不参与轮换'}>
                        <input
                          type="checkbox"
                          checked={worker.enabled}
                          onChange={() => toggleWorker(worker.id)}
                          aria-label={`启用 ${worker.label}`}
                        />
                        <span className="worker-toggle-slider" aria-hidden="true" />
                        <span className="worker-toggle-text">{worker.enabled ? '已启用' : '已停用'}</span>
                      </label>
                      <div className="worker-order-btns" role="group" aria-label={`${worker.label} 顺序调整`}>
                        <button
                          type="button"
                          className="guard-btn guard-btn-small worker-order-btn"
                          disabled={index === 0}
                          aria-label={`上移 ${worker.label}`}
                          onClick={() => moveWorker(index, -1)}
                        >
                          ↑
                        </button>
                        <button
                          type="button"
                          className="guard-btn guard-btn-small worker-order-btn"
                          disabled={index === workers.length - 1}
                          aria-label={`下移 ${worker.label}`}
                          onClick={() => moveWorker(index, 1)}
                        >
                          ↓
                        </button>
                      </div>
                    </div>
                  </div>

                  {hasBody && (
                    <div className="worker-body">
                      {!worker.installed && (
                        <>
                          {worker.install_supported && installEnabled && worker.install_command ? (
                            <div className="worker-install-section">
                              {!isConfirmingInstall ? (
                                <div className="worker-install-action">
                                  <button
                                    type="button"
                                    className="guard-btn guard-btn-small"
                                    onClick={() => { setConfirmInstallId(worker.id); setActionProblem(null) }}
                                  >
                                    安装
                                  </button>
                                </div>
                              ) : (
                                <div className="guard-inline-confirm" role="group" aria-label={`确认安装 ${worker.label}`}>
                                  <p>将在服务器上运行：<code>{worker.install_command}</code></p>
                                  <div className="guard-inline-confirm-actions">
                                    <button
                                      type="button"
                                      className="guard-btn guard-btn-small"
                                      disabled={isInstalling}
                                      onClick={() => setConfirmInstallId('')}
                                    >
                                      取消
                                    </button>
                                    <button
                                      type="button"
                                      className="guard-btn guard-btn-small guard-btn-primary"
                                      disabled={isInstalling}
                                      onClick={() => { void installWorker(worker) }}
                                    >
                                      {isInstalling ? '安装中…' : '确认安装'}
                                    </button>
                                  </div>
                                </div>
                              )}
                            </div>
                          ) : worker.install_command ? (
                            <div className="worker-command-box">
                              <p className="guard-sub">在服务器上运行这条命令安装：</p>
                              <div className="worker-command-row">
                                <code className="worker-code">{worker.install_command}</code>
                                <button
                                  type="button"
                                  className="guard-btn guard-btn-small"
                                  onClick={() => { void copyCommand(worker.install_command!) }}
                                >
                                  {copiedCommand === worker.install_command ? '已复制' : '复制'}
                                </button>
                              </div>
                            </div>
                          ) : (
                            <div className="worker-manual-hint">
                              <p className="guard-sub">{worker.id === 'agy' ? '从 Antigravity 官方渠道安装' : '需在服务器上手动安装'}</p>
                            </div>
                          )}
                        </>
                      )}

                      {out && (
                        <details className="worker-details" open>
                          <summary>详情</summary>
                          <pre className="worker-pre">{out.output}</pre>
                        </details>
                      )}

                      {worker.installed && (worker.login_supported || session) && (worker.auth === 'needs_login' || worker.auth === 'unknown' || session) && (
                        <div className="worker-login-box">
                          {!session ? (
                            <div className="worker-login-action">
                              <button
                                type="button"
                                className="guard-btn guard-btn-small guard-btn-primary"
                                disabled={startingLoginId === worker.id}
                                onClick={() => { void startLogin(worker.id) }}
                              >
                                {startingLoginId === worker.id ? '正在准备登录…' : '登录'}
                              </button>
                            </div>
                          ) : (
                            <div className="worker-login-flow" role="group" aria-label={`${worker.label} 登录流程`}>
                              {(session.state === 'waiting_url' || (startingLoginId === worker.id && !session.url)) && (
                                <div className="worker-login-waiting">
                                  <span className="guard-muted">正在准备登录…</span>
                                  <button
                                    type="button"
                                    className="guard-btn guard-btn-small"
                                    disabled={session.canceling}
                                    onClick={() => { void cancelLogin(worker.id) }}
                                  >
                                    取消
                                  </button>
                                </div>
                              )}

                              {(session.state === 'open_url' || (session.state === 'needs_code' && session.url)) && (
                                <div className="worker-login-open-section">
                                  <div className="worker-login-url-row">
                                    {session.url && (
                                      <a
                                        href={session.url}
                                        target="_blank"
                                        rel="noopener"
                                        role="button"
                                        className="guard-btn guard-btn-small guard-btn-primary"
                                      >
                                        打开登录页面
                                      </a>
                                    )}
                                    <button
                                      type="button"
                                      className="guard-btn guard-btn-small"
                                      disabled={session.canceling}
                                      onClick={() => { void cancelLogin(worker.id) }}
                                    >
                                      取消
                                    </button>
                                  </div>
                                  <p className="guard-sub worker-login-hint">在浏览器打开的页面完成授权后返回此处</p>
                                </div>
                              )}

                              {session.state === 'needs_code' && (
                                <form
                                  className="worker-login-code-form"
                                  onSubmit={e => {
                                    e.preventDefault()
                                    void submitCode(worker.id)
                                  }}
                                >
                                  <input
                                    type="text"
                                    className="guard-input worker-login-code-input"
                                    placeholder="把登录页面显示的登录码粘贴到这里"
                                    aria-label="把登录页面显示的登录码粘贴到这里"
                                    value={session.codeDraft}
                                    disabled={session.submittingCode}
                                    onChange={e => {
                                      const val = e.target.value
                                      setLoginSessions(prev => ({
                                        ...prev,
                                        [worker.id]: { ...prev[worker.id], codeDraft: val },
                                      }))
                                    }}
                                    autoFocus
                                  />
                                  <div className="worker-login-code-actions">
                                    <button
                                      type="submit"
                                      className="guard-btn guard-btn-small guard-btn-primary"
                                      disabled={!session.codeDraft.trim() || session.submittingCode}
                                    >
                                      {session.submittingCode ? '提交中…' : '提交'}
                                    </button>
                                    {!session.url && (
                                      <button
                                        type="button"
                                        className="guard-btn guard-btn-small"
                                        disabled={session.canceling}
                                        onClick={() => { void cancelLogin(worker.id) }}
                                      >
                                        取消
                                      </button>
                                    )}
                                  </div>
                                </form>
                              )}

                              {session.state === 'done' && (
                                <p className="guard-feedback guard-feedback-ok" role="status">
                                  {session.message || '登录成功。'}
                                </p>
                              )}

                              {session.state === 'failed' && (
                                <div className="worker-login-failed-row" role="alert">
                                  <span className="guard-feedback guard-feedback-error">
                                    {session.message || '登录失败，请重试。'}
                                  </span>
                                  <button
                                    type="button"
                                    className="guard-btn guard-btn-small"
                                    onClick={() => { void startLogin(worker.id) }}
                                  >
                                    重试
                                  </button>
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      )}

                      {worker.installed && !worker.login_supported && !session && worker.auth === 'needs_login' && (
                        <div className="worker-login-box">
                          <p className="guard-sub">在服务器终端里运行，按提示在浏览器完成登录，然后点 重新检测：</p>
                          {worker.login_command && (
                            <div className="worker-command-row">
                              <code className="worker-code">{worker.login_command}</code>
                              <button
                                type="button"
                                className="guard-btn guard-btn-small"
                                onClick={() => { void copyCommand(worker.login_command!) }}
                              >
                                {copiedCommand === worker.login_command ? '已复制' : '复制'}
                              </button>
                            </div>
                          )}
                        </div>
                      )}

                      {worker.installed && worker.models_supported && (
                        <div className="worker-model-box">
                          <div className="worker-model-row">
                            <label htmlFor={`worker-model-${worker.id}`} className="guard-sub worker-model-label">
                              模型：
                            </label>
                            <select
                              id={`worker-model-${worker.id}`}
                              aria-label={`${worker.label} 模型选择`}
                              className="guard-select worker-model-select"
                              value={worker.model ?? ''}
                              onFocus={() => { void loadModels(worker.id) }}
                              onClick={() => { void loadModels(worker.id) }}
                              onChange={e => {
                                const val = e.target.value === '' ? null : e.target.value
                                void setWorkerModel(worker.id, val)
                              }}
                              disabled={savingModelId === worker.id}
                            >
                              <option value="">使用默认</option>
                              {workerModels[worker.id]?.models?.map(m => (
                                <option key={m.id} value={m.id}>{m.label || m.id}</option>
                              ))}
                              {worker.model && !workerModels[worker.id]?.models?.some(m => m.id === worker.model) && (
                                <option value={worker.model}>{worker.model}</option>
                              )}
                            </select>
                            {loadingModelsId === worker.id && <span className="guard-muted worker-model-loading">加载中…</span>}
                            {savedModelTickId === worker.id && <span className="worker-saved-tick" role="status">✓ 已保存</span>}
                          </div>
                        </div>
                      )}

                      {worker.installed && worker.quota?.state === 'cooling' && (
                        <div className="worker-cooling-box">
                          <div className="worker-cooling-row">
                            <div className="worker-cooling-info">
                              <span className="guard-sub">预计恢复时间：{formatRelativeTime(worker.quota.reset_at)}</span>
                              {worker.quota.reason && <span className="guard-tech">{worker.quota.reason}</span>}
                            </div>
                            <button
                              type="button"
                              className="guard-btn guard-btn-small guard-btn-quiet worker-reset-btn"
                              disabled={isResetting}
                              onClick={() => { void resetQuota(worker.id) }}
                            >
                              {isResetting ? '重置中…' : '重置额度状态'}
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}
