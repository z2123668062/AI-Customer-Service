import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期直接代理到 FastAPI，前端不碰域名与 CORS
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
