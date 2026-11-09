import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { SupervisorRepo } from './SupervisorWorkspace'

// ---------------------------------------------------------------------------
// 自动检查 contract types — mirrors supervisor_validation.py payloads.
// All owner-facing copy is Simplified Chinese; raw backend codes never render.
// ---------------------------------------------------------------------------

/** Where the commands that gate a finished round come from. */
export type SupervisorChecksSource = 'env' | 'repo' | 'none'

/** One server-detected check the owner may switch on for a repository. */
export type SupervisorChecksCandidate = {
  id: string
  label: string
  argv: string[]
  reason: string
  available: boolean
  unavailable_reason: string
}

/** GET/POST /api/supervisor/repositories/<repo_id>/validation payload. */
export type SupervisorChecksState = {
  repo_id: string
  candidates: SupervisorChecksCandidate[]
  selected: string[]
  source: SupervisorChecksSource
  env_commands: string[]
  effective: Array<{ name: string; argv: string[] }>
}

/** Compact status the host surfaces (goal form warning, repository badge). */
export type SupervisorChecksSummary = {
  source: SupervisorChecksSource
  selectedCount: number
  loaded: boolean
}

const CHECKS_LOAD_ERROR = '没有读到这个仓库的自动检查设置，请稍后再试。'

// Backend errors arrive as {error, code}; some messages are English. The owner only
// reads Chinese, so anything without Chinese characters becomes one generic line.
export function checksChineseError(err: unknown): string {
  const code = (err as { code?: string } | null | undefined)?.code
  if (code === 'unknown_check') return '这个检查项目已经不在了，请刷新页面后再保存一次。'
  const raw = err instanceof Error ? err.message.trim() : ''
  if (raw && /[\u4e00-\u9fff]/.test(raw)) return raw
  return '保存没有成功，请稍后再试。'
}

function sameSelection(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false
  const sortedA = [...a].sort()
  const sortedB = [...b].sort()
  return sortedA.every((value, index) => value === sortedB[index])
}

/**
 * 「自动检查」 card for one repository. Reads the server-detected candidates once,
 * lets the owner tick the ones that must pass before a round counts as done, and
 * saves the selection back. Never renders ids, argv or backend codes.
 */
