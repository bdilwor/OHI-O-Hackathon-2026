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
  build: {
    // Split the big map libraries into their own cacheable chunks; the views that use them load lazily.
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined;
          if (id.includes('maplibre-gl')) return 'maplibre';
          if (id.includes('@deck.gl') || id.includes('@luma.gl') || id.includes('@loaders.gl') || id.includes('@math.gl')) return 'deckgl';
          if (id.includes('react')) return 'react';
          return 'vendor';
        },
      },
    },
    chunkSizeWarningLimit: 1200,  // MapLibre alone is ~1.1 MB minified and cannot be split further
  },
});
