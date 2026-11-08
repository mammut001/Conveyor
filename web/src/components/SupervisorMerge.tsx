import { useCallback, useEffect, useState } from 'react'
import type { SupervisorWorktree } from './SupervisorWorkspace'
import { ChangedFileList, type FileStat } from './TaskParts'

// ---------------------------------------------------------------------------
// 「合并到主分支」：GET /api/supervisor/worktrees/<id>/merge-preview 与
// POST /api/supervisor/worktrees/<id>/merge 的读取与确认。老板只看中文：
// 读不到、还不能合、合失败，都有一句白话；后端码永远不出现在页面上。
// ---------------------------------------------------------------------------

export type SupervisorMergeBlocker = {
  code: string
  message: string
}

export type SupervisorMergeFile = {
  path: string
  status: string
  additions: number
  deletions: number
}

export type SupervisorMergePreview = {
  worktree_id: string
  repo_id: string
  source_branch: string
  target_branch: string
  can_merge: boolean
  blockers: SupervisorMergeBlocker[]
  files: SupervisorMergeFile[]
  commits_ahead: number
  has_uncommitted: boolean
}

export type SupervisorMergeResult = {
  merged: boolean
  target_branch: string
  merge_commit: string
  files_changed: number
}

const MERGE_LOADING_NOTE = '正在检查这个工作目录能不能合并…'
const MERGE_ERROR_NOTE = '暂时读不到这个工作目录的合并信息，稍后再试。'
const MERGE_NOTHING_NOTE = '这个工作目录还没有可以合并的改动。'
const MERGE_RECHECK_LABEL = '重新检查'
const MERGE_MERGING_LABEL = '正在合并…'
const MERGE_CONFIRM_LABEL = '确定合并'
const MERGE_CANCEL_LABEL = '取消'
const MERGE_ERROR_TITLE = '没有合并成功'
const MERGE_CONFLICT_NOTE = '主分支没有被改动。'
const MERGE_GENERIC_ERROR = '合并没有成功，请稍后再试。'

/** 一句话说清楚要合并什么：哪个分支的几个文件改动，合到主分支的哪个分支。 */
export function mergeSummaryText(source: string, fileCount: number, target: string): string {
  return `把 ${source} 里的 ${fileCount} 个文件改动合并到 ${target}`
}

export function mergeButtonText(target: string): string {
  return `合并到 ${target}`
}

export function mergeConfirmText(target: string): string {
  return `合并后主分支 ${target} 会包含这些改动。不会推送到远程。确定合并吗？`
}

export function mergeSuccessText(target: string, shortSha: string): string {
  return `已合并到 ${target}（提交 ${shortSha}）。`
}

/** 后端错误是 {error, code}：message 是中文就照常说，否则换成一句通用的白话。 */
export function mergeChineseError(err: unknown): { message: string; conflict: boolean } {
  const code = (err as { code?: string } | null | undefined)?.code
  const raw = err instanceof Error ? err.message.trim() : ''
  const message = raw && /[\u4e00-\u9fff]/.test(raw) ? raw : MERGE_GENERIC_ERROR
  return { message, conflict: code === 'merge_conflict' }
}

function shortMergeSha(sha?: string | null): string {
  const raw = (sha || '').trim()
  return raw ? raw.slice(0, 7) : ''
}

/** 未跟踪的新文件按「新增」显示（后端可能给 '?' 或 '??'）。 */
function mergeFileChip(status?: string): string {
  const code = (status || 'M').trim().toUpperCase()
  return code === '?' || code === '??' ? 'A' : code
}

/**
 * 「合并到主分支」 card：点开才读预览（懒加载），列出待合并的文件，确认后才
 * POST {confirm: true}。合成功显示提交号并重新读一次预览（这回什么都没有了）。
 */
