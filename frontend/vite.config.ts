import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// /api 代理到后端 5000，前端不写死绝对地址（换了端口只改这里）
export default defineConfig({
  plugins: [vue()],
  server: {
    // host: true 同时监听 0.0.0.0 与 ::，否则 Windows 上
    // 127.0.0.1 与 localhost(::1) 只有其一可用，评审容易踩坑
    host: true,
    port: 5173,
    // 端口被占用时直接失败，而不是悄悄换成 5174
    // （README 写的是 5173，静默换端口等于让评审打开一个空白页）
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
    },
  },
})
