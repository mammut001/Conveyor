import { useEffect, useState } from 'react';

type DeciderProvider = 'off' | 'gemini' | 'jev' | 'custom';

type IntentDeciderConfig = {
  provider: DeciderProvider;
  configured_provider?: string;
  auto_selected?: boolean;
  endpoint?: string;
  model?: string;
  secret_name?: string;
  secret_configured?: boolean;
  available_secrets?: string[];
  ready?: boolean;
  error?: string;
  health?: DeciderHealth;
};

type DeciderHealth = {
  provider?: string;
  consecutive_failures?: number;
  last_error_kind?: string | null;
  last_error_at?: string | null;
  last_ok_at?: string | null;
  last_error_message?: string;
  unhealthy?: boolean;
};

type HealthLine = { unhealthy: boolean; text: string; kind: string };

const HEALTH_KIND_TEXT: Record<string, string> = {
  auth: '密钥被拒绝了',
  quota: '额度用完了',
  rate_limit: '请求太频繁',
  timeout: '等回复超时',
  network: '连不上服务商',
  bad_request: '服务商认为请求格式不对',
  other: '服务商返回了错误',
};

/** 上次成功：<相对时间> — short Chinese, no clock reading required. */
function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return '';
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 60) return '刚刚';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months} 个月前`;
  return `${Math.floor(months / 12)} 年前`;
}

function healthLine(health: DeciderHealth | null | undefined, ready?: boolean): HealthLine | null {
  if (!health || health.provider === 'off') return null;
  const kind = health.last_error_kind || '';
  const failures = Number(health.consecutive_failures || 0);
  const unhealthy = Boolean(health.unhealthy) || kind === 'auth' || failures >= 3;
  if (unhealthy) {
    const detail = (health.last_error_message || '').trim() || HEALTH_KIND_TEXT[kind] || '服务商返回了错误';
    return { unhealthy: true, text: `理解指令暂时用不了：${detail}。`, kind };
  }
  // Nothing configured yet: the 未添加 badge above already says what to do.
  if (ready === false) return null;
  const lastOk = (health.last_ok_at || '').trim();
  return {
    unhealthy: false,
    text: lastOk ? `正常（上次成功：${relativeTime(lastOk)}）` : '正常（还没有成功用过）',
    kind: '',
  };
}

type TestResult = {
  choice: string;
  choice_label: string;
  confidence: number;
  latency_ms: number;
};

const CHOICE_LABELS: Record<string, string> = {
  progress: '查看进度',
  stop: '停止目标',
  resume: '继续目标',
  create_project: '新建项目',
  timed_goal: '定时改进',
  chat: '普通对话',
};

function defaultSecretName(provider: DeciderProvider, customSecret: string): string {
  if (provider === 'gemini') return 'GEMINI_API_KEY';
  if (provider === 'jev') return 'TYPESAFE_API_KEY';
  if (provider === 'custom') return customSecret.trim().toUpperCase();
  return '';
}

export function IntentDeciderSection({
  token,
  onOpenSecrets,
}: {
  token: string;
  onOpenSecrets: () => void;
}) {
  const [provider, setProvider] = useState<DeciderProvider>('off');
  const [endpoint, setEndpoint] = useState('');
  const [model, setModel] = useState('');
  const [secretName, setSecretName] = useState('');
  const [serverSnapshot, setServerSnapshot] = useState<IntentDeciderConfig | null>(null);
  const [availableSecrets, setAvailableSecrets] = useState<string[] | null>(null);
  const health = healthLine(serverSnapshot?.health, serverSnapshot?.ready);
  const [saving, setSaving] = useState(false);
  const [saveNotice, setSaveNotice] = useState('');
  const [saveError, setSaveError] = useState('');
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [testError, setTestError] = useState('');

  useEffect(() => {
    let active = true;
    void fetch('/api/config/intent-decider', {
      headers: { Authorization: 'Bearer ' + token },
      cache: 'no-store',
    })
      .then(async res => {
        if (!res.ok) return null;
        return await res.json().catch(() => null) as IntentDeciderConfig | null;
      })
      .then(data => {
        if (!active || !data || typeof data.provider !== 'string') return;
        const nextProvider = (['off', 'gemini', 'jev', 'custom'].includes(data.provider)
          ? data.provider
          : 'off') as DeciderProvider;
        setServerSnapshot(data);
        setProvider(nextProvider);
        setEndpoint(typeof data.endpoint === 'string' ? data.endpoint : '');
        setModel(typeof data.model === 'string' ? data.model : '');
        setSecretName(typeof data.secret_name === 'string' ? data.secret_name : '');
        if (Array.isArray(data.available_secrets)) {
          setAvailableSecrets(data.available_secrets.filter(s => typeof s === 'string'));
        }
      })
      .catch(() => {});
    return () => { active = false; };
  }, [token]);

  function changeProvider(next: DeciderProvider) {
    setProvider(next);
    setSaveNotice('');
    setSaveError('');
    setTestResult(null);
    setTestError('');
    if (next === 'jev' && !model.trim()) {
      setModel('jev-latest');
    } else if (next === 'gemini') {
      setSecretName('GEMINI_API_KEY');
    } else if (next === 'jev') {
      setSecretName('TYPESAFE_API_KEY');
    } else if (next === 'custom' && (secretName === 'GEMINI_API_KEY' || secretName === 'TYPESAFE_API_KEY')) {
      setSecretName('');
    }
  }

  const activeSecretName = defaultSecretName(provider, secretName);
  const secretPresent = Boolean(
    activeSecretName && (
      availableSecrets !== null
        ? availableSecrets.includes(activeSecretName)
        : Boolean(serverSnapshot && serverSnapshot.provider === provider && serverSnapshot.secret_configured)
    ),
  );

  async function saveConfig() {
    if (saving) return;
    setSaving(true);
    setSaveNotice('');
    setSaveError('');
    try {
      const payload = {
        provider,
        endpoint: provider === 'jev' || provider === 'custom' ? endpoint.trim() : '',
        model: provider === 'jev' ? (model.trim() || 'jev-latest') : provider === 'custom' ? model.trim() : '',
        secret_name: provider === 'custom' ? secretName.trim().toUpperCase() : '',
      };
      const res = await fetch('/api/config/intent-decider', {
        method: 'POST',
        headers: {
          Authorization: 'Bearer ' + token,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => null) as { ok?: boolean; config?: IntentDeciderConfig; error?: string } | null;
      if (!res.ok) {
        setSaveError(typeof data?.error === 'string' && data.error ? data.error : '保存失败，请检查输入后再试');
        return;
      }
      if (data?.config) {
        setServerSnapshot(data.config);
        if (Array.isArray(data.config.available_secrets)) {
          setAvailableSecrets(data.config.available_secrets);
        }
      }
      setSaveNotice('✓ 已保存理解指令设置');
    } catch {
      setSaveError('连不上服务器，设置没有保存');
    } finally {
      setSaving(false);
    }
  }

  async function runTest() {
    if (testing || provider === 'off') return;
    setTesting(true);
    setTestResult(null);
    setTestError('');
    try {
      const payload = {
        provider,
        endpoint: provider === 'jev' || provider === 'custom' ? endpoint.trim() : '',
        model: provider === 'jev' ? (model.trim() || 'jev-latest') : provider === 'custom' ? model.trim() : '',
        secret_name: provider === 'custom' ? secretName.trim().toUpperCase() : activeSecretName,
      };
      const res = await fetch('/api/config/intent-decider/test', {
        method: 'POST',
        headers: {
          Authorization: 'Bearer ' + token,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => null) as {
        ok?: boolean;
        choice?: string;
        choice_label?: string;
        confidence?: number;
        latency_ms?: number;
        error?: string;
      } | null;
      if (!res.ok || !data?.ok || typeof data.choice !== 'string') {
        setTestError(typeof data?.error === 'string' && data.error ? data.error : '测试失败，请检查配置后再试');
        return;
      }
      setTestResult({
        choice: data.choice,
        choice_label: data.choice_label || CHOICE_LABELS[data.choice] || data.choice,
        confidence: typeof data.confidence === 'number' ? data.confidence : 0,
        latency_ms: typeof data.latency_ms === 'number' ? data.latency_ms : 0,
      });
    } catch {
      setTestError('连不上服务器，暂时无法测试');
    } finally {
      setTesting(false);
    }
  }

  const textProps = { autoCapitalize: 'off', autoCorrect: 'off', spellCheck: false, autoComplete: 'off' } as const;

  return (
    <section className="intent-decider-section" aria-labelledby="intent-decider-title">
      <div className="intent-decider-head">
        <h3 id="intent-decider-title">理解指令</h3>
        <p className="intent-decider-desc">用轻量判断模型理解更多日常口语（查进度、暂停、接着做等），超时会自动按普通对话回复。</p>
      </div>
      {health && (
        <p
          className={health.unhealthy ? 'intent-decider-health is-error' : 'intent-decider-health is-ok'}
          role={health.unhealthy ? 'alert' : 'status'}
        >
          <span>{health.text}</span>
          {health.unhealthy && health.kind === 'auth' && (
            <a
              href="#secrets"
              className="intent-decider-secret-link"
              onClick={event => {
                event.preventDefault();
                onOpenSecrets();
              }}
            >
              去密钥页面
            </a>
          )}
        </p>
      )}
      <div className="form-grid intent-decider-grid">
        <div className="dialog-field wide">
          <label htmlFor="intent-decider-provider">服务商</label>
          <select
            id="intent-decider-provider"
            value={provider}
            onChange={event => changeProvider(event.target.value as DeciderProvider)}
          >
            <option value="off">关闭</option>
            <option value="gemini">Gemini Decisions</option>
            <option value="jev">Jev</option>
            <option value="custom">自定义</option>
          </select>
        </div>

        {(provider === 'jev' || provider === 'custom') && (
          <>
            <div className="dialog-field wide">
              <label htmlFor="intent-decider-endpoint">接口地址</label>
              <input
                id="intent-decider-endpoint"
                {...textProps}
                type="url"
                inputMode="url"
                className="mono-input"
                value={endpoint}
                placeholder="https://你的判断服务地址/decisions"
                onChange={event => {
                  setEndpoint(event.target.value);
                  setSaveNotice('');
                  setSaveError('');
                }}
              />
            </div>
            <div className="dialog-field">
              <label htmlFor="intent-decider-model">模型</label>
              <input
                id="intent-decider-model"
                {...textProps}
                className="mono-input"
                value={model}
                placeholder={provider === 'jev' ? 'jev-latest' : '模型名称'}
                onChange={event => {
                  setModel(event.target.value);
                  setSaveNotice('');
                  setSaveError('');
                }}
              />
            </div>
          </>
        )}

        {provider === 'custom' && (
          <div className="dialog-field">
            <label htmlFor="intent-decider-secret">密钥名称</label>
            <input
              id="intent-decider-secret"
              {...textProps}
              className="mono-input"
              value={secretName}
              placeholder="例如 MY_DECIDER_API_KEY"
              onChange={event => {
                setSecretName(event.target.value.toUpperCase());
                setSaveNotice('');
                setSaveError('');
              }}
            />
          </div>
        )}
      </div>

      {provider !== 'off' && (
        <div className="intent-decider-secret-row">
          {activeSecretName ? (
            <>
              <span>读取密钥：<code>{activeSecretName}</code></span>
              {secretPresent
                ? <span className="intent-decider-badge is-ok">已添加</span>
                : (
                  <>
                    <span className="intent-decider-badge is-missing">未添加</span>
                    <a
                      href="#secrets"
                      className="intent-decider-secret-link"
                      onClick={event => {
                        event.preventDefault();
                        onOpenSecrets();
                      }}
                    >
                      去密钥页面添加
                    </a>
                  </>
                )}
            </>
          ) : (
            <>
              <span>请先填写密钥名称，再到密钥页面添加对应的值</span>
              <a
                href="#secrets"
                className="intent-decider-secret-link"
                onClick={event => {
                  event.preventDefault();
                  onOpenSecrets();
                }}
              >
                去密钥页面添加
              </a>
            </>
          )}
        </div>
      )}

      <div className="intent-decider-actions">
        <button
          type="button"
          className="intent-decider-btn"
          disabled={saving}
          onClick={() => void saveConfig()}
        >
          {saving ? '正在保存…' : '保存理解指令'}
        </button>
        {provider !== 'off' && (
          <button
            type="button"
            className="intent-decider-btn"
            disabled={testing}
            onClick={() => void runTest()}
          >
            {testing ? '测试中…' : '测试'}
          </button>
        )}
      </div>

      {saveNotice && (
        <p className="intent-decider-feedback is-ok" role="status">{saveNotice}</p>
      )}
      {saveError && (
        <p className="intent-decider-feedback is-error" role="alert">{saveError}</p>
      )}
      {testResult && (
        <p className="intent-decider-feedback is-ok" role="status">
          判断为：{testResult.choice_label}（把握 {Math.round(testResult.confidence * 100)}%，{Math.round(testResult.latency_ms)} 毫秒）
        </p>
      )}
      {testError && (
        <p className="intent-decider-feedback is-error" role="alert">{testError}</p>
      )}
    </section>
  );
}
