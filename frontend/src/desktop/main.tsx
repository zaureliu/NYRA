import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { DesktopApp } from './DesktopApp'
import { installTauriBackendBridge } from '../runtime/backend'
import './desktop.css'
import './settings.css'
import './v33-desktop.css'

installTauriBackendBridge()

createRoot(document.getElementById('root')!).render(<StrictMode><DesktopApp/></StrictMode>)
