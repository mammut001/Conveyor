import { useCallback, useEffect, useRef, useState } from 'react';

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
        throw new Error(errBody.error || `Failed to load servers (${res.status})`);
      }
      const data = (await res.json()) as ServersResponse;
      if (gen !== fetchGen.current) return;
      setServers(data.items || []);
      setConfigPath(data.config_path || '');
      setError('');
    } catch (err) {
      if (gen !== fetchGen.current) return;
      setError(err instanceof Error ? err.message : 'Failed to load MCP servers');
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
            [serverName]: body.error || 'Failed to connect to MCP server',
          }));
        }
      } else {
        setActionError((prev) => ({
          ...prev,
          [serverName]: body.error || `Refresh failed (${res.status})`,
        }));
      }
    } catch (err) {
      setActionError((prev) => ({
        ...prev,
        [serverName]: err instanceof Error ? err.message : 'Network error during refresh',
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
          [serverName]: body.error || `Toggle failed (${res.status})`,
        }));
        return;
      }
      setServers((prev) =>
        prev.map((s) => (s.name === serverName ? (body as ServerItem) : s))
      );
    } catch (err) {
      setActionError((prev) => ({
        ...prev,
        [serverName]: err instanceof Error ? err.message : 'Network error during toggle',
      }));
    } finally {
      setToggling((prev) => ({ ...prev, [serverName]: false }));
    }
  };

  if (disabled) {
    return (
      <div className="connectors-panel">
        <div className="connectors-disabled-banner">
          MCP connectors are disabled. Set <code>CONVEYOR_MCP_ENABLED=true</code> in your environment to enable them.
        </div>
      </div>
    );
  }

  return (
    <div className="connectors-panel">
      {configPath && (
        <div className="connectors-config-hint">
          <strong>Configuration file:</strong> <code>{configPath}</code>
          <p className="connectors-config-note">Edit the JSON file to add or configure MCP servers (up to 20 servers).</p>
        </div>
      )}

      {error && <div className="error-banner">{error}</div>}

      {loading && servers.length === 0 ? (
        <div className="connectors-loading">Loading MCP servers…</div>
      ) : servers.length === 0 ? (
        <div className="connectors-empty">
          <p>No MCP servers configured yet.</p>
          <p className="connectors-secondary">
            Add server entries to <code>{configPath || 'mcp_servers.json'}</code> to expose tools to the chat tier.
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
                        {server.status}
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
                      {toggling[server.name] ? 'Saving…' : server.enabled ? 'Enabled' : 'Disabled'}
                    </button>
                    <button
                      type="button"
                      className="connectors-refresh-btn"
                      disabled={refreshing[server.name]}
                      onClick={() => void handleRefresh(server.name)}
                    >
                      {refreshing[server.name] ? 'Refreshing…' : 'Refresh'}
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
                    <h4>Discovered Tools ({server.tool_count})</h4>
                    {server.checked_at && (
                      <span className="connectors-checked-at">
                        Checked {new Date(server.checked_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                      </span>
                    )}
                  </div>

                  {server.tools.length === 0 ? (
                    <div className="connectors-tools-empty">
                      {server.status === 'unknown'
                        ? 'Not checked yet. Click "Refresh" to discover tools.'
                        : 'No tools discovered for this server.'}
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
                              {tool.read_only ? 'Read' : 'Approval required'}
                            </span>
                            <span
                              className={`connectors-pill ${
                                tool.exposed ? 'pill-exposed' : 'pill-blocked'
                              }`}
                            >
                              {tool.exposed ? 'Allowlisted' : 'Not exposed'}
                            </span>
                          </div>
                          <div className="connectors-tool-desc" title={tool.description}>
                            {tool.description || <span className="connectors-muted">No description</span>}
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
