import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { BAD_SHAPE_MESSAGE, techDetail, useLoadingTimeout } from '../ux/knowledgeKit'

type Grant = { id: string; kind: 'path'|'actions'|'query'; host: string; port: number; path: string; query_field: string|null; expires_at: number; approver: string; action_types: string[] }
type Event = { timestamp: number; host: string; method: string; outcome: string; reason: string }
type Problem = { message: string; detail?: string }

const pad = (n: number) => String(n).padStart(2, '0')

function sameDay(a: Date, b: Date) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}

/** "11月3日 14:05"; the year only when it differs from this year. */
function absolute(seconds: number, withSeconds = false) {
  const d = new Date(seconds * 1000)
  if (Number.isNaN(d.getTime())) return '—'
  const year = d.getFullYear() === new Date().getFullYear() ? '' : `${d.getFullYear()}年`
  return `${year}${d.getMonth() + 1}月${d.getDate()}日 ${pad(d.getHours())}:${pad(d.getMinutes())}${withSeconds ? ':' + pad(d.getSeconds()) : ''}`
}

/** Journal time: "今天 14:05:12" / "昨天 23:10:03" / "11月1日 09:12:44". */
function journalTime(seconds: number) {
  const d = new Date(seconds * 1000)
  if (Number.isNaN(d.getTime())) return '—'
  const now = new Date()
  const clock = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  if (sameDay(d, now)) return '今天 ' + clock
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  if (sameDay(d, yesterday)) return '昨天 ' + clock
  return absolute(seconds, true)
}

function remaining(seconds: number) {
  const left = seconds * 1000 - Date.now()
  if (left <= 0) return '已到期'
  const minutes = Math.round(left / 60000)
  if (minutes < 1) return '即将到期'
  if (minutes < 60) return `还剩 ${minutes} 分钟`
  const hours = Math.round(minutes / 60)
  if (hours < 48) return `还剩 ${hours} 小时`
  return `还剩 ${Math.round(hours / 24)} 天`
}

const ACTION_LABELS: Record<string, string> = {
  follow_link: '点击链接', paginate: '翻页', expand: '展开内容', scroll: '滚动页面', search_get: '搜索',
}
const actionLabel = (type: string) => ACTION_LABELS[type] || type

/** Egress reasons are fixed machine codes; translate the ones people actually see. */
const REASONS: Record<string, string> = {
  approved_readonly: '已批准的只读访问',
  g2_step_initial: '打开已批准的页面',
  external_kill_switch: '外部浏览已紧急关闭',
  external_browser_disabled: '外部浏览功能未开启',
  approval_missing: '没有找到对应的批准',
  approval_changed: '批准已变更或被撤销',
  approval_store_unavailable: '暂时无法读取批准记录',
  redirect_denied: '页面跳转到了未批准的地址',
  navigation_denied: '页面跳转未获批准',
  subresource_denied: '页面加载的其他资源已拦截',
  websocket_denied: '页面的实时连接已拦截',
  popup_denied: '弹出窗口已拦截',
  download_denied: '下载已拦截',
  host_denied: '网站未获批准',
  origin_denied: '网站未获批准',
  path_denied: '页面未获批准',
  url_denied: '网址不符合要求',
  ip_literal_denied: '不允许直接访问 IP 地址',
  dns_private_address_denied: '网址指向内网地址，已拦截',
  unsafe_http_method: '不允许的请求方式',
  request_budget_denied: '超出单次访问的请求次数上限',
  nontext_response_denied: '返回的内容不是网页文字',
  operator_denied: '不是批准人发起的访问',
  g2_disabled: '页面操作功能未开启',
  g2_interaction_denied: '页面操作未获批准',
  g2_query_denied: '搜索未获批准',
  g2_search_scope_denied: '搜索不在批准范围内',
  g2_target_denied: '操作目标不在批准范围内',
  g2_form_denied: '表单操作未获批准',
  g2_sensitive_dom_denied: '页面包含敏感输入框，已停止',
  g2_dom_race_denied: '页面在操作时发生变化，已停止',
}

