import React, { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { FormattedText } from './FormattedText';

export type SubagentProgress = {
  call_id: string;
  index: number;
  title: string;
  status: 'queued' | 'running' | 'ok' | 'timeout' | 'error';
  elapsed: number;
  tools: string[];
};

export type ChatMessage = {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  created_at: string;
  subagents?: SubagentProgress[];
  pendingApproval?: {
    id: string;
    tool_name: string;
    arg: string;
    summary: string;
    text: string;
    expires_in_seconds?: number;
  };
  // A decided/expired approval prompt restored from history: rendered as a
  // compact resolved record instead of a live-looking confirmation prompt.
  resolvedApproval?: {
    tool_name: string;
    arg: string;
    status: 'approved' | 'denied' | 'expired' | string;
  };
};

type HistoryApproval = { id: string; tool_name: string; arg: string; status: string; expires_in_seconds?: number };
type HistoryMessage = { role: 'user' | 'assistant'; text: string; created_at?: string; kind?: string; approval?: HistoryApproval };

export type ChatPanelProps = {
  token: string;
  onApprovalDecided?: () => void;
  onSessionChange?: (sessionId: string) => void;
  initialInput?: string;
  onInitialInputConsumed?: () => void;
};

function SubagentCardGroup({ tasks, live }: { tasks: SubagentProgress[]; live?: boolean }) {
  if (!tasks || tasks.length === 0) return null;

  const okCount = tasks.filter(t => t.status === 'ok').length;
  const timeoutCount = tasks.filter(t => t.status === 'timeout').length;
  const errorCount = tasks.filter(t => t.status === 'error').length;
  const runningCount = tasks.filter(t => t.status === 'running' || t.status === 'queued').length;

  const summaryParts: string[] = [];
  summaryParts.push(`${tasks.length} 个子任务`);
  if (okCount > 0) summaryParts.push(`${okCount} ✓`);
  if (timeoutCount > 0) summaryParts.push(`${timeoutCount} timeout`);
  if (errorCount > 0) summaryParts.push(`${errorCount} error`);
  if (runningCount > 0) summaryParts.push(`${runningCount} 运行中`);
  const summaryText = summaryParts.join(' · ');

  const content = (
    <div className="subagent-card-group">
      {live && (
        <div className="subagent-group-header">
          <span className="subagent-spinner">🧩</span>
          <span>子任务执行中 ({tasks.length - runningCount}/{tasks.length})</span>
        </div>
      )}
      {tasks.map(task => {
        let chip = null;
        if (task.status === 'queued') {
          chip = <span className="subagent-chip chip-running">… 排队中</span>;
        } else if (task.status === 'running') {
          chip = <span className="subagent-chip chip-running"><span className="subagent-spinner">⟳</span> 运行中</span>;
        } else if (task.status === 'ok') {
          chip = <span className="subagent-chip chip-ok">✓ 完成</span>;
        } else if (task.status === 'timeout') {
          chip = <span className="subagent-chip chip-timeout">⌛ 超时</span>;
        } else {
          chip = <span className="subagent-chip chip-error">⚠ 错误</span>;
        }

        return (
          <div key={`${task.call_id}-${task.index}`} className="subagent-row">
            <div className="subagent-row-main">
              {chip}
              <span className="subagent-title" title={task.title}>{task.title}</span>
            </div>
            <div className="subagent-row-meta">
              <span>{task.elapsed.toFixed(1)}s</span>
              {task.tools && task.tools.length > 0 && (
                <span className="subagent-tools" title={task.tools.join(', ')}>
                  <code>{task.tools.join(', ')}</code>
                </span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );

  if (live) {
    return content;
  }

  return (
    <details className="subagent-summary-details">
      <summary className="subagent-summary-summary">
        <span>🧩</span>
        <span>{summaryText}</span>
      </summary>
      {content}
    </details>
  );
}

export function ChatPanel({ token, onApprovalDecided, onSessionChange, initialInput, onInitialInputConsumed }: ChatPanelProps) {
  const [sessionId, setSessionId] = useState<string>(() => localStorage.getItem('conveyor-chat-session') || '');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [streamingDelta, setStreamingDelta] = useState('');
  const [statusText, setStatusText] = useState('');
  const [subagents, setSubagents] = useState<SubagentProgress[]>([]);
  const subagentsRef = useRef<SubagentProgress[]>([]);
  subagentsRef.current = subagents;
  const [approvalsInProgress, setApprovalsInProgress] = useState<Record<string, boolean>>({});
  const streamRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (initialInput) {
      setInput(initialInput);
      onInitialInputConsumed?.();
    }
  }, [initialInput, onInitialInputConsumed]);

  const loadHistory = useCallback(async (sid: string) => {
    if (!sid) {
      setMessages([]);
      return;
    }
    setError('');
    try {
      const res = await fetch(`/api/chat/history?session_id=${encodeURIComponent(sid)}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 401) {
        setError('Token rejected');
        return;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Failed to load history (${res.status})`);
      }
      const data = await res.json();
      if (Array.isArray(data.messages)) {
        setMessages(
          (data.messages as HistoryMessage[]).map((m, idx) => {
            const base: ChatMessage = {
              id: `hist-${idx}-${m.created_at || Date.now()}`,
              role: m.role,
              text: m.text,
              created_at: m.created_at || new Date().toISOString(),
            };
            const appr = m.approval;
            if (!appr) return base;
            if (appr.status === 'pending' && appr.id) {
              return {
                ...base,
                pendingApproval: {
                  id: appr.id, tool_name: appr.tool_name, arg: appr.arg, summary: '',
                  text: m.text, expires_in_seconds: appr.expires_in_seconds,
                },
              };
            }
            return { ...base, resolvedApproval: { tool_name: appr.tool_name, arg: appr.arg, status: appr.status } };
          })
        );
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load chat history');
    }
  }, [token]);

  // Load history once for the session restored from localStorage. Sessions
  // assigned mid-request (SSE `session` event) must not trigger a reload:
  // that raced the first persisted turn and wiped the in-flight messages.
  const initialSessionId = useRef(sessionId);
  useEffect(() => {
    if (initialSessionId.current) {
      void loadHistory(initialSessionId.current);
    }
  }, [loadHistory]);

  useEffect(() => {
    const node = streamRef.current;
    if (node) {
      node.scrollTo({ top: node.scrollHeight, behavior: 'smooth' });
    }
  }, [messages.length, streamingDelta, statusText, subagents.length]);

  const handleNewChat = () => {
    setSessionId('');
    localStorage.removeItem('conveyor-chat-session');
    setMessages([]);
    setStreamingDelta('');
    setStatusText('');
    setSubagents([]);
    subagentsRef.current = [];
    setError('');
    if (onSessionChange) onSessionChange('');
  };

  const handleApprovalDecision = async (approvalId: string, approve: boolean) => {
    setApprovalsInProgress(prev => ({ ...prev, [approvalId]: true }));
    setError('');
    try {
      const res = await fetch(`/api/approvals/${encodeURIComponent(approvalId)}/${approve ? 'approve' : 'reject'}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({}),
      });
      // 404 = token already decided elsewhere or expired.
      if (!res.ok && res.status !== 404) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Approval action failed (${res.status})`);
      }
      const data = res.ok ? await res.json() : { status: 'expired', result: '' };
      const status = data.status === 'accepted' ? 'approved' : data.status === 'rejected' ? 'denied' : 'expired';
      setMessages(prev => {
        const next: ChatMessage[] = [];
        for (const m of prev) {
          if (m.pendingApproval?.id === approvalId) {
            next.push({
              ...m,
              pendingApproval: undefined,
              resolvedApproval: { tool_name: m.pendingApproval.tool_name, arg: m.pendingApproval.arg, status },
            });
            if (data.result) {
              next.push({ id: `result-${approvalId}`, role: 'assistant', text: data.result, created_at: new Date().toISOString() });
            }
          } else {
            next.push(m);
          }
        }
        return next;
      });
      if (onApprovalDecided) onApprovalDecided();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Approval failed');
    } finally {
      setApprovalsInProgress(prev => ({ ...prev, [approvalId]: false }));
    }
  };

  const handleSend = async (e?: FormEvent) => {
    if (e) e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;

    setInput('');
    setBusy(true);
    setError('');
    setStatusText('');
    setStreamingDelta('');
    setSubagents([]);
    subagentsRef.current = [];

    const userMsg: ChatMessage = {
      id: `user-${Date.now()}`,
      role: 'user',
      text,
      created_at: new Date().toISOString(),
    };
    setMessages(prev => [...prev, userMsg]);

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          message: text,
          session_id: sessionId || undefined,
        }),
      });

      if (res.status === 409) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || 'Chat tier is disabled');
      }
      if (res.status === 401) {
        throw new Error('Token rejected');
      }
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `Request failed (${res.status})`);
      }
      if (!res.body) {
        throw new Error('Response body stream not available');
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split('\n\n');
        buffer = frames.pop() || '';

        for (const frame of frames) {
          const lines = frame.split('\n');
          let eventName = '';
          let dataStr = '';
          for (const line of lines) {
            if (line.startsWith('event: ')) {
              eventName = line.slice(7).trim();
            } else if (line.startsWith('data: ')) {
              dataStr = line.slice(6);
            }
          }
          if (!eventName || !dataStr) continue;

          try {
            const payload = JSON.parse(dataStr);
            if (eventName === 'session') {
              if (payload.session_id) {
                setSessionId(payload.session_id);
                localStorage.setItem('conveyor-chat-session', payload.session_id);
                if (onSessionChange) onSessionChange(payload.session_id);
              }
            } else if (eventName === 'delta') {
              if (payload.text !== undefined) {
                setStreamingDelta(payload.text);
              }
            } else if (eventName === 'status') {
              if (payload.text) {
                setStatusText(payload.text);
              }
            } else if (eventName === 'subagent') {
              setSubagents(prev => {
                const existingIdx = prev.findIndex(item => item.call_id === payload.call_id && item.index === payload.index);
                let updated: SubagentProgress[];
                if (existingIdx >= 0) {
                  updated = [...prev];
                  updated[existingIdx] = { ...updated[existingIdx], ...payload };
                } else {
                  updated = [...prev, payload];
                }
                subagentsRef.current = updated;
                return updated;
              });
            } else if (eventName === 'approval') {
              setStreamingDelta('');
              setStatusText('');
              setMessages(prev => [
                ...prev,
                {
                  id: `appr-${payload.id}`,
                  role: 'assistant',
                  text: payload.text || 'Tool approval required',
                  created_at: new Date().toISOString(),
                  pendingApproval: payload,
                },
              ]);
            } else if (eventName === 'message') {
              setStreamingDelta('');
              setStatusText('');
              const finishedSubagents = subagentsRef.current.length > 0 ? [...subagentsRef.current] : undefined;
              setMessages(prev => [
                ...prev,
                {
                  id: `asst-${Date.now()}`,
                  role: 'assistant',
                  text: payload.text,
                  created_at: new Date().toISOString(),
                  subagents: finishedSubagents,
                },
              ]);
              setSubagents([]);
              subagentsRef.current = [];
            } else if (eventName === 'error') {
              setError(payload.error || 'Chat request failed');
            } else if (eventName === 'done') {
              setStreamingDelta('');
              setStatusText('');
            }
          } catch (parseErr) {
            console.error('Failed to parse SSE JSON', parseErr);
          }
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to send chat message');
    } finally {
      setBusy(false);
      setStreamingDelta('');
      setStatusText('');
      setSubagents([]);
      subagentsRef.current = [];
    }
  };

  return (
    <div className="chat-panel-container" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="event-stream" ref={streamRef} style={{ flex: 1, overflowY: 'auto' }}>
        {messages.length === 0 && !streamingDelta && (
          <div className="welcome-state">
            <div className="brand-mark">C</div>
            <h2>Conveyor Direct Chat</h2>
            <p>Direct chat tier answering in seconds without Codex. READ tools run automatically; WRITE tools require approval.</p>
          </div>
        )}

        {messages.map(message => {
          if (message.role === 'user') {
            return (
              <article key={message.id} className="transcript-message role-user">
                <div className="transcript-content">
                  <FormattedText content={message.text} />
                </div>
              </article>
            );
          }

          if (message.pendingApproval) {
            const appr = message.pendingApproval;
            const inProgress = Boolean(approvalsInProgress[appr.id]);
            return (
              <article key={message.id} className="transcript-message role-assistant">
                <div className="transcript-avatar" aria-hidden="true">⚙</div>
                <div className="transcript-body" style={{ width: '100%' }}>
                  <section className="approval-card" style={{ margin: '4px 0 10px 0' }}>
                    <p className="eyebrow">⚠️ TOOL CONFIRMATION REQUIRED</p>
                    <h3>
                      Execute <code>{appr.tool_name}</code>?
                    </h3>
                    <p>{appr.summary || appr.text}</p>
                    {appr.arg && (
                      <p style={{ fontFamily: 'ui-monospace, monospace', fontSize: 11 }}>
                        Target: <code>{appr.arg}</code>
                      </p>
                    )}
                    <div className="action-row" style={{ marginTop: 8 }}>
                      <button
                        type="button"
                        className="danger"
                        disabled={inProgress}
                        onClick={() => void handleApprovalDecision(appr.id, false)}
                      >
                        Deny
                      </button>
                      <button
                        type="button"
                        className="primary"
                        disabled={inProgress}
                        onClick={() => void handleApprovalDecision(appr.id, true)}
                      >
                        {inProgress ? 'Executing…' : 'Approve'}
                      </button>
                    </div>
                  </section>
                </div>
              </article>
            );
          }

          if (message.resolvedApproval) {
            const r = message.resolvedApproval;
            const label = r.status === 'approved' ? '✅ Approved'
              : r.status === 'denied' ? '❌ Denied'
              : r.status === 'expired' ? '⌛ Expired'
              : '☑️ No longer pending';
            return (
              <article key={message.id} className="transcript-message role-assistant">
                <div className="transcript-avatar" aria-hidden="true">⚙</div>
                <div className="transcript-body">
                  <div className="transcript-content">
                    <p className="formatted-paragraph">
                      <strong>{label}</strong>
                      {r.tool_name ? <> · tool <code>{r.tool_name}</code></> : ' · tool confirmation'}
                      {r.arg ? <> · <code>{r.arg}</code></> : null}
                    </p>
                  </div>
                </div>
              </article>
            );
          }

          return (
            <article key={message.id} className="transcript-message role-assistant">
              <div className="transcript-avatar" aria-hidden="true">🤖</div>
              <div className="transcript-body">
                {message.subagents && message.subagents.length > 0 && (
                  <SubagentCardGroup tasks={message.subagents} />
                )}
                <div className="transcript-content">
                  <FormattedText content={message.text} />
                </div>
                <footer className="transcript-footer">
                  <time dateTime={message.created_at}>
                    {new Date(message.created_at).toLocaleString([], {
                      month: 'short',
                      day: 'numeric',
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </time>
                </footer>
              </div>
            </article>
          );
        })}

        {subagents.length > 0 && (
          <article className="transcript-message role-assistant live-streaming">
            <div className="transcript-avatar" aria-hidden="true">🧩</div>
            <div className="transcript-body">
              <SubagentCardGroup tasks={subagents} live />
            </div>
          </article>
        )}

        {statusText && (
          <div className="live-job-turn" style={{ padding: '4px 0' }}>
            <div className="live-job-banner queued" style={{ margin: '0 12px' }}>
              <span className="live-pulse queued" />
              <span>{statusText}</span>
            </div>
          </div>
        )}

        {streamingDelta && (
          <article className="transcript-message role-assistant live-streaming">
            <div className="transcript-avatar" aria-hidden="true">🤖</div>
            <div className="transcript-body">
              <div className="transcript-header" style={{ marginBottom: 4 }}>
                <span className="streaming-indicator">● Streaming answer…</span>
              </div>
              <div className="transcript-content">
                <FormattedText content={streamingDelta} />
                <span className="typing-cursor">▌</span>
              </div>
            </div>
          </article>
        )}
      </div>

      {error && (
        <div className="error-banner global" style={{ margin: '6px 12px' }}>
          {error}
          <button type="button" onClick={() => setError('')}>×</button>
        </div>
      )}

      <form className="composer" onSubmit={handleSend}>
        <button
          type="button"
          className="mode-switch"
          style={{ padding: '6px 10px', alignSelf: 'center', cursor: 'pointer', border: 0 }}
          title="Start fresh conversation"
          onClick={handleNewChat}
          disabled={busy}
        >
          ＋ New
        </button>
        <textarea
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder="Ask Conveyor on the chat tier…"
          rows={2}
          maxLength={8000}
          disabled={busy}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
        />
        <button className="send-button" type="submit" disabled={!input.trim() || busy}>
          {busy ? '…' : 'Send'} <span>↗</span>
        </button>
      </form>
    </div>
  );
}
