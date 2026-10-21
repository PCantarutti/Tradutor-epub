import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// O back-end FastAPI roda em http://localhost:8000 e expõe a API em /api.
// Em desenvolvimento o Vite serve o front-end na 5173 e faz proxy de /api.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    // O build é offline e determinístico: nada de requisição ao back-end.
    emptyOutDir: true,
  },
})