export default function SupervisorMerge({
  apiCall,
  worktree,
}: {
  apiCall: <T>(path: string, init?: RequestInit) => Promise<T>
  worktree: SupervisorWorktree
}) {
  const [preview, setPreview] = useState<SupervisorMergePreview | null>(null)
  const [loadState, setLoadState] = useState<'loading' | 'ready' | 'error'>('loading')
  // 「重新检查」/ 合并成功之后把预览再读一次：只加一个 key，effect 自己重跑。
  const [reloadKey, setReloadKey] = useState(0)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [merging, setMerging] = useState(false)
  const [mergeError, setMergeError] = useState('')
  const [mergeConflict, setMergeConflict] = useState(false)
  const [mergedSha, setMergedSha] = useState('')

  const worktreeId = worktree.id
  const reload = useCallback(() => setReloadKey(key => key + 1), [])

  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()
    setLoadState('loading')
    setPreview(null)
    void (async () => {
      try {
        const data = await apiCall<SupervisorMergePreview>(
          `/api/supervisor/worktrees/${encodeURIComponent(worktreeId)}/merge-preview`,
          { signal: controller.signal },
        )
        if (cancelled || controller.signal.aborted) return
        setPreview(data)
        setLoadState('ready')
      } catch {
        if (cancelled || controller.signal.aborted) return
        setPreview(null)
        setLoadState('error')
      }
    })()
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [apiCall, reloadKey, worktreeId])

  const handleMerge = useCallback(async () => {
    if (merging) return
    setMerging(true)
    setMergeError('')
    setMergeConflict(false)
    try {
      const result = await apiCall<SupervisorMergeResult>(
        `/api/supervisor/worktrees/${encodeURIComponent(worktreeId)}/merge`,
        { method: 'POST', body: JSON.stringify({ confirm: true }) },
      )
      setMergedSha(shortMergeSha(result.merge_commit))
      setConfirmOpen(false)
      reload()
    } catch (err) {
      const { message, conflict } = mergeChineseError(err)
      setMergeError(message)
      setMergeConflict(conflict)
      setConfirmOpen(false)
    } finally {
      setMerging(false)
    }
  }, [apiCall, merging, reload, worktreeId])

  const files = Array.isArray(preview?.files) ? preview!.files : []
  const blockers = Array.isArray(preview?.blockers) ? preview!.blockers : []
  const source = (preview?.source_branch || worktree.branch || '').trim()
  const target = (preview?.target_branch || '').trim()
  // 一个文件改动都没有、也没有没提交的改动：没什么可合并的。
  const nothingToMerge = loadState === 'ready' && files.length === 0 && !preview?.has_uncommitted
  const stats: Record<string, FileStat> = {}
  const chipFiles: { path: string; status: string }[] = []
  for (const file of files) {
    if (!file?.path) continue
    stats[file.path] = { additions: file.additions || 0, deletions: file.deletions || 0 }
    chipFiles.push({ path: file.path, status: mergeFileChip(file.status) })
  }

  return (
    <section className="sup-card sup-merge-card" aria-label="合并到主分支">
      <div className="sup-card-head">
        <h4>合并到主分支</h4>
        <span className="sup-card-hint">只在本地合并，不会推送到远程</span>
      </div>

      {mergedSha && target && (
        <p className="sup-merge-success" role="status">{mergeSuccessText(target, mergedSha)}</p>
      )}

      {loadState === 'loading' && <p className="sup-muted-note">{MERGE_LOADING_NOTE}</p>}

      {loadState === 'error' && (
        <div className="sup-merge-block">
          <p className="sup-muted-note">{MERGE_ERROR_NOTE}</p>
          <button type="button" className="shell-button" onClick={reload}>{MERGE_RECHECK_LABEL}</button>
        </div>
      )}

      {loadState === 'ready' && nothingToMerge && (
        <p className="sup-muted-note">{MERGE_NOTHING_NOTE}</p>
      )}

      {loadState === 'ready' && !nothingToMerge && blockers.length > 0 && (
        <div className="sup-merge-block">
          <ul className="sup-merge-blockers" aria-label="暂时不能合并的原因">
            {blockers.map((blocker, index) => (
              <li key={`${index}-${blocker.code}`}>{blocker.message}</li>
            ))}
          </ul>
          <button type="button" className="shell-button" onClick={reload}>{MERGE_RECHECK_LABEL}</button>
        </div>
      )}

      {loadState === 'ready' && !nothingToMerge && blockers.length === 0 && (
        <div className="sup-merge-block">
          <p className="sup-merge-summary">{mergeSummaryText(source, files.length, target)}</p>
          <div className="sup-changes">
            <ChangedFileList files={chipFiles} stats={stats} />
          </div>
          {preview?.has_uncommitted && (
            <p className="sup-merge-note">工作目录里还有没提交的改动，合并时会自动提交成一个合并提交。</p>
          )}
          {!confirmOpen && (
            <button
              type="button"
              className="shell-button primary"
              disabled={merging}
              onClick={() => setConfirmOpen(true)}
            >
              {mergeButtonText(target)}
            </button>
          )}
        </div>
      )}

      {confirmOpen && (
        <div className="sup-banner sup-banner--warning sup-merge-confirm" role="alert">
          <div className="sup-banner-text">
            <span>{mergeConfirmText(target)}</span>
          </div>
          <div className="sup-merge-confirm-actions">
            <button
              type="button"
              className="shell-button"
              disabled={merging}
              onClick={() => setConfirmOpen(false)}
            >
              {MERGE_CANCEL_LABEL}
            </button>
            <button
              type="button"
              className="shell-button primary"
              disabled={merging}
              onClick={() => void handleMerge()}
            >
              {merging ? MERGE_MERGING_LABEL : MERGE_CONFIRM_LABEL}
            </button>
          </div>
        </div>
      )}

      {mergeError && (
        <div className="sup-banner sup-banner--error" role="alert">
          <strong>{MERGE_ERROR_TITLE}</strong>
          <span>{mergeError}</span>
          {mergeConflict && <span className="sup-merge-conflict-note">{MERGE_CONFLICT_NOTE}</span>}
        </div>
      )}
    </section>
  )
}
