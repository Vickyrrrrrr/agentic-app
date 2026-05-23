import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    minify: 'esbuild',
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) {
            return undefined
          }

          if (
            id.includes('react-markdown') ||
            id.includes('remark-gfm') ||
            id.includes('/remark-') ||
            id.includes('/rehype-') ||
            id.includes('/unified/') ||
            id.includes('/micromark') ||
            id.includes('/mdast-')
          ) {
            return 'markdown-stack'
          }

          if (id.includes('@monaco-editor') || id.includes('/monaco-editor/')) {
            return 'monaco-stack'
          }

          if (id.includes('@supabase') || id.includes('/axios/')) {
            return 'data-stack'
          }

          return undefined
        },
      },
    },
  },
  server: {
    proxy: {
      // When VITE_API_BASE_URL is not set, proxy /api/* to the local Docker backend
      '/api': {
        target: 'http://localhost:7860',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
