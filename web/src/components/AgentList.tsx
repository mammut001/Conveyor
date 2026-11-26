import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import type { GithubRepoItem, LocalWorkspaceItem } from './WorkspaceBindingPicker'

export type Agent = {
  id: string
  name: string
  color: string
  instructions: string
  workspace_path: string
  workspace_status?: '' | 'ok' | 'missing' | 'not_git'
  github_repo?: string
  workspace_source?: string
  session_id: string
  is_default: boolean
  status: 'idle' | 'working' | 'waiting'
  last_message: string
  last_message_role?: string | null
  last_activity?: string | null
  message_count: number
}

export const AGENT_COLORS = ['#2f7df6', '#f59e0b', '#f97316', '#8b5cf6', '#10b981', '#ec4899', '#a16207', '#64748b']

/** Last path segment or GitHub repo: what the agent is "about", shown as a tag beside its name. */
export function agentTag(agent: Pick<Agent, 'workspace_path' | 'github_repo'>): string {
  if (agent.github_repo) return `🐙 ${agent.github_repo}`
  return agent.workspace_path.replace(/\/+$/, '').split('/').pop() || ''
}

export function AgentAvatar({ agent, size = 44 }: { agent: Pick<Agent, 'name' | 'color'>; size?: number }) {
  return (
    <span className="agent-avatar" style={{ width: size, height: size, background: agent.color, fontSize: Math.round(size * 0.42) }} aria-hidden="true">
      {(agent.name.trim()[0] || '?').toUpperCase()}
    </span>
  )
}

function preview(agent: Agent): string {
  if (agent.status === 'waiting') return 'Waiting for you'
  if (agent.status === 'working') return 'Working…'
  if (agent.last_message) return (agent.last_message_role === 'user' ? 'You: ' : '') + agent.last_message
  return agent.instructions || 'No messages yet'
}

export function AgentList({ agents, selectedId, onSelect, onNew, onEdit }: {
  agents: Agent[]
  selectedId: string
  onSelect: (agent: Agent) => void
  onNew: () => void
  onEdit: (agent: Agent) => void
}) {
  return (
    <>
      <div className="panel-heading">
        <div><p className="eyebrow">AGENTS</p><h2>Conversations</h2></div>
        <button className="icon-button" onClick={onNew} aria-label="New agent">＋</button>
      </div>
      <div className="session-list agent-list">
        {agents.map(agent => (
          <div key={agent.id} className="agent-row">
            <button
              type="button"
              className={`agent-item ${agent.id === selectedId ? 'active' : ''}`}
              aria-pressed={agent.id === selectedId}
              onClick={() => onSelect(agent)}
            >
              <AgentAvatar agent={agent} />
              <span className="agent-item-text">
                <span className="agent-item-title">
                  <strong>{agent.name}</strong>
                  {agentTag(agent) && <span className="agent-tag">{agentTag(agent)}</span>}
                </span>
                <small className={agent.status === 'waiting' ? 'waiting' : ''}>{preview(agent)}</small>
              </span>
              {agent.status !== 'idle' && <span className={`agent-status-dot ${agent.status}`} aria-label={agent.status === 'waiting' ? 'Waiting for you' : 'Working'} />}
            </button>
            <button type="button" className="agent-edit-btn" title="Edit agent" aria-label={`Edit ${agent.name}`} onClick={() => onEdit(agent)}>⋯</button>
          </div>
        ))}
      </div>
    </>
  )
}

export type AgentDraft = {
  name: string
  instructions: string
  workspace_path: string
  github_repo?: string
  workspace_source?: string
  color: string
}

