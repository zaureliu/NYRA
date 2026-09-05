import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: { rollupOptions: { input: { dashboard: 'index.html', desktop: 'desktop.html' } } },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api/ws': { target: 'ws://127.0.0.1:8000', ws: true },
      '/api/stt/stream': { target: 'ws://127.0.0.1:8000', ws: true },
      '/api': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
})
