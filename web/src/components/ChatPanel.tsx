import { FormEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { FormattedText } from './FormattedText';
import { VoiceRecorder } from './VoiceRecorder';
import { CopyButton } from './CopyButton';
import { useMediaQuery } from '../ux/shellNav';
import {
  CHANNEL_LABELS, ChatRequestError, STREAM_INTERRUPTED, describeApproval, describeChatError, formatChatTime, formatFullTime,
  looksLikeRawError, stripDeepHint,
} from './chatFormat';

export type SubagentProgress = {
  call_id: string;
  index: number;
  title: string;
  status: 'queued' | 'running' | 'ok' | 'timeout' | 'error';
  elapsed: number;
  tools: string[];
};

export type TaskSuggestion = { prompt: string; session_id: string };

export type ChatMessage = {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  created_at: string;
  subagents?: SubagentProgress[];
  interrupted?: boolean;
  /** A stored reply that was really a backend error: shown as a short notice, raw text under 详情. */
  failedReply?: string;
  taskSuggestion?: TaskSuggestion;
  originChannel?: 'web' | 'telegram' | 'feishu';
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
type HistoryMessage = { origin_channel?: 'web' | 'telegram' | 'feishu'; role: 'user' | 'assistant'; text: string; created_at?: string; kind?: string; approval?: HistoryApproval; task_suggestion?: TaskSuggestion };

/** One short Chinese sentence; the technical text stays behind 详情. */
type ChatError = { message: string; detail?: string; action?: 'send' | 'history' };
type FailedSend = { text: string; audio?: Blob; messageId: string };

export type ChatPanelProps = {
  token: string;
  active?: boolean;
  onApprovalDecided?: () => void;
  onPrepareTask?: (suggestion: TaskSuggestion) => void;
  onSessionChange?: (sessionId: string) => void;
  initialInput?: string;
  onInitialInputConsumed?: () => void;
  /** When set, this panel is one agent's conversation: the session is fixed
   *  and never read from or written to this browser's "last chat" slot. */
  agentSessionId?: string;
  agentName?: string;
  /** The Agent's colour, used for its avatar beside replies. */
  agentColor?: string;
  onBusyChange?: (busy: boolean) => void;
};

const VOICE_PLACEHOLDER = '🎤 正在识别语音…';
const MAX_LENGTH = 8000;
const COUNTER_FROM = Math.ceil(MAX_LENGTH * 0.9);
const INTERRUPTED_NOTICE = '上次的回复被打断了，消息已放回输入框，可以重新发送';

function SubagentCardGroup({ tasks, live }: { tasks: SubagentProgress[]; live?: boolean }) {
  if (!tasks || tasks.length === 0) return null;

  const okCount = tasks.filter(t => t.status === 'ok').length;
  const timeoutCount = tasks.filter(t => t.status === 'timeout').length;
  const errorCount = tasks.filter(t => t.status === 'error').length;
  const runningCount = tasks.filter(t => t.status === 'running' || t.status === 'queued').length;

  const summaryParts: string[] = [];
  summaryParts.push(`${tasks.length} 个子任务`);
  if (okCount > 0) summaryParts.push(`${okCount} 个已完成`);
  if (timeoutCount > 0) summaryParts.push(`${timeoutCount} 个超时`);
  if (errorCount > 0) summaryParts.push(`${errorCount} 个出错`);
  if (runningCount > 0) summaryParts.push(`${runningCount} 个运行中`);
  const summaryText = summaryParts.join(' · ');

  const content = (
    <div className="subagent-card-group">
      {live && (
        <div className="subagent-group-header">
          <span className="subagent-spinner" aria-hidden="true">🧩</span>
          <span>子任务进行中（{tasks.length - runningCount}/{tasks.length}）</span>
        </div>
      )}
      {tasks.map(task => {
        let chip = null;
        if (task.status === 'queued') {
          chip = <span className="subagent-chip chip-running">排队中</span>;
        } else if (task.status === 'running') {
          chip = <span className="subagent-chip chip-running"><span className="subagent-spinner" aria-hidden="true">⟳</span> 运行中</span>;
        } else if (task.status === 'ok') {
          chip = <span className="subagent-chip chip-ok">已完成</span>;
        } else if (task.status === 'timeout') {
          chip = <span className="subagent-chip chip-timeout">超时</span>;
        } else {
          chip = <span className="subagent-chip chip-error">出错</span>;
        }

        return (
          <div key={`${task.call_id}-${task.index}`} className="subagent-row">
            <div className="subagent-row-main">
              {chip}
              <span className="subagent-title" title={task.title}>{task.title}</span>
            </div>
            <div className="subagent-row-meta">
              <span>{task.elapsed.toFixed(1)} 秒</span>
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
        <span aria-hidden="true">🧩</span>
        <span>{summaryText}</span>
      </summary>
      {content}
    </details>
  );
}

function sourceLabel(message: ChatMessage): string {
  if (message.originChannel) return CHANNEL_LABELS[message.originChannel] || '';
  // Older history did not record its entry point; say nothing rather than guess.
  return message.id.startsWith('hist-') ? '' : CHANNEL_LABELS.web;
}

function MessageTime({ value }: { value: string }) {
  const label = formatChatTime(value);
  if (!label) return null;
  return <time dateTime={value} title={formatFullTime(value)}>{label}</time>;
}

function approvalLabel(status: string): string {
  return status === 'approved' ? '已批准'
    : status === 'denied' ? '已拒绝'
    : status === 'expired' ? '已过期'
    : '已处理';
}

export function ChatPanel({ token, active = true, onPrepareTask, onApprovalDecided, onSessionChange, initialInput, onInitialInputConsumed, agentSessionId, agentName, agentColor, onBusyChange }: ChatPanelProps) {
  const [sessionId, setSessionId] = useState<string>(() => agentSessionId || localStorage.getItem('conveyor-chat-session') || '');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const draftKey = `conveyor-chat-draft:v1:${agentSessionId || 'free-chat'}`;
  const [input, setInput] = useState(() => {
    try { return sessionStorage.getItem(draftKey) || ''; }
    catch { return ''; }
  });
  useEffect(() => {
    try {
      if (input) sessionStorage.setItem(draftKey, input);
      else sessionStorage.removeItem(draftKey);
    } catch { /* Typing still works if browser storage is unavailable. */ }
  }, [draftKey, input]);
  const [busy, setBusy] = useState(false);
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  const [error, setError] = useState<ChatError | null>(null);
  // Not an error: tells the owner that a reply cut off by leaving the page was put back in the box.
  const [notice, setNotice] = useState('');
  // Return is a line break on touch keyboards (no Shift+Enter there); 发送 is the way to send.
  const touchKeyboard = useMediaQuery('(pointer: coarse)');
  const [streamingDelta, setStreamingDelta] = useState('');
  const [statusText, setStatusText] = useState('');
  const [subagents, setSubagents] = useState<SubagentProgress[]>([]);
  const subagentsRef = useRef<SubagentProgress[]>([]);
  subagentsRef.current = subagents;
  const [approvalsInProgress, setApprovalsInProgress] = useState<Record<string, boolean>>({});
  const streamRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const lastScrollTopRef = useRef(0);
  const followStreamRef = useRef(true);
  const sendingRef = useRef(false);
  const requestRef = useRef<AbortController | null>(null);
  const historyVersionRef = useRef(0);
  const failedSendRef = useRef<FailedSend | null>(null);
  const [showLatest, setShowLatest] = useState(false);
  const [unseen, setUnseen] = useState(false);
  const displayName = agentName || 'Conveyor';
  const avatar = (
    <div className="transcript-avatar chat-avatar" aria-hidden="true" style={agentColor ? { background: agentColor } : undefined}>
      {(displayName.trim()[0] || 'C').toUpperCase()}
    </div>
  );

  useEffect(() => () => { requestRef.current?.abort(); }, []);

  // A message sent while the reply was still streaming is remembered per conversation until the
  // reply ends. Leaving mid-reply (switching assistant, reload) aborts the request and the server
  // stores nothing, so on return the words go back into the box instead of vanishing.
  const inflightKey = `conveyor-chat-inflight:v1:${agentSessionId || 'free-chat'}`;
  const takeInflight = useCallback((): string => {
    try {
      const pending = sessionStorage.getItem(inflightKey) || '';
      sessionStorage.removeItem(inflightKey);
      return pending;
    } catch {
      return '';
    }
  }, [inflightKey]);
  const restoreInterrupted = useCallback((pending: string, stored: HistoryMessage[] | null) => {
    if (!pending || sendingRef.current) return;
    // The server did keep that turn after all: nothing was lost, so do not offer it again.
    const lastUser = [...(stored || [])].reverse().find(m => m.role === 'user');
    if (lastUser && lastUser.text.trim() === pending.trim()) return;
    setInput(current => current || pending);
    setNotice(INTERRUPTED_NOTICE);
  }, []);

  useEffect(() => {
    if (initialInput) {
      setInput(initialInput);
      onInitialInputConsumed?.();
    }
  }, [initialInput, onInitialInputConsumed]);

  const loadHistory = useCallback(async (sid: string): Promise<HistoryMessage[] | null> => {
    if (!sid) {
      setMessages([]);
      return [];
    }
    const version = ++historyVersionRef.current;
    setError(null);
    try {
      const res = await fetch(`/api/chat/history?session_id=${encodeURIComponent(sid)}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (version !== historyVersionRef.current) return null;
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new ChatRequestError(body.error || '', res.status);
      }
      const data = await res.json();
      if (version !== historyVersionRef.current) return null;
      if (Array.isArray(data.messages)) {
        setMessages(
          (data.messages as HistoryMessage[]).map((m, idx) => {
            const base: ChatMessage = {
              id: `hist-${idx}-${m.created_at || Date.now()}`,
              role: m.role,
              text: m.text,
              failedReply: m.role === 'assistant' && !m.approval && looksLikeRawError(m.text) ? m.text : undefined,
              taskSuggestion: m.task_suggestion,
              originChannel: m.origin_channel,
              // No recorded time: show none rather than pretend it is "now".
              created_at: m.created_at || '',
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
        return data.messages as HistoryMessage[];
      }
    } catch (err) {
      if (version === historyVersionRef.current) {
        const human = describeChatError(err);
        setError(err instanceof ChatRequestError && err.status === 401
          ? { message: human.message, detail: human.detail }
          : { message: '暂时无法读取聊天记录', detail: human.detail, action: 'history' });
      }
    }
    return null;
  }, [token]);

  // Load history once for the session restored from localStorage. Sessions
  // assigned mid-request (SSE `session` event) must not trigger a reload:
  // that raced the first persisted turn and wiped the in-flight messages.
  const initialSessionId = useRef(sessionId);
  useEffect(() => {
    const pending = takeInflight();
    if (initialSessionId.current) {
      const startVersion = historyVersionRef.current + 1;
      void loadHistory(initialSessionId.current).then(stored => {
        if (historyVersionRef.current === startVersion) restoreInterrupted(pending, stored);
      });
    } else {
      restoreInterrupted(pending, null);
    }
  }, [loadHistory, takeInflight, restoreInterrupted]);

  const scrollToLatest = useCallback(() => {
    const node = streamRef.current;
    if (node) {
      node.scrollTo({ top: node.scrollHeight, behavior: 'instant' });
      lastScrollTopRef.current = node.scrollTop;
    }
  }, []);

  useEffect(() => {
    if (active && followStreamRef.current) scrollToLatest();
  }, [active, messages, streamingDelta, statusText, subagents, error, scrollToLatest]);

  // New content while the reader is up in the history: offer a jump instead of moving them.
  useEffect(() => {
    if (!followStreamRef.current) setUnseen(true);
  }, [messages, streamingDelta]);

  // Markdown can finish loading after the message render; viewport changes can
  // also reflow a long answer. Follow those size changes only for readers at end.
  useEffect(() => {
    if (!active || !streamRef.current || !contentRef.current) return;
    const observer = new ResizeObserver(() => {
      if (followStreamRef.current) scrollToLatest();
    });
    observer.observe(streamRef.current);
    observer.observe(contentRef.current);
    return () => observer.disconnect();
  }, [active, scrollToLatest]);

  // The box grows with its text (1 to about 8 lines), then scrolls.
  const resizeComposer = useCallback(() => {
    const node = textareaRef.current;
    if (!node || node.offsetParent === null) return;
    // Measuring briefly collapses the box, which lets the browser clamp the
    // message list's scroll position; put the reader back where they were.
    const stream = streamRef.current;
    const previousTop = stream?.scrollTop ?? 0;
    node.style.height = 'auto';
    const max = parseFloat(getComputedStyle(node).maxHeight) || 200;
    node.style.height = `${Math.min(node.scrollHeight, max)}px`;
    node.style.overflowY = node.scrollHeight > max ? 'auto' : 'hidden';
    if (stream) {
      stream.scrollTo({ top: followStreamRef.current ? stream.scrollHeight : previousTop, behavior: 'instant' });
      lastScrollTopRef.current = stream.scrollTop;
    }
  }, []);
  useLayoutEffect(() => { resizeComposer(); }, [input, active, resizeComposer]);
  useEffect(() => {
    window.addEventListener('resize', resizeComposer);
    return () => window.removeEventListener('resize', resizeComposer);
  }, [resizeComposer]);

  const jumpToLatest = () => {
    followStreamRef.current = true;
    setShowLatest(false);
    setUnseen(false);
    scrollToLatest();
  };

  const handleNewChat = () => {
    historyVersionRef.current++;
    followStreamRef.current = true;
    failedSendRef.current = null;
    setShowLatest(false);
    setUnseen(false);
    setNotice('');
    setSessionId('');
    setInput('');
    localStorage.removeItem('conveyor-chat-session');
    setMessages([]);
    setStreamingDelta('');
    setStatusText('');
    setSubagents([]);
    subagentsRef.current = [];
    setError(null);
    if (onSessionChange) onSessionChange('');
    textareaRef.current?.focus();
  };

  const handleApprovalDecision = async (approvalId: string, approve: boolean) => {
    setApprovalsInProgress(prev => ({ ...prev, [approvalId]: true }));
    setError(null);
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
        throw new ChatRequestError(body.error || '', res.status);
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
      const human = describeChatError(err, {
        fallback: approve ? '没能批准这个操作，请再试一次' : '没能拒绝这个操作，请再试一次',
        network: '网络中断，操作没有完成，请再试一次',
      });
      setError({ message: human.message, detail: human.detail });
    } finally {
      setApprovalsInProgress(prev => ({ ...prev, [approvalId]: false }));
    }
  };

  const handleSend = async (e?: FormEvent, audio?: Blob, retry?: FailedSend) => {
    if (e) e.preventDefault();
    const voice = retry ? retry.audio : audio;
    const text = retry ? retry.text : voice ? VOICE_PLACEHOLDER : input.trim();
    if (!text || sendingRef.current) return;
    sendingRef.current = true;
    historyVersionRef.current++;
    followStreamRef.current = true;
    failedSendRef.current = null;
    setShowLatest(false);
    setUnseen(false);
    const controller = new AbortController();
    requestRef.current = controller;
    let partialText = '';
    let receivedAnswer = false;
    let completed = false;
    let streamError = false;
    let failure: unknown = null;

    if (retry) {
      // The failed bubble is replaced by the retried one when nothing followed it.
      setMessages(prev => prev.length && prev[prev.length - 1].id === retry.messageId ? prev.slice(0, -1) : prev);
      // The failed text was put back in the box for editing; retrying sends it, so clear that copy.
      if (!voice) setInput(current => current.trim() === retry.text.trim() ? '' : current);
    } else if (!voice) {
      setInput('');
    }
    setBusy(true);
    onBusyChange?.(true);
    setError(null);
    setNotice('');
    if (!voice) {
      try { sessionStorage.setItem(inflightKey, text); } catch { /* optional safety net */ }
    }
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
      const res = await fetch(
        voice ? `/api/chat/voice?session_id=${encodeURIComponent(sessionId)}` : '/api/chat',
        {
          method: 'POST',
          signal: controller.signal,
          headers: {
            'Content-Type': voice ? voice.type : 'application/json',
            Authorization: `Bearer ${token}`,
          },
          body: voice || JSON.stringify({
            message: text,
            session_id: sessionId || undefined,
          }),
        },
      );

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new ChatRequestError(data.error || '', res.status);
      }
      if (!res.body) {
        throw new ChatRequestError('Response body stream not available');
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        let chunk: ReadableStreamReadResult<Uint8Array>;
        try {
          chunk = await reader.read();
        } catch (readErr) {
          // The server had already started answering: the connection dropped, not the send.
          if (controller.signal.aborted) throw readErr;
          throw new Error(STREAM_INTERRUPTED);
        }
        const { value, done } = chunk;
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
                if (!agentSessionId) localStorage.setItem('conveyor-chat-session', payload.session_id);
                if (onSessionChange) onSessionChange(payload.session_id);
              }
            } else if (eventName === 'heard') {
              const heard = payload.transcript
                ? `🎤 ${String(payload.transcript).trim()}`
                : String(payload.text || '').replace(/^🎤\s*(?:heard:|听到[：:])\s*/i, '🎤 ');
              setMessages(prev => prev.map(m => m.id === userMsg.id ? { ...m, text: heard } : m));
            } else if (eventName === 'delta') {
              if (payload.text !== undefined) {
                partialText = payload.text;
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
              receivedAnswer = true;
              partialText = '';
              setStreamingDelta('');
              setStatusText('');
              setMessages(prev => [
                ...prev,
                {
                  id: `appr-${payload.id}`,
                  role: 'assistant',
                  text: payload.text || '这个操作需要你确认',
                  created_at: new Date().toISOString(),
                  pendingApproval: payload,
                },
              ]);
            } else if (eventName === 'message' && looksLikeRawError(String(payload.text || ''))) {
              // The backend failed and its exception text came back as the "answer": a failed reply, not a bubble.
              partialText = '';
              setStreamingDelta('');
              setStatusText('');
              streamError = true;
              failure = new ChatRequestError(String(payload.text));
            } else if (eventName === 'message') {
              receivedAnswer = true;
              partialText = '';
              setStreamingDelta('');
              setStatusText('');
              const finishedSubagents = subagentsRef.current.length > 0 ? [...subagentsRef.current] : undefined;
              setMessages(prev => [
                ...prev,
                {
                  id: `asst-${Date.now()}`,
                  role: 'assistant',
                  text: payload.text,
                  taskSuggestion: payload.task_suggestion,
                  created_at: new Date().toISOString(),
                  subagents: finishedSubagents,
                },
              ]);
              setSubagents([]);
              subagentsRef.current = [];
            } else if (eventName === 'error') {
              streamError = true;
              failure = new ChatRequestError(payload.error || '');
            } else if (eventName === 'done') {
              completed = true;
              setStreamingDelta('');
              setStatusText('');
            }
          } catch (parseErr) {
            console.error('Failed to parse SSE JSON', parseErr);
          }
        }
      }
      if (!completed) throw new Error(STREAM_INTERRUPTED);
    } catch (err) {
      if (!controller.signal.aborted) failure = failure || err;
    } finally {
      if (!controller.signal.aborted && partialText) {
        setMessages(prev => [...prev, {
          id: `partial-${Date.now()}`, role: 'assistant', text: partialText,
          created_at: new Date().toISOString(), interrupted: true,
        }]);
      }
      const unanswered = !receivedAnswer && (!completed || streamError);
      if (!controller.signal.aborted) {
        // The request ended on its own (answer or failure): nothing is in flight any more.
        try { sessionStorage.removeItem(inflightKey); } catch { /* ignore */ }
      }
      if (!controller.signal.aborted && failure) {
        // A stream error means the message arrived but the reply failed.
        const human = describeChatError(failure, streamError ? { fallback: '这次没有拿到回复，可以重试' } : {});
        const canRetry = human.retryable && unanswered;
        setError({ message: human.message, detail: human.detail, action: canRetry ? 'send' : undefined });
        failedSendRef.current = canRetry ? { text, audio: voice, messageId: userMsg.id } : null;
      }
      // A message that got no reply at all is not left in the transcript as if it had been answered:
      // the bubble goes away, the words return to the box, and 重试 sends them exactly once.
      if (!controller.signal.aborted && unanswered && failure && !partialText) {
        setMessages(prev => prev.filter(m => m.id !== userMsg.id));
        if (!voice) setInput(draft => draft || text);
      }
      sendingRef.current = false;
      requestRef.current = null;
      setBusy(false);
      setStreamingDelta('');
      setStatusText('');
      setSubagents([]);
      subagentsRef.current = [];
    }
  };

  const retryError = () => {
    if (error?.action === 'history') {
      void loadHistory(sessionId);
      return;
    }
    const failed = failedSendRef.current;
    if (failed) void handleSend(undefined, undefined, failed);
  };

  const placeholder = `给 ${displayName} 发消息…`;
  const showWelcome = messages.length === 0 && !streamingDelta && !busy && error?.action !== 'history';

  return (
    <div className="chat-panel-container" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="chat-stream-wrap" style={{ position: 'relative', display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <div className="event-stream" ref={streamRef} style={{ flex: 1, overflowY: 'auto' }}
        onScroll={e => {
          const node = e.currentTarget;
          const nearBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
          if (nearBottom) followStreamRef.current = true;
          else if (node.scrollTop < lastScrollTopRef.current) followStreamRef.current = false;
          lastScrollTopRef.current = node.scrollTop;
          setShowLatest(!followStreamRef.current);
          if (followStreamRef.current) setUnseen(false);
        }}>
        <div ref={contentRef} className="chat-stream-content">
        {showWelcome && (
          <div className="welcome-state">
            <div className="chat-welcome-avatar" aria-hidden="true" style={agentColor ? { background: agentColor } : undefined}>
              {(displayName.trim()[0] || 'C').toUpperCase()}
            </div>
            <h2>{agentName ? `和 ${agentName} 聊点什么？` : '想聊点什么？'}</h2>
            <p>可以帮你查资料、跑命令、改代码，删除等危险操作会先问你。</p>
            <details className="welcome-nightly-commands">
              <summary>夜间任务怎么用？</summary>
              <p>夜间任务只在预览环境运行，不碰生产；默认运行 1 小时，最多处理 12 个任务，达到时间限制或任务配额会自动停止，发现的 Issue 会保留，方便之后继续处理。</p>
              <p>示例：<code>{'/nightly_start 检查预览环境并修复失败的测试'}</code></p>
              <ul>
                <li><code>{'/nightly_start <objective>'}</code> 启动夜间任务</li>
                <li><code>{'/nightly_status <run ID>'}</code> 查看运行状态</li>
                <li><code>{'/nightly_cancel <run ID>'}</code> 取消运行中的任务</li>
              </ul>
              <p>请保留 <code>/nightly_start</code> 返回的 run ID，之后用它查询运行状态或取消任务。</p>
            </details>
          </div>
        )}

        {messages.map(message => {
          const source = sourceLabel(message);
          if (message.role === 'user') {
            return (
              <article key={message.id} className="transcript-message role-user chat-turn">
                <div className="chat-bubble">
                  <div className="transcript-content">
                    <FormattedText content={message.text} />
                  </div>
                </div>
                <footer className="transcript-footer chat-meta">
                  {source && (
                    <span className="chat-source-pill is-user">
                      <i className="chat-source-hash" aria-hidden="true">#</i>
                      <span className="transcript-source">{source}</span>
                    </span>
                  )}
                  <MessageTime value={message.created_at} />
                  <span className="chat-sent-check" aria-hidden="true">✓</span>
                </footer>
              </article>
            );
          }

          if (message.pendingApproval) {
            const appr = message.pendingApproval;
            const inProgress = Boolean(approvalsInProgress[appr.id]);
            const described = describeApproval(appr.tool_name, `${appr.summary} ${appr.text}`);
            return (
              <article key={message.id} className="transcript-message role-assistant chat-turn">
                {avatar}
                <div className="transcript-body">
                  <section className="approval-card chat-approval-card" aria-label="需要你确认">
                    <p className="chat-approval-kicker">需要你确认</p>
                    <h3>要执行这个操作吗？</h3>
                    <p className="chat-approval-summary">
                      操作：{described.action}{described.risk ? ` · 风险：${described.risk}` : ''}
                    </p>
                    {(appr.summary || appr.text || described.raw) && (
                      <details className="chat-approval-detail">
                        <summary>详情</summary>
                        {described.raw && <code>{described.raw}</code>}
                        <p>{appr.summary || appr.text}</p>
                      </details>
                    )}
                    {appr.arg && (
                      <p className="chat-approval-target">
                        对象：<code>{appr.arg}</code>
                      </p>
                    )}
                    <div className="action-row">
                      <button
                        type="button"
                        className="danger"
                        disabled={inProgress}
                        onClick={() => void handleApprovalDecision(appr.id, false)}
                      >
                        拒绝
                      </button>
                      <button
                        type="button"
                        className="primary"
                        disabled={inProgress}
                        onClick={() => void handleApprovalDecision(appr.id, true)}
                      >
                        {inProgress ? '正在执行…' : '批准'}
                      </button>
                    </div>
                  </section>
                </div>
              </article>
            );
          }

          if (message.resolvedApproval) {
            const r = message.resolvedApproval;
            return (
              <article key={message.id} className="transcript-message role-assistant chat-turn">
                {avatar}
                <div className="transcript-body">
                  <div className="transcript-content">
                    <p className="formatted-paragraph chat-approval-record">
                      <strong>{approvalLabel(r.status)}</strong>
                      {r.tool_name ? <> · {describeApproval(r.tool_name).action}</> : ' · 操作确认'}
                      {r.arg ? <> · <code>{r.arg}</code></> : null}
                    </p>
                  </div>
                </div>
              </article>
            );
          }

          if (message.failedReply) {
            return (
              <article key={message.id} className="transcript-message role-assistant chat-turn">
                {avatar}
                <div className="transcript-body">
                  <div className="transcript-content chat-failed-reply">
                    <p className="formatted-paragraph">这条消息没有得到回复</p>
                    <details>
                      <summary>详情</summary>
                      <code>{message.failedReply}</code>
                    </details>
                  </div>
                </div>
              </article>
            );
          }

          const shownText = stripDeepHint(message.text);
          return (
            <article key={message.id} className="transcript-message role-assistant chat-turn">
              {avatar}
              <div className="transcript-body">
                <div className="chat-bubble-head">
                  <span className="chat-bubble-avatar" aria-hidden="true" style={agentColor ? { background: agentColor } : undefined}>
                    {(displayName.trim()[0] || 'C').toUpperCase()}
                  </span>
                  <span className="chat-bubble-author" aria-hidden="true">{displayName}</span>
                  {source && (
                    <span className="chat-source-pill">
                      <i className="chat-source-hash" aria-hidden="true">#</i>
                      <span className="transcript-source">{source}</span>
                      <i className="chat-source-arrow" aria-hidden="true">›</i>
                    </span>
                  )}
                </div>
                {message.subagents && message.subagents.length > 0 && (
                  <SubagentCardGroup tasks={message.subagents} />
                )}
                <div className="transcript-content">
                  <FormattedText content={shownText} />
                </div>
                {message.taskSuggestion && onPrepareTask && (
                  <div className="action-row" style={{ marginTop: 10 }}>
                    <button type="button" className="primary" disabled={busy}
                      onClick={() => onPrepareTask(message.taskSuggestion!)}>
                      准备执行任务 →
                    </button>
                  </div>
                )}
                <footer className="transcript-footer chat-meta">
                  {message.interrupted && <span>回答中断 · 已保留收到的内容</span>}
                  <MessageTime value={message.created_at} />
                  <CopyButton text={shownText} className="chat-copy-button" title="复制这条回复" />
                </footer>
              </div>
            </article>
          );
        })}

        {subagents.length > 0 && (
          <article className="transcript-message role-assistant live-streaming chat-turn">
            {avatar}
            <div className="transcript-body">
              <SubagentCardGroup tasks={subagents} live />
            </div>
          </article>
        )}

        {streamingDelta && (
          <article className="transcript-message role-assistant live-streaming chat-turn">
            {avatar}
            <div className="transcript-body">
              <div className="transcript-content">
                <FormattedText content={streamingDelta} />
                <span className="typing-cursor" aria-hidden="true">▌</span>
              </div>
              <footer className="transcript-footer chat-meta">
                <span className="chat-live-label">{statusText || '正在回复…'}</span>
              </footer>
            </div>
          </article>
        )}

        {busy && !streamingDelta && (
          <div className="chat-thinking" role="status">
            {avatar}
            <span className="chat-thinking-dots" aria-hidden="true"><i /><i /><i /></span>
            <span>{statusText || '正在思考…'}</span>
          </div>
        )}

        {notice && (
          <div className="chat-inline-error chat-inline-notice" role="status">
            <div className="chat-inline-error-body"><p>{notice}</p></div>
            <div className="chat-inline-error-actions">
              <button type="button" onClick={() => setNotice('')}>关闭</button>
            </div>
          </div>
        )}

        {error && (
          <div className="chat-inline-error" role="alert">
            <svg className="chat-inline-error-icon" aria-hidden="true" viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
              <circle cx="10" cy="10" r="7.5" /><path d="M10 6v4.5M10 13.6v.2" />
            </svg>
            <div className="chat-inline-error-body">
              <p>{error.message}</p>
              {error.detail && (
                <details>
                  <summary>详情</summary>
                  <code>{error.detail}</code>
                </details>
              )}
            </div>
            <div className="chat-inline-error-actions">
              {error.action && <button type="button" className="chat-retry-button" onClick={retryError} disabled={busy}>重试</button>}
              <button type="button" onClick={() => setError(null)}>关闭</button>
            </div>
          </div>
        )}
        </div>
      </div>

      {showLatest && (
        <button type="button" className={`chat-jump-latest ${unseen ? 'has-new' : ''}`} onClick={jumpToLatest}>
          <span aria-hidden="true">↓</span> {unseen ? '有新消息' : '回到最新'}
        </button>
      )}
      </div>

      <form className="composer chat-composer" onSubmit={handleSend}>
        <div className="chat-composer-box">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            placeholder={placeholder}
            rows={1}
            aria-label={`给 ${displayName} 发消息`}
            onKeyDown={e => {
              if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
              if (touchKeyboard) return;
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
          />
          {input.length >= COUNTER_FROM && (
            <div className={`chat-composer-count ${input.length >= MAX_LENGTH ? 'is-limit' : ''}`} role="status">
              {input.length.toLocaleString('en-US')} / {MAX_LENGTH.toLocaleString('en-US')}
              {input.length > MAX_LENGTH && ` · 超出 ${(input.length - MAX_LENGTH).toLocaleString('en-US')} 字，请删减后再发送`}
            </div>
          )}
          <div className="chat-composer-bar">
            {!agentSessionId && (
              <button type="button" className="chat-new-button" title="开始一段新的对话" onClick={handleNewChat} disabled={busy}>
                <span aria-hidden="true">＋</span>新会话
              </button>
            )}
            <span className="chat-composer-hint">
              {!touchKeyboard && <>Enter 发送<span className="chat-hint-newline"> · Shift + Enter 换行</span></>}
            </span>
            <VoiceRecorder disabled={busy} onAudio={audio => { void handleSend(undefined, audio); }} onError={message => setError({ message })} />
            <button className="chat-send-button" type="submit" disabled={!input.trim() || busy || input.length > MAX_LENGTH}
              // Tapping 发送 must not pull focus off the message box: on a phone that closes the
              // keyboard, the layout shifts and the tap misses the button.
              onMouseDown={e => e.preventDefault()}>
              发送
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}
