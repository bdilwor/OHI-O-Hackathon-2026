import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// /api/* is proxied to the FastAPI backend (http://localhost:8000 on the host, http://api:8000 in Docker).
const target = process.env.VITE_API_TARGET || 'http://localhost:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': { target, changeOrigin: true, rewrite: (path) => path.replace(/^\/api/, '') },
    },
  },
  worker: { format: 'es' }, // MapLibre's worker is an ES module
  build: { chunkSizeWarningLimit: 2500 },
});
