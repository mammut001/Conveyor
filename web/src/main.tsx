import { Component, StrictMode, type ErrorInfo, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { HumanTakeoverPanel } from './components/HumanTakeoverPanel'
import './styles.css'
import './v2.css'
import './primary.css'
import './takeover.css'
import './workflows.css'
import './ux/shell.css'
import './ux/chat.css'
import './ux/tasks.css'
import './ux/guard.css'
import './ux/knowledge.css'
import './ux/manage.css'

/** Last line of defence: a render error shows a Chinese page instead of a blank screen. */
class RootErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null; attempt: number }> {
  state = { error: null as Error | null, attempt: 0 }
  static getDerivedStateFromError(error: Error) { return { error } }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('render error', error, info.componentStack) }
  render() {
    const { error, attempt } = this.state
    if (!error) return <div key={attempt} className="primary-interface">{this.props.children}</div>
    const detail = String(error.message || error).split('\n')[0].slice(0, 200)
    return (
      <div className="root-error" role="alert">
        <div className="root-error-card">
          <h1>页面出了点问题</h1>
          <p>别担心，你的数据没有丢。可以先重试；如果还是不行，请刷新页面。</p>
          <div className="root-error-actions">
            <button type="button" className="root-error-btn primary" onClick={() => this.setState({ error: null, attempt: attempt + 1 })}>重试</button>
            <button type="button" className="root-error-btn" onClick={() => window.location.reload()}>刷新页面</button>
          </div>
          <details><summary>详情</summary><code>{detail}</code></details>
        </div>
      </div>
    )
  }
}

/** First focusable element: jumps past the navigation to the page content. */
function SkipLink() {
  return (
    <a className="skip-link" href="#main-content" onClick={event => {
      event.preventDefault()
      const target = document.querySelector<HTMLElement>('.stream-panel, .unlock-card')
      if (!target) return
      if (!target.hasAttribute('tabindex')) target.tabIndex = -1
      target.focus()
      target.scrollIntoView?.({ block: 'start' })
    }}>跳到主要内容</a>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RootErrorBoundary>
      <SkipLink />
      <App />
      <HumanTakeoverPanel />
    </RootErrorBoundary>
  </StrictMode>,
)
