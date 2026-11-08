import React, { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { applyInboxDecision, isStale } from '../approvalFreshness';
import { FormattedText } from './FormattedText';
import {
  channelLabel, cronClock, describeCron, ErrorNote, formatDateTime, formatUpcoming, formatWhen, friendlyError,
  mapCronServerError, presetFor, responseError, SCHEDULE_PRESETS, scheduleLabel, splitCron, validateCron, withClock,
  type FriendlyError, pickArray, readJson, useLoadingTimeout } from '../ux/knowledgeKit';

export type RoutineHook = {
  hook_id: string;
  created_at: string;
  last_fired_at?: string | null;
  fire_count: number;
};

export type Routine = {
  id: number;
  name: string;
  schedule: string;
  schedule_cron?: string;
  prompt: string;
  deliver: string[];
  enabled: boolean;
  created_at: string;
  updated_at: string;
  last_run_at?: string | null;
  next_run_at?: string | null;
  last_run_status?: string | null;
  consecutive_failures?: number;
  hook?: RoutineHook | null;
  agent_id?: string | null;
};

export type InboxItem = {
  id: number;
  routine_id: number;
  routine_name: string;
  started_at: string;
  finished_at: string;
  status: string;
  output: string;
  approval_id?: string | null;
  delivery?: Record<string, string>;
  read_at?: string | null;
  trigger?: string;
  approval?: {
    id: string;
    status: string;
    expires_at?: string;
  } | null;
};

export type InboxPanelProps = {
  token: string;
  onUnreadChange?: (count: number) => void;
  onApprovalDecided?: () => void;
  webhooksEnabled?: boolean;
  /** New routines are created for this agent and report into its conversation. */
  agentId?: string;
  agentName?: string;
};

type CreatedHookInfo = {
  routineId: number;
  routineName: string;
  hook_id: string;
  secret: string;
  path: string;
};

type ConfirmHookAction = {
  routineId: number;
  action: 'rotate' | 'delete';
};

type Tone = 'success' | 'danger' | 'warning' | 'muted';

const RUN_STATUS: Record<string, { label: string; tone: Tone }> = {
  ok: { label: '已完成', tone: 'success' },
  executed: { label: '已批准并执行', tone: 'success' },
  approval_pending: { label: '等你批准', tone: 'warning' },
  error: { label: '失败', tone: 'danger' },
  escalate: { label: '需要你手动处理', tone: 'warning' },
  unavailable: { label: '没有运行', tone: 'danger' },
  denied: { label: '已拒绝', tone: 'muted' },
  expired: { label: '批准已过期', tone: 'muted' },
  cancelled: { label: '已取消', tone: 'muted' },
};

const APPROVAL_STATUS: Record<string, { label: string; tone: Tone }> = {
  approved: { label: '已批准', tone: 'success' },
  denied: { label: '已拒绝', tone: 'muted' },
  cancelled: { label: '已取消', tone: 'muted' },
  expired: { label: '批准已过期', tone: 'muted' },
};

function statusOf(status?: string | null) {
  return RUN_STATUS[status || ''] || { label: status || '未知', tone: 'muted' as Tone };
}

function itemStatus(item: InboxItem) {
  const appr = item.approval;
  if (item.status === 'approval_pending' && appr && appr.status !== 'pending') {
    return APPROVAL_STATUS[appr.status] || statusOf(appr.status);
  }
  return statusOf(item.status);
}

/** Server text for these outcomes is English; say what happened in Chinese first. */
function runExplanation(item: InboxItem): string | null {
  switch (item.status) {
    case 'escalate':
      return '这次需要改代码或执行命令，定时任务不会自动去做。需要的话，请在“任务”里手动发起。';
    case 'unavailable':
      return '当时对话服务不可用，这次没有运行。';
    case 'error':
      return /timed out|运行超时/i.test(item.output || '') ? '运行超时，已自动停止。' : '运行时出错了。';
    default:
      return null;
  }
}

function deliveryNotes(delivery?: Record<string, string>): string[] {
  if (!delivery) return [];
  return Object.entries(delivery)
    .filter(([channel]) => channel !== 'web')
    .map(([channel, state]) => {
      const label = channelLabel(channel);
      // Space between Chinese and Latin words: "发到 Telegram 失败", "发到飞书失败".
      const target = /^[\u3400-\u9fff]/.test(label) ? label : ` ${label} `;
      const value = String(state || '');
      if (value === 'ok') return `已发到${target}`.trim();
      if (value.startsWith('skipped')) {
        const why = /not configured/i.test(value) ? '（未连接）' : /no target/i.test(value) ? '（没有接收对象）' : '';
        return `没有发到${target}`.trim() + why;
      }
      return `发到${target}失败`;
    });
}

function hasLongOutput(text: string) {
  return text.length > 600 || text.split('\n').length > 12;
}

const DEFAULT_SCHEDULE = '0 9 * * *';
const HOURS = Array.from({ length: 24 }, (_, hour) => hour);
const MINUTES = Array.from({ length: 12 }, (_, step) => step * 5);
const pad2 = (value: number) => String(value).padStart(2, '0');

export function InboxPanel({ token, onUnreadChange, onApprovalDecided, webhooksEnabled, agentId, agentName }: InboxPanelProps) {
  const [disabled, setDisabled] = useState(false);
  const disabledRef = useRef(false);
  const [items, setItems] = useState<InboxItem[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [routinesList, setRoutinesList] = useState<Routine[]>([]);
  const [loaded, setLoaded] = useState({ inbox: false, routines: false });
  const [error, setError] = useState<FriendlyError | null>(null);
  const [notice, setNotice] = useState<{ text: string; showInbox?: boolean } | null>(null);
  const [approvalsInProgress, setApprovalsInProgress] = useState<Record<string, 'approve' | 'reject' | undefined>>({});
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const fetchGen = useRef(0);

  // Routine actions
  const [routineBusy, setRoutineBusy] = useState<{ id: number; action: 'run' | 'toggle' | 'delete' } | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);

  // Webhook state
  const [confirmHookAction, setConfirmHookAction] = useState<ConfirmHookAction | null>(null);
  const [createdHookInfo, setCreatedHookInfo] = useState<CreatedHookInfo | null>(null);
  const [hookBusyId, setHookBusyId] = useState<number | null>(null);
  const [copiedSecret, setCopiedSecret] = useState(false);
  const [copiedCurl, setCopiedCurl] = useState(false);

  // Create form state
  const [formOpen, setFormOpen] = useState(false);
  const [name, setName] = useState('');
  const [schedule, setSchedule] = useState(DEFAULT_SCHEDULE);
  const [clock, setClock] = useState('09:00');
  const [prompt, setPrompt] = useState('');
  const [deliverTg, setDeliverTg] = useState(false);
  const [deliverFs, setDeliverFs] = useState(false);
  const [formBusy, setFormBusy] = useState(false);
  const [formError, setFormError] = useState<FriendlyError | null>(null);
  const [nameError, setNameError] = useState('');
  const [promptError, setPromptError] = useState('');
  const [channels, setChannels] = useState<{ telegram?: boolean; feishu?: boolean } | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const cronRef = useRef<HTMLInputElement>(null);
  const inboxRef = useRef<HTMLElement>(null);
  const newRoutineRef = useRef<HTMLButtonElement>(null);
  const formWasOpen = useRef(false);

  useLoadingTimeout(!error && (!loaded.inbox || !loaded.routines), () => setError(friendlyError('timeout', '加载太久了，请重试')));

  const fetchInbox = useCallback(async () => {
    if (disabledRef.current || !token) return;
    const gen = ++fetchGen.current;
    try {
      const res = await fetch('/api/inbox?limit=50', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 409) {
        disabledRef.current = true;
        setDisabled(true);
        return;
      }
      if (!res.ok) throw await responseError(res);
      const data = await readJson(res);
      const list = pickArray<InboxItem>(data, 'items');
      if (isStale(gen, fetchGen.current)) return;
      setItems(list);
      setLoaded((prev) => (prev.inbox ? prev : { ...prev, inbox: true }));
      const count = Number((data as { unread?: unknown })?.unread || 0) || 0;
      setUnreadCount(count);
      onUnreadChange?.(count);
    } catch (err) {
      if (isStale(gen, fetchGen.current) || disabledRef.current) return;
      setError(friendlyError(err, '没能读取收件箱，请稍后重试'));
    }
  }, [token, onUnreadChange]);

  const fetchRoutines = useCallback(async () => {
    if (disabledRef.current || !token) return;
    try {
      const res = await fetch('/api/routines', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 409) {
        disabledRef.current = true;
        setDisabled(true);
        return;
      }
      if (!res.ok) throw await responseError(res);
      const list = pickArray<Routine>(await readJson(res), 'routines');
      setRoutinesList(list);
      setLoaded((prev) => (prev.routines ? prev : { ...prev, routines: true }));
    } catch (err) {
      if (!disabledRef.current) {
        setError(friendlyError(err, '没能读取定时任务，请稍后重试'));
      }
    }
  }, [token]);

  useEffect(() => {
    void fetchInbox();
    void fetchRoutines();
  }, [fetchInbox, fetchRoutines]);

  useEffect(() => {
    if (disabled || disabledRef.current) return;
    const timer = window.setInterval(() => {
      if (disabledRef.current) return;
      void fetchInbox();
      void fetchRoutines();
    }, 15_000);
    return () => window.clearInterval(timer);
  }, [fetchInbox, fetchRoutines, disabled]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  // Which delivery channels are connected, so the form can say so.
  useEffect(() => {
    if (!formOpen || channels || !token) return;
    let active = true;
    fetch('/api/system/status', { headers: { Authorization: `Bearer ${token}` } })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!active) return;
        const known = data?.channels;
        setChannels(known ? { telegram: Boolean(known.telegram?.configured), feishu: Boolean(known.feishu?.configured) } : {});
      })
      .catch(() => { if (active) setChannels({}); });
    return () => { active = false; };
  }, [formOpen, channels, token]);

  // Focus moves into the form when it opens and back to its button when it closes.
  useEffect(() => {
    if (formOpen) nameRef.current?.focus();
    else if (formWasOpen.current) newRoutineRef.current?.focus();
    formWasOpen.current = formOpen;
  }, [formOpen]);

  const handleMarkRead = async (runId: number) => {
    try {
      await fetch(`/api/inbox/${runId}/read`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: '{}',
      });
      setItems((prev) =>
        prev.map((it) => (it.id === runId ? { ...it, read_at: new Date().toISOString() } : it))
      );
      setUnreadCount((c) => Math.max(0, c - 1));
      onUnreadChange?.(Math.max(0, unreadCount - 1));
    } catch {
      // ignore
    }
  };

  const handleMarkAllRead = async () => {
    try {
      await fetch('/api/inbox/read-all', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: '{}',
      });
      setItems((prev) =>
        prev.map((it) => ({ ...it, read_at: it.read_at || new Date().toISOString() }))
      );
      setUnreadCount(0);
      onUnreadChange?.(0);
    } catch (err) {
      setError(friendlyError(err, '没能全部标为已读，请重试'));
    }
  };

  const handleApprovalDecision = async (approvalId: string, approve: boolean) => {
    setApprovalsInProgress((prev) => ({ ...prev, [approvalId]: approve ? 'approve' : 'reject' }));
    try {
      const action = approve ? 'approve' : 'reject';
      const res = await fetch(`/api/approvals/${encodeURIComponent(approvalId)}/${action}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: '{}',
      });
      if (!res.ok && res.status !== 404) throw await responseError(res);
      const status = !res.ok ? 'expired' : approve ? 'approved' : 'denied';
      // Drop an in-flight poll, then paint the decision before the refetch returns.
      fetchGen.current += 1;
      setItems((prev) => applyInboxDecision(prev, approvalId, status));
      await fetchInbox();
      onApprovalDecided?.();
    } catch (err) {
      setError(friendlyError(err, approve ? '批准没有成功，请重试' : '拒绝没有成功，请重试'));
    } finally {
      setApprovalsInProgress((prev) => ({ ...prev, [approvalId]: undefined }));
    }
  };

  const handleToggleRoutine = async (r: Routine) => {
    const action = r.enabled ? 'pause' : 'resume';
    setRoutineBusy({ id: r.id, action: 'toggle' });
    try {
      const res = await fetch(`/api/routines/${r.id}/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: '{}',
      });
      if (!res.ok) throw await responseError(res);
      await fetchRoutines();
    } catch (err) {
      setError(friendlyError(err, r.enabled ? '暂停失败，请重试' : '恢复失败，请重试'));
    } finally {
      setRoutineBusy(null);
    }
  };

  const handleRunNow = async (r: Routine) => {
    setRoutineBusy({ id: r.id, action: 'run' });
    setNotice(null);
    try {
      const res = await fetch(`/api/routines/${r.id}/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: '{}',
      });
      if (!res.ok) throw await responseError(res);
      setNotice({ text: `“${r.name}”已运行完，结果已放进收件箱。`, showInbox: true });
      await fetchInbox();
      await fetchRoutines();
    } catch (err) {
      setError(friendlyError(err, '运行失败，请稍后重试'));
    } finally {
      setRoutineBusy(null);
    }
  };

  const handleDeleteRoutine = async (r: Routine) => {
    setRoutineBusy({ id: r.id, action: 'delete' });
    try {
      const res = await fetch(`/api/routines/${r.id}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw await responseError(res);
      setConfirmDeleteId(null);
      setNotice({ text: `已删除定时任务“${r.name}”。` });
      await fetchRoutines();
    } catch (err) {
      setError(friendlyError(err, '删除失败，请重试'));
    } finally {
      setRoutineBusy(null);
    }
  };

  const handleCreateOrRotateHook = async (routine: Routine, action: 'create' | 'rotate') => {
    setHookBusyId(routine.id);
    setError(null);
    try {
      const res = await fetch(`/api/routines/${routine.id}/hook`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: '{}',
      });
      if (!res.ok) throw await responseError(res);
      const data = await res.json();
      setCreatedHookInfo({
        routineId: routine.id,
        routineName: routine.name,
        hook_id: data.hook_id,
        secret: data.secret,
        path: data.path,
      });
      setConfirmHookAction(null);
      setCopiedSecret(false);
      setCopiedCurl(false);
      await fetchRoutines();
    } catch (err) {
      setError(friendlyError(err, action === 'create' ? '创建 Webhook 失败，请重试' : '更换密钥失败，请重试'));
    } finally {
      setHookBusyId(null);
    }
  };

  const handleDeleteHook = async (routineId: number) => {
    setHookBusyId(routineId);
    setError(null);
    try {
      const res = await fetch(`/api/routines/${routineId}/hook`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok && res.status !== 404) throw await responseError(res);
      if (createdHookInfo?.routineId === routineId) {
        setCreatedHookInfo(null);
      }
      setConfirmHookAction(null);
      await fetchRoutines();
    } catch (err) {
      setError(friendlyError(err, '移除 Webhook 失败，请重试'));
    } finally {
      setHookBusyId(null);
    }
  };

  const resetForm = () => {
    setName('');
    setSchedule(DEFAULT_SCHEDULE);
    setClock('09:00');
    setPrompt('');
    setDeliverTg(false);
    setDeliverFs(false);
    setFormError(null);
    setNameError('');
    setPromptError('');
  };

  const handleCreateRoutine = async (e: FormEvent) => {
    e.preventDefault();
    const trimmedName = name.trim();
    const trimmedPrompt = prompt.trim();
    const badName = !trimmedName ? '请填写名称' : '';
    const badPrompt = !trimmedPrompt ? '请写下要做的事' : '';

    setNameError(badName);
    setPromptError(badPrompt);

    if (badName) {
      nameRef.current?.focus();
      return;
    }
    if (badPrompt) {
      promptRef.current?.focus();
      return;
    }

    const fieldCount = splitCron(schedule).length;
    if (fieldCount !== 5) {
      setFormError({ message: 'cron 需要 5 段，用空格分开：分 时 日 月 周，例如 0 9 * * *', detail: '', raw: '' });
      cronRef.current?.focus();
      return;
    }
    const cronValidationError = validateCron(schedule);
    if (cronValidationError) {
      setFormError({ message: cronValidationError, detail: '', raw: '' });
      cronRef.current?.focus();
      return;
    }

    setFormBusy(true);
    setFormError(null);
    const deliver = ['web'];
    if (deliverTg) deliver.push('telegram');
    if (deliverFs) deliver.push('feishu');

    try {
      const res = await fetch('/api/routines', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          ...(agentId ? { agent: agentId } : {}),
          name: trimmedName,
          schedule: splitCron(schedule).join(' '),
          prompt: trimmedPrompt,
          deliver,
        }),
      });
      if (!res.ok) throw await responseError(res);
      const created = await res.json().catch(() => null) as Routine | null;
      setNotice({ text: `已创建定时任务“${created?.name || trimmedName}”${created?.next_run_at ? `，下次运行：${formatUpcoming(created.next_run_at)}` : ''}。` });
      resetForm();
      setFormOpen(false);
      await fetchRoutines();
    } catch (err) {
      const raw = err instanceof Error ? err.message : typeof err === 'string' ? err : '';
      const mapped = mapCronServerError(raw);
      if (mapped) {
        setFormError({ message: mapped, detail: '', raw });
      } else {
        setFormError(friendlyError(err, '创建失败，请检查后重试'));
      }
    } finally {
      setFormBusy(false);
    }
  };

  if (disabled) {
    return (
      <div className="stream-body kn-page kn-inbox">
        <p className="kn-off">定时任务功能未开启。</p>
      </div>
    );
  }

  const who = agentId && agentName ? agentName : 'Conveyor';
  const activePreset = presetFor(schedule);
  const scheduleClock = cronClock(schedule);
  const clockEditable = Boolean(scheduleClock) || !schedule.trim();
  const fieldCount = splitCron(schedule).length;
  const cronError = fieldCount === 5 ? validateCron(schedule) : null;
  const described = describeCron(schedule);
  const reading = !schedule.trim()
    ? { text: '请选一个常用时间，或直接填写 cron 表达式。', bad: false }
    : fieldCount !== 5
      ? { text: 'cron 需要 5 段，用空格分开：分 时 日 月 周，例如 0 9 * * *', bad: true }
      : cronError
        ? { text: cronError, bad: true }
        : described
          ? { text: `${described}${/\d$/.test(described) ? ' ' : ''}运行（服务器时间）`, bad: false }
          : { text: '自定义时间，创建时会检查格式。', bad: false };

  const applyPreset = (id: string) => {
    const preset = SCHEDULE_PRESETS.find((p) => p.id === id);
    if (!preset) return;
    // Remember the last clock time so 每小时 → 每天 keeps what the user picked.
    if (scheduleClock) setClock(scheduleClock);
    const [h, m] = (scheduleClock || clock).split(':').map(Number);
    setSchedule(preset.build(h, m));
    if (formError) setFormError(null);
  };

  const [clockHour, clockMinute] = (scheduleClock || clock).split(':').map(Number);
  const minuteOptions = MINUTES.includes(clockMinute) ? MINUTES : [...MINUTES, clockMinute].sort((a, b) => a - b);

  const changeClock = (value: string) => {
    setClock(value);
    if (!value) return;
    if (!schedule.trim()) {
      const [h, m] = value.split(':').map(Number);
      setSchedule(`${m} ${h} * * *`);
    } else if (scheduleClock) {
      setSchedule(withClock(schedule, value));
    }
  };

  const fullHookUrl = createdHookInfo
    ? (typeof window !== 'undefined' ? `${window.location.origin}${createdHookInfo.path}` : createdHookInfo.path)
    : '';
  const curlSnippet = createdHookInfo
    ? `BODY='{"event":"ping"}'\nSECRET='${createdHookInfo.secret}'\nSIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')\ncurl -X POST "${fullHookUrl}" \\\n  -H "Content-Type: application/json" \\\n  -H "X-Conveyor-Signature: sha256=$SIG" \\\n  -d "$BODY"`
    : '';

  return (
    <div className="stream-body kn-page kn-inbox">
      {error && (
        <ErrorNote
          error={error}
          onRetry={() => { setError(null); void fetchInbox(); void fetchRoutines(); }}
          onDismiss={() => setError(null)}
        />
      )}
      {notice && (
        <div className="kn-notice" role="status">
          <span>{notice.text}</span>
          {notice.showInbox && (
            <button type="button" className="kn-link" onClick={() => inboxRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>
              查看收件箱
            </button>
          )}
          <button type="button" className="kn-notice-close" aria-label="关闭提示" onClick={() => setNotice(null)}>×</button>
        </div>
      )}

      <section className="kn-section" aria-labelledby="kn-inbox-title" ref={inboxRef}>
        <header className="kn-section-head">
          <div>
            {/* The page header already says 收件箱; this section is its list of results. */}
            <h3 id="kn-inbox-title">
              运行结果{unreadCount > 0 && <span className="kn-count kn-count-accent">{unreadCount} 条未读</span>}
            </h3>
            <p className="kn-section-sub">定时任务每次运行的结果都在这里。</p>
          </div>
          {unreadCount > 0 && (
            <button type="button" className="kn-btn" onClick={() => void handleMarkAllRead()}>
              全部标为已读
            </button>
          )}
        </header>

        {!loaded.inbox ? (
          !error && <p className="kn-loading">加载中…</p>
        ) : items.length === 0 ? (
          <div className="kn-empty">
            <p className="kn-empty-title">收件箱是空的。</p>
            <p>{routinesList.length ? '定时任务运行后，结果会出现在这里；也可以点下面的“立即运行”马上试一次。' : '还没有运行结果。先在下面新建一个定时任务吧。'}</p>
          </div>
        ) : (
          <ul className="kn-inbox-list" aria-label="收件箱">
            {items.map((item) => {
              const isUnread = !item.read_at;
              const appr = item.approval;
              const inProgress = appr ? approvalsInProgress[appr.id] : undefined;
              const status = itemStatus(item);
              const explanation = runExplanation(item);
              const output = item.status === 'approval_pending' && (item.output === 'Approval pending' || item.output === '等你批准') ? '' : (item.output || '');
              const long = hasLongOutput(output);
              const open = Boolean(expanded[item.id]);
              const notes = deliveryNotes(item.delivery);

              return (
                <li
                  key={item.id}
                  className={`kn-card kn-inbox-item${isUnread ? ' is-unread' : ''}`}
                  onClick={() => {
                    if (isUnread) void handleMarkRead(item.id);
                  }}
                >
                  <header className="kn-inbox-head">
                    <div className="kn-inbox-title">
                      <strong>{item.routine_name}</strong>
                      <span className={`kn-status kn-status-${status.tone}`}>{status.label}</span>
                      {item.trigger === 'manual' && <span className="kn-tag kn-tag-muted">手动运行</span>}
                      {item.trigger === 'webhook' && <span className="kn-tag kn-tag-muted">Webhook 触发</span>}
                      {isUnread && <span className="kn-unread-label">未读</span>}
                    </div>
                    <time dateTime={item.started_at} title={formatDateTime(item.started_at)}>
                      {formatWhen(item.started_at)}
                    </time>
                  </header>

                  {explanation && <p className="kn-inbox-explain">{explanation}</p>}
                  {output && (explanation ? (
                    <details className="kn-raw">
                      <summary>查看原始信息</summary>
                      <pre>{output}</pre>
                    </details>
                  ) : (
                    <>
                      <div className={`kn-inbox-output${long && !open ? ' is-clamped' : ''}`}>
                        <FormattedText content={output} />
                      </div>
                      {long && (
                        <button
                          type="button"
                          className="kn-link"
                          onClick={(e) => {
                            e.stopPropagation();
                            setExpanded((prev) => ({ ...prev, [item.id]: !open }));
                          }}
                        >
                          {open ? '收起' : '展开全文'}
                        </button>
                      )}
                    </>
                  ))}

                  {appr && (
                    <div className={`kn-approval${appr.status === 'pending' ? ' is-pending' : ''}`}>
                      {appr.status === 'pending' ? (
                        <>
                          <p>
                            <strong>需要你批准才能继续。</strong>
                            {appr.expires_at ? <span>{formatDateTime(appr.expires_at)} 前有效</span> : null}
                          </p>
                          <div className="kn-row-actions">
                            <button
                              type="button"
                              className="kn-btn kn-btn-primary"
                              disabled={Boolean(inProgress)}
                              onClick={(e) => {
                                e.stopPropagation();
                                void handleApprovalDecision(appr.id, true);
                              }}
                            >
                              {inProgress === 'approve' ? '执行中…' : '批准'}
                            </button>
                            <button
                              type="button"
                              className="kn-btn kn-btn-danger"
                              disabled={Boolean(inProgress)}
                              onClick={(e) => {
                                e.stopPropagation();
                                void handleApprovalDecision(appr.id, false);
                              }}
                            >
                              {inProgress === 'reject' ? '处理中…' : '拒绝'}
                            </button>
                          </div>
                        </>
                      ) : (
                        <p className="kn-approval-done">
                          {appr.status === 'approved'
                            ? '已批准并执行。'
                            : appr.status === 'denied'
                            ? '已拒绝，没有执行。'
                            : appr.status === 'cancelled'
                            ? '定时任务已删除，这次批准已作废。'
                            : '批准已过期，没有执行。'}
                        </p>
                      )}
                    </div>
                  )}

                  {(notes.length > 0 || isUnread) && (
                    <footer className="kn-inbox-foot">
                      {notes.length > 0 && <span className="kn-inbox-delivery">{notes.join(' · ')}</span>}
                      {isUnread && (
                        <button
                          type="button"
                          className="kn-btn kn-btn-quiet"
                          onClick={(e) => {
                            e.stopPropagation();
                            void handleMarkRead(item.id);
                          }}
                        >
                          标为已读
                        </button>
                      )}
                    </footer>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className="kn-section" aria-labelledby="kn-routines-title">
        <header className="kn-section-head">
          <div>
            <h3 id="kn-routines-title">
              定时任务{routinesList.length > 0 && <span className="kn-count">{routinesList.length}</span>}
            </h3>
            <p className="kn-section-sub">
              {agentId && agentName
                ? `新建的定时任务由 ${agentName} 执行，结果放进收件箱，也会发到和 ${agentName} 的对话里。`
                : '到点后自动执行，结果放进收件箱。'}
            </p>
          </div>
          {!formOpen && (
            <button type="button" ref={newRoutineRef} className="kn-btn kn-btn-primary" onClick={() => { resetForm(); setFormOpen(true); }}>
              ＋ 新建定时任务
            </button>
          )}
        </header>

        {createdHookInfo && (
          <section className="kn-card kn-hook-card" aria-label="Webhook 信息">
            <div className="kn-hook-head">
              <div>
                <strong>“{createdHookInfo.routineName}”的 Webhook 信息</strong>
                <p className="kn-hook-warning">密钥只显示这一次，请现在复制保存。</p>
              </div>
              <button type="button" className="kn-btn" onClick={() => setCreatedHookInfo(null)}>关闭</button>
            </div>
            <div className="kn-hook-field">
              <span>Webhook 地址</span>
              <code>{fullHookUrl}</code>
            </div>
            <div className="kn-hook-field">
              <div className="kn-hook-field-head">
                <span>密钥</span>
                <button
                  type="button"
                  className="kn-btn kn-btn-small"
                  onClick={() => {
                    void navigator.clipboard.writeText(createdHookInfo.secret);
                    setCopiedSecret(true);
                    setTimeout(() => setCopiedSecret(false), 2000);
                  }}
                >
                  {copiedSecret ? '已复制' : '复制密钥'}
                </button>
              </div>
              <code>{createdHookInfo.secret}</code>
            </div>
            <div className="kn-hook-field">
              <div className="kn-hook-field-head">
                <span>curl 示例（可直接复制）</span>
                <button
                  type="button"
                  className="kn-btn kn-btn-small"
                  onClick={() => {
                    void navigator.clipboard.writeText(curlSnippet);
                    setCopiedCurl(true);
                    setTimeout(() => setCopiedCurl(false), 2000);
                  }}
                >
                  {copiedCurl ? '已复制' : '复制 curl'}
                </button>
              </div>
              <pre>{curlSnippet}</pre>
            </div>
          </section>
        )}

        {formOpen && (
          <form
            className="kn-card kn-routine-form"
            onSubmit={handleCreateRoutine}
            noValidate
            onKeyDown={(e) => {
              if (e.key === 'Escape' && !formBusy && !e.nativeEvent.isComposing) {
                setFormOpen(false);
                setFormError(null);
                setNameError('');
                setPromptError('');
              }
            }}
            aria-label="新建定时任务"
          >
            <div className="kn-form-head">
              <h4>新建定时任务</h4>
              <p>由 {who} 按你写的内容执行，不会自动改代码或执行命令。</p>
            </div>

            <div className="kn-field">
              <label htmlFor="kn-routine-name">名称</label>
              <input
                id="kn-routine-name"
                ref={nameRef}
                className="kn-input"
                type="text"
                placeholder="例如：每日邮件摘要"
                value={name}
                maxLength={80}
                onChange={(e) => {
                  setName(e.target.value);
                  if (nameError) setNameError('');
                }}
                aria-invalid={nameError ? true : undefined}
                aria-describedby={nameError ? 'kn-routine-name-error' : undefined}
              />
              {nameError && <p id="kn-routine-name-error" className="kn-field-error">{nameError}</p>}
            </div>

            <div className="kn-field">
              <label htmlFor="kn-routine-task">要做的事</label>
              <textarea
                id="kn-routine-task"
                ref={promptRef}
                className="kn-input"
                placeholder="像平时交代事情一样写，例如：看看我今天新收到的邮件，挑出需要回复的，用几句话总结给我。"
                value={prompt}
                maxLength={2000}
                onChange={(e) => {
                  setPrompt(e.target.value);
                  if (promptError) setPromptError('');
                }}
                rows={3}
                aria-invalid={promptError ? true : undefined}
                aria-describedby={promptError ? 'kn-routine-task-error' : undefined}
              />
              {promptError && <p id="kn-routine-task-error" className="kn-field-error">{promptError}</p>}
              <small className="kn-hint">{prompt.length} / 2000</small>
            </div>

            <fieldset className="kn-field">
              <legend>什么时候运行</legend>
              <div className="kn-chips" role="group" aria-label="常用时间">
                {SCHEDULE_PRESETS.map((preset) => (
                  <button
                    key={preset.id}
                    type="button"
                    className="kn-chip-btn"
                    aria-pressed={activePreset === preset.id}
                    onClick={() => applyPreset(preset.id)}
                  >
                    {preset.label}
                  </button>
                ))}
              </div>
              <div className="kn-schedule-row">
                <div className="kn-inline-field">
                  <span id="kn-routine-time-label">时间</span>
                  <div className="kn-time" role="group" aria-labelledby="kn-routine-time-label" title={clockEditable ? undefined : '这个运行频率不需要选时间'}>
                    <select
                      className="kn-input"
                      aria-label="小时"
                      value={clockHour}
                      disabled={!clockEditable}
                      onChange={(e) => changeClock(`${pad2(Number(e.target.value))}:${pad2(clockMinute)}`)}
                    >
                      {HOURS.map((hour) => <option key={hour} value={hour}>{pad2(hour)}</option>)}
                    </select>
                    <span aria-hidden="true">:</span>
                    <select
                      className="kn-input"
                      aria-label="分钟"
                      value={clockMinute}
                      disabled={!clockEditable}
                      onChange={(e) => changeClock(`${pad2(clockHour)}:${pad2(Number(e.target.value))}`)}
                    >
                      {minuteOptions.map((minute) => <option key={minute} value={minute}>{pad2(minute)}</option>)}
                    </select>
                  </div>
                </div>
                <label className="kn-inline-field kn-cron-field">
                  <span>cron 表达式（高级）</span>
                  <input
                    ref={cronRef}
                    className="kn-input kn-mono"
                    type="text"
                    value={schedule}
                    placeholder="0 9 * * *"
                    spellCheck={false}
                    autoCapitalize="off"
                    autoCorrect="off"
                    onChange={(e) => { setSchedule(e.target.value); if (formError) setFormError(null); }}
                    aria-describedby="kn-cron-reading"
                  />
                </label>
              </div>
              <p id="kn-cron-reading" className={`kn-cron-reading${reading.bad ? ' is-bad' : ''}`}>{reading.text}</p>
            </fieldset>

            <fieldset className="kn-field">
              <legend>结果发送到</legend>
              <div className="kn-checks">
                <label><input type="checkbox" checked disabled /> 收件箱（总是）</label>
                <label>
                  <input type="checkbox" checked={deliverTg} disabled={channels?.telegram === false} onChange={(e) => setDeliverTg(e.target.checked)} />
                  {' '}Telegram{channels?.telegram === false ? '（未连接）' : ''}
                </label>
                <label>
                  <input type="checkbox" checked={deliverFs} disabled={channels?.feishu === false} onChange={(e) => setDeliverFs(e.target.checked)} />
                  {' '}飞书{channels?.feishu === false ? '（未连接）' : ''}
                </label>
              </div>
            </fieldset>

            {formError && <ErrorNote error={formError} className="kn-error-inline" />}
            <div className="kn-form-actions">
              <button type="submit" disabled={formBusy} className="kn-btn kn-btn-primary">
                {formBusy ? '创建中…' : '创建定时任务'}
              </button>
              <button type="button" className="kn-btn" onClick={() => { setFormOpen(false); setFormError(null); setNameError(''); setPromptError(''); }}>
                取消
              </button>
            </div>
          </form>
        )}

        {!loaded.routines ? (
          !error && <p className="kn-loading">加载中…</p>
        ) : routinesList.length === 0 ? (
          !formOpen && (
            <div className="kn-empty">
              <p className="kn-empty-title">还没有定时任务。</p>
              <p>例如：每天早上 9 点总结新邮件，或每周一列出本周要做的事。</p>
            </div>
          )
        ) : (
          <ul className="kn-routine-list" aria-label="定时任务列表">
            {routinesList.map((r) => {
              const cron = r.schedule_cron || r.schedule;
              const failures = r.consecutive_failures || 0;
              const autoPaused = !r.enabled && failures >= 3;
              const last = r.last_run_status ? statusOf(r.last_run_status) : null;
              const extra = (r.deliver || []).filter((c) => c !== 'web').map(channelLabel);
              const busy = routineBusy?.id === r.id ? routineBusy.action : null;
              return (
                <li key={r.id} className={`kn-card kn-routine${r.enabled ? '' : ' is-paused'}`}>
                  <div className="kn-routine-main">
                    <div className="kn-routine-title">
                      <span className={`kn-dot ${r.enabled ? 'is-on' : 'is-off'}`} aria-hidden="true" />
                      <strong>{r.name}</strong>
                      {!r.enabled && <span className="kn-tag kn-tag-muted">{autoPaused ? '连续失败，已自动暂停' : '已暂停'}</span>}
                      {agentId && agentName && r.agent_id === agentId && <span className="kn-tag">属于 {agentName}</span>}
                    </div>
                    <p className="kn-routine-when">
                      <span className="kn-routine-schedule" title={`cron：${cron}`}>{scheduleLabel(cron)}</span>
                      {r.enabled && r.next_run_at && <span>下次 {formatUpcoming(r.next_run_at)}</span>}
                    </p>
                    {r.prompt && <p className="kn-routine-prompt" title={r.prompt}>{r.prompt}</p>}
                    <p className="kn-routine-last">
                      {r.last_run_at ? (
                        <span>
                          上次 <time dateTime={r.last_run_at} title={formatDateTime(r.last_run_at)}>{formatWhen(r.last_run_at)}</time>
                          {last && <> · <span className={`kn-tone-${last.tone}`}>{last.label}</span></>}
                        </span>
                      ) : (
                        <span>还没有运行过</span>
                      )}
                      {failures > 0 && <span className="kn-tone-danger">连续失败 {failures} 次</span>}
                      {extra.length > 0 && <span>同时发到 {extra.join('、')}</span>}
                      {webhooksEnabled && r.hook && (
                        <span>
                          Webhook 已触发 {r.hook.fire_count} 次
                          {r.hook.last_fired_at ? ` · 上次 ${formatWhen(r.hook.last_fired_at)}` : ''}
                        </span>
                      )}
                    </p>
                  </div>

                  <div className="kn-routine-actions">
                    {confirmDeleteId === r.id ? (
                      <div className="kn-confirm" role="group" aria-label="确认删除">
                        <span className="kn-confirm-text">删除后不再运行，没处理的批准也会作废。</span>
                        <div className="kn-row-actions">
                          <button type="button" className="kn-btn kn-btn-danger" disabled={busy === 'delete'} onClick={() => void handleDeleteRoutine(r)}>
                            {busy === 'delete' ? '删除中…' : '确定删除'}
                          </button>
                          <button type="button" className="kn-btn" disabled={busy === 'delete'} onClick={() => setConfirmDeleteId(null)}>
                            取消
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className="kn-row-actions">
                        <button
                          type="button"
                          className="kn-btn"
                          disabled={Boolean(busy)}
                          onClick={() => void handleRunNow(r)}
                          title="现在就运行一次，不影响原来的时间安排"
                        >
                          {busy === 'run' ? '运行中…' : '立即运行'}
                        </button>
                        <button type="button" className="kn-btn" disabled={Boolean(busy)} onClick={() => void handleToggleRoutine(r)}>
                          {busy === 'toggle' ? '处理中…' : r.enabled ? '暂停' : '恢复'}
                        </button>

                        {webhooksEnabled && (
                          <>
                            {!r.hook ? (
                              <button
                                type="button"
                                className="kn-btn"
                                disabled={hookBusyId === r.id}
                                onClick={() => void handleCreateOrRotateHook(r, 'create')}
                              >
                                {hookBusyId === r.id ? '创建中…' : '创建 Webhook'}
                              </button>
                            ) : confirmHookAction?.routineId === r.id ? (
                              <span className="kn-confirm-inline">
                                <span className="kn-confirm-text">
                                  {confirmHookAction.action === 'rotate' ? '旧密钥会立即失效' : '移除后外部系统无法再触发'}
                                </span>
                                <button
                                  type="button"
                                  className="kn-btn kn-btn-danger"
                                  disabled={hookBusyId === r.id}
                                  onClick={() => void (confirmHookAction.action === 'rotate' ? handleCreateOrRotateHook(r, 'rotate') : handleDeleteHook(r.id))}
                                >
                                  {hookBusyId === r.id
                                    ? (confirmHookAction.action === 'rotate' ? '更换中…' : '移除中…')
                                    : (confirmHookAction.action === 'rotate' ? '确定更换' : '确定移除')}
                                </button>
                                <button type="button" className="kn-btn" disabled={hookBusyId === r.id} onClick={() => setConfirmHookAction(null)}>
                                  取消
                                </button>
                              </span>
                            ) : (
                              <>
                                <button type="button" className="kn-btn" disabled={hookBusyId === r.id} onClick={() => setConfirmHookAction({ routineId: r.id, action: 'rotate' })}>
                                  更换密钥
                                </button>
                                <button type="button" className="kn-btn kn-btn-danger-text" disabled={hookBusyId === r.id} onClick={() => setConfirmHookAction({ routineId: r.id, action: 'delete' })}>
                                  移除 Webhook
                                </button>
                              </>
                            )}
                          </>
                        )}

                        <button
                          type="button"
                          className="kn-btn kn-btn-danger-text"
                          disabled={Boolean(busy)}
                          aria-label={`删除定时任务：${r.name}`}
                          onClick={() => setConfirmDeleteId(r.id)}
                        >
                          删除
                        </button>
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
