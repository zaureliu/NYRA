import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './styles.css'
import './v3.css'
import './voice.css'
import './v33.css'
import './app-shell.css'
import './ops.css'
import { installTauriBackendBridge } from './runtime/backend'

installTauriBackendBridge()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><App /></React.StrictMode>,
)
