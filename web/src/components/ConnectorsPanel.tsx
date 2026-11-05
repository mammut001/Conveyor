import { useCallback, useEffect, useRef, useState } from 'react';
import { formatWhen, friendlyError } from '../ux/knowledgeKit';

const STATUS_LABEL: Record<string, string> = {
  ok: '正常', error: '出错', config_error: '配置有误', disabled: '已停用', unknown: '未检查',
};

export type ToolItem = {
  name: string;
  exposed: boolean;
  read_only: boolean;
  description: string;
};

export type ServerItem = {
  name: string;
  transport: 'stdio' | 'http';
  target: string;
  enabled: boolean;
  status: 'ok' | 'error' | 'config_error' | 'disabled' | 'unknown';
  error: string | null;
  tool_count: number;
  tools: ToolItem[];
  checked_at: string | null;
};

type ServersResponse = {
  items: ServerItem[];
  config_path: string;
  count: number;
};

export type ConnectorsPanelProps = {
  token: string;
};

export function ConnectorsPanel({ token }: ConnectorsPanelProps) {
  const [disabled, setDisabled] = useState(false);
  const disabledRef = useRef(false);
  const [servers, setServers] = useState<ServerItem[]>([]);
  const [configPath, setConfigPath] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState<Record<string, string>>({});
  const [refreshing, setRefreshing] = useState<Record<string, boolean>>({});
  const [toggling, setToggling] = useState<Record<string, boolean>>({});
  const fetchGen = useRef(0);

  const load = useCallback(async () => {
    if (disabledRef.current || !token) return;
    const gen = ++fetchGen.current;
    try {
      setLoading(true);
      const res = await fetch('/api/mcp/servers', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 409) {
        disabledRef.current = true;
        setDisabled(true);
        setLoading(false);
        return;
      }
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(errBody.error || `HTTP ${res.status}`);
      }
      const data = (await res.json()) as ServersResponse;
      if (gen !== fetchGen.current) return;
      setServers(data.items || []);
      setConfigPath(data.config_path || '');
      setError('');
    } catch (err) {
      if (gen !== fetchGen.current) return;
      setError(friendlyError(err, '没能读取 MCP 连接器，请稍后重试').message);
    } finally {
      if (gen === fetchGen.current) {
        setLoading(false);
      }
    }
  }, [token]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleRefresh = async (serverName: string) => {
    setRefreshing((prev) => ({ ...prev, [serverName]: true }));
    setActionError((prev) => ({ ...prev, [serverName]: '' }));
    try {
      const res = await fetch(`/api/mcp/servers/${encodeURIComponent(serverName)}/refresh`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({}),
      });

      const body = await res.json().catch(() => ({}));
      if (res.status === 200 || res.status === 502) {
        // Both 200 and 502 return the updated ServerItem
        if (body && body.name) {
          setServers((prev) =>
            prev.map((s) => (s.name === serverName ? (body as ServerItem) : s))
          );
        }
        if (res.status === 502) {
          setActionError((prev) => ({
            ...prev,
            [serverName]: body.error ? `连不上这个 MCP 服务：${body.error}` : '连不上这个 MCP 服务',
          }));
        }
      } else {
        setActionError((prev) => ({
          ...prev,
          [serverName]: body.error ? `刷新失败：${body.error}` : `刷新失败（HTTP ${res.status}）`,
        }));
      }
    } catch (err) {
      setActionError((prev) => ({
        ...prev,
        [serverName]: friendlyError(err, '刷新时网络出错，请重试').message,
      }));
    } finally {
      setRefreshing((prev) => ({ ...prev, [serverName]: false }));
    }
  };

  const handleToggle = async (serverName: string, currentEnabled: boolean) => {
    setToggling((prev) => ({ ...prev, [serverName]: true }));
    setActionError((prev) => ({ ...prev, [serverName]: '' }));
    try {
      const res = await fetch(`/api/mcp/servers/${encodeURIComponent(serverName)}`, {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ enabled: !currentEnabled }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setActionError((prev) => ({
          ...prev,
          [serverName]: body.error ? `切换失败：${body.error}` : `切换失败（HTTP ${res.status}）`,
        }));
        return;
      }
      setServers((prev) =>
        prev.map((s) => (s.name === serverName ? (body as ServerItem) : s))
      );
    } catch (err) {
      setActionError((prev) => ({
        ...prev,
        [serverName]: friendlyError(err, '切换时网络出错，请重试').message,
      }));
    } finally {
      setToggling((prev) => ({ ...prev, [serverName]: false }));
    }
  };

  if (disabled) {
    return (
      <div className="connectors-panel">
        <div className="connectors-disabled-banner">
          MCP 连接器未开启。
        </div>
      </div>
    );
  }

  return (
    <div className="connectors-panel">
      {configPath && (
        <div className="connectors-config-hint">
          <strong>配置文件：</strong> <code>{configPath}</code>
          <p className="connectors-config-note">在这个 JSON 文件里添加或修改 MCP 服务（最多 20 个）。</p>
        </div>
      )}

      {error && <div className="error-banner">{error}</div>}

      {loading && servers.length === 0 ? (
        <div className="connectors-loading">加载中…</div>
      ) : servers.length === 0 ? (
        <div className="connectors-empty">
          <p>还没有配置 MCP 服务。</p>
          <p className="connectors-secondary">
            在 <code>{configPath || 'mcp_servers.json'}</code> 里添加服务后，对话就能使用它们的工具。
          </p>
        </div>
      ) : (
        <div className="connectors-list">
          {servers.map((server) => {
            const hasError = Boolean(actionError[server.name] || server.error);
            const errText = actionError[server.name] || server.error;

            return (
              <div key={server.name} className="connectors-card">
                <div className="connectors-card-header">
                  <div className="connectors-server-info">
                    <div className="connectors-title-row">
                      <h3 className="connectors-server-name">{server.name}</h3>
                      <span className="connectors-transport-badge">{server.transport}</span>
                      <span className={`connectors-status-badge status-${server.status}`}>
                        {STATUS_LABEL[server.status] || server.status}
                      </span>
                    </div>
                    <div className="connectors-target-row" title={server.target}>
                      <code>{server.target}</code>
                    </div>
                  </div>

                  <div className="connectors-card-actions">
                    <button
                      type="button"
                      className={`connectors-toggle-btn ${server.enabled ? 'active' : ''}`}
                      disabled={toggling[server.name]}
                      onClick={() => void handleToggle(server.name, server.enabled)}
                    >
                      {toggling[server.name] ? '保存中…' : server.enabled ? '已启用' : '已停用'}
                    </button>
                    <button
                      type="button"
                      className="connectors-refresh-btn"
                      disabled={refreshing[server.name]}
                      onClick={() => void handleRefresh(server.name)}
                    >
                      {refreshing[server.name] ? '刷新中…' : '刷新'}
                    </button>
                  </div>
                </div>

                {hasError && (
                  <div className="connectors-error-banner">
                    {errText}
                  </div>
                )}

                <div className="connectors-tools-section">
                  <div className="connectors-tools-header">
                    <h4>发现的工具（{server.tool_count}）</h4>
                    {server.checked_at && (
                      <span className="connectors-checked-at">
                        {formatWhen(server.checked_at)} 检查过
                      </span>
                    )}
                  </div>

                  {server.tools.length === 0 ? (
                    <div className="connectors-tools-empty">
                      {server.status === 'unknown'
                        ? '还没检查过。点“刷新”查找它提供的工具。'
                        : '这个服务没有提供工具。'}
                    </div>
                  ) : (
                    <div className="connectors-tools-table">
                      {server.tools.map((tool) => (
                        <div key={tool.name} className="connectors-tool-row">
                          <div className="connectors-tool-left">
                            <span className="connectors-tool-name">
                              <code>{tool.name}</code>
                            </span>
                            <span
                              className={`connectors-pill ${
                                tool.read_only ? 'pill-read' : 'pill-write'
                              }`}
                            >
                              {tool.read_only ? '只读' : '需要批准'}
                            </span>
                            <span
                              className={`connectors-pill ${
                                tool.exposed ? 'pill-exposed' : 'pill-blocked'
                              }`}
                            >
                              {tool.exposed ? '已允许' : '未开放'}
                            </span>
                          </div>
                          <div className="connectors-tool-desc" title={tool.description}>
                            {tool.description || <span className="connectors-muted">没有说明</span>}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
