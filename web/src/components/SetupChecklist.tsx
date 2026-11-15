/** The subset of the chosen executor's last 测试 the checklist needs. */
export type SetupExecutorCheck = {
  ok: boolean | null
  kind?: string
  message?: string
} | null

export interface SetupChecklistProps {
  workersEnabled: boolean
  /** The built-in Pi is installed: it needs a model in 模型设置, never a login (QA N28). */
  builtinPiInstalled?: boolean
  /** The chosen executor is installed (so a 测试 can be run against it). */
  executorInstalled?: boolean
  /** The chosen executor's last 测试 result (null = never tested). */
  executorCheck?: SetupExecutorCheck
  /** A 测试 is running right now. */
  checkingExecutor?: boolean
  modelConfigured: boolean
  hasSentFirstMessage: boolean
  /** Every step just finished in this page: the card says so once before it goes away. */
  allDone?: boolean
  onNavigateWorkers: () => void
  onOpenSettings: () => void
  onRunExecutorCheck?: () => void
  onDismiss: () => void
}

export function SetupChecklist({
  workersEnabled,
  builtinPiInstalled = false,
  executorInstalled = false,
  executorCheck = null,
  checkingExecutor = false,
  modelConfigured,
  hasSentFirstMessage,
  allDone = false,
  onNavigateWorkers,
  onOpenSettings,
  onRunExecutorCheck,
  onDismiss,
}: SetupChecklistProps) {
  // ① is honest: the chosen executor must be installed AND actually work. "Installed" alone is
  // not enough (the executor may have a bad API key). An executor that cannot be tested at all
  // (kind: unsupported) counts as done. With the 执行者 feature off the owner cannot act, so it
  // does not keep the checklist open (today's behaviour).
  const executorTestPassed = executorCheck
    ? executorCheck.kind === 'unsupported' || executorCheck.ok === true
    : false
  const step1Done = !workersEnabled || (Boolean(executorInstalled) && executorTestPassed)
  const step1Testable = workersEnabled && Boolean(executorInstalled) && !step1Done
  const step1Failed = Boolean(executorCheck) && executorCheck!.ok === false
  const step2Done = Boolean(modelConfigured)
  const step3Done = Boolean(hasSentFirstMessage)
  const finished = allDone || (step1Done && step2Done && step3Done)
  // Pi（内置）never asks for a login: it only needs a model. Saying 「并登录」 sends the owner
  // looking for a login that does not exist (QA N28).
  const step1Label = builtinPiInstalled
    ? '选一个执行者（内置 Pi 只要配好模型就能用）'
    : '选一个执行者并登录'

  return (
    <div className="setup-checklist-card" role="region" aria-label="还差几步就能用">
      <div className="setup-checklist-header">
        <div className="setup-checklist-title">
          <span className="setup-checklist-icon" aria-hidden="true">✨</span>
          <strong>还差几步就能用</strong>
        </div>
        <button
          type="button"
          className="setup-checklist-close"
          onClick={onDismiss}
          aria-label="关闭"
          title="关闭引导"
        >
          ✕
        </button>
      </div>

      <ol className="setup-checklist-items">
        <li className={`setup-checklist-item ${step1Done ? 'is-done' : ''}`} data-step="1">
          <span className="setup-checklist-badge" aria-label={step1Done ? '已完成' : '步骤1'}>
            {step1Done ? '✓' : '①'}
          </span>
          <div className="setup-checklist-body">
            {workersEnabled ? (
              <div className="setup-checklist-text-wrap">
              <a
                href="#workers"
                className="setup-checklist-link"
                onClick={(e) => {
                  e.preventDefault()
                  onNavigateWorkers()
                }}
              >
                {step1Label}
              </a>
              <span className="setup-checklist-muted">提问不需要执行者；改代码需要</span>
              {step1Testable && onRunExecutorCheck && (
                <span className="setup-checklist-check">
                  <button
                    type="button"
                    className="setup-checklist-test-btn"
                    disabled={checkingExecutor}
                    onClick={onRunExecutorCheck}
                  >
                    {checkingExecutor ? '正在测试…' : '测试'}
                  </button>
                  {step1Failed && executorCheck?.message && (
                    <span className="setup-checklist-check-error" role="alert">{executorCheck.message}</span>
                  )}
                </span>
              )}
              </div>
            ) : (
              <div className="setup-checklist-text-wrap">
                <span className="setup-checklist-label">{step1Label}</span>
                <span className="setup-checklist-muted">管理员可以开启「执行者」功能</span>
              </div>
            )}
          </div>
        </li>

        <li className={`setup-checklist-item ${step2Done ? 'is-done' : ''}`} data-step="2">
          <span className="setup-checklist-badge" aria-label={step2Done ? '已完成' : '步骤2'}>
            {step2Done ? '✓' : '②'}
          </span>
          <div className="setup-checklist-body">
            <button
              type="button"
              className="setup-checklist-link setup-checklist-action-btn"
              onClick={onOpenSettings}
            >
              配置模型
            </button>
          </div>
        </li>

        <li className={`setup-checklist-item ${step3Done ? 'is-done' : ''}`} data-step="3">
          <span className="setup-checklist-badge" aria-label={step3Done ? '已完成' : '步骤3'}>
            {step3Done ? '✓' : '③'}
          </span>
          <div className="setup-checklist-body">
            <span className="setup-checklist-label">发第一条消息</span>
          </div>
        </li>
      </ol>
      {finished && <p className="setup-checklist-done" role="status">都准备好了，可以开始用。</p>}
    </div>
  )
}
