import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './styles.css'
import './v2.css'
import './primary.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <div className="primary-interface">
      <div className="workbench-modebar" role="note" aria-label="Conveyor interface roles">
        <span className="workbench-label">Web Workbench</span>
        <strong>Serious work · inspect, refine, review, apply</strong>
        <span className="workbench-channel-note">Telegram / Feishu · quick control from anywhere</span>
      </div>
      <App />
    </div>
  </StrictMode>,
)
