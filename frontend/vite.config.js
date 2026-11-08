import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = { ...process.env, ...loadEnv(mode, process.cwd(), '') }
  const operatorKey = env.RECOVERYOS_OPERATOR_API_KEY || env.VITE_OPERATOR_API_KEY

  const apiProxy = {
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
    rewrite: (path) => path.replace(/^\/api/, ''),
  }

  if (operatorKey) {
    apiProxy.headers = {
      Authorization: `Bearer ${operatorKey}`,
    }
    apiProxy.configure = (proxy) => {
      proxy.on('proxyReq', (proxyReq, req) => {
        if (req.headers.authorization) {
          proxyReq.setHeader('Authorization', req.headers.authorization)
        }
      })
    }
  }

  return {
    plugins: [react()],
    server: {
      proxy: {
        // Frontend calls fetch('/api/...'); Vite proxies to the FastAPI backend
        // (stripping the /api prefix) so there is no CORS in local development.
        '/api': apiProxy,
      },
    },
  }
})