function describeReason(reason: string) {
  if (REASONS[reason]) return REASONS[reason]
  if (reason.startsWith('g2_step_')) return '已批准的页面操作：' + actionLabel(reason.slice('g2_step_'.length))
  if (reason.startsWith('g5_')) return '网站未通过上线检查'
  if (reason.startsWith('g2_')) return '页面操作已拦截'
  if (reason.startsWith('login_')) return '登录相关的限制'
  return '其他原因'
}

function describeKillError(code: string): Problem {
  if (code === 'kill_path_unmanaged') return { message: '服务器使用了自定义的紧急开关文件，只能在服务器上切换。', detail: code }
  if (code === 'kill_path_unsafe') return { message: '服务器上的紧急开关文件不安全，已拒绝操作。', detail: code }
  return { message: '切换失败，请稍后重试。', detail: code }
}

async function errorCode(response: Response) {
  const body = await response.json().catch(() => null) as { error?: unknown } | null
  return typeof body?.error === 'string' ? body.error : `HTTP ${response.status}`
}

const hostLabel = (g: Grant) => g.host + (g.port === 443 ? '' : ':' + g.port)

/** Let long paths wrap after each "/" instead of in the middle of a word. */
const breakablePath = (path: string) =>
  path.split('/').map((part, i) => i === 0 ? part : <Fragment key={i}>/<wbr />{part}</Fragment>)

function permission(g: Grant) {
  if (g.kind === 'query') return <>在这个页面搜索{g.query_field && <span className="guard-sub">搜索字段 <code>{g.query_field}</code></span>}</>
  if (g.kind === 'actions') return <>页面操作：{g.action_types.map(actionLabel).join('、') || '—'}</>
  return <>只读查看页面</>
}

function revokeEffect(g: Grant) {
  const target = hostLabel(g) + g.path
  if (g.kind === 'path') return `撤销后，助手不能再访问 ${target}，这个页面上的其他操作授权也会一起撤销。`
  if (g.kind === 'query') return `撤销后，助手不能再在 ${target} 上搜索，但仍可以查看这个页面。`
  return `撤销后，助手不能再在 ${target} 上${g.action_types.map(actionLabel).join('、') || '操作'}，但仍可以查看这个页面。`
}

