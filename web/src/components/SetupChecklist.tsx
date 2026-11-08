export interface SetupChecklistProps {
  workersEnabled: boolean
  hasEligibleWorker: boolean
  modelConfigured: boolean
  hasSentFirstMessage: boolean
  onNavigateWorkers: () => void
  onOpenSettings: () => void
  onDismiss: () => void
}

export function SetupChecklist({
  workersEnabled,
  hasEligibleWorker,
  modelConfigured,
  hasSentFirstMessage,
  onNavigateWorkers,
  onOpenSettings,
  onDismiss,
}: SetupChecklistProps) {
  const step1Done = Boolean(hasEligibleWorker)
  const step2Done = Boolean(modelConfigured)
  const step3Done = Boolean(hasSentFirstMessage)

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
              <a
                href="#workers"
                className="setup-checklist-link"
                onClick={(e) => {
                  e.preventDefault()
                  onNavigateWorkers()
                }}
              >
                选一个执行者并登录
              </a>
            ) : (
              <div className="setup-checklist-text-wrap">
                <span className="setup-checklist-label">选一个执行者并登录</span>
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
    </div>
  )
}