export default function SupervisorChecks({
  apiCall,
  repo,
  onSummaryChange,
  onSaved,
}: {
  apiCall: <T>(path: string, init?: RequestInit) => Promise<T>
  repo: SupervisorRepo
  onSummaryChange?: (summary: SupervisorChecksSummary) => void
  onSaved?: () => void
}) {
  const [state, setState] = useState<SupervisorChecksState | null>(null)
  const [draft, setDraft] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [saveError, setSaveError] = useState('')

  const validationPath = `/api/supervisor/repositories/${encodeURIComponent(repo.id)}/validation`

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError('')
    setState(null)
    setDraft([])
    setSaved(false)
    setSaveError('')
    void (async () => {
      try {
        const res = await apiCall<SupervisorChecksState>(validationPath)
        if (cancelled) return
        setState(res)
        setDraft(Array.isArray(res.selected) ? res.selected : [])
        setLoading(false)
      } catch {
        if (cancelled) return
        setLoadError(CHECKS_LOAD_ERROR)
        setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [apiCall, validationPath])

  // Until the payload lands, trust the flag that ships with the repository list.
  const source: SupervisorChecksSource = state?.source ?? repo.validation_source ?? 'none'
  const selectedCount = state ? state.selected.length : 0
  const loaded = !loading && !loadError && Boolean(state)
  const summaryText = source === 'env'
    ? '自动检查：服务器已配置'
    : selectedCount > 0
      ? `自动检查：${selectedCount} 项`
      : '自动检查：未设置'

  const lastSummaryRef = useRef<SupervisorChecksSummary | null>(null)
  useEffect(() => {
    const next: SupervisorChecksSummary = { source, selectedCount, loaded }
    const prev = lastSummaryRef.current
    if (prev && prev.source === next.source && prev.selectedCount === next.selectedCount && prev.loaded === next.loaded) {
      return
    }
    lastSummaryRef.current = next
    onSummaryChange?.(next)
  }, [loaded, onSummaryChange, selectedCount, source])

  const dirty = useMemo(() => (state ? !sameSelection(draft, state.selected) : false), [draft, state])

  const toggle = useCallback((id: string, checked: boolean) => {
    setDraft(prev => (checked ? [...prev, id] : prev.filter(value => value !== id)))
    setSaved(false)
    setSaveError('')
  }, [])

  const handleSave = useCallback(async () => {
    if (!state || saving || !dirty) return
    setSaving(true)
    setSaveError('')
    setSaved(false)
    try {
      const res = await apiCall<SupervisorChecksState>(validationPath, {
        method: 'POST',
        body: JSON.stringify({ selected: draft }),
      })
      setState(res)
      setDraft(Array.isArray(res.selected) ? res.selected : [])
      setSaved(true)
      onSaved?.()
    } catch (err) {
      setSaveError(checksChineseError(err))
    } finally {
      setSaving(false)
    }
  }, [apiCall, dirty, draft, onSaved, saving, state, validationPath])

  const candidates = state?.candidates || []
  const hasList = source !== 'env' && candidates.length > 0
  const summaryHint = source === 'env' ? '点开可以查看' : '点开可以勾选每一轮要跑的命令'

  return (
    <details className="sup-checks">
      <summary className="sup-checks-summary">
        <span className="sup-checks-chevron" aria-hidden="true">▸</span>
        <span>{summaryText}</span>
        <span className="sup-checks-summary-hint">{summaryHint}</span>
      </summary>
      <div className="sup-checks-body">
        <div className="sup-checks-head">
          <strong>自动检查</strong>
          <span className="sup-checks-hint">每一轮做完会自动跑一遍</span>
        </div>

        {loading && <p className="sup-muted-note">正在读取这个仓库里的检查命令…</p>}

        {!loading && loadError && (
          <div className="sup-banner sup-banner--error" role="alert">
            <span>{loadError}</span>
          </div>
        )}

        {!loading && !loadError && state && source === 'env' && (
          <p className="sup-checks-env-line">由服务器管理员配置：{state.env_commands.join('、')}</p>
        )}

        {!loading && !loadError && state && source !== 'env' && candidates.length === 0 && (
          <p className="sup-checks-empty">
            没有在这个仓库里找到测试命令。没有自动检查时，每一轮做完都会停下来请你检查。
          </p>
        )}

        {hasList && (
          <ul className="sup-checks-list">
            {candidates.map(candidate => {
              const checked = draft.includes(candidate.id)
              return (
                <li
                  key={candidate.id}
                  className={`sup-checks-item ${candidate.available ? '' : 'is-unavailable'}`}
                >
                  <label className="sup-checks-label">
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={!candidate.available || saving}
                      onChange={event => toggle(candidate.id, event.target.checked)}
                    />
                    <span className="sup-checks-label-text">{candidate.label}</span>
                  </label>
                  {candidate.reason && <small className="sup-checks-reason">{candidate.reason}</small>}
                  {/* The raw command is technical: it stays behind 技术详情 (QA N29). */}
                  {candidate.argv.length > 0 && (
                    <details className="sup-checks-tech">
                      <summary>技术详情</summary>
                      <code>{candidate.argv.join(' ')}</code>
                    </details>
                  )}
                  {!candidate.available && candidate.unavailable_reason && (
                    <small className="sup-checks-unavailable">{candidate.unavailable_reason}</small>
                  )}
                </li>
              )
            })}
          </ul>
        )}

        {hasList && (
          <div className="sup-checks-actions">
            <button
              type="button"
              className="shell-button primary"
              disabled={!dirty || saving}
              onClick={() => void handleSave()}
            >
              {saving ? '正在保存…' : '保存'}
            </button>
            {saved && (
              <span className="sup-checks-saved" role="status">已保存</span>
            )}
            {saveError && (
              <span className="sup-checks-save-error" role="alert">{saveError}</span>
            )}
          </div>
        )}

        {hasList && (
          <p className="sup-checks-note">每一轮改完后会在工作目录里运行勾选的命令，全部通过才算这一轮完成。</p>
        )}
      </div>
    </details>
  )
}
