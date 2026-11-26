import React, { useCallback, useEffect, useMemo, useState } from 'react';

export type WorkspaceBinding = {
  session_id?: string;
  workspace_path: string;
  workspace_status?: '' | 'ok' | 'missing' | 'not_git';
  github_repo: string;
  workspace_source: '' | 'local' | 'github' | 'agent' | string;
};

export type GithubRepoItem = {
  full_name: string;
  name: string;
  description: string;
  private: boolean;
  updated_at: string;
  default_branch: string;
  url: string;
  local_path: string;
  cloned: boolean;
};

export type LocalWorkspaceItem = {
  path: string;
  name: string;
  github_repo: string;
  workspace_status: string;
};

export type WorkspaceBindingPickerProps = {
  token: string;
  sessionId: string;
  chatMode?: boolean;
  binding: WorkspaceBinding;
  welcomeMode?: boolean;
  onBound: (next: WorkspaceBinding & { session_id: string }) => void;
};

export function WorkspaceBindingPicker({
  token,
  sessionId,
  chatMode = false,
  binding,
  welcomeMode = false,
  onBound,
}: WorkspaceBindingPickerProps) {
  const isBound = Boolean(binding.workspace_path || binding.github_repo);
  const [open, setOpen] = useState<boolean>(welcomeMode && !isBound);
  const [tab, setTab] = useState<'github' | 'local'>(() =>
    binding.workspace_source === 'local' && !binding.github_repo ? 'local' : 'github'
  );
  const [repos, setRepos] = useState<GithubRepoItem[]>([]);
  const [ghAuth, setGhAuth] = useState<{ installed?: boolean; authenticated?: boolean; account?: string; source?: string; detail?: string }>({});
  const [localFolders, setLocalFolders] = useState<LocalWorkspaceItem[]>([]);
  const [defaultWorkspace, setDefaultWorkspace] = useState<string>('');
  const [loadingRepos, setLoadingRepos] = useState(false);
  const [loadingLocal, setLoadingLocal] = useState(false);
  const [bindingBusy, setBindingBusy] = useState<string>('');
  const [error, setError] = useState('');
  const [repoSearch, setRepoSearch] = useState('');
  const [customPath, setCustomPath] = useState(() => binding.workspace_path || '');

  useEffect(() => {
    if (welcomeMode && !isBound) {
      setOpen(true);
    }
  }, [welcomeMode, isBound]);

  useEffect(() => {
    if (binding.workspace_path) {
      setCustomPath(binding.workspace_path);
    }
  }, [binding.workspace_path]);

  const fetchGithubRepos = useCallback(async (refresh = false) => {
    if (!token) return;
    setLoadingRepos(true);
    setError('');
    try {
      const url = `/api/github/repos?limit=100${refresh ? '&refresh=1' : ''}`;
      const res = await fetch(url, {
        headers: { Authorization: `Bearer ${token}` },
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error || `Failed to load GitHub repositories (${res.status})`);
      }
      setRepos(Array.isArray(data.repos) ? data.repos : []);
      setGhAuth(data.auth || {});
    } catch (err) {
      setError(err instanceof Error ? err.message : '無法加载 GitHub 仓库列表');
    } finally {
      setLoadingRepos(false);
    }
  }, [token]);

  const fetchLocalWorkspaces = useCallback(async () => {
    if (!token) return;
    setLoadingLocal(true);
    try {
      const res = await fetch('/api/workspaces/local', {
        headers: { Authorization: `Bearer ${token}` },
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        setLocalFolders(Array.isArray(data.workspaces) ? data.workspaces : []);
        setDefaultWorkspace(String(data.default_workspace || ''));
      }
    } catch {
      // ignore background discovery error
    } finally {
      setLoadingLocal(false);
    }
  }, [token]);

  useEffect(() => {
    if (!open) return;
    void fetchGithubRepos(false);
    void fetchLocalWorkspaces();
  }, [open, fetchGithubRepos, fetchLocalWorkspaces]);

  const filteredRepos = useMemo(() => {
    const q = repoSearch.trim().toLowerCase();
    if (!q) return repos;
    return repos.filter(
      r =>
        r.full_name.toLowerCase().includes(q) ||
        (r.description || '').toLowerCase().includes(q) ||
        (r.local_path || '').toLowerCase().includes(q)
    );
  }, [repos, repoSearch]);

  const applyBinding = async (payload: {
    source: 'github' | 'local' | 'clear';
    github_repo?: string;
    workspace_path?: string;
    busyLabel: string;
  }) => {
    setBindingBusy(payload.busyLabel);
    setError('');
    try {
      const res = await fetch('/api/sessions/bind', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          session_id: sessionId || undefined,
          chat_mode: chatMode,
          source: payload.source,
          github_repo: payload.github_repo || '',
          workspace_path: payload.workspace_path || '',
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error || `绑定失败 (${res.status})`);
      }
      onBound({
        session_id: String(data.session_id || sessionId || ''),
        workspace_path: String(data.workspace_path || ''),
        workspace_status: (data.workspace_status || '') as WorkspaceBinding['workspace_status'],
        github_repo: String(data.github_repo || ''),
        workspace_source: String(data.workspace_source || ''),
      });
      if (payload.source !== 'clear') {
        setOpen(false);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '绑定工作区失败');
    } finally {
      setBindingBusy('');
    }
  };

  if (!open && isBound) {
    return (
      <div className="workspace-bound-bar" role="region" aria-label="当前会话绑定的工作区">
        <div className="workspace-bound-info">
          {binding.github_repo && (
            <span className="workspace-pill github" title={`GitHub 仓库: ${binding.github_repo}`}>
              🐙 <strong>{binding.github_repo}</strong>
            </span>
          )}
          {binding.workspace_path && (
            <span className="workspace-pill folder" title={`本地工作目录: ${binding.workspace_path}`}>
              📁 <code>{binding.workspace_path}</code>
            </span>
          )}
          {binding.workspace_status === 'missing' && (
            <span className="workspace-status-chip error">目录不存在</span>
          )}
          {binding.workspace_status === 'not_git' && (
            <span className="workspace-status-chip warn">非 Git 仓库</span>
          )}
          {binding.workspace_status === 'ok' && (
            <span className="workspace-status-chip ok">✓ Git 就绪</span>
          )}
        </div>
        <div className="workspace-bound-actions">
          <button
            type="button"
            className="workspace-action-link"
            onClick={() => setOpen(true)}
            disabled={Boolean(bindingBusy)}
          >
            切换绑定
          </button>
          <button
            type="button"
            className="workspace-action-link muted"
            onClick={() =>
              void applyBinding({
                source: 'clear',
                busyLabel: '解除绑定中…',
              })
            }
            disabled={Boolean(bindingBusy)}
          >
            解除
          </button>
        </div>
      </div>
    );
  }

  if (!open && !isBound) {
    return (
      <div className="workspace-unbound-bar">
        <button
          type="button"
          className="workspace-bind-trigger"
          onClick={() => setOpen(true)}
        >
          <span>🐙 / 📁</span>
          <span>绑定 GitHub 仓库或本地工作文件夹</span>
        </button>
      </div>
    );
  }

  return (
    <section className={`workspace-picker-card ${welcomeMode ? 'welcome' : 'inline'}`} aria-label="绑定工作文件夹或 GitHub 仓库">
      <header className="workspace-picker-header">
        <div>
          <p className="eyebrow">WORKSPACE & GITHUB BINDING</p>
          <h3>为当前对话绑定工作目录或 GitHub 仓库</h3>
          <p className="workspace-picker-subtitle">
            绑定后，对话、代码检索、GitHub 工具与任务执行都会直接围绕该仓库 / 文件夹工作。
          </p>
        </div>
        {(!welcomeMode || isBound) && (
          <button
            type="button"
            className="workspace-close-btn"
            onClick={() => setOpen(false)}
            aria-label="收起绑定面板"
          >
            ×
          </button>
        )}
      </header>

      <div className="workspace-picker-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'github'}
          className={`workspace-tab-btn ${tab === 'github' ? 'active' : ''}`}
          onClick={() => setTab('github')}
        >
          🐙 GitHub 仓库 (gh connect)
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'local'}
          className={`workspace-tab-btn ${tab === 'local' ? 'active' : ''}`}
          onClick={() => setTab('local')}
        >
          📁 本地工作文件夹
        </button>
      </div>

      {error && (
        <div className="workspace-picker-error" role="alert">
          <span>⚠️ {error}</span>
          <button type="button" onClick={() => setError('')}>×</button>
        </div>
      )}

      {bindingBusy && (
        <div className="workspace-picker-busy" role="status">
          <span className="live-pulse running" />
          <span>{bindingBusy}</span>
        </div>
      )}

      {tab === 'github' ? (
        <div className="workspace-tab-panel">
          <div className="workspace-gh-toolbar">
            <div className="workspace-gh-status">
              {ghAuth.authenticated ? (
                <>
                  <span className="gh-status-dot online" />
                  <span>
                    已连接 GitHub{ghAuth.account ? <> (<strong>@{ghAuth.account}</strong>)</> : ''} · 共 {repos.length} 个仓库
                  </span>
                </>
              ) : loadingRepos ? (
                <span>正在读取 gh 连接状态…</span>
              ) : (
                <>
                  <span className="gh-status-dot offline" />
                  <span>未检测到 gh 登录状态（请在终端运行 <code>gh auth login</code>）</span>
                </>
              )}
            </div>
            <div className="workspace-gh-controls">
              <input
                type="search"
                className="workspace-search-input"
                value={repoSearch}
                onChange={e => setRepoSearch(e.target.value)}
                placeholder="搜索仓库名称或描述…"
                aria-label="搜索 GitHub 仓库"
              />
              <button
                type="button"
                className="workspace-refresh-btn"
                onClick={() => void fetchGithubRepos(true)}
                disabled={loadingRepos || Boolean(bindingBusy)}
              >
                {loadingRepos ? '加载中…' : '↻ 刷新'}
              </button>
            </div>
          </div>

          <div className="workspace-repo-list" role="list">
            {loadingRepos && repos.length === 0 ? (
              <div className="workspace-empty-hint">正在从 gh CLI 获取你的 GitHub 仓库列表…</div>
            ) : filteredRepos.length === 0 ? (
              <div className="workspace-empty-hint">
                {repoSearch ? '没有匹配的 GitHub 仓库' : '暂无可用的 GitHub 仓库'}
              </div>
            ) : (
              filteredRepos.map(repo => {
                const isCurrent = binding.github_repo.toLowerCase() === repo.full_name.toLowerCase();
                return (
                  <button
                    key={repo.full_name}
                    type="button"
                    role="listitem"
                    className={`workspace-repo-item ${isCurrent ? 'selected' : ''}`}
                    disabled={Boolean(bindingBusy)}
                    onClick={() =>
                      void applyBinding({
                        source: 'github',
                        github_repo: repo.full_name,
                        workspace_path: repo.local_path || undefined,
                        busyLabel: repo.cloned
                          ? `正在绑定 ${repo.full_name}…`
                          : `正在从 GitHub Clone 并绑定 ${repo.full_name}…`,
                      })
                    }
                  >
                    <div className="workspace-repo-main">
                      <div className="workspace-repo-title-row">
                        <strong className="workspace-repo-name">🐙 {repo.full_name}</strong>
                        <span className={`workspace-visibility-badge ${repo.private ? 'private' : 'public'}`}>
                          {repo.private ? 'Private' : 'Public'}
                        </span>
                        {repo.default_branch && (
                          <span className="workspace-branch-badge">⎇ {repo.default_branch}</span>
                        )}
                      </div>
                      {repo.description && (
                        <p className="workspace-repo-desc">{repo.description}</p>
                      )}
                      {repo.local_path && (
                        <small className="workspace-repo-path">📁 {repo.local_path}</small>
                      )}
                    </div>
                    <div className="workspace-repo-right">
                      {isCurrent ? (
                        <span className="workspace-clone-badge active">✓ 当前已绑定</span>
                      ) : repo.cloned ? (
                        <span className="workspace-clone-badge local">✓ 本地已就绪</span>
                      ) : (
                        <span className="workspace-clone-badge clone">☁️ 选择并自动 Clone</span>
                      )}
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>
      ) : (
        <div className="workspace-tab-panel">
          <div className="workspace-local-input-row">
            <input
              type="text"
              className="workspace-path-input"
              value={customPath}
              onChange={e => setCustomPath(e.target.value)}
              placeholder={defaultWorkspace || '/Users/yourname/Documents/GitHub/my-project'}
              spellCheck={false}
              autoCapitalize="off"
              aria-label="本地工作文件夹绝对路径"
            />
            <button
              type="button"
              className="primary workspace-bind-btn"
              disabled={!customPath.trim() || Boolean(bindingBusy)}
              onClick={() =>
                void applyBinding({
                  source: 'local',
                  workspace_path: customPath.trim(),
                  busyLabel: `正在绑定本地目录 ${customPath.trim()}…`,
                })
              }
            >
              绑定文件夹
            </button>
          </div>

          <div className="workspace-local-suggestions">
            <p className="eyebrow">本机已发现的 Git 工作目录 ({localFolders.length})</p>
            {loadingLocal && localFolders.length === 0 ? (
              <div className="workspace-empty-hint">正在扫描本机工作目录…</div>
            ) : localFolders.length === 0 ? (
              <div className="workspace-empty-hint">可在上方直接粘贴任意本机文件夹绝对路径进行绑定。</div>
            ) : (
              <div className="workspace-repo-list">
                {localFolders.map(folder => {
                  const isCurrent = binding.workspace_path === folder.path;
                  return (
                    <button
                      key={folder.path}
                      type="button"
                      className={`workspace-repo-item ${isCurrent ? 'selected' : ''}`}
                      disabled={Boolean(bindingBusy)}
                      onClick={() => {
                        setCustomPath(folder.path);
                        void applyBinding({
                          source: 'local',
                          workspace_path: folder.path,
                          busyLabel: `正在绑定本地目录 ${folder.name}…`,
                        });
                      }}
                    >
                      <div className="workspace-repo-main">
                        <div className="workspace-repo-title-row">
                          <strong className="workspace-repo-name">📁 {folder.name}</strong>
                          {folder.github_repo && (
                            <span className="workspace-branch-badge">🐙 {folder.github_repo}</span>
                          )}
                        </div>
                        <small className="workspace-repo-path">{folder.path}</small>
                      </div>
                      <div className="workspace-repo-right">
                        {isCurrent ? (
                          <span className="workspace-clone-badge active">✓ 当前已绑定</span>
                        ) : (
                          <span className="workspace-clone-badge local">点击绑定</span>
                        )}
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