export function AgentDialog({ agent, token, onClose, onSave, onArchive }: {
  agent?: Agent
  token?: string
  onClose: () => void
  onSave: (draft: AgentDraft) => Promise<void>
  onArchive?: () => Promise<void>
}) {
  const [draft, setDraft] = useState<AgentDraft>(() => ({
    name: agent?.name || '',
    instructions: agent?.instructions || '',
    workspace_path: agent?.workspace_path || '',
    github_repo: agent?.github_repo || '',
    workspace_source: agent?.workspace_source || (agent?.github_repo ? 'github' : agent?.workspace_path ? 'local' : ''),
    color: agent?.color || AGENT_COLORS[Math.floor(Math.random() * AGENT_COLORS.length)],
  }))
  const [bindMode, setBindMode] = useState<'github' | 'local'>(() =>
    agent?.workspace_source === 'local' && !agent?.github_repo ? 'local' : 'github'
  )
  const [repos, setRepos] = useState<GithubRepoItem[]>([])
  const [localFolders, setLocalFolders] = useState<LocalWorkspaceItem[]>([])
  const [repoSearch, setRepoSearch] = useState('')
  const [loadingRepos, setLoadingRepos] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const nameRef = useRef<HTMLInputElement>(null)

  useEffect(() => { nameRef.current?.focus() }, [])
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  useEffect(() => {
    if (!token) return
    setLoadingRepos(true)
    fetch('/api/github/repos?limit=100', { headers: { Authorization: `Bearer ${token}` } })
      .then(r => r.ok ? r.json() : { repos: [] })
      .then(data => { if (Array.isArray(data.repos)) setRepos(data.repos) })
      .catch(() => {})
      .finally(() => setLoadingRepos(false))

    fetch('/api/workspaces/local', { headers: { Authorization: `Bearer ${token}` } })
      .then(r => r.ok ? r.json() : { workspaces: [] })
      .then(data => { if (Array.isArray(data.workspaces)) setLocalFolders(data.workspaces) })
      .catch(() => {})
  }, [token])

  const filteredRepos = useMemo(() => {
    const q = repoSearch.trim().toLowerCase()
    if (!q) return repos
    return repos.filter(r => r.full_name.toLowerCase().includes(q) || (r.description || '').toLowerCase().includes(q))
  }, [repos, repoSearch])

  const run = async (action: () => Promise<void>) => {
    setBusy(true); setError('')
    try { await action(); onClose() }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not save the agent') }
    finally { setBusy(false) }
  }
  const submit = (event: FormEvent) => { event.preventDefault(); void run(() => onSave({ ...draft, name: draft.name.trim() })) }

  return (
    <div className="settings-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
      <section className="settings-sheet agent-dialog" role="dialog" aria-modal="true" aria-label={agent ? `Edit ${agent.name}` : 'New agent'}>
        <header className="agent-dialog-header">
          <AgentAvatar agent={{ name: draft.name || '?', color: draft.color }} size={52} />
          <div><p className="eyebrow">{agent ? 'EDIT AGENT' : 'NEW AGENT'}</p><h2>{draft.name.trim() || 'Untitled agent'}</h2></div>
          <button type="button" className="close-button" onClick={onClose} aria-label="Close">×</button>
        </header>
        <form onSubmit={submit}>
          <label>Name
            <input ref={nameRef} value={draft.name} maxLength={60} required onChange={event => setDraft({ ...draft, name: event.target.value })} placeholder="Astra" />
          </label>
          <label>What is this agent for?
            <textarea value={draft.instructions} maxLength={4000} rows={4} onChange={event => setDraft({ ...draft, instructions: event.target.value })}
              placeholder="Its role and standing instructions. It sees these in every reply and every task it runs in this conversation." />
          </label>

          <div className="agent-workspace-section">
            <div className="agent-workspace-header">
              <span>绑定工作空间 / GitHub 仓库 <small>optional</small></span>
              <div className="workspace-picker-tabs compact">
                <button
                  type="button"
                  className={`workspace-tab-btn ${bindMode === 'github' ? 'active' : ''}`}
                  onClick={() => setBindMode('github')}
                >
                  🐙 GitHub 仓库
                </button>
                <button
                  type="button"
                  className={`workspace-tab-btn ${bindMode === 'local' ? 'active' : ''}`}
                  onClick={() => setBindMode('local')}
                >
                  📁 本地文件夹
                </button>
              </div>
            </div>

            {bindMode === 'github' ? (
              <div className="agent-gh-picker">
                <input
                  type="search"
                  value={repoSearch}
                  onChange={e => setRepoSearch(e.target.value)}
                  placeholder="搜索已连接 gh 的 GitHub 仓库 (owner/repo)…"
                />
                {draft.github_repo && (
                  <div className="agent-bound-preview">
                    <span>已选仓库: <strong>🐙 {draft.github_repo}</strong> {draft.workspace_path ? `(${draft.workspace_path})` : '(保存时自动 Clone)'}</span>
                    <button
                      type="button"
                      className="workspace-action-link muted"
                      onClick={() => setDraft({ ...draft, github_repo: '', workspace_path: '', workspace_source: '' })}
                    >
                      清除
                    </button>
                  </div>
                )}
                <div className="workspace-repo-list compact">
                  {loadingRepos ? (
                    <div className="workspace-empty-hint">正在从 gh CLI 获取仓库列表…</div>
                  ) : filteredRepos.length === 0 ? (
                    <div className="workspace-empty-hint">未找到匹配的 GitHub 仓库</div>
                  ) : (
                    filteredRepos.map(repo => {
                      const selected = (draft.github_repo || '').toLowerCase() === repo.full_name.toLowerCase()
                      return (
                        <button
                          key={repo.full_name}
                          type="button"
                          className={`workspace-repo-item ${selected ? 'selected' : ''}`}
                          onClick={() => {
                            setDraft({
                              ...draft,
                              name: draft.name || repo.name,
                              github_repo: repo.full_name,
                              workspace_path: repo.local_path || '',
                              workspace_source: 'github',
                            })
                          }}
                        >
                          <div className="workspace-repo-main">
                            <div className="workspace-repo-title-row">
                              <strong className="workspace-repo-name">🐙 {repo.full_name}</strong>
                              <span className={`workspace-visibility-badge ${repo.private ? 'private' : 'public'}`}>
                                {repo.private ? 'Private' : 'Public'}
                              </span>
                            </div>
                            {repo.local_path && <small className="workspace-repo-path">📁 {repo.local_path}</small>}
                          </div>
                          <div className="workspace-repo-right">
                            <span className={`workspace-clone-badge ${selected ? 'active' : repo.cloned ? 'local' : 'clone'}`}>
                              {selected ? '✓ 已选' : repo.cloned ? '本地已就绪' : '自动 Clone'}
                            </span>
                          </div>
                        </button>
                      )
                    })
                  )}
                </div>
              </div>
            ) : (
              <div className="agent-gh-picker">
                <input
                  value={draft.workspace_path}
                  maxLength={512}
                  onChange={event => setDraft({ ...draft, workspace_path: event.target.value, workspace_source: event.target.value ? 'local' : '' })}
                  placeholder="/Users/yourname/Documents/GitHub/my-project"
                  spellCheck={false}
                  autoCapitalize="off"
                />
                {localFolders.length > 0 && (
                  <div className="workspace-repo-list compact">
                    {localFolders.map(folder => {
                      const selected = draft.workspace_path === folder.path
                      return (
                        <button
                          key={folder.path}
                          type="button"
                          className={`workspace-repo-item ${selected ? 'selected' : ''}`}
                          onClick={() =>
                            setDraft({
                              ...draft,
                              name: draft.name || folder.name,
                              workspace_path: folder.path,
                              github_repo: folder.github_repo || '',
                              workspace_source: 'local',
                            })
                          }
                        >
                          <div className="workspace-repo-main">
                            <div className="workspace-repo-title-row">
                              <strong className="workspace-repo-name">📁 {folder.name}</strong>
                              {folder.github_repo && <span className="workspace-branch-badge">🐙 {folder.github_repo}</span>}
                            </div>
                            <small className="workspace-repo-path">{folder.path}</small>
                          </div>
                          <div className="workspace-repo-right">
                            <span className={`workspace-clone-badge ${selected ? 'active' : 'local'}`}>
                              {selected ? '✓ 已选' : '选择'}
                            </span>
                          </div>
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="agent-colors" role="radiogroup" aria-label="Color">
            {AGENT_COLORS.map(color => (
              <button key={color} type="button" role="radio" aria-checked={draft.color === color} aria-label={color}
                className={draft.color === color ? 'selected' : ''} style={{ background: color }} onClick={() => setDraft({ ...draft, color })} />
            ))}
          </div>
          {error && <div className="error-banner" role="alert">{error}</div>}
          <footer className="agent-dialog-actions">
            {agent && !agent.is_default && onArchive && (
              <button type="button" className="danger" disabled={busy} onClick={() => {
                if (window.confirm(`Remove ${agent.name}? Its conversation is kept but hidden.`)) void run(onArchive)
              }}>Remove</button>
            )}
            <span />
            <button type="button" disabled={busy} onClick={onClose}>Cancel</button>
            <button type="submit" className="primary" disabled={busy || !draft.name.trim()}>{busy ? 'Saving…' : agent ? 'Save' : 'Create agent'}</button>
          </footer>
        </form>
      </section>
    </div>
  )
}
