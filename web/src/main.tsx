import { StrictMode } from 'react'
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

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <div className="primary-interface">
      <App />
      <HumanTakeoverPanel />
    </div>
  </StrictMode>,
)