export function BrowserApprovalsPanel({ token }: { token: string }) {
  const [grants, setGrants] = useState<Grant[]>([])
  const [events, setEvents] = useState<Event[]>([])
  const [killed, setKilled] = useState(false)
  const [limit, setLimit] = useState(50)
  const [busy, setBusy] = useState(false)
  const [loaded, setLoaded] = useState(false)
  // Whether the approvals (and so the kill-switch state) have been read successfully at least once.
  const [known, setKnown] = useState(false)
  const [problem, setProblem] = useState<Problem | null>(null)
  const [actionProblem, setActionProblem] = useState<Problem | null>(null)
  const [journalProblem, setJournalProblem] = useState<Problem | null>(null)
  const [notice, setNotice] = useState('')
  const [confirmKill, setConfirmKill] = useState(false)
  const [confirmRevoke, setConfirmRevoke] = useState('')
  const killDialog = useRef<HTMLDialogElement>(null)
  const killCancel = useRef<HTMLButtonElement>(null)
  const revokeButtons = useRef(new Map<string, HTMLButtonElement>())
  const headers = { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }

  useLoadingTimeout(!loaded, () => { setProblem({ message: '加载太久了，请重试。' }); setLoaded(true) })

  const reload = useCallback(async () => {
    const [a, b] = await Promise.all([
      fetch('/api/browser/approvals', { headers, cache:'no-store' }).catch(() => null),
      fetch('/api/browser/egress?limit=' + limit, { headers, cache:'no-store' }).catch(() => null),
    ])
    try {
      if (!a) setProblem({ message: '无法连接服务器，网站授权暂时读取不到。' })
      else if (!a.ok) setProblem({ message: '无法读取网站授权，请稍后重试。', detail: await errorCode(a) })
      else {
        const approvals = await a.json().catch(() => null)
        if (!approvals || typeof approvals !== 'object' || !Array.isArray(approvals.approvals)) setProblem({ message: BAD_SHAPE_MESSAGE })
        else {
          setGrants(approvals.approvals.filter((g: unknown) => g && typeof g === 'object'))
          setKilled(Boolean(approvals.kill_switch))
          setKnown(true)
          setProblem(null)
        }
      }
      if (!b) setJournalProblem({ message: '无法连接服务器，访问记录暂时读取不到。' })
      else if (!b.ok) setJournalProblem({ message: '无法读取访问记录，请稍后重试。', detail: await errorCode(b) })
      else {
        const log = await b.json().catch(() => null)
        if (!log || typeof log !== 'object' || !Array.isArray(log.requests)) setJournalProblem({ message: BAD_SHAPE_MESSAGE })
        else {
          setEvents(log.requests.filter((e: unknown) => e && typeof e === 'object'))
          setJournalProblem(null)
        }
      }
    } catch {
      setProblem({ message: '服务器返回的数据无法识别，请稍后重试。' })
    } finally {
      setLoaded(true)
    }
  }, [token,limit])

  useEffect(() => { void reload() }, [reload])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 8000)
    return () => window.clearTimeout(timer)
  }, [notice])

  useEffect(() => {
    const dialog = killDialog.current
    if (!dialog) return
    if (confirmKill && !dialog.open) {
      if (typeof dialog.showModal === 'function') dialog.showModal()
      else dialog.setAttribute('open', '')
      killCancel.current?.focus()
    } else if (!confirmKill && dialog.open) {
      dialog.close()
    }
  }, [confirmKill])

  const setKill = async (enabled: boolean) => {
    setBusy(true)
    setNotice('')
    setActionProblem(null)
    try {
      const res = await fetch('/api/browser/kill', {
        method:'POST', headers, body:JSON.stringify({enabled}),
      })
      if (!res.ok) setActionProblem(describeKillError(await errorCode(res)))
      else setNotice(enabled ? '已紧急关闭外部浏览，助手现在不能访问任何外部网站。' : '已恢复外部浏览，已批准的网站重新生效。')
      setConfirmKill(false)
      await reload()
    } catch {
      setActionProblem({ message: '无法连接服务器，外部浏览状态没有改变。' })
      setConfirmKill(false)
    } finally { setBusy(false) }
  }

  const cancelRevoke = (id: string) => {
    setConfirmRevoke('')
    requestAnimationFrame(() => revokeButtons.current.get(id)?.focus())
  }

  const revoke = async (g: Grant) => {
    setBusy(true)
    setNotice('')
    setActionProblem(null)
    try {
      const res = await fetch('/api/browser/approvals/' + encodeURIComponent(g.id), {
        method:'DELETE', headers,
      })
      if (!res.ok) setActionProblem({ message: '撤销失败，这项批准可能已经到期或被撤销。', detail: await errorCode(res) })
      else setNotice(`已撤销 ${hostLabel(g)}${g.path} 的授权。`)
      setConfirmRevoke('')
      await reload()
    } catch {
      setActionProblem({ message: '无法连接服务器，授权没有撤销。' })
    } finally { setBusy(false) }
  }

  return <div className="guard-page guard-browser">
    <div className="guard-stack">
      <p className="guard-intro">助手访问外部网站前需要你批准；这里列出已批准的网站和最近的访问记录。</p>

      <section className={`guard-card guard-status-card${killed ? ' is-danger' : ''}`} aria-labelledby="guard-browser-status-title">
        <div className="guard-status-main">
          <h3 id="guard-browser-status-title" className="guard-status-title">
            <span className={`guard-dot ${!known ? 'is-muted' : killed ? 'is-danger' : 'is-ok'}`} aria-hidden="true" />
            {!loaded ? '外部浏览：读取中…' : !known ? '外部浏览：状态未知' : killed ? '外部浏览：已紧急关闭' : '外部浏览：正常'}
          </h3>
          <p className="guard-muted">{!loaded ? '正在读取外部浏览的状态。'
            : !known ? '暂时读取不到状态。紧急情况下仍可以直接关闭外部浏览。'
            : killed ? '助手现在不能访问任何外部网站。批准记录都还在，恢复后立即生效。'
            : '助手只能访问下面已批准的网站和页面，其他网站一律拦截。'}</p>
        </div>
        <div className="guard-actions">
          <button type="button" className="guard-btn" disabled={busy} onClick={() => void reload()}>刷新</button>
          {killed
            ? <button type="button" className="guard-btn guard-btn-primary" disabled={busy || !loaded}
                onClick={() => void setKill(false)}>{busy ? '处理中…' : '恢复外部浏览'}</button>
            : <button type="button" className="guard-btn guard-btn-danger" disabled={busy || !loaded}
                onClick={() => { setNotice(''); setConfirmKill(true) }}>紧急关闭外部浏览</button>}
        </div>
      </section>

      {(notice || actionProblem || problem) && <div className="guard-messages" aria-live="polite">
        {notice && <p role="status" className="guard-feedback guard-feedback-ok">{notice}</p>}
        {[actionProblem, problem].map((item, index) => item && <p key={index} role="alert" className="guard-feedback guard-feedback-error">
          {item.message}
          {techDetail(item.detail) && <span className="guard-tech">{techDetail(item.detail)}</span>}
        </p>)}
      </div>}

      <section className="guard-card" aria-labelledby="guard-browser-grants-title">
        <div className="guard-card-head">
          <h3 id="guard-browser-grants-title" className="guard-card-title">已批准的网站</h3>
          {grants.length > 0 && <span className="guard-count">{grants.length} 项</span>}
        </div>
        <p className="guard-card-desc">每项批准只对指定的页面和操作有效，到期自动失效。网页上的内容不能替你批准。</p>
        {!loaded ? <p className="guard-empty">加载中…</p>
          : !known ? <p className="guard-empty">暂时读取不到批准记录。</p>
          : !grants.length ? <p className="guard-empty">还没有批准任何网站。助手需要访问网站时，会先征求你的同意。</p>
          : <div className="guard-table-wrap">
            <table className="guard-table guard-grants-table">
              <thead><tr>
                <th scope="col">网址</th>
                <th scope="col">允许的操作</th>
                <th scope="col">到期时间</th>
                <th scope="col">批准人</th>
                <th scope="col"><span className="guard-visually-hidden">操作</span></th>
              </tr></thead>
              <tbody>{grants.map(g => <Fragment key={g.id}>
                <tr className={confirmRevoke === g.id ? 'is-confirming' : undefined}>
                  <td data-label="网址" className="guard-cell-site">
                    <span className="guard-host">{hostLabel(g)}</span>
                    <code className="guard-path">{breakablePath(g.path)}</code>
                  </td>
                  <td data-label="允许的操作" className="guard-cell-perm">{permission(g)}</td>
                  <td data-label="到期时间" className="guard-cell-expiry">{absolute(g.expires_at)}<span className="guard-sub">{remaining(g.expires_at)}</span></td>
                  <td data-label="批准人" className="guard-cell-approver"><code className="guard-fingerprint" title="批准人的匿名标识">{g.approver.replace(/^operator:/, '')}</code></td>
                  <td className="guard-cell-actions">
                    {confirmRevoke !== g.id && <button type="button" className="guard-btn guard-btn-small guard-btn-danger" disabled={busy}
                      ref={element => { if (element) revokeButtons.current.set(g.id, element); else revokeButtons.current.delete(g.id) }}
                      aria-label={`撤销 ${hostLabel(g)}${g.path} 的授权`}
                      onClick={() => { setNotice(''); setConfirmRevoke(g.id) }}>撤销</button>}
                  </td>
                </tr>
                {confirmRevoke === g.id && <tr className="guard-confirm-row">
                  <td colSpan={5}>
                    <div className="guard-inline-confirm" role="group" aria-label={`确认撤销 ${hostLabel(g)}${g.path} 的授权`}
                      onKeyDown={event => { if (event.key === 'Escape') cancelRevoke(g.id) }}>
                      <p>{revokeEffect(g)}</p>
                      <div className="guard-inline-confirm-actions">
                        <button type="button" className="guard-btn guard-btn-small" autoFocus disabled={busy}
                          onClick={() => cancelRevoke(g.id)}>取消</button>
                        <button type="button" className="guard-btn guard-btn-small guard-btn-danger-solid" disabled={busy}
                          onClick={() => void revoke(g)}>{busy ? '撤销中…' : '确认撤销'}</button>
                      </div>
                    </div>
                  </td>
                </tr>}
              </Fragment>)}</tbody>
            </table>
          </div>}
      </section>

      <section className="guard-card" aria-labelledby="guard-browser-journal-title">
        <div className="guard-card-head">
          <h3 id="guard-browser-journal-title" className="guard-card-title">最近访问记录</h3>
          <label className="guard-select-label">显示最近
            <select className="guard-select" value={limit} onChange={e => setLimit(Number(e.target.value))}>
              {[20,50,100,200].map(n => <option key={n} value={n}>{n}</option>)}
            </select>条</label>
        </div>
        {journalProblem && <p role="alert" className="guard-feedback guard-feedback-error">
          {journalProblem.message}
          {techDetail(journalProblem.detail) && <span className="guard-tech">{techDetail(journalProblem.detail)}</span>}
        </p>}
        {!loaded ? <p className="guard-empty">加载中…</p>
          : !events.length ? (!journalProblem && <p className="guard-empty">还没有访问记录。</p>)
          : <div className="guard-table-wrap">
            <table className="guard-table guard-journal-table">
              <thead><tr>
                <th scope="col">时间</th>
                <th scope="col">网站</th>
                <th scope="col">结果</th>
                <th scope="col">说明</th>
              </tr></thead>
              <tbody>{events.map((e,i) => <tr key={e.timestamp + '-' + i}>
                <td data-label="时间" className="guard-cell-time"><time dateTime={Number.isFinite(e.timestamp) ? new Date(e.timestamp * 1000).toISOString() : undefined} title={absolute(e.timestamp, true)}>{journalTime(e.timestamp)}</time></td>
                <td data-label="网站" className="guard-cell-host">{e.host === 'unknown' ? <span className="guard-unknown">未知网站</span> : e.host}</td>
                <td data-label="结果" className="guard-cell-outcome">{e.outcome === 'allowed'
                  ? <span className="guard-badge guard-badge-ok">已放行</span>
                  : <span className="guard-badge guard-badge-danger">已拦截</span>}</td>
                <td data-label="说明" className="guard-cell-reason">{describeReason(e.reason)}<span className="guard-sub guard-tech">{e.method} · {e.reason}</span></td>
              </tr>)}</tbody>
            </table>
          </div>}
        <p className="guard-privacy">为保护隐私，这里不记录网址路径、搜索内容、Cookie、提交的数据和网页文字。</p>
      </section>
    </div>

    <dialog ref={killDialog} className="guard-dialog" aria-labelledby="guard-kill-title" aria-describedby="guard-kill-desc"
      onCancel={event => { event.preventDefault(); if (!busy) setConfirmKill(false) }}
      onClose={() => setConfirmKill(false)}
      onClick={event => { if (event.target === event.currentTarget && !busy) setConfirmKill(false) }}>
      <div className="guard-dialog-body">
        <h3 id="guard-kill-title" className="guard-dialog-title">紧急关闭外部浏览？</h3>
        <div id="guard-kill-desc" className="guard-dialog-text">
          <p>关闭后立即生效：</p>
          <ul>
            <li>助手不能再访问任何外部网站，正在进行的网页访问会被中断。</li>
            <li>已批准的网站会保留，但在恢复之前都不起作用。</li>
            <li>之后可以随时在这里点“恢复外部浏览”。</li>
          </ul>
        </div>
        <div className="guard-dialog-actions">
          <button ref={killCancel} type="button" className="guard-btn" disabled={busy} onClick={() => setConfirmKill(false)}>取消</button>
          <button type="button" className="guard-btn guard-btn-danger-solid" disabled={busy}
            onClick={() => void setKill(true)}>{busy ? '正在关闭…' : '立即关闭'}</button>
        </div>
      </div>
    </dialog>
  </div>
}
