import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import './SupervisorPanel.css'

type Handoff = { id: string; from_role: string; to_role: string; summary: string; created_at: string }
type Task = {
  id: string; goal: string; repo_path: string; worktree_path: string; state: string; stage: string
  error?: string; updated_at: string; handoffs?: Handoff[]
  acceptance?: { passed: boolean; sha: string } | null
}
type Workspace = { path: string; name: string; workspace_status?: string }

const labels: Record<string, string> = {
  pending_dispatch: '等待启动', analyzing: '分析中', implementing: '修改中',
  reviewing: '验证或审阅中', repair_pending: '准备修复', completed: '已完成',
  failed: '失败', cancelled: '已停止', waiting_user: '等待处理', interrupted: '已中断',
}
const active = new Set(['analyzing', 'implementing', 'reviewing', 'repair_pending'])

export function SupervisorPanel({ token }: { token: string }) {
  const [tasks, setTasks] = useState<Task[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [detail, setDetail] = useState<Task | null>(null)
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [repo, setRepo] = useState('')
  const [goal, setGoal] = useState('')
  const [criteria, setCriteria] = useState('')
  const [commands, setCommands] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const refreshVersion = useRef(0)

  const request = useCallback(async <T,>(path: string, body?: unknown): Promise<T> => {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    const result = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(result.error || `请求失败 (${response.status})`)
    return result as T
  }, [token])

  const refresh = useCallback(async () => {
    const version = ++refreshVersion.current
    try {
      const [data, item] = await Promise.all([
        request<{ tasks: Task[] }>('/api/supervisor/tasks'),
        selectedId ? request<Task>(`/api/supervisor/tasks/${encodeURIComponent(selectedId)}`) : Promise.resolve(null),
      ])
      if (version !== refreshVersion.current) return
      setTasks(data.tasks)
      if (item) setDetail(item)
    } catch (err) {
      if (version === refreshVersion.current) setError(err instanceof Error ? err.message : '加载任务失败')
    }
  }, [request, selectedId])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), 4000)
    return () => { window.clearInterval(timer); refreshVersion.current++ }
  }, [refresh])

  useEffect(() => {
    let cancelled = false
    void request<{ workspaces: Workspace[]; default_workspace: string }>('/api/workspaces/local')
      .then(data => {
        if (!cancelled) {
          setWorkspaces(data.workspaces || [])
          setRepo(current => current || data.default_workspace || '')
        }
      }).catch(() => { /* A path can still be entered manually. */ })
    return () => { cancelled = true }
  }, [request])

  function validationCommands(): unknown {
    if (!commands.trim()) return undefined
    let parsed: unknown
    try { parsed = JSON.parse(commands) } catch { throw new Error('测试命令需要是 JSON 数组，例如 [["python3", "-m", "pytest"]]') }
    if (!Array.isArray(parsed) || !parsed.length || !parsed.every(command => Array.isArray(command) && command.length && command.every(arg => typeof arg === 'string' && arg.length))) {
      throw new Error('每条测试命令都需要是非空字符串数组')
    }
    return parsed
  }

  async function dispatch(id: string) {
    const validation = validationCommands()
    await request(`/api/supervisor/tasks/${encodeURIComponent(id)}/dispatch`, {
      acceptance_criteria: criteria.trim(), ...(validation ? { validation_commands: validation } : {}),
    })
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (busy || !repo.trim() || !goal.trim()) return
    setBusy(true); setError('')
    try {
      validationCommands() // Validate before creating a persistent task.
      const task = await request<Task>('/api/supervisor/tasks', { goal: goal.trim(), repo_path: repo.trim() })
      setSelectedId(task.id); setDetail(task); setGoal('')
      await dispatch(task.id)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '启动任务失败')
    } finally { setBusy(false) }
  }

  async function action(id: string, kind: 'cancel' | 'dispatch') {
    setBusy(true); setError('')
    try {
      if (kind === 'dispatch') await dispatch(id)
      else await request(`/api/supervisor/tasks/${encodeURIComponent(id)}/cancel`, {})
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败')
    } finally { setBusy(false) }
  }

  return <div className="supervisor-panel">
    <form className="supervisor-form" onSubmit={submit}>
      <p>描述目标后，Conveyor 会在独立工作树中分析、修改、测试和审阅。</p>
      <label>项目目录<input required value={repo} onChange={event => setRepo(event.target.value)} list="supervisor-workspaces" placeholder="服务器上的 Git 仓库路径" /></label>
      <datalist id="supervisor-workspaces">{workspaces.filter(item => item.workspace_status === 'ok').map(item => <option key={item.path} value={item.path}>{item.name}</option>)}</datalist>
      <label>想完成什么<textarea required rows={3} value={goal} onChange={event => setGoal(event.target.value)} placeholder="例如：修复登录失败，并验证相关测试" /></label>
      <details>
        <summary>验收要求与测试命令（可选）</summary>
        <label>验收要求<input value={criteria} onChange={event => setCriteria(event.target.value)} placeholder="如何判断结果符合要求" /></label>
        <label>测试命令（JSON 参数数组）<textarea rows={2} value={commands} onChange={event => setCommands(event.target.value)} placeholder={'[["python3", "-m", "pytest"]]'} /></label>
        <small>留空时尝试根据项目配置识别测试。无法识别时，可补充命令后重新启动。</small>
      </details>
      <button className="primary" type="submit" disabled={busy || !repo.trim() || !goal.trim()}>{busy ? '处理中…' : '开始任务'}</button>
    </form>
    {error && <div className="error-banner" role="alert">{error}</div>}
    <section aria-label="自动任务">
      <h3>自动任务</h3>
      {!tasks.length && <p>还没有自动任务。</p>}
      <div className="supervisor-task-list">{tasks.map(task => <button type="button" key={task.id}
        aria-pressed={task.id === selectedId} className={task.id === selectedId ? 'selected' : ''}
        onClick={() => { setDetail(null); setSelectedId(task.id); setError('') }}>
        <strong>{task.goal}</strong><span>{labels[task.state] || task.state}</span>
      </button>)}</div>
    </section>
    {detail && <section className="supervisor-detail" aria-label="任务详情">
      <h3>{detail.goal}</h3>
      <p>状态：{labels[detail.state] || detail.state} · {detail.stage}</p>
      {detail.error && <div className="error-banner">{detail.error}</div>}
      <p>项目：{detail.repo_path}</p>
      {detail.worktree_path && <p>工作树：{detail.worktree_path}</p>}
      {detail.acceptance ? <p>验收：{detail.acceptance.passed ? '通过' : '未通过'} · {detail.acceptance.sha.slice(0, 12)}</p> : detail.state === 'completed' ? <p>验收已失效，请重新验证。</p> : null}
      {detail.state === 'pending_dispatch' && <button className="action-btn" type="button" disabled={busy} onClick={() => void action(detail.id, 'dispatch')}>重新启动</button>}
      {active.has(detail.state) && <button className="action-btn" type="button" disabled={busy} onClick={() => void action(detail.id, 'cancel')}>停止任务</button>}
      {(detail.handoffs || []).map(item => <details key={item.id}>
        <summary>{item.from_role} → {item.to_role}</summary><pre>{item.summary}</pre>
      </details>)}
    </section>}
  </div>
}
