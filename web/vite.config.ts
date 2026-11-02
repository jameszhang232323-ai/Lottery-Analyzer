import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期把 /api 代理到本机后端；生产由同源反向代理提供，不在浏览器里写死后端地址。
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
