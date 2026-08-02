import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev-time proxy to the FastAPI backend (server/config.py's CRM_PORT,
// default 8500 -- see get_port()). Same-origin in dev so the session
// cookie set by /setup and /auth/login works with no CORS configuration
// needed anywhere.
const API_TARGET = process.env.CRM_API_URL ?? 'http://localhost:8500'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/records': API_TARGET,
      '/associations': API_TARGET,
      '/auth': API_TARGET,
      '/setup': API_TARGET,
      '/users': API_TARGET,
      '/roles': API_TARGET,
      '/health': API_TARGET,
      '/settings': API_TARGET,
      '/accounts': API_TARGET,
      '/proposals': API_TARGET,
    },
  },
})
