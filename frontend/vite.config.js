import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const backendEnvDir = path.resolve(__dirname, '../backend')

export default defineConfig(({ mode }) => {
  const backendEnv = loadEnv(mode, backendEnvDir, '')
  const localEnv = loadEnv(mode, __dirname, '')
  const operatorKey = (
    process.env.RECOVERYOS_OPERATOR_API_KEY ||
    process.env.VITE_OPERATOR_API_KEY ||
    localEnv.RECOVERYOS_OPERATOR_API_KEY ||
    localEnv.VITE_OPERATOR_API_KEY ||
    backendEnv.RECOVERYOS_OPERATOR_API_KEY ||
    backendEnv.VITE_OPERATOR_API_KEY ||
    ''
  ).trim()

  return {
    plugins: [react()],
    envDir: backendEnvDir,
    define: {
      'import.meta.env.VITE_OPERATOR_API_KEY': JSON.stringify(operatorKey),
    },
    server: {
      proxy: {
        '/api': {
          target: 'http://127.0.0.1:8000',
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/api/, ''),
          ...(operatorKey
            ? {
                headers: {
                  Authorization: `Bearer ${operatorKey}`,
                },
              }
            : {}),
          configure: (proxy) => {
            proxy.on('proxyReq', (proxyReq, req) => {
              if (req.headers.authorization) {
                proxyReq.setHeader('Authorization', req.headers.authorization)
              }
            })
          },
        },
      },
    },
  }
})
