import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    // W4 §4.1：dev 代理 /api → Java 8080，同源无 CORS；Java 再转发 Python 8000
    proxy: {
      '/api': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'element-plus': ['element-plus'],
          vendor: ['vue', 'vue-router', 'pinia', 'axios'],
          markdown: ['marked', 'dompurify'],
        },
      },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.js'],
  },
})
